"""Client playback authorization, events and history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.account.auth import optional_user as _optional_user, require_user as _require_user
from app.modules.account.models import User
from app.modules.content import service as content
from app.modules.content.client_views import _drama_card, _episode_counts
from app.modules.content.models import Drama, Episode, Tag
from app.modules.playback import service as playback
from app.modules.playback.models import PlaybackEvent, PlaybackProgress
from app.modules.playback.schemas import EventIn, PlaybackIn
from app.modules.social.models import Comment, Reaction
from app.modules.wallet.models import ENTITLEMENT_TYPE_COIN, Entitlement

router = APIRouter(tags=["public-flow"])


def _tags_of(db: Session, drama_id: int) -> list[Tag]:
    return content.tags_by_drama(db, [drama_id]).get(drama_id, [])


@router.post("/playback")
def playback_auth(body: PlaybackIn, user: User | None = Depends(_optional_user), db: Session = Depends(get_db)):
    result = playback.authorize(
        db, drama_id=body.drama_id, episode_id=body.episode_id, user_id=None if user is None else user.id, country=body.country
    )
    if result.get("reason") == "not_available":
        raise HTTPException(status_code=404, detail="not available")
    return result


@router.post("/playback/start")
def playback_start(body: EventIn, user: User | None = Depends(_optional_user), db: Session = Depends(get_db)):
    playback.record_event(db, episode_id=body.episode_id, kind="start", position_seconds=body.position_seconds, user_id=None if user is None else user.id)
    return {"ok": True}


@router.post("/playback/progress")
def playback_progress(body: EventIn, user: User | None = Depends(_optional_user), db: Session = Depends(get_db)):
    playback.record_event(db, episode_id=body.episode_id, kind="progress", position_seconds=body.position_seconds, user_id=None if user is None else user.id)
    return {"ok": True}


@router.post("/playback/complete")
def playback_complete(body: EventIn, user: User | None = Depends(_optional_user), db: Session = Depends(get_db)):
    playback.record_event(db, episode_id=body.episode_id, kind="complete", position_seconds=body.position_seconds, user_id=None if user is None else user.id)
    return {"ok": True}


@router.get("/dramas/{drama_id}/play")
def drama_play(drama_id: int, user: User | None = Depends(_optional_user), db: Session = Depends(get_db)):
    """The drama's own play page: every episode, its lock state, and the resume point.

    Two doors lead here - the viewer taps a poster by hand, or the For-You player
    finishes an episode and hands off to this page. Either way the payload has to
    stand alone: the vertical episode scroller is painted from this one response,
    so there is no second call to fetch the episode list.

    Playback URLs are still absent; `POST /v1/playback` mints those and applies
    the free / membership / coin-unlock rules.
    """
    drama = content.get_published_drama(db, drama_id)
    if drama is None:
        raise HTTPException(status_code=404, detail="drama not found")

    uid = None if user is None else user.id
    member = playback.subscription_active(db, uid)
    progress_rows = (
        {
            episode_id: int(position or 0)
            for episode_id, position in db.execute(
                select(PlaybackProgress.episode_id, PlaybackProgress.position_seconds).where(
                    PlaybackProgress.user_id == uid
                )
            )
        }
        if uid is not None
        else {}
    )
    completed = (
        set(
            db.scalars(
                select(PlaybackEvent.episode_id).where(
                    PlaybackEvent.user_id == uid, PlaybackEvent.kind == "complete"
                )
            )
        )
        if uid is not None
        else set()
    )
    unlocked = (
        set(
            db.scalars(
                select(Entitlement.episode_id).where(
                    Entitlement.user_id == uid,
                    Entitlement.entitlement_type == ENTITLEMENT_TYPE_COIN,
                )
            )
        )
        if uid is not None
        else set()
    )

    items: list[dict] = []
    resume: dict | None = None
    episodes = content.list_published_episodes(db, drama_id)
    # One grouped query keeps the payload from turning into N+1 counts.
    comment_counts = (
        {
            int(episode_id): int(total)
            for episode_id, total in db.execute(
                select(Comment.episode_id, func.count())
                .where(
                    Comment.episode_id.in_([episode.id for episode in episodes]),
                    Comment.status == "approved",
                )
                .group_by(Comment.episode_id)
            )
        }
        if episodes
        else {}
    )
    if uid is not None and episodes:
        # The author's own pending rows count for them only, so the rail number
        # matches what their comment sheet actually shows.
        for episode_id, total in db.execute(
            select(Comment.episode_id, func.count())
            .where(
                Comment.episode_id.in_([episode.id for episode in episodes]),
                Comment.status == "pending",
                Comment.user_id == uid,
            )
            .group_by(Comment.episode_id)
        ):
            key = int(episode_id)
            comment_counts[key] = comment_counts.get(key, 0) + int(total)
    for episode in episodes:
        free = episode.episode_number <= drama.free_episode_count
        locked = not (free or member or episode.id in unlocked)
        position = progress_rows.get(episode.id, 0)
        items.append(
            {
                "id": episode.id,
                "episode_number": episode.episode_number,
                "title": episode.title,
                "duration_seconds": episode.duration_seconds,
                "free": free,
                "locked": locked,
                "progress_seconds": position,
                # A finished episode reads differently from an untouched one in
                # the episode sheet, so the flag travels with the row.
                "completed": episode.id in completed,
                "comment_count": comment_counts.get(episode.id, 0),
                "unlock": {"coin_price": drama.coin_price_per_episode} if locked else None,
            }
        )
        # Resume = the first watchable episode the viewer started but did not
        # finish; otherwise the first watchable episode at all.
        if resume is None and not locked and position > 0 and episode.id not in completed:
            resume = {"episode_id": episode.id, "position_seconds": position}
    if resume is None:
        first = next((item for item in items if not item["locked"]), None)
        resume = {"episode_id": first["id"], "position_seconds": 0} if first else None

    tags = _tags_of(db, drama.id)
    like_count = db.scalar(
        select(func.count())
        .select_from(Reaction)
        .where(Reaction.target_type == "drama", Reaction.target_id == drama.id, Reaction.kind == "like")
    ) or 0
    comment_count = db.scalar(
        select(func.count())
        .select_from(Comment)
        .where(Comment.drama_id == drama.id, Comment.status == "approved")
    ) or 0
    mine: set[str] = set()
    if uid is not None:
        mine = set(
            db.scalars(
                select(Reaction.kind).where(
                    Reaction.user_id == uid,
                    Reaction.target_type == "drama",
                    Reaction.target_id == drama.id,
                )
            ).all()
        )
    return {
        "drama": {
            "id": drama.id,
            "title": drama.title,
            "synopsis": drama.synopsis,
            "cover_url": drama.cover_url,
            "tags": [tag.name_en for tag in tags],
            "free_episode_count": drama.free_episode_count,
            "coin_price_per_episode": drama.coin_price_per_episode,
            "play_count": drama.play_count,
            "like_count": int(like_count),
            "comment_count": int(comment_count),
            "liked": "like" in mine,
            "favorited": "favorite" in mine,
        },
        "items": items,
        "resume": resume,
        "entitlement": {
            "member": member,
            "unlocked_episode_ids": sorted(unlocked),
        },
    }


@router.get("/me/history")
def history(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    """Watch history as cards: newest progress first, joined to drama + episode.

    The Me page paints a poster grid, so returning bare episode ids would leave
    the client with nothing to show. Unpublished dramas are filtered out the
    same way favorites are, and the legacy episode fields stay for old clients.
    """
    rows = db.execute(
        select(PlaybackProgress, Episode, Drama)
        .join(Episode, Episode.id == PlaybackProgress.episode_id)
        .join(Drama, Drama.id == Episode.drama_id)
        .where(PlaybackProgress.user_id == user.id, Drama.status == "published")
        .order_by(PlaybackProgress.updated_at.desc())
        .limit(60)
    ).all()
    totals = _episode_counts(db, [drama.id for _, _, drama in rows])
    return {
        "items": [
            {
                "episode_id": progress.episode_id,
                "position_seconds": progress.position_seconds,
                "episode_number": episode.episode_number,
                "duration_seconds": episode.duration_seconds,
                "updated_at": progress.updated_at.isoformat(),
                "drama": _drama_card(drama, totals),
            }
            for progress, episode, drama in rows
        ]
    }
