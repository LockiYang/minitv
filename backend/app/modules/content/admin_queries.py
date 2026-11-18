"""Shared content lookup for admin endpoints."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.modules.content.models import Drama


def _drama_or_404(db: Session, drama_id: int) -> Drama:
    drama = db.get(Drama, drama_id)
    if drama is None:
        raise HTTPException(status_code=404, detail="drama not found")
    return drama
