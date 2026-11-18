"""Pydantic request and response models."""

from __future__ import annotations

from pydantic import BaseModel


class PlaybackIn(BaseModel):
    drama_id: int
    episode_id: int
    country: str | None = None


class EventIn(BaseModel):
    episode_id: int
    position_seconds: int = 0
