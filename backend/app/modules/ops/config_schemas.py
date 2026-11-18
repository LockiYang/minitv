"""Pydantic request and response models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class CopyIn(BaseModel):
    key: str
    en: str = ""
    zh: str = ""


class TabIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    title_en: str = Field(min_length=1, max_length=64)
    title_zh: str = Field(default="", max_length=64)
    position: int = 0
    enabled: bool = True
    group_id: int | None = None


class TabUpdateIn(BaseModel):
    title_en: str | None = Field(default=None, max_length=64)
    title_zh: str | None = Field(default=None, max_length=64)
    position: int | None = None
    enabled: bool | None = None
    group_id: int | None = None


class BannerIn(BaseModel):
    drama_id: int
    slot: str = "home"
    status: str = "online"
    position: int = 0
    kicker: str = Field(default="", max_length=80)
    headline: str = Field(default="", max_length=80)


class BannerUpdateIn(BaseModel):
    drama_id: int | None = None
    status: str | None = None
    position: int | None = None
    kicker: str | None = Field(default=None, max_length=80)
    headline: str | None = Field(default=None, max_length=80)
