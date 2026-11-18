"""Pydantic request and response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.ops.api_keys import DEFAULT_TTL_DAYS
from app.modules.ops.models import ROLE_OPS


# ---- schemas ----
class AdminUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    display_name: str
    role: str


class KeyCreateIn(BaseModel):
    user_id: int
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] | None = None
    # ``None`` explicitly means "never expires" and is admin-only by design
    # (the endpoint itself is admin-only).
    expires_in_days: int | None = Field(default=DEFAULT_TTL_DAYS)


class KeyUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    scopes: list[str] | None = None
    expires_at: datetime | None = None


class RevokeIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class RotateIn(BaseModel):
    expires_in_days: int | None = Field(default=DEFAULT_TTL_DAYS)


class GenerateIn(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")


class StatusIn(BaseModel):
    status: str = Field(pattern="^(confirmed|paid)$")
    note: str = Field(default="", max_length=500)


class LoginIn(BaseModel):
    username: str
    password: str


class StaffCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(default="", max_length=64)
    role: str = Field(default=ROLE_OPS)
    password: str = Field(min_length=8, max_length=128)
    # Required when role=partner, forbidden otherwise (one account per supplier).
    supplier_id: int | None = None


class StaffUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, max_length=64)
    role: str | None = None


class StaffStatusIn(BaseModel):
    status: str
    reason: str | None = Field(default=None, max_length=200)


class StaffPasswordIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class PasswordChangeIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class SupplierCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    contact_name: str = Field(default="", max_length=64)
    contact_email: str = Field(default="", max_length=320)
    revenue_share_bps: int = Field(default=8000, ge=0, le=10000)
    note: str = Field(default="", max_length=500)


class SupplierUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    contact_name: str | None = Field(default=None, max_length=64)
    contact_email: str | None = Field(default=None, max_length=320)
    note: str | None = Field(default=None, max_length=500)


class SupplierStatusIn(BaseModel):
    status: str
    reason: str | None = Field(default=None, max_length=200)


class SupplierShareIn(BaseModel):
    share_bps: int = Field(ge=0, le=10000)
    note: str = Field(default="", max_length=200)


# ---- schemas ----
class PartnerPasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class PartnerKeyCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] | None = None
    expires_in_days: int | None = Field(default=90)


class PartnerKeyRevokeIn(BaseModel):
    reason: str | None = Field(default=None, max_length=200)


class PartnerKeyUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    scopes: list[str] | None = None
