"""Client comments, reactions and inbox."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.account.auth import optional_user as _optional_user, require_user as _require_user
from app.modules.account.models import User
from app.modules.content.client_views import _drama_card, _episode_counts
from app.modules.content.models import Drama, Episode
from app.modules.social import moderation
from app.modules.social.models import Comment, InboxMessage, Reaction
from app.modules.social.schemas import CommentIn, EpisodeCommentIn, ReactionIn

router = APIRouter(tags=["public-flow"])


@router.get("/dramas/{drama_id}/comments")
def list_comments(drama_id: int, db: Session = Depends(get_db)):
    """Legacy drama-wide list. Kept for older clients; new UI reads per episode."""
    rows = db.scalars(
        select(Comment)
        .where(Comment.drama_id == drama_id, Comment.status == "approved")
        .order_by(Comment.id.desc())
    ).all()
    return {"items": [{"id": c.id, "body": c.body, "user_id": c.user_id} for c in rows]}


COMMENT_PAGE_MAX = 50


COMMENT_COOLDOWN_SECONDS = 30


COMMENT_DAILY_LIMIT = 50


def _comment_user(user: User | None) -> dict:
    if user is None:
        return {"public_id": "", "display_name": "Deleted user", "avatar_url": ""}
    return {
        "public_id": user.public_id or "",
        "display_name": user.display_name or "MiniTV user",
        "avatar_url": user.avatar_url or "",
    }


def _comment_payload(row: Comment, user: User | None, *, like_count: int = 0, liked: bool = False) -> dict:
    return {
        "id": row.id,
        "body": row.body,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "like_count": int(like_count),
        "liked": liked,
        "mine": False,
        "user": _comment_user(user),
    }


def _comment_visibility(user: User | None):
    if user is None:
        return Comment.status == "approved"
    return or_(
        Comment.status == "approved",
        and_(Comment.status == "pending", Comment.user_id == user.id),
    )


def _comment_total(db: Session, episode_id: int, user: User | None) -> int:
    """Public approved count plus the viewer's own pending rows (kept in sync
    with what the list shows, and with per-viewer comment_count)."""
    public = db.scalar(
        select(func.count())
        .select_from(Comment)
        .where(Comment.episode_id == episode_id, Comment.status == "approved")
    ) or 0
    own_pending = 0
    if user is not None:
        own_pending = db.scalar(
            select(func.count())
            .select_from(Comment)
            .where(
                Comment.episode_id == episode_id,
                Comment.status == "pending",
                Comment.user_id == user.id,
            )
        ) or 0
    return int(public) + int(own_pending)


@router.get("/episodes/{episode_id}/comments")
def list_episode_comments(
    episode_id: int,
    limit: int = 20,
    cursor: int | None = None,
    user: User | None = Depends(_optional_user),
    db: Session = Depends(get_db),
):
    """One episode, one thread.

    Everyone sees approved comments. A signed-in viewer additionally sees their
    own pending rows, so posting feels instant without leaking unmoderated
    content to anyone else.
    """
    episode = db.get(Episode, episode_id)
    if episode is None or episode.status != "published":
        raise HTTPException(status_code=404, detail="episode not found")

    size = max(1, min(int(limit), COMMENT_PAGE_MAX))
    stmt = (
        select(Comment, User)
        .join(User, User.id == Comment.user_id)
        .where(Comment.episode_id == episode_id, _comment_visibility(user))
    )
    if cursor:
        stmt = stmt.where(Comment.id < cursor)
    rows = db.execute(stmt.order_by(Comment.id.desc()).limit(size + 1)).all()
    has_more = len(rows) > size
    rows = rows[:size]
    # The cursor is the last row on this page, not the peeked extra one.
    next_cursor = rows[-1][0].id if has_more and rows else None

    ids = [row[0].id for row in rows]
    like_counts: dict[int, int] = {}
    liked: set[int] = set()
    if ids:
        like_counts = {
            int(target_id): int(total)
            for target_id, total in db.execute(
                select(Reaction.target_id, func.count())
                .where(
                    Reaction.target_type == "comment",
                    Reaction.kind == "like",
                    Reaction.target_id.in_(ids),
                )
                .group_by(Reaction.target_id)
            )
        }
        if user is not None:
            liked = {
                int(target_id)
                for target_id in db.scalars(
                    select(Reaction.target_id).where(
                        Reaction.user_id == user.id,
                        Reaction.target_type == "comment",
                        Reaction.kind == "like",
                        Reaction.target_id.in_(ids),
                    )
                )
            }

    total = _comment_total(db, episode_id, user)

    items = []
    for comment, author in rows:
        item = _comment_payload(
            comment,
            author,
            like_count=like_counts.get(comment.id, 0),
            liked=comment.id in liked,
        )
        item["mine"] = user is not None and comment.user_id == user.id
        item["pending"] = comment.status == "pending"
        items.append(item)

    return {"items": items, "next_cursor": next_cursor, "total": int(total)}


def _published_episode_or_404(db: Session, episode_id: int) -> Episode:
    episode = db.get(Episode, episode_id)
    if episode is None or episode.status != "published":
        raise HTTPException(status_code=404, detail="episode not found")
    drama = db.get(Drama, episode.drama_id)
    if drama is None or drama.status != "published":
        raise HTTPException(status_code=404, detail="episode not found")
    return episode


@router.post("/episodes/{episode_id}/comments")
def create_episode_comment(
    episode_id: int,
    body: EpisodeCommentIn,
    background: BackgroundTasks,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Post into one episode's thread and return immediately.

    Visibility starts as pending: the author sees it right away, everyone else
    sees it only after the background worker approves it.
    """
    episode = _published_episode_or_404(db, episode_id)
    text = body.body.strip()
    if not text:
        raise HTTPException(status_code=422, detail="comment is empty")

    now = datetime.now(timezone.utc)
    recent = db.scalar(
        select(Comment.id)
        .where(
            Comment.user_id == user.id,
            Comment.episode_id == episode_id,
            Comment.created_at >= now - timedelta(seconds=COMMENT_COOLDOWN_SECONDS),
        )
        .limit(1)
    )
    if recent is not None:
        raise HTTPException(status_code=429, detail="You are commenting too fast. Try again in a moment.")
    daily = db.scalar(
        select(func.count())
        .select_from(Comment)
        .where(Comment.user_id == user.id, Comment.created_at >= now - timedelta(days=1))
    ) or 0
    if daily >= COMMENT_DAILY_LIMIT:
        raise HTTPException(status_code=429, detail="Daily comment limit reached.")

    queue = moderation.queue_state(db)
    row = Comment(
        user_id=user.id,
        drama_id=episode.drama_id,
        episode_id=episode.id,
        body=text,
        status="pending",
        moderation_state=queue,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    if queue:
        # Runs after the response is sent; the sweeper retries if this process dies.
        background.add_task(moderation.process_comment, row.id)

    payload = _comment_payload(row, user)
    payload["mine"] = True
    payload["pending"] = True
    return {
        "id": row.id,
        "status": "pending",
        "item": payload,
        "total": _comment_total(db, episode_id, user),
        "moderation": {"state": queue or "disabled"},
    }


@router.post("/comments")
def create_comment(
    body: CommentIn,
    background: BackgroundTasks,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """Backward-compatible alias: episode is now mandatory."""
    if body.episode_id is None:
        raise HTTPException(status_code=422, detail="episode_id is required")
    return create_episode_comment(
        body.episode_id, EpisodeCommentIn(body=body.body), background, user, db
    )


@router.delete("/comments/{comment_id}")
def delete_comment(comment_id: int, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    row = db.get(Comment, comment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="comment not found")
    if row.user_id != user.id:
        raise HTTPException(status_code=403, detail="not your comment")
    if row.status == "deleted":
        return {"status": "deleted"}
    if row.created_at < datetime.now(timezone.utc) - timedelta(hours=24):
        raise HTTPException(status_code=400, detail="Comments can only be deleted within 24 hours.")
    row.status = "deleted"
    db.commit()
    return {"status": "deleted"}


@router.post("/reactions")
def react(body: ReactionIn, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    if body.target_type not in {"drama", "episode", "comment"}:
        raise HTTPException(status_code=422, detail="bad target_type")
    if body.target_type == "comment":
        target = db.get(Comment, body.target_id)
        if target is None or target.status != "approved":
            raise HTTPException(status_code=404, detail="comment not found")
    existing = db.scalar(
        select(Reaction).where(
            Reaction.user_id == user.id,
            Reaction.target_type == body.target_type,
            Reaction.target_id == body.target_id,
            Reaction.kind == body.kind,
        )
    )
    if body.remove:
        if existing is not None:
            db.delete(existing)
            db.commit()
        return {"active": False}
    if existing is None:
        db.add(Reaction(user_id=user.id, target_type=body.target_type, target_id=body.target_id, kind=body.kind))
        db.commit()
    return {"active": True}


@router.get("/me/favorites")
def my_favorites(
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(_require_user),
    db: Session = Depends(get_db),
):
    """The dramas this viewer saved, newest first.

    Favorites are a reactions row, but the client thinks in drama cards, so the
    join filters out anything unpublished since it was saved.
    """
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    base = (
        select(Drama)
        .join(Reaction, Reaction.target_id == Drama.id)
        .where(
            Reaction.user_id == user.id,
            Reaction.target_type == "drama",
            Reaction.kind == "favorite",
            Drama.status == "published",
        )
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(base.order_by(Reaction.id.desc()).offset(offset).limit(limit)).all()
    totals = _episode_counts(db, [drama.id for drama in rows])
    return {
        "total": int(total),
        "items": [
            {
                "id": drama.id,
                "title": drama.title,
                "cover_url": drama.cover_url,
                "synopsis": drama.synopsis,
                "play_count": drama.play_count,
                "episode_count": totals.get(drama.id, 0),
            }
            for drama in rows
        ],
    }


@router.get("/me/messages")
def messages(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(InboxMessage).where(InboxMessage.user_id == user.id).order_by(InboxMessage.id.desc())).all()
    unread = sum(1 for row in rows if row.read_at is None)
    return {
        "unread": unread,
        "items": [
            {"id": row.id, "kind": row.kind, "title": row.title, "body": row.body, "read": row.read_at is not None}
            for row in rows
        ],
    }


@router.post("/me/messages/read")
def read_messages(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for row in db.scalars(select(InboxMessage).where(InboxMessage.user_id == user.id, InboxMessage.read_at.is_(None))):
        row.read_at = now
    db.commit()
    return {"unread": 0}


@router.get("/me/lists/{kind}")
def my_list(kind: str, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Reaction).where(Reaction.user_id == user.id, Reaction.kind == kind)).all()
    drama_ids = [row.target_id for row in rows if row.target_type == "drama"]
    dramas = {}
    if drama_ids:
        dramas = {
            drama.id: drama
            for drama in db.scalars(
                select(Drama).where(Drama.id.in_(drama_ids), Drama.status == "published")
            ).all()
        }
    totals = _episode_counts(db, list(dramas))
    items = []
    for row in rows:
        if row.target_type == "drama":
            drama = dramas.get(row.target_id)
            if drama is None:  # unpublished since it was liked
                continue
            items.append(
                {
                    "target_type": row.target_type,
                    "target_id": row.target_id,
                    "drama": _drama_card(drama, totals),
                }
            )
        else:
            items.append({"target_type": row.target_type, "target_id": row.target_id})
    return {"total": len(items), "items": items}
