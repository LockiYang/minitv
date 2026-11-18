"""Admin comment moderation and push records."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.account.models import User
from app.modules.common.models import AuditLog
from app.modules.content.models import Drama, Episode
from app.modules.ops.auth import require_admin, require_admin_role, require_content_writer
from app.modules.ops.models import AdminUser
from app.modules.social import moderation
from app.modules.social.models import Comment, ModerationSettings, PushRecord
from app.modules.social.schemas import CommentModIn, ModerationSettingsIn, ModerationTestIn

router = APIRouter(tags=["admin-flow"])


@router.get("/comments")
def admin_comments(
    status: str | None = None,
    drama_id: int | None = None,
    episode_id: int | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """Moderation queue with everything the reviewer needs in one row."""
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    stmt = (
        select(
            Comment,
            Drama.title,
            Episode.episode_number,
            User.display_name,
            User.email,
        )
        .join(Drama, Drama.id == Comment.drama_id)
        .join(Episode, Episode.id == Comment.episode_id, isouter=True)
        .join(User, User.id == Comment.user_id)
    )
    if status:
        stmt = stmt.where(Comment.status == status)
    if drama_id:
        stmt = stmt.where(Comment.drama_id == drama_id)
    if episode_id:
        stmt = stmt.where(Comment.episode_id == episode_id)
    if q and q.strip():
        stmt = stmt.where(Comment.body.ilike(f"%{q.strip()}%"))

    # Tab counters: same filters except status, so the numbers always match what the tabs would show.
    counts_stmt = select(Comment.status, func.count()).group_by(Comment.status)
    if drama_id:
        counts_stmt = counts_stmt.where(Comment.drama_id == drama_id)
    if episode_id:
        counts_stmt = counts_stmt.where(Comment.episode_id == episode_id)
    if q and q.strip():
        counts_stmt = counts_stmt.where(Comment.body.ilike(f"%{q.strip()}%"))
    counts = {"pending": 0, "approved": 0, "deleted": 0, "total": 0}
    for row_status, row_count in db.execute(counts_stmt).all():
        counts[row_status] = int(row_count)
        counts["total"] += int(row_count)

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(Comment.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "body": row.body,
                "status": row.status,
                "drama_id": row.drama_id,
                "drama_title": drama_title,
                "episode_id": row.episode_id,
                "episode_number": episode_number,
                "user_id": row.user_id,
                "user_display_name": display_name,
                "user_email": email,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "moderation_verdict": row.moderation_verdict,
                "moderation_score": row.moderation_score,
                "moderation_reason": row.moderation_reason,
                "moderation_model": row.moderation_model,
                "moderation_state": row.moderation_state,
            }
            for row, drama_title, episode_number, display_name, email in rows
        ],
        "total": int(total),
        "counts": counts,
        "page": page,
        "page_size": page_size,
    }


def _moderation_payload(row: ModerationSettings) -> dict:
    return {
        "enabled": row.enabled,
        "base_url": row.base_url,
        "model": row.model,
        "categories": list(row.categories or []),
        "system_prompt": row.system_prompt,
        "approve_threshold": row.approve_threshold,
        "block_threshold": row.block_threshold,
        "fallback_action": row.fallback_action,
        "timeout_seconds": row.timeout_seconds,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
        "key": moderation.key_status(row),
        "key_from_database": bool((row.api_key_secret or "").strip()),
        "key_env_configured": bool(moderation.env_key()),
        "default_base_url": moderation.default_base_url(),
        "default_categories": moderation.DEFAULT_CATEGORIES,
        "default_prompt": moderation.DEFAULT_SYSTEM_PROMPT,
    }


@router.get("/moderation/settings")
def read_moderation_settings(db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    return _moderation_payload(moderation.get_settings(db))


@router.post("/moderation/settings")
def update_moderation_settings(
    body: ModerationSettingsIn,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin_role),
):
    """Save the moderation policy (and optionally a new key); audit the change."""
    row = moderation.get_settings(db)
    before = {
        "enabled": row.enabled,
        "model": row.model,
        "base_url": row.base_url,
        "approve_threshold": row.approve_threshold,
        "block_threshold": row.block_threshold,
        "fallback_action": row.fallback_action,
        "timeout_seconds": row.timeout_seconds,
        "categories": list(row.categories or []),
        "key_source": moderation.key_status(row)["source"],
    }
    key_action = "unchanged"
    if body.clear_api_key:
        moderation.clear_api_key(row)
        key_action = "cleared"
    elif body.api_key:
        try:
            moderation.set_api_key(row, body.api_key)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        key_action = "set"
    row.enabled = body.enabled
    row.base_url = body.base_url
    row.model = body.model.strip()
    row.categories = body.categories
    row.system_prompt = body.system_prompt
    row.approve_threshold = body.approve_threshold
    row.block_threshold = body.block_threshold
    row.fallback_action = body.fallback_action
    row.timeout_seconds = body.timeout_seconds
    row.updated_by = user.id
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=user.id,
            action="moderation_settings_change",
            target_type="moderation_settings",
            target_id=row.id,
            payload={
                "before": before,
                "after": {
                    "enabled": body.enabled,
                    "model": body.model.strip(),
                    "base_url": body.base_url,
                    "approve_threshold": body.approve_threshold,
                    "block_threshold": body.block_threshold,
                    "fallback_action": body.fallback_action,
                    "timeout_seconds": body.timeout_seconds,
                    "categories": body.categories,
                    "key_source": moderation.key_status(row)["source"],
                },
                # Never the credential itself: action + masked tail only.
                "key_action": key_action,
                "key_masked": moderation.key_status(row)["masked"],
                "prompt_chars": len(body.system_prompt),
            },
        )
    )
    db.commit()
    db.refresh(row)
    return _moderation_payload(row)


@router.post("/moderation/test")
def test_moderation(
    body: ModerationTestIn,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """Dry-run one piece of text against the saved policy. Writes nothing.

    ``body.api_key`` lets the admin page verify a key before saving it; the
    draft credential is only used for this call and never persisted or logged.
    """
    settings = moderation.get_settings(db)
    try:
        result = moderation.classify(body.text, settings, key=body.api_key)
    except moderation.ModerationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "enabled": settings.enabled,
        "status": moderation.decide(result.verdict, result.score, settings),
        "result": result.as_dict(),
    }


@router.post("/comments/{comment_id}")
def moderate(comment_id: int, body: CommentModIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = db.get(Comment, comment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="comment not found")
    if body.status not in {"approved", "deleted", "pending"}:
        raise HTTPException(status_code=422, detail="bad status")
    row.status = body.status
    # A human decision wins over a late model response: the worker's final
    # UPDATE is guarded by moderation_state='running'.
    if row.moderation_state in {"queued", "running"}:
        row.moderation_state = "canceled"
    db.commit()
    return {"status": row.status, "moderation_state": row.moderation_state}


@router.post("/pushes")
def create_push(title: str, body: str = "", drama_id: int | None = None, delivered_count: int = 0, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = PushRecord(title=title, body=body, drama_id=drama_id, delivered_count=delivered_count)
    db.add(row)
    db.commit()
    return {"id": row.id, "delivered_count": row.delivered_count, "sent_at": row.sent_at.isoformat()}


@router.get("/pushes")
def list_pushes(db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    rows = db.scalars(select(PushRecord).order_by(PushRecord.id.desc())).all()
    return {"items": [{"id": row.id, "title": row.title, "delivered_count": row.delivered_count, "sent_at": row.sent_at.isoformat()} for row in rows]}
