"""Pydantic request and response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CodeIn(BaseModel):
    email: str
    purpose: str = "login"


class RegisterIn(BaseModel):
    email: str
    code: str
    password: str | None = None


class LoginIn(BaseModel):
    email: str
    code: str


class GoogleIn(BaseModel):
    id_token: str = Field(min_length=1)


class FacebookIn(BaseModel):
    """Either a Limited Login id_token or an SDK access token, never both."""

    access_token: str | None = Field(default=None, min_length=1)
    id_token: str | None = Field(default=None, min_length=1)


class ProfileIn(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None
    gender: str | None = None
    bio: str | None = None
    birthday: str | None = None
    region: str | None = None
    locale: str | None = None


class RewardIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    amounts: list[int] = Field(min_length=1, max_length=7)


class UserUpdateIn(BaseModel):
    """Admin-side profile edit. Omitted fields stay untouched."""

    display_name: str | None = Field(default=None, max_length=64)
    email: str | None = Field(default=None, max_length=320)
    avatar_url: str | None = Field(default=None, max_length=500)
    gender: str | None = Field(default=None, max_length=16)
    bio: str | None = Field(default=None, max_length=500)
    region: str | None = Field(default=None, max_length=8)
    locale: str | None = Field(default=None, max_length=8)
