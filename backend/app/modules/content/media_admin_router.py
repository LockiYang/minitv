"""Admin uploads, previews and media processing endpoints."""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.common import bunny
from app.modules.content.admin_queries import _drama_or_404
from app.modules.content.media_service import (
    COVER_MAX_BYTES,
    COVER_MIME,
    SUBTITLE_MAX_BYTES,
    _cover_remote,
    _forget_other_covers,
    _latest_media_upload,
    _purge_cover,
    _remote_has_bytes,
    _require_fake_mode,
    _sniff_cover_ext,
    apply_remote_video,
)
from app.modules.content.models import Episode, MediaUpload
from app.modules.content.schemas import UploadFailedIn, UploadIn
from app.modules.content.service import InvalidTransition, transition_episode
from app.modules.content.upload_service import UploadRejected, create_upload_session, sign_callback
from app.modules.ops.auth import require_admin, require_content_writer
from app.modules.ops.models import AdminUser
from app.modules.playback.service import authorize

router = APIRouter(tags=["admin-flow"])


@router.get("/episodes/{episode_id}/preview-url")
def episode_preview_url(
    episode_id: int,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """Return a short-lived Bunny Player URL for an admin-only preview."""
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if not episode.bunny_video_id:
        raise HTTPException(status_code=409, detail="episode has no Bunny video")
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
def upload_session(episode_id: int, body: UploadIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    try:
        return create_upload_session(
            db,
            episode=episode,
            filename=body.filename,
            mime=body.mime,
            size_bytes=body.size_bytes,
            width=body.width,
            height=body.height,
            duration_seconds=body.duration_seconds,
        )
    except UploadRejected as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc
    except bunny.BunnyError as exc:
        raise HTTPException(status_code=502, detail=f"bunny upstream: {exc}") from exc


@router.post("/episodes/{episode_id}/upload-headers")
def episode_upload_headers(
    episode_id: int,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    """Re-mint TUS authorization for an upload that is already in flight.

    TUS signatures expire (1h). Without this endpoint an interrupted upload can
    only be restarted from byte 0, because ``upload-session`` refuses to run
    while the episode is ``transcoding``. This call changes no state — it just
    hands back fresh headers plus the video id the client should resume.
    """
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if episode.status != "transcoding":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "upload_not_resumable",
                "message": f"分集当前是 {episode.status}，只有 transcoding 状态可以续传",
            },
        )
    upload = _latest_media_upload(db, episode.id, episode.bunny_video_id)
    if upload is None or upload.status != "created" or not episode.bunny_video_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "upload_not_resumable", "message": "没有可续传的上传会话"},
        )
    if bunny.live():
        return {
            "video_id": episode.bunny_video_id,
            "upload_url": bunny.STREAM_TUS_ENDPOINT,
            "headers": bunny.tus_headers(episode.bunny_video_id),
        }
    return {
        "video_id": episode.bunny_video_id,
        "upload_url": f"/admin/v1/dev-tus/{episode.bunny_video_id}",
        "headers": {
            "AuthorizationExpire": "300",
            "AuthorizationSignature": sign_callback(episode.bunny_video_id.encode()),
        },
    }


@router.post("/episodes/{episode_id}/sync-status")
def sync_episode_status(episode_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    """Poll Bunny once and advance a ``transcoding`` episode.

    The transcode callback is not wired in every environment, so the admin
    console uses this endpoint as its post-upload source of truth: remote
    status 4 means the video finished transcoding, >= 5 means it failed. In
    fake mode the local receiver already completed the upload, so a
    transcoding row is treated as finished.
    """
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if not episode.bunny_video_id:
        raise HTTPException(status_code=409, detail="episode has no video")

    remote: dict | None = None
    source = "fake"
    if bunny.live():
        source = "bunny"
        if episode.status == "transcoding" or episode.duration_seconds == 0:
            try:
                remote = bunny.stream_video(episode.bunny_video_id)
            except bunny.BunnyError as exc:
                raise HTTPException(status_code=502, detail=f"bunny upstream: {exc}") from exc
    try:
        result = apply_remote_video(db, episode, remote)
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result["changed"]:
        db.commit()
    return {
        "episode_id": episode.id,
        "status": episode.status,
        "remote_status": result["remote_status"],
        "duration_seconds": episode.duration_seconds,
        "source": source,
    }


@router.post("/episodes/{episode_id}/upload-failed")
def upload_failed(
    episode_id: int,
    body: UploadFailedIn | None = None,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    """Report a browser-side upload failure so the episode can be retried.

    ``create_upload_session`` moves the episode to ``transcoding`` before any
    bytes exist. When the direct upload dies mid-flight, the row would be
    stuck there (a second session is refused while transcoding), so the admin
    console calls this: ``transcoding -> transcode_failed`` is the legal edge
    that re-opens uploads.
    """
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    payload = body or UploadFailedIn()
    if episode.status != "transcoding":
        return {
            "episode_id": episode.id,
            "status": episode.status,
            "updated": False,
            "reason": payload.reason,
        }
    # A browser error after the bytes have landed must not stamp 转码失败 onto
    # a video Bunny has already accepted. Status 0 with no original means the
    # object was created and nothing arrived — that one stays retryable.
    video_id = payload.video_id or episode.bunny_video_id
    if bunny.live() and video_id:
        try:
            remote = bunny.stream_video(video_id)
        except bunny.BunnyError:
            remote = None
        if remote is not None and _remote_has_bytes(remote):
            try:
                apply_remote_video(db, episode, remote)
            except InvalidTransition as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            db.commit()
            return {
                "episode_id": episode.id,
                "status": episode.status,
                "updated": episode.status != "transcoding",
                "reason": payload.reason,
                "ignored": "bytes already at bunny",
            }
    try:
        transition_episode(db, episode, "transcode_failed")
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    upload = _latest_media_upload(db, episode.id, video_id)
    if upload is not None:
        upload.status = "failed"
    db.commit()
    return {
        "episode_id": episode.id,
        "status": episode.status,
        "updated": True,
        "reason": payload.reason,
    }


@router.post("/dev-tus/{video_id}")
def dev_tus_create(video_id: str, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    """TUS create step of the fake-mode upload receiver (dev/test only).

    Mirrors Bunny's ``POST /tusupload``: answers 201 with the Location the
    client must PATCH its bytes to.
    """
    _require_fake_mode()
    upload = db.scalar(select(MediaUpload).where(MediaUpload.video_id == video_id))
    if upload is None:
        raise HTTPException(status_code=404, detail="unknown video id")
    return Response(
        status_code=201,
        headers={"Location": f"/admin/v1/dev-tus/{video_id}", "Tus-Resumable": "1.0.0"},
    )


@router.head("/dev-tus/{video_id}")
def dev_tus_head(
    video_id: str,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    """TUS HEAD for the fake receiver: how many bytes are already stored."""
    _require_fake_mode()
    upload = db.scalar(select(MediaUpload).where(MediaUpload.video_id == video_id))
    if upload is None:
        raise HTTPException(status_code=404, detail="unknown video id")
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
async def dev_tus_patch(
    video_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    """TUS data step of the fake-mode receiver: completes the upload at once."""
    _require_fake_mode()
    upload = db.scalar(select(MediaUpload).where(MediaUpload.video_id == video_id))
    if upload is None:
        raise HTTPException(status_code=404, detail="unknown video id")
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


@router.post("/dramas/{drama_id}/cover")
async def upload_cover(
    drama_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    drama = _drama_or_404(db, drama_id)
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
    drama.cover_url = f"{bunny.image_url(remote_path)}?v={time.time_ns()}"
    db.commit()
    _purge_cover(drama.id, ext)
    _forget_other_covers(drama.id, ext)
    return {"cover_url": drama.cover_url}


@router.delete("/dramas/{drama_id}/cover")
def delete_cover(
    drama_id: int,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    drama = _drama_or_404(db, drama_id)
    drama.cover_url = ""
    db.commit()
    _forget_other_covers(drama.id, None)
    return {"cover_url": ""}


@router.post("/episodes/{episode_id}/subtitle")
async def upload_subtitle(
    episode_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if not bunny.live():
        raise HTTPException(status_code=503, detail="bunny storage not configured (BUNNY_MODE=fake)")
    data = await file.read()
    if not data or len(data) > SUBTITLE_MAX_BYTES:
        raise HTTPException(status_code=422, detail="subtitle must be between 1 byte and 2MB")
    name = os.path.basename(file.filename or "subtitle.vtt")
    ext = os.path.splitext(name)[1].lower()
    if ext not in {".vtt", ".srt"}:
        raise HTTPException(status_code=422, detail="subtitle must be .vtt or .srt")
    remote_path = f"subs/{episode.id}/sub{ext}"
    try:
        bunny.storage_upload(remote_path, data, "text/vtt" if ext == ".vtt" else "application/x-subrip")
    except bunny.BunnyError as exc:
        raise HTTPException(status_code=502, detail=f"bunny storage: {exc}") from exc
    episode.subtitle_url = remote_path
    db.commit()
    return {"subtitle_url": remote_path}


@router.get("/dramas/{drama_id}/preview")
def preview(drama_id: int, episode_id: int | None = None, db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    drama = _drama_or_404(db, drama_id)
    episode = db.scalar(select(Episode).where(Episode.drama_id == drama.id).order_by(Episode.episode_number))
    if episode_id:
        episode = db.get(Episode, episode_id)
    episodes = db.scalars(select(Episode).where(Episode.drama_id == drama.id).order_by(Episode.episode_number)).all()
    playback = None
    if episode is not None and drama.status == "published" and episode.status == "published":
        playback = authorize(db, drama_id=drama.id, episode_id=episode.id, user_id=None)
    return {
        "preview_kind": "player",
        "drama_id": drama.id,
        "title": drama.title,
        "current_episode": None
        if episode is None
        else {
            "id": episode.id,
            "number": episode.episode_number,
            "title": episode.title,
            "subtitle_url": episode.subtitle_url,
        },
        "episodes": [{"id": ep.id, "number": ep.episode_number, "status": ep.status} for ep in episodes],
        "playback": playback,
    }
