"""Pydantic request and response models."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class UnlockIn(BaseModel):
    episode_id: int


class AdjustIn(BaseModel):
    delta: int
    reason: str = Field(min_length=1)

    @field_validator("delta")
    @classmethod
    def _nonzero(cls, value: int) -> int:
        # A zero delta would insert a coin_ledger row that violates
        # ck_coin_ledger_amount_nonzero, so reject it before hitting the DB.
        if value == 0:
            raise ValueError("delta must be non-zero")
        return value
