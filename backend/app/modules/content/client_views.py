"""Shared client drama cards and episode counts."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.modules.content.models import Drama, Episode


def _episode_counts(db: Session, drama_ids: list[int]) -> dict[int, int]:
    """Published episode count per drama: the "共 N 集" line on every card."""
    ids = [drama_id for drama_id in set(drama_ids) if drama_id]
    if not ids:
        return {}
    rows = db.execute(
        select(Episode.drama_id, func.count())
        .where(Episode.drama_id.in_(ids), Episode.status == "published")
        .group_by(Episode.drama_id)
    ).all()
    return {int(drama_id): int(count) for drama_id, count in rows}


def _drama_card(drama: Drama, totals: dict[int, int]) -> dict:
    return {
        "id": drama.id,
        "title": drama.title,
        "cover_url": drama.cover_url,
        "episode_count": totals.get(drama.id, 0),
    }
