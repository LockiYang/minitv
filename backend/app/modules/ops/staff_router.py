"""Back-office sign-in and staff account administration."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.common.models import AuditLog
from app.modules.common.serialization import iso_or_none
from app.modules.ops import service as ops_service
from app.modules.ops.auth import (
    _revoke_or_503,
    _bearer_token,
    _session_store_503,
    require_admin,
    require_admin_role,
)
from app.modules.ops.models import (
    AdminUser,
    ROLES,
    ROLE_ADMIN,
    ROLE_PARTNER,
    STATUSES,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    Supplier,
)
from app.modules.ops.schemas import (
    AdminUserOut,
    LoginIn,
    PasswordChangeIn,
    StaffCreateIn,
    StaffPasswordIn,
    StaffStatusIn,
    StaffUpdateIn,
)

router = APIRouter(tags=["admin"])


# ---- auth ----
@router.post("/auth/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    try:
        result = ops_service.login(db, username=body.username, password=body.password)
    except ops_service.SessionStoreUnavailable as exc:
        raise _session_store_503() from exc
    if result is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    user, token = result
    return {"token": token, "user": AdminUserOut.model_validate(user).model_dump()}


@router.get("/auth/me")
def me(user: AdminUser = Depends(require_admin)):
    return AdminUserOut.model_validate(user).model_dump()


@router.post("/auth/logout")
def logout(request: Request, user: AdminUser = Depends(require_admin)):
    try:
        ops_service.logout(_bearer_token(request))
    except ops_service.SessionStoreUnavailable as exc:
        raise _session_store_503() from exc
    return {"ok": True}


@router.post("/auth/password")
def change_own_password(
    body: PasswordChangeIn,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """Self-service password change. Requires the current password.

    Every session of this account is revoked on success — including the one
    making this call — so the client must send the user back to the login page
    and sign in again with the new password.
    """
    if not ops_service.verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    if body.new_password == body.old_password:
        raise HTTPException(status_code=422, detail="新密码不能与原密码相同")
    _validate_password(body.new_password, user.username)
    killed = _revoke_or_503(user.id)
    user.password_hash = ops_service.hash_password(body.new_password)
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=user.id,
            action="staff_password_change",
            target_type="admin_user",
            target_id=user.id,
            payload={"revoked_sessions": killed},
        )
    )
    db.commit()
    return {"revoked_sessions": killed}


# ---- staff accounts ----
#
# Accounts are never deleted. Disabling is enforced on every request by
# `resolve_session_token`, which re-reads the row and rejects a non-active
# status, so a disabled account loses access immediately rather than at TTL.
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,31}$")


def _staff_out(row: AdminUser) -> dict:
    """Never serialise password_hash — not even a prefix."""
    return {
        "id": row.id,
        "username": row.username,
        "display_name": row.display_name,
        "role": row.role,
        "status": row.status,
        "disable_reason": row.disable_reason,
        "supplier_id": row.supplier_id,
        "last_login_at": iso_or_none(row.last_login_at),
        "created_at": iso_or_none(row.created_at),
        "updated_at": iso_or_none(row.updated_at),
    }


def _get_staff_or_404(db: Session, staff_id: int) -> AdminUser:
    row = db.get(AdminUser, staff_id)
    if row is None:
        raise HTTPException(status_code=404, detail="staff not found")
    return row


def _clean_username(raw: str) -> str:
    username = raw.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(
            status_code=422,
            detail="账号需为 3–32 位小写字母、数字或 . _ -，并以字母或数字开头",
        )
    return username


def _validate_role(role: str) -> str:
    if role not in ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {', '.join(ROLES)}")
    return role


def _validate_password(password: str, username: str) -> str:
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="密码至少 8 位")
    if len(password) > 128:
        raise HTTPException(status_code=422, detail="密码最多 128 位")
    if password != password.strip():
        raise HTTPException(status_code=422, detail="密码首尾不能是空格")
    if password.lower() == username.lower():
        raise HTTPException(status_code=422, detail="密码不能与账号相同")
    return password


def _ensure_admin_remains(db: Session, row: AdminUser) -> None:
    """Belt and braces for the "never leave the back office without admins" rule.

    The reachable paths are already blocked (nobody may disable or demote
    themselves), but a future relaxation of those rules must not be able to
    strand the last active admin.
    """
    remaining = (
        db.scalar(
            select(func.count(AdminUser.id)).where(
                AdminUser.role == ROLE_ADMIN,
                AdminUser.status == STATUS_ACTIVE,
                AdminUser.id != row.id,
            )
        )
        or 0
    )
    if remaining == 0:
        raise HTTPException(status_code=409, detail="至少要保留一个启用状态的管理员")


@router.get("/staff")
def list_staff(
    q: str | None = None,
    role: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin_role),
):
    stmt = select(AdminUser)
    needle = (q or "").strip()
    if needle:
        pattern = f"%{needle}%"
        stmt = stmt.where(
            or_(AdminUser.username.ilike(pattern), AdminUser.display_name.ilike(pattern))
        )
    if role:
        stmt = stmt.where(AdminUser.role == role)
    if status:
        stmt = stmt.where(AdminUser.status == status)
    rows = db.scalars(stmt.order_by(AdminUser.id).limit(200)).all()
    return {"items": [_staff_out(row) for row in rows]}


@router.post("/staff", status_code=201)
def create_staff(
    body: StaffCreateIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    username = _clean_username(body.username)
    _validate_role(body.role)
    _validate_password(body.password, username)
    supplier = None
    if body.role == ROLE_PARTNER:
        if body.supplier_id is None:
            raise HTTPException(status_code=422, detail="上剧方账号必须绑定供应商")
        supplier = db.get(Supplier, body.supplier_id)
        if supplier is None:
            raise HTTPException(status_code=404, detail="supplier not found")
        bound = db.scalar(
            select(AdminUser.id).where(AdminUser.supplier_id == body.supplier_id)
        )
        if bound is not None:
            raise HTTPException(status_code=409, detail="该供应商已有账号")
    elif body.supplier_id is not None:
        raise HTTPException(status_code=422, detail="只有上剧方账号可以绑定供应商")

    if db.scalar(select(AdminUser.id).where(AdminUser.username == username)) is not None:
        raise HTTPException(status_code=409, detail="账号已存在")
    row = AdminUser(
        username=username,
        display_name=body.display_name.strip(),
        role=body.role,
        password_hash=ops_service.hash_password(body.password),
        status=STATUS_ACTIVE,
        supplier_id=body.supplier_id,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:  # concurrent insert of the same username
        db.rollback()
        raise HTTPException(status_code=409, detail="账号已存在") from exc
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=actor.id,
            action="staff_create",
            target_type="admin_user",
            target_id=row.id,
            payload={"username": row.username, "role": row.role},
        )
    )
    db.commit()
    db.refresh(row)
    return _staff_out(row)


@router.patch("/staff/{staff_id}")
def update_staff(
    staff_id: int,
    body: StaffUpdateIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    row = _get_staff_or_404(db, staff_id)
    before: dict = {}
    changed: list[str] = []

    if body.display_name is not None:
        display_name = body.display_name.strip()
        if display_name != row.display_name:
            before["display_name"] = row.display_name
            row.display_name = display_name
            changed.append("display_name")

    if body.role is not None and body.role != row.role:
        _validate_role(body.role)
        if row.id == actor.id:
            raise HTTPException(status_code=400, detail="不能修改自己的角色")
        # Supplier accounts are created together with their tenant; moving a
        # staff account into (or out of) a supplier would orphan its dramas.
        if row.role == ROLE_PARTNER or body.role == ROLE_PARTNER:
            raise HTTPException(
                status_code=400, detail="上剧方账号的角色不可修改，请改绑供应商"
            )
        if row.role == ROLE_ADMIN and row.status == STATUS_ACTIVE:
            _ensure_admin_remains(db, row)
        before["role"] = row.role
        row.role = body.role
        changed.append("role")

    if not changed:
        return {"before": {}, "after": _staff_out(row)}

    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=actor.id,
            action="staff_update",
            target_type="admin_user",
            target_id=row.id,
            payload={
                "changed": changed,
                "from": before,
                "to": {key: getattr(row, key) for key in changed},
            },
        )
    )
    db.commit()
    db.refresh(row)
    return {"before": before, "after": _staff_out(row)}


@router.post("/staff/{staff_id}/status")
def set_staff_status(
    staff_id: int,
    body: StaffStatusIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    if body.status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(STATUSES)}")
    row = _get_staff_or_404(db, staff_id)
    if body.status == row.status:
        return {
            "id": row.id,
            "status": row.status,
            "disable_reason": row.disable_reason,
            "revoked_sessions": 0,
        }

    reason = (body.reason or "").strip() or None
    killed = 0
    if body.status == STATUS_DISABLED:
        if row.id == actor.id:
            raise HTTPException(status_code=400, detail="不能禁用自己")
        if row.role == ROLE_ADMIN and row.status == STATUS_ACTIVE:
            _ensure_admin_remains(db, row)
        # Revoke before commit: if Redis is down the request fails and the
        # account stays usable, which is easier to explain than a disabled
        # account with a live session.
        killed = _revoke_or_503(row.id)

    row.status = body.status
    row.disable_reason = reason if body.status == STATUS_DISABLED else None
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=actor.id,
            action="staff_disable" if body.status == STATUS_DISABLED else "staff_enable",
            target_type="admin_user",
            target_id=row.id,
            payload={"reason": reason, "revoked_sessions": killed},
        )
    )
    db.commit()
    return {
        "id": row.id,
        "status": row.status,
        "disable_reason": row.disable_reason,
        "revoked_sessions": killed,
    }


@router.post("/staff/{staff_id}/password", status_code=204)
def reset_staff_password(
    staff_id: int,
    body: StaffPasswordIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    row = _get_staff_or_404(db, staff_id)
    _validate_password(body.new_password, row.username)
    killed = _revoke_or_503(row.id)
    row.password_hash = ops_service.hash_password(body.new_password)
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=actor.id,
            action="staff_password_reset",
            target_type="admin_user",
            target_id=row.id,
            payload={"revoked_sessions": killed},
        )
    )
    db.commit()
    return Response(status_code=204)
