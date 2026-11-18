"""Pydantic request and response models."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


MAX_BATCH = 50


class DecisionIn(BaseModel):
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=500)


class BatchIn(BaseModel):
    drama_ids: list[int] = Field(min_length=1, max_length=MAX_BATCH)
    decision: str = Field(pattern="^(approve|reject)$")
    note: str = Field(default="", max_length=500)


class EpisodeIn(BaseModel):
    episode_number: int = Field(ge=1)
    title: str = Field(default="", max_length=200)


class BulkIn(BaseModel):
    count: int = Field(ge=1, le=200)


class UploadIn(BaseModel):
    filename: str = Field(max_length=255)
    mime: str
    size_bytes: int = Field(ge=0)
    width: int
    height: int
    # Browser-probed length; optional so older clients keep working.
    duration_seconds: int = Field(default=0, ge=0)


class UploadFailedIn(BaseModel):
    """Browser-side upload failure report (see ``upload_failed`` below)."""

    video_id: str = ""
    reason: str = ""


class EpisodeUpdateIn(BaseModel):
    """Partial metadata update for one episode (title / episode number)."""

    title: str | None = Field(default=None, max_length=200)
    episode_number: int | None = Field(default=None, ge=1, le=2000)


class StatusIn(BaseModel):
    to_status: str


class EpisodeBulkStatusIn(BaseModel):
    episode_ids: list[int] = Field(min_length=1, max_length=200)
    action: str = Field(pattern="^(publish|unpublish)$")


class ScheduleIn(BaseModel):
    at: datetime


class DeleteIn(BaseModel):
    confirm: bool = False


FLOOR_STYLES = {"poster3", "rank", "grid2"}


FLOOR_BADGES = {"rank", "label"}


class GroupIn(BaseModel):
    name: str
    position: int = 0


class GroupUpdateIn(BaseModel):
    """Rename, reorder, restyle, or pause a homepage group. Any subset of fields is accepted."""

    name: str | None = Field(default=None, max_length=64)
    position: int | None = None
    status: str | None = None
    style: str | None = None
    badge: str | None = None

    @field_validator("status")
    @classmethod
    def _status(cls, value: str | None) -> str | None:
        if value is not None and value not in {"active", "inactive"}:
            raise ValueError("status must be active or inactive")
        return value

    @field_validator("style")
    @classmethod
    def _style(cls, value: str | None) -> str | None:
        if value is not None and value not in FLOOR_STYLES:
            raise ValueError("style must be poster3, rank or grid2")
        return value

    @field_validator("badge")
    @classmethod
    def _badge(cls, value: str | None) -> str | None:
        if value is not None and value not in FLOOR_BADGES:
            raise ValueError("badge must be rank or label")
        return value


class GroupAddIn(BaseModel):
    drama_id: int
    confirm: bool = False


class PositionIn(BaseModel):
    position: int


class DramaCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class DramaUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    synopsis: str | None = Field(default=None, max_length=20000)
    cover_url: str | None = Field(default=None, max_length=500)
    free_episode_count: int | None = Field(default=None, ge=0)
    coin_price_per_episode: int | None = Field(default=None, ge=0)
    tag_ids: list[int] | None = None


class TagIn(BaseModel):
    name_en: str = Field(min_length=1, max_length=64)
    name_zh: str = Field(default="", max_length=64)
    category: str = Field(default="", max_length=32)
    status: str = Field(default="enabled", max_length=16)


class TagUpdateIn(BaseModel):
    name_en: str | None = Field(default=None, min_length=1, max_length=64)
    name_zh: str | None = Field(default=None, max_length=64)
    category: str | None = Field(default=None, max_length=32)
    status: str | None = Field(default=None, max_length=16)
