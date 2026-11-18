"""Administration of client accounts and rewards."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.account.models import User
from app.modules.account.schemas import RewardIn, UserUpdateIn
from app.modules.account.service import (
    AccountError,
    recent_reward_changes,
    reward_config_overview,
    save_reward_config,
)
from app.modules.billing.service import active_membership
from app.modules.common.models import AuditLog
from app.modules.ops.auth import require_admin, require_admin_role, require_content_writer
from app.modules.ops.models import AdminUser
from app.modules.wallet.models import WalletAccount

router = APIRouter(tags=["admin-flow"])


@router.get("/rewards/config")
def get_reward_config(db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    data = reward_config_overview(db)
    data["recent_changes"] = recent_reward_changes(db)
    return data


@router.post("/rewards/config")
def reward_config(body: RewardIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    try:
        row = save_reward_config(db, code=body.code, amounts=body.amounts)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    actor = user.display_name or user.username
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=user.id,
            action="reward_config_update",
            target_type="reward_config",
            target_id=row.id,
            payload={
                "code": row.code,
                "amounts": list(row.amounts),
                "effective_on": row.effective_on.isoformat(),
                "actor": actor,
            },
        )
    )
    db.commit()
    return {"code": row.code, "amounts": list(row.amounts), "effective_on": row.effective_on.isoformat()}


GENDERS = ("unspecified", "female", "male", "other")


@router.get("/users")
def list_users(q: str | None = None, db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    stmt = select(User)
    if q:
        needle = q.strip()
        if needle:
            stmt = stmt.where(
                or_(
                    User.display_name.ilike(f"%{needle}%"),
                    User.public_id == needle,
                    User.email.ilike(f"%{needle}%"),
                )
            )
    rows = db.scalars(stmt.order_by(User.id.desc()).limit(50)).all()
    items = []
    for row in rows:
        wallet = db.get(WalletAccount, row.id)
        # Read the entitlement the same way playback does. Looking at the
        # subscription row directly reported `current_period_end`, which for a
        # one-time pass is just the purchase moment — the real expiry lives in
        # `pass_end`, so the column showed "valid until <a past date>".
        membership = active_membership(db, row.id)
        items.append(
            {
                "id": row.id,
                "public_id": row.public_id,
                "display_name": row.display_name,
                "email": row.email,
                "avatar_url": row.avatar_url,
                "gender": row.gender,
                "bio": row.bio,
                "region": row.region,
                "locale": row.locale,
                "coins": 0 if wallet is None else wallet.balance,
                "member": membership["active"],
                "member_until": membership["until"],
                "canceling": membership["canceling"],
                "billing_mode": membership["billing_mode"],
                "created_at": row.created_at.isoformat(),
            }
        )
    return {"items": items}


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    body: UserUpdateIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    row = db.get(User, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="user not found")

    data = body.model_dump(exclude_unset=True)
    before: dict = {}

    if "display_name" in data:
        name = (data["display_name"] or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="昵称不能为空")
        row.display_name = name
        before["display_name"] = name
    if "email" in data:
        email = (data["email"] or "").strip().lower() or None
        if email is not None and ("@" not in email or email.startswith("@") or email.endswith("@")):
            raise HTTPException(status_code=422, detail="邮箱格式不正确")
        if email is not None:
            conflict = db.scalar(select(User.id).where(User.email == email, User.id != user_id))
            if conflict is not None:
                raise HTTPException(status_code=409, detail="该邮箱已被其他用户绑定")
        row.email = email
        before["email"] = email
    if "gender" in data:
        gender = (data["gender"] or "").strip() or "unspecified"
        if gender not in GENDERS:
            raise HTTPException(status_code=422, detail="性别取值不合法")
        row.gender = gender
        before["gender"] = gender
    if "avatar_url" in data:
        row.avatar_url = (data["avatar_url"] or "").strip()
        before["avatar_url"] = row.avatar_url
    if "bio" in data:
        row.bio = (data["bio"] or "").strip()
        before["bio"] = row.bio
    if "region" in data:
        row.region = (data["region"] or "").strip()
        before["region"] = row.region
    if "locale" in data:
        row.locale = (data["locale"] or "").strip() or "en"
        before["locale"] = row.locale

    if not before:
        return {"updated": False}

    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=actor.id,
            action="user_update",
            target_type="user",
            target_id=user_id,
            payload=before,
        )
    )
    db.commit()
    return {"updated": True, "user": {"id": row.id, "public_id": row.public_id, **before}}
