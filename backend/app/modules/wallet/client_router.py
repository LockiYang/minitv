"""Client coin unlock and wallet ledger."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.account.auth import require_user as _require_user
from app.modules.account.models import User
from app.modules.wallet.models import CoinLedgerEntry
from app.modules.wallet.schemas import UnlockIn
from app.modules.wallet.service import (
    EpisodeNotFoundError,
    EpisodeNotPurchasableError,
    InsufficientCoinsError,
    unlock_episode,
)

router = APIRouter(tags=["public-flow"])


@router.post("/unlock")
def unlock(body: UnlockIn, request: Request, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    key = request.headers.get("Idempotency-Key")
    if not key:
        raise HTTPException(status_code=400, detail="Idempotency-Key required")
    try:
        result = unlock_episode(db, user_id=user.id, episode_id=body.episode_id, idempotency_key=key)
    except InsufficientCoinsError as exc:
        raise HTTPException(status_code=402, detail={"need": exc.need, "have": exc.have}) from exc
    except EpisodeNotPurchasableError as exc:
        raise HTTPException(status_code=409, detail="episode is not purchasable") from exc
    except EpisodeNotFoundError as exc:
        raise HTTPException(status_code=404, detail="episode not found") from exc
    return result.__dict__


@router.get("/me/ledger")
def ledger(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(CoinLedgerEntry).where(CoinLedgerEntry.user_id == user.id).order_by(CoinLedgerEntry.id.desc())).all()
    return {
        "items": [
            {
                "amount": row.amount,
                "balance_after": row.balance_after,
                "biz_type": row.biz_type,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ]
    }
