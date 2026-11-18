"""Pydantic request and response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CheckoutIn(BaseModel):
    plan_code: str
    success_url: str | None = None
    cancel_url: str | None = None
    # Which drama the buyer came from. Optional: opening the paywall from the
    # profile page has no attribution.
    source_drama_id: int | None = None


class PriceIn(BaseModel):
    price_cents: int
    benefits: dict = Field(default_factory=dict)


class BillingModeIn(BaseModel):
    billing_mode: str


class MembershipGrantIn(BaseModel):
    plan_code: str
    days: int | None = Field(default=None, ge=1, le=3650)
    # extend = stack on what is left; restart = count from today (burns the rest)
    mode: str = "extend"
    reason: str = ""


class MembershipRevokeIn(BaseModel):
    # now = lose access immediately; period_end = keep access, stop renewing
    mode: str = "now"
    reason: str = ""
