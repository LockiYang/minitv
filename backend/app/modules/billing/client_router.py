"""Client billing, checkout and purchase history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.account.auth import require_user as _require_user
from app.modules.account.models import User
from app.modules.billing import service as billing
from app.modules.billing.schemas import CheckoutIn
from app.modules.billing.service import BillingError

router = APIRouter(tags=["public-flow"])


@router.get("/me/orders")
def my_orders(
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    limit = max(1, min(limit, 50))
    return billing.list_user_orders(db, user.id, limit=limit, offset=max(0, offset))


@router.get("/plans")
def plans(db: Session = Depends(get_db)):
    return {"items": billing.list_current_plans(db), "billing_mode": billing.billing_mode(db)}


@router.post("/billing/cancel")
def cancel(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    try:
        billing.cancel_subscription(db, user.id)
    except BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"ok": True}


@router.post("/billing/checkout")
def checkout(body: CheckoutIn, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    """Start a Stripe Checkout for one of the plans and hand back the URL."""
    try:
        return billing.create_checkout_session(
            db,
            user_id=user.id,
            email=user.email,
            plan_code=body.plan_code,
            success_url=body.success_url,
            cancel_url=body.cancel_url,
            source_drama_id=body.source_drama_id,
        )
    except BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
