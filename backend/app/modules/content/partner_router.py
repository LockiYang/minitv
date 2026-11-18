"""Supplier-scoped catalog, episodes and media uploads."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.common.serialization import iso_or_none
from app.modules.content import upload_service
from app.modules.content.media_service import (
    COVER_MAX_BYTES,
    COVER_MIME,
    _cover_remote,
    _forget_other_covers,
    _purge_cover,
    _require_fake_mode,
    _sniff_cover_ext,
)
from app.modules.content.models import Drama, DramaTag, Episode, MediaUpload, Tag
from app.modules.content.partner_schemas import (
    MAX_EPISODES,
    MIN_FREE_EPISODES,
    DramaCreateIn,
    DramaUpdateIn,
    EpisodeCreateIn,
    EpisodeUpdateIn,
    UploadIn,
)
from app.modules.content.service import InvalidTransition, transition_episode
from app.modules.ops.models import AdminUser
from app.modules.ops.partner_audit import _audit
from app.modules.ops.partner_auth import require_partner

router = APIRouter(tags=["partner"])


def _partner_drama_or_404(db: Session, user: AdminUser, drama_id: int) -> Drama:
    """Ownership first, state later: a 403 would confirm the row exists."""
    row = db.scalar(
        select(Drama).where(Drama.id == drama_id, Drama.supplier_id == user.supplier_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="drama not found")
    return row


def _editable(drama: Drama) -> None:
    """Suppliers may only touch drafts (including a rejected draft under repair)."""
    if drama.status != "draft":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "drama_locked",
                "message": f"剧集当前状态 {drama.status}，上剧方只能编辑草稿",
            },
        )
    if drama.review_state == "pending":
        raise HTTPException(
            status_code=409,
            detail={"code": "drama_in_review", "message": "审核中不可修改"},
        )


def _episode_or_404(db: Session, user: AdminUser, episode_id: int) -> Episode:
    row = db.scalar(
        select(Episode)
        .join(Drama, Drama.id == Episode.drama_id)
        .where(Episode.id == episode_id, Drama.supplier_id == user.supplier_id)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="episode not found")
    return row


def _episode_count(db: Session, drama_id: int) -> int:
    return db.scalar(select(func.count(Episode.id)).where(Episode.drama_id == drama_id)) or 0


def _drama_out(db: Session, drama: Drama, *, with_episodes: bool = False) -> dict:
    payload = {
        "id": drama.id,
        "title": drama.title,
        "synopsis": drama.synopsis,
        "cover_url": drama.cover_url,
        "status": drama.status,
        "review_state": drama.review_state,
        "review_note": drama.review_note,
        "free_episode_count": drama.free_episode_count,
        # Read-only on this surface: pricing belongs to the platform.
        "coin_price_per_episode": drama.coin_price_per_episode,
        "play_count": drama.play_count,
        "episode_count": _episode_count(db, drama.id),
        "submitted_at": iso_or_none(drama.submitted_at),
        "reviewed_at": iso_or_none(drama.reviewed_at),
        "updated_at": iso_or_none(drama.updated_at),
        "editable": drama.status == "draft" and drama.review_state != "pending",
    }
    if with_episodes:
        episodes = db.scalars(
            select(Episode)
            .where(Episode.drama_id == drama.id)
            .order_by(Episode.episode_number)
        ).all()
        uploads = {
            row.episode_id: row.status
            for row in db.scalars(
                select(MediaUpload)
                .where(MediaUpload.episode_id.in_([item.id for item in episodes]))
                .order_by(MediaUpload.id)
            ).all()
        }
        payload["episodes"] = [
            {
                "id": item.id,
                "episode_number": item.episode_number,
                "title": item.title,
                "status": item.status,
                "upload_status": uploads.get(item.id),
                "duration_seconds": item.duration_seconds,
                "size_bytes": item.size_bytes,
                # Drives the row-level 预览 button: an episode without a Bunny
                # video has nothing to sign, so the UI must not offer playback.
                "has_video": bool(item.bunny_video_id),
            }
            for item in episodes
        ]
        payload["tag_ids"] = [
            row.tag_id
            for row in db.scalars(select(DramaTag).where(DramaTag.drama_id == drama.id)).all()
        ]
    return payload


def _replace_tags(db: Session, drama: Drama, tag_ids: list[int]) -> None:
    rows = db.scalars(select(Tag).where(Tag.id.in_(tag_ids))).all() if tag_ids else []
    if len(rows) != len(set(tag_ids)):
        raise HTTPException(status_code=422, detail="存在无效的标签")
    if any(tag.status != "enabled" for tag in rows):
        raise HTTPException(status_code=422, detail="存在已下线的标签")
    db.execute(delete(DramaTag).where(DramaTag.drama_id == drama.id))
    for tag in rows:
        db.add(DramaTag(drama_id=drama.id, tag_id=tag.id))


# ---- dramas ----
@router.get("/tags")
def list_tags(
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    """Only enabled tags; suppliers may pick from the platform taxonomy."""
    rows = db.scalars(
        select(Tag).where(Tag.status == "enabled").order_by(Tag.category, Tag.id)
    ).all()
    return {
        "items": [
            {
                "id": tag.id,
                "name_zh": tag.name_zh,
                "name_en": tag.name_en,
                "category": tag.category,
            }
            for tag in rows
        ]
    }


@router.get("/dramas")
def list_dramas(
    status: str | None = None,
    review_state: str | None = None,
    q: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    base = select(Drama).where(Drama.supplier_id == user.supplier_id)
    if status:
        base = base.where(Drama.status == status)
    if review_state:
        base = base.where(Drama.review_state == review_state)
    if q and q.strip():
        base = base.where(Drama.title.ilike(f"%{q.strip()}%"))
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(
        base.order_by(Drama.updated_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    counts = dict(
        db.execute(
            select(Drama.status, func.count(Drama.id))
            .where(Drama.supplier_id == user.supplier_id)
            .group_by(Drama.status)
        ).all()
    )
    counts["all"] = db.scalar(
        select(func.count(Drama.id)).where(Drama.supplier_id == user.supplier_id)
    ) or 0
    review_counts = dict(
        db.execute(
            select(Drama.review_state, func.count(Drama.id))
            .where(Drama.supplier_id == user.supplier_id)
            .group_by(Drama.review_state)
        ).all()
    )
    def _count(*where) -> int:
        return db.scalar(
            select(func.count(Drama.id)).where(Drama.supplier_id == user.supplier_id, *where)
        ) or 0

    stage_counts = {
        "all": total,
        "draft": _count(Drama.status == "draft", Drama.review_state.in_(["none", "rejected"])),
        "pending": _count(Drama.review_state == "pending"),
        "approved": _count(
            Drama.review_state == "approved", Drama.status.in_(["draft", "in_review"])
        ),
        "published": _count(Drama.status == "published"),
        "unpublished": _count(Drama.status == "unpublished"),
    }
    return {
        "items": [_drama_out(db, row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "status_counts": counts,
        "review_counts": review_counts,
        "stage_counts": stage_counts,
    }


@router.post("/dramas", status_code=201)
def create_drama(
    body: DramaCreateIn,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    title = body.title.strip()
    if not title:
        raise HTTPException(status_code=422, detail="剧名不能为空")
    exists = db.scalar(
        select(Drama).where(Drama.supplier_id == user.supplier_id, Drama.title == title)
    )
    if exists is not None:
        raise HTTPException(
            status_code=409,
            detail={"code": "title_exists", "message": "你已有同名剧", "drama_id": exists.id},
        )
    drama = Drama(
        title=title,
        synopsis=body.synopsis.strip(),
        supplier_id=user.supplier_id,
        free_episode_count=body.free_episode_count,
        country_allowlist=body.country_allowlist.strip(),
        age_rating=body.age_rating,
        coin_price_per_episode=0,  # pricing stays platform-side
        status="draft",
        review_state="none",
    )
    db.add(drama)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="同名剧已存在") from exc
    _audit(db, user, "partner_drama_create", drama.id, {"title": drama.title})
    db.commit()
    db.refresh(drama)
    return _drama_out(db, drama, with_episodes=True)


@router.get("/dramas/{drama_id}")
def get_drama(
    drama_id: int,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    row = _partner_drama_or_404(db, user, drama_id)
    return _drama_out(db, row, with_episodes=True)


@router.patch("/dramas/{drama_id}")
def update_drama(
    drama_id: int,
    body: DramaUpdateIn,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    drama = _partner_drama_or_404(db, user, drama_id)
    _editable(drama)
    data = body.model_dump(exclude_unset=True)

    if data.get("title") is not None:
        title = data["title"].strip()
        if not title:
            raise HTTPException(status_code=422, detail="剧名不能为空")
        other = db.scalar(
            select(Drama).where(
                Drama.supplier_id == user.supplier_id,
                Drama.title == title,
                Drama.id != drama.id,
            )
        )
        if other is not None:
            raise HTTPException(
                status_code=409,
                detail={"code": "title_exists", "message": "你已有同名剧", "drama_id": other.id},
            )
        drama.title = title
    for field in ("synopsis", "cover_url", "country_allowlist"):
        if data.get(field) is not None:
            setattr(drama, field, data[field].strip())
    if data.get("free_episode_count") is not None:
        drama.free_episode_count = data["free_episode_count"]
    if "age_rating" in data:
        drama.age_rating = data["age_rating"]
    if body.tag_ids is not None:
        _replace_tags(db, drama, body.tag_ids)
    if drama.review_state == "rejected":
        # Editing a rejected drama clears the verdict; it must be resubmitted.
        drama.review_state = "none"
        drama.review_note = None
    _audit(db, user, "partner_drama_update", drama.id, {"fields": sorted(data)})
    db.commit()
    db.refresh(drama)
    return _drama_out(db, drama, with_episodes=True)


@router.post("/dramas/{drama_id}/cover")
async def upload_cover(
    drama_id: int,
    file: UploadFile = File(...),
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    from app.modules.common import bunny

    drama = _partner_drama_or_404(db, user, drama_id)
    _editable(drama)
    if not bunny.live():
        raise HTTPException(status_code=503, detail="图片存储未配置，可先填写封面链接")
    data = await file.read(COVER_MAX_BYTES + 1)
    if not data or len(data) > COVER_MAX_BYTES:
        raise HTTPException(status_code=422, detail="封面大小需在 1 字节到 5MB 之间")
    ext = _sniff_cover_ext(data)
    if ext is None:
        raise HTTPException(status_code=422, detail="封面只支持 JPG、PNG、WebP")
    remote_path = _cover_remote(drama.id, ext)
    try:
        bunny.storage_upload(remote_path, data, COVER_MIME[ext])
    except bunny.BunnyError as exc:
        raise HTTPException(status_code=502, detail=f"封面上传失败：{exc}") from exc
    drama.cover_url = f"{bunny.image_url(remote_path)}?v={datetime.now(timezone.utc).timestamp()}"
    _audit(db, user, "partner_cover_upload", drama.id, {})
    db.commit()
    _purge_cover(drama.id, ext)
    _forget_other_covers(drama.id, ext)
    return {"cover_url": drama.cover_url}


# ---- episodes ----
@router.post("/dramas/{drama_id}/episodes", status_code=201)
def create_episode(
    drama_id: int,
    body: EpisodeCreateIn,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    drama = _partner_drama_or_404(db, user, drama_id)
    _editable(drama)
    count = _episode_count(db, drama.id)
    if count >= MAX_EPISODES:
        raise HTTPException(status_code=422, detail=f"单部剧最多 {MAX_EPISODES} 集")
    number = body.episode_number
    if number is None:
        number = (
            db.scalar(
                select(func.max(Episode.episode_number)).where(Episode.drama_id == drama.id)
            )
            or 0
        ) + 1
    if db.scalar(
        select(Episode.id).where(
            Episode.drama_id == drama.id, Episode.episode_number == number
        )
    ):
        raise HTTPException(status_code=409, detail=f"第 {number} 集已存在")
    episode = Episode(
        drama_id=drama.id,
        episode_number=number,
        title=body.title.strip() or f"第 {number} 集",
        status="not_uploaded",
    )
    db.add(episode)
    db.flush()
    _audit(db, user, "partner_episode_create", drama.id, {"episode": number})
    db.commit()
    db.refresh(episode)
    return {
        "id": episode.id,
        "episode_number": episode.episode_number,
        "title": episode.title,
        "status": episode.status,
    }


@router.patch("/episodes/{episode_id}")
def update_episode(
    episode_id: int,
    body: EpisodeUpdateIn,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    episode = _episode_or_404(db, user, episode_id)
    drama = db.get(Drama, episode.drama_id)
    _editable(drama)
    if body.episode_number is not None and body.episode_number != episode.episode_number:
        taken = db.scalar(
            select(Episode.id).where(
                Episode.drama_id == episode.drama_id,
                Episode.episode_number == body.episode_number,
                Episode.id != episode.id,
            )
        )
        if taken:
            raise HTTPException(status_code=409, detail=f"第 {body.episode_number} 集已存在")
        episode.episode_number = body.episode_number
    if body.title is not None:
        episode.title = body.title.strip()
    db.commit()
    return {
        "id": episode.id,
        "title": episode.title,
        "episode_number": episode.episode_number,
    }


@router.delete("/episodes/{episode_id}", status_code=204)
def delete_episode(
    episode_id: int,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    episode = _episode_or_404(db, user, episode_id)
    drama = db.get(Drama, episode.drama_id)
    _editable(drama)
    if episode.status != "not_uploaded":
        raise HTTPException(
            status_code=409,
            detail={"code": "episode_has_video", "message": "已上传视频的分集不能删除"},
        )
    number = episode.episode_number
    db.delete(episode)
    _audit(db, user, "partner_episode_delete", drama.id, {"episode": number})
    db.commit()
    return None


@router.get("/episodes/{episode_id}/preview-url")
def episode_preview_url(
    episode_id: int,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    """Signed Bunny player URL so a supplier can review its own episode.

    Read-only and ownership-scoped (a key with only ``read`` may use it), and
    deliberately available in every state — reviewing a clip while it waits in
    the review queue or is already published is the point.
    """
    from app.modules.common import bunny

    episode = _episode_or_404(db, user, episode_id)
    if not episode.bunny_video_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "no_video", "message": "该集还没有可预览的视频"},
        )
    try:
        signed = bunny.stream_embed_url(episode.bunny_video_id, ttl=3600)
    except bunny.BunnyError as exc:
        raise HTTPException(status_code=503, detail=f"preview signing unavailable: {exc}") from exc
    return {
        **signed,
        "episode_id": episode.id,
        "episode_number": episode.episode_number,
        "video_id": episode.bunny_video_id,
    }


@router.post("/episodes/{episode_id}/upload-session")
def upload_session(
    episode_id: int,
    body: UploadIn,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    from app.modules.common import bunny

    episode = _episode_or_404(db, user, episode_id)
    drama = db.get(Drama, episode.drama_id)
    _editable(drama)
    try:
        payload = upload_service.create_upload_session(
            db,
            episode=episode,
            filename=body.filename,
            mime=body.mime,
            size_bytes=body.size_bytes,
            width=body.width,
            height=body.height,
            duration_seconds=body.duration_seconds,
        )
    except upload_service.UploadRejected as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc
    except bunny.BunnyError as exc:
        raise HTTPException(status_code=502, detail=f"bunny upstream: {exc}") from exc
    # Fake mode's receiver lives under the staff surface; point the supplier at
    # its own copy so the path guard stays intact.
    if not bunny.live() and payload.get("upload_url", "").startswith("/admin/v1/"):
        payload["upload_url"] = payload["upload_url"].replace("/admin/v1/", "/partner/v1/")
    return payload


def _own_upload(db: Session, user: AdminUser, video_id: str) -> MediaUpload:
    upload = db.scalar(
        select(MediaUpload)
        .join(Episode, Episode.id == MediaUpload.episode_id)
        .join(Drama, Drama.id == Episode.drama_id)
        .where(
            MediaUpload.video_id == video_id,
            Drama.supplier_id == user.supplier_id,
        )
    )
    if upload is None:
        raise HTTPException(status_code=404, detail="unknown video id")
    return upload


@router.post("/dev-tus/{video_id}")
def partner_dev_tus_create(
    video_id: str,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    """Fake-mode TUS receiver for local development (never used in production)."""
    _require_fake_mode()
    _own_upload(db, user, video_id)
    return Response(
        status_code=201,
        headers={"Location": f"/partner/v1/dev-tus/{video_id}", "Tus-Resumable": "1.0.0"},
    )


@router.head("/dev-tus/{video_id}")
def partner_dev_tus_head(
    video_id: str,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    _require_fake_mode()
    upload = _own_upload(db, user, video_id)
    offset = upload.size_bytes if upload.status == "completed" else 0
    return Response(
        status_code=200,
        headers={
            "Upload-Offset": str(offset),
            "Upload-Length": str(upload.size_bytes),
            "Tus-Resumable": "1.0.0",
        },
    )


@router.patch("/dev-tus/{video_id}")
async def partner_dev_tus_patch(
    video_id: str,
    request: Request,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    _require_fake_mode()
    upload = _own_upload(db, user, video_id)
    if upload.status == "completed":
        return Response(
            status_code=204,
            headers={"Upload-Offset": str(upload.size_bytes), "Tus-Resumable": "1.0.0"},
        )
    if (request.headers.get("upload-offset") or "0") != "0":
        raise HTTPException(status_code=409, detail="only single-shot uploads are supported")
    body = await request.body()
    episode = db.get(Episode, upload.episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode missing")
    if episode.status == "transcoding":
        try:
            transition_episode(db, episode, "in_review")
        except InvalidTransition as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    upload.status = "completed"
    upload.size_bytes = len(body)
    episode.size_bytes = len(body)
    db.commit()
    return Response(
        status_code=204,
        headers={"Upload-Offset": str(len(body)), "Tus-Resumable": "1.0.0"},
    )


@router.post("/episodes/{episode_id}/upload-headers")
def upload_headers(
    episode_id: int,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    from app.modules.common import bunny

    episode = _episode_or_404(db, user, episode_id)
    if episode.status != "transcoding":
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_not_resumable", "message": "只有转码中的分集可以续传"},
        )
    upload = db.scalars(
        select(MediaUpload)
        .where(
            MediaUpload.episode_id == episode.id,
            MediaUpload.video_id == episode.bunny_video_id,
        )
        .order_by(MediaUpload.id.desc())
    ).first()
    if upload is None:
        raise HTTPException(status_code=404, detail="upload not found")
    return {
        "episode_id": episode.id,
        "video_id": episode.bunny_video_id,
        "upload_url": bunny.STREAM_TUS_ENDPOINT if bunny.live() else "",
        "headers": bunny.tus_headers(episode.bunny_video_id) if bunny.live() else {},
    }


@router.get("/episodes/{episode_id}/upload-status")
def upload_status(
    episode_id: int,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    episode = _episode_or_404(db, user, episode_id)
    upload = db.scalars(
        select(MediaUpload)
        .where(MediaUpload.episode_id == episode.id)
        .order_by(MediaUpload.id.desc())
    ).first()
    # A session that never received bytes would otherwise sit in "transcoding"
    # forever (the supplier sees 转码中 and cannot retry). 30 minutes is beyond
    # a realistic 200MB upload on a slow link, so it is safe to call it dead.
    if (
        episode.status == "transcoding"
        and upload is not None
        and upload.status == "created"
        and upload.created_at is not None
    ):
        age = datetime.now(timezone.utc) - upload.created_at
        if age > timedelta(minutes=30):
            from app.modules.common import bunny

            has_bytes = False
            if bunny.live() and episode.bunny_video_id:
                try:
                    remote = bunny.stream_video(episode.bunny_video_id)
                    # hasOriginal flips true as soon as the first TUS bytes
                    # arrive and stays true for the whole (possibly hours-long)
                    # upload, so it is the liveness signal here. Size/length
                    # only appear after the last byte.
                    has_bytes = bool(
                        remote.get("hasOriginal")
                        or int(remote.get("storageSize") or 0) > 0
                        or int(remote.get("length") or 0) > 0
                        or int(remote.get("status") or 0) >= 3
                        or int(remote.get("encodeProgress") or 0) > 0
                    )
                except bunny.BunnyError:
                    has_bytes = False
            if not has_bytes:
                try:
                    transition_episode(db, episode, "transcode_failed")
                    upload.status = "failed"
                    db.commit()
                except InvalidTransition:
                    db.rollback()
    return {
        "episode_id": episode.id,
        "episode_number": episode.episode_number,
        "status": episode.status,
        "duration_seconds": episode.duration_seconds,
        "size_bytes": episode.size_bytes,
        "video_id": episode.bunny_video_id,
        "upload_status": upload.status if upload else None,
    }


@router.post("/episodes/{episode_id}/upload-failed")
def upload_failed(
    episode_id: int,
    body: dict | None = None,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    """Let the supplier release a dead upload session immediately."""
    episode = _episode_or_404(db, user, episode_id)
    drama = db.get(Drama, episode.drama_id)
    _editable(drama)
    if episode.status != "transcoding":
        return {"episode_id": episode.id, "status": episode.status, "updated": False}
    from app.modules.common import bunny

    video_id = episode.bunny_video_id
    if bunny.live() and video_id:
        try:
            remote = bunny.stream_video(video_id)
        except bunny.BunnyError:
            remote = None
        if remote is not None and (
            int(remote.get("storageSize") or 0) > 0
            or int(remote.get("length") or 0) > 0
            or int(remote.get("status") or 0) >= 3
            or int(remote.get("encodeProgress") or 0) > 0
        ):
            return {
                "episode_id": episode.id,
                "status": episode.status,
                "updated": False,
                "ignored": "bytes already at bunny",
            }
    try:
        transition_episode(db, episode, "transcode_failed")
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    upload = db.scalars(
        select(MediaUpload)
        .where(MediaUpload.episode_id == episode.id)
        .order_by(MediaUpload.id.desc())
    ).first()
    if upload is not None:
        upload.status = "failed"
    db.commit()
    return {"episode_id": episode.id, "status": episode.status, "updated": True}


@router.delete("/dramas/{drama_id}", status_code=204)
def delete_drama(
    drama_id: int,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    """Suppliers may delete their own drafts; anything with video or review is locked."""
    drama = _partner_drama_or_404(db, user, drama_id)
    _editable(drama)
    episodes = db.scalars(select(Episode).where(Episode.drama_id == drama.id)).all()
    if any(item.status != "not_uploaded" for item in episodes):
        raise HTTPException(
            status_code=409,
            detail={"code": "drama_has_video", "message": "已上传视频的剧集不能删除"},
        )
    title = drama.title
    for item in episodes:
        db.delete(item)
    db.execute(delete(DramaTag).where(DramaTag.drama_id == drama.id))
    db.delete(drama)
    _audit(db, user, "partner_drama_delete", None, {"title": title, "drama_id": drama_id})
    db.commit()
    return None


@router.post("/dramas/{drama_id}/submit")
def submit_drama(
    drama_id: int,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    drama = _partner_drama_or_404(db, user, drama_id)
    if drama.status != "draft":
        raise HTTPException(
            status_code=409,
            detail={"code": "drama_locked", "message": "只有草稿可以提交审核"},
        )
    if drama.review_state not in ("none", "rejected"):
        raise HTTPException(
            status_code=409,
            detail={"code": "drama_in_review", "message": "已提交，等待审核"},
        )
    if not drama.cover_url:
        raise HTTPException(
            status_code=422, detail={"code": "cover_required", "message": "请先上传封面"}
        )
    if len((drama.synopsis or "").strip()) < 30:
        raise HTTPException(
            status_code=422,
            detail={"code": "synopsis_too_short", "message": "简介至少 30 字"},
        )
    episodes = db.scalars(
        select(Episode).where(Episode.drama_id == drama.id).order_by(Episode.episode_number)
    ).all()
    if not episodes:
        raise HTTPException(
            status_code=422, detail={"code": "episodes_required", "message": "至少上传 1 集"}
        )
    pending = [e.episode_number for e in episodes if e.status != "in_review"]
    if pending:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "episodes_not_ready",
                "message": f"第 {', '.join(map(str, pending))} 集尚未完成转码",
                "episodes": pending,
            },
        )
    floor = min(MIN_FREE_EPISODES, len(episodes))
    if not (floor <= drama.free_episode_count <= len(episodes)):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "free_episode_count_out_of_range",
                "message": f"免费集数需在 {floor}–{len(episodes)} 之间",
                "floor": floor,
                "max": len(episodes),
            },
        )
    drama.review_state = "pending"
    drama.submitted_at = datetime.now(timezone.utc)
    drama.review_note = None
    _audit(db, user, "partner_drama_submit", drama.id, {"episodes": len(episodes)})
    db.commit()
    db.refresh(drama)
    return _drama_out(db, drama, with_episodes=True)
