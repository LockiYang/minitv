"""Administration of suppliers and revenue shares."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.common.models import AuditLog
from app.modules.common.serialization import iso_or_none
from app.modules.content.models import Drama
from app.modules.ops.auth import _revoke_or_503, require_admin, require_admin_role
from app.modules.ops.models import (
    AdminUser,
    STATUSES,
    STATUS_ACTIVE,
    STATUS_DISABLED,
    Supplier,
    SupplierShareHistory,
)
from app.modules.ops.schemas import (
    SupplierCreateIn,
    SupplierShareIn,
    SupplierStatusIn,
    SupplierUpdateIn,
)

router = APIRouter(tags=["admin"])


# ---- suppliers (上剧方) ----
def _supplier_out(db: Session, row: Supplier) -> dict:
    drama_count = (
        db.scalar(select(func.count(Drama.id)).where(Drama.supplier_id == row.id)) or 0
    )
    account = db.scalar(select(AdminUser).where(AdminUser.supplier_id == row.id))
    return {
        "id": row.id,
        "name": row.name,
        "contact_name": row.contact_name,
        "contact_email": row.contact_email,
        "status": row.status,
        "revenue_share_bps": row.revenue_share_bps,
        "note": row.note,
        "drama_count": drama_count,
        "account": (
            {
                "id": account.id,
                "username": account.username,
                "status": account.status,
                "last_login_at": iso_or_none(account.last_login_at),
            }
            if account is not None
            else None
        ),
        "created_at": iso_or_none(row.created_at),
    }


def _get_supplier_or_404(db: Session, supplier_id: int) -> Supplier:
    row = db.get(Supplier, supplier_id)
    if row is None:
        raise HTTPException(status_code=404, detail="supplier not found")
    return row


def _record_share(
    db: Session, supplier: Supplier, share_bps: int, *, actor_id: int, note: str
) -> None:
    """Close the open history row and open a new one (effective now)."""
    now = datetime.now(timezone.utc)
    for row in db.scalars(
        select(SupplierShareHistory).where(
            SupplierShareHistory.supplier_id == supplier.id,
            SupplierShareHistory.effective_to.is_(None),
        )
    ).all():
        row.effective_to = now
    db.add(
        SupplierShareHistory(
            supplier_id=supplier.id,
            share_bps=share_bps,
            effective_from=now,
            changed_by=actor_id,
            note=note,
        )
    )
    supplier.revenue_share_bps = share_bps


@router.get("/suppliers")
def list_suppliers(
    q: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """Readable by every staff role (ops needs it when reviewing)."""
    stmt = select(Supplier)
    needle = (q or "").strip()
    if needle:
        stmt = stmt.where(Supplier.name.ilike(f"%{needle}%"))
    if status:
        stmt = stmt.where(Supplier.status == status)
    rows = db.scalars(stmt.order_by(Supplier.id).limit(200)).all()
    return {"items": [_supplier_out(db, row) for row in rows]}


@router.post("/suppliers", status_code=201)
def create_supplier(
    body: SupplierCreateIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    if db.scalar(select(Supplier.id).where(Supplier.name == body.name.strip())) is not None:
        raise HTTPException(status_code=409, detail="供应商名称已存在")
    row = Supplier(
        name=body.name.strip(),
        contact_name=body.contact_name.strip(),
        contact_email=body.contact_email.strip(),
        revenue_share_bps=body.revenue_share_bps,
        note=body.note.strip(),
        status=STATUS_ACTIVE,
    )
    db.add(row)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="供应商名称已存在") from exc
    _record_share(
        db, row, body.revenue_share_bps, actor_id=actor.id, note="初始比例"
    )
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=actor.id,
            action="supplier_create",
            target_type="supplier",
            target_id=row.id,
            payload={"name": row.name, "share_bps": row.revenue_share_bps},
        )
    )
    db.commit()
    db.refresh(row)
    return _supplier_out(db, row)


@router.patch("/suppliers/{supplier_id}")
def update_supplier(
    supplier_id: int,
    body: SupplierUpdateIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    row = _get_supplier_or_404(db, supplier_id)
    data = body.model_dump(exclude_unset=True)
    changed = []
    for field in ("name", "contact_name", "contact_email", "note"):
        if field in data and data[field] is not None:
            value = data[field].strip() if isinstance(data[field], str) else data[field]
            if value != getattr(row, field):
                setattr(row, field, value)
                changed.append(field)
    if changed:
        db.add(
            AuditLog(
                actor_type="admin",
                actor_id=actor.id,
                action="supplier_update",
                target_type="supplier",
                target_id=row.id,
                payload={"changed": changed},
            )
        )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="供应商名称已存在") from exc
    db.refresh(row)
    return _supplier_out(db, row)


@router.post("/suppliers/{supplier_id}/status")
def set_supplier_status(
    supplier_id: int,
    body: SupplierStatusIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    if body.status not in STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {', '.join(STATUSES)}")
    row = _get_supplier_or_404(db, supplier_id)
    reason = (body.reason or "").strip() or None
    account = db.scalar(select(AdminUser).where(AdminUser.supplier_id == row.id))
    killed = 0
    if body.status == STATUS_DISABLED and account is not None:
        # Disabling the tenant must also cut off its login immediately.
        account.status = STATUS_DISABLED
        account.disable_reason = reason or "供应商停用"
        killed = _revoke_or_503(account.id)
    elif body.status == STATUS_ACTIVE and account is not None:
        account.status = STATUS_ACTIVE
        account.disable_reason = None
    row.status = body.status
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=actor.id,
            action="supplier_disable" if body.status == STATUS_DISABLED else "supplier_enable",
            target_type="supplier",
            target_id=row.id,
            payload={"reason": reason, "revoked_sessions": killed},
        )
    )
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "status": row.status,
        "account_status": account.status if account else None,
        "revoked_sessions": killed,
    }


@router.post("/suppliers/{supplier_id}/share")
def set_supplier_share(
    supplier_id: int,
    body: SupplierShareIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    row = _get_supplier_or_404(db, supplier_id)
    before = row.revenue_share_bps
    if before != body.share_bps:
        _record_share(db, row, body.share_bps, actor_id=actor.id, note=body.note.strip())
        db.add(
            AuditLog(
                actor_type="admin",
                actor_id=actor.id,
                action="supplier_share_change",
                target_type="supplier",
                target_id=row.id,
                payload={"from": before, "to": body.share_bps, "note": body.note},
            )
        )
    db.commit()
    db.refresh(row)
    return {"id": row.id, "revenue_share_bps": row.revenue_share_bps}


@router.get("/suppliers/{supplier_id}/share")
def get_supplier_share(
    supplier_id: int,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    row = _get_supplier_or_404(db, supplier_id)
    history = db.scalars(
        select(SupplierShareHistory)
        .where(SupplierShareHistory.supplier_id == row.id)
        .order_by(SupplierShareHistory.effective_from.desc())
        .limit(50)
    ).all()
    return {
        "supplier_id": row.id,
        "current_bps": row.revenue_share_bps,
        "history": [
            {
                "share_bps": item.share_bps,
                "effective_from": iso_or_none(item.effective_from),
                "effective_to": iso_or_none(item.effective_to),
                "changed_by": item.changed_by,
                "note": item.note,
            }
            for item in history
        ],
    }
