"""Payment-provider webhooks."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.billing import service as billing
from app.modules.billing.service import BillingError

router = APIRouter(tags=["public-flow"])


@router.post("/billing/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("X-Pay-Signature", "")
    try:
        payload = json.loads(body)
        return billing.apply_webhook(db, body=body, signature=signature, payload=payload)
    except BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/billing/stripe/webhook")
async def stripe_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    signature = request.headers.get("Stripe-Signature", "")
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid payload") from exc
    try:
        return billing.apply_stripe_event(db, body=body, signature=signature, payload=payload)
    except BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
