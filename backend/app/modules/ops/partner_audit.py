"""Audit records for supplier operations."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.common.models import AuditLog
from app.modules.ops.models import AdminUser, ROLE_PARTNER


def _audit(db: Session, actor: AdminUser, action: str, drama_id: int | None, payload: dict) -> None:
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=actor.id,
            action=action,
            target_type="drama" if drama_id else "supplier",
            target_id=drama_id or actor.supplier_id,
            payload={**payload, "actor_role": ROLE_PARTNER, "supplier_id": actor.supplier_id},
        )
    )
