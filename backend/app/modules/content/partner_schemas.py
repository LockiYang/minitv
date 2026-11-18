"""Pydantic request and response models."""

from __future__ import annotations

from pydantic import BaseModel, Field

MIN_FREE_EPISODES = 5
DEFAULT_FREE_EPISODES = 5
MAX_EPISODES = 300


class DramaCreateIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    synopsis: str = Field(default="", max_length=5000)
    free_episode_count: int = Field(default=DEFAULT_FREE_EPISODES, ge=0, le=1000)
    country_allowlist: str = Field(default="", max_length=200)
    age_rating: int | None = Field(default=None, ge=0, le=18)


class DramaUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    synopsis: str | None = Field(default=None, max_length=5000)
    cover_url: str | None = Field(default=None, max_length=500)
    tag_ids: list[int] | None = None
    free_episode_count: int | None = Field(default=None, ge=0, le=1000)
    country_allowlist: str | None = Field(default=None, max_length=200)
    age_rating: int | None = Field(default=None, ge=0, le=18)


class EpisodeCreateIn(BaseModel):
    episode_number: int | None = Field(default=None, ge=1, le=MAX_EPISODES)
    title: str = Field(default="", max_length=200)


class EpisodeUpdateIn(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    episode_number: int | None = Field(default=None, ge=1, le=MAX_EPISODES)


class UploadIn(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    mime: str = Field(min_length=1, max_length=64)
    size_bytes: int = Field(ge=1)
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    duration_seconds: int = Field(default=0, ge=0)
