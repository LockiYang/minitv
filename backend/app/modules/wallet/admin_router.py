"""Admin wallet adjustments and finance ledger."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.account.models import User
from app.modules.common.models import AuditLog
from app.modules.ops.auth import require_admin, require_admin_role
from app.modules.ops.models import AdminUser
from app.modules.wallet.models import CoinLedgerEntry, WalletAccount
from app.modules.wallet.schemas import AdjustIn

router = APIRouter(tags=["admin-flow"])


@router.post("/users/{user_id}/coins")
def adjust(user_id: int, body: AdjustIn, db: Session = Depends(get_db), actor: AdminUser = Depends(require_admin_role)):
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    wallet = db.get(WalletAccount, user_id, with_for_update=True)
    if wallet is None:
        # Legacy rows may predate the wallet table; create it on demand so the
        # operator does not have to run a migration just to grant coins.
        wallet = WalletAccount(user_id=user_id, balance=0)
        db.add(wallet)
        db.flush()
    before = wallet.balance
    after = before + body.delta
    if after < 0:
        raise HTTPException(status_code=409, detail="balance cannot be negative")
    wallet.balance = after
    db.add(
        CoinLedgerEntry(
            user_id=user_id,
            amount=body.delta,
            balance_after=after,
            biz_type="adjustment",
            biz_key=f"adjust:{actor.id}:{user_id}:{before}:{body.delta}:{datetime.now(timezone.utc).timestamp()}",
            note=body.reason,
        )
    )
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=actor.id,
            action="coin_adjust",
            target_type="user",
            target_id=user_id,
            payload={"before": before, "delta": body.delta, "after": after, "reason": body.reason},
        )
    )
    db.commit()
    return {"before": before, "delta": body.delta, "after": after}


@router.get("/finance/ledger")
def finance_ledger(db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    rows = db.scalars(select(CoinLedgerEntry).order_by(CoinLedgerEntry.id.desc()).limit(100)).all()
    return {"items": [{"user_id": row.user_id, "amount": row.amount, "biz_type": row.biz_type, "note": row.note} for row in rows]}
