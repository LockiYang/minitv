"""Shared cover storage and video transcode reconciliation."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_engine, make_session_factory
from app.modules.common import bunny
from app.modules.common.models import AuditLog
from app.modules.content.models import Episode, MediaUpload
from app.modules.content.service import InvalidTransition, transition_episode


def _latest_media_upload(db: Session, episode_id: int, video_id: str = "") -> MediaUpload | None:
    stmt = select(MediaUpload).where(MediaUpload.episode_id == episode_id)
    if video_id:
        stmt = stmt.where(MediaUpload.video_id == video_id)
    return db.scalars(stmt.order_by(MediaUpload.id.desc())).first()


# Bunny Stream video status (VideoModelStatus). 0 is "created" (no bytes, or
# bytes just landed and encode has not started). 3 is "transcoding" and is
# already playable once resolutions exist; 4 is "finished". >= 5 is a failure.
_REMOTE_FINISHED = 4


_REMOTE_TRANSCODING = 3


_REMOTE_FAILED = 5


# An upload this far past session creation with no pipeline movement is not
# queued any more: Bunny silently dropped it. Both observed flavors are
# permanent until the object is replaced — ``reencode`` is a no-op on the
# second and answers ``400 Original file is missing`` on the first. Long
# enough to ride out peak-hour delays.
_REMOTE_STALLED_AFTER_SECONDS = 3600


def _remote_upload_stalled(remote: dict) -> bool:
    """True when Bunny kept the object but the pipeline never moved on it.

    Two flavors seen in production:

    - status 0 with no bytes accounted for — the upload died in transit and
      the original never made it to storage (CDN ``/{guid}/original`` 404s).
    - status 1/2 with zero encode progress and no resolutions — Bunny has
      the original but the encoder never picked it up (sits 转码中 forever).

    Either way the row must fall to ``transcode_failed`` so the retry path
    (closed while transcoding) re-opens; without that the episode can never
    be edited, replaced or deleted again. Fresh uploads inside the queue
    window are left alone.
    """
    status = int(remote.get("status") or 0)
    if status not in (0, 1, 2):
        return False
    uploaded = remote.get("dateUploaded")
    if not uploaded:
        return False
    try:
        uploaded_at = datetime.fromisoformat(str(uploaded))
    except ValueError:
        return False
    if uploaded_at.tzinfo is None:
        uploaded_at = uploaded_at.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - uploaded_at).total_seconds() <= _REMOTE_STALLED_AFTER_SECONDS:
        return False
    if status == 0:
        # No bytes accounted for: the original never survived the upload.
        return int(remote.get("storageSize") or 0) == 0 and int(remote.get("length") or 0) == 0
    # Bytes landed but nothing was ever encoded.
    return int(remote.get("encodeProgress") or 0) == 0 and not (remote.get("availableResolutions") or "")


def _remote_has_bytes(remote: dict) -> bool:
    """True when Bunny has the original, even if encode has not started.

    ``hasOriginal`` flips true as soon as the TUS body arrives. ``status`` can
    stay 0 for a while after that, with no hash and no storage size yet — that
    is a queue delay, not a failed upload.
    """
    if remote.get("hasOriginal"):
        return True
    if int(remote.get("storageSize") or 0) > 0:
        return True
    if int(remote.get("length") or 0) > 0:
        return True
    return int(remote.get("status") or 0) >= _REMOTE_TRANSCODING


def apply_remote_video(db: Session, episode: Episode, remote: dict | None) -> dict:
    """Advance one ``transcoding`` episode from a Bunny video object.

    ``remote is None`` is the fake-mode signal: the local receiver already
    finished the upload, so the row moves to ``in_review``. A missing remote
    object (deleted upstream) is not the same thing and must be passed as an
    empty dict, which leaves the row alone.
    """
    remote_status: int | None = None
    duration_updated = False
    if remote is not None:
        remote_status = int(remote.get("status") or 0)
        length = int(remote.get("length") or 0)
        if length > 0 and episode.duration_seconds != length:
            episode.duration_seconds = length
            duration_updated = True

    changed = duration_updated
    if episode.status == "transcoding":
        resolutions = (remote or {}).get("availableResolutions") or ""
        length = int((remote or {}).get("length") or 0)
        target = None
        if remote is None or remote_status == _REMOTE_FINISHED:
            target = "in_review"
        elif remote_status == _REMOTE_TRANSCODING and resolutions and length > 0:
            # Early play: at least one resolution is already watchable.
            target = "in_review"
        elif remote_status is not None and remote_status >= _REMOTE_FAILED:
            target = "transcode_failed"
        elif _remote_upload_stalled(remote):
            # Bunny kept an object the pipeline dropped (no bytes, or an
            # encoder that never picked it up); only a fresh session
            # (re-opened by transcode_failed) can recover it.
            target = "transcode_failed"
        if target is not None:
            transition_episode(db, episode, target)
            upload = _latest_media_upload(db, episode.id, episode.bunny_video_id)
            if upload is not None:
                upload.status = "completed" if target == "in_review" else "failed"
            changed = True
    return {
        "episode_id": episode.id,
        "status": episode.status,
        "remote_status": remote_status,
        "duration_seconds": episode.duration_seconds,
        "changed": changed,
    }


def reconcile_transcoding(db: Session, remote_by_guid: dict[str, dict]) -> dict[str, int]:
    """Move every finished ``transcoding`` row forward from one library listing.

    Rows whose video is still queued or encoding stay put. A GUID absent from
    the listing is left alone too: a transient empty page must not be read as
    "Bunny deleted the video".
    """
    rows = list(db.scalars(select(Episode).where(Episode.status == "transcoding", Episode.bunny_video_id != "")))
    advanced = failed = pending = missing = 0
    for episode in rows:
        remote = remote_by_guid.get(episode.bunny_video_id)
        if remote is None:
            missing += 1
            continue
        before = episode.status
        try:
            apply_remote_video(db, episode, remote)
        except InvalidTransition:
            pending += 1
            continue
        if episode.status == before:
            pending += 1
        elif episode.status == "transcode_failed":
            failed += 1
        else:
            advanced += 1
    if advanced or failed:
        db.add(
            AuditLog(
                actor_type="system",
                actor_id=None,
                action="reconcile_transcoding",
                target_type="episode",
                target_id=None,
                payload={"advanced": advanced, "failed": failed, "pending": pending, "missing": missing},
            )
        )
        db.commit()
    return {"advanced": advanced, "failed": failed, "pending": pending, "missing": missing, "seen": len(rows)}


_reconcile_log = logging.getLogger("minitv.transcode")


_reconcile_started = False


_RECONCILE_INTERVAL_SECONDS = 120


_library_cache: tuple[float, dict[str, dict]] = (0.0, {})


_LIBRARY_CACHE_SECONDS = 30.0


def _cached_library() -> dict[str, dict]:
    """Library listing reused across page views. A failed fetch keeps the last good copy."""
    global _library_cache
    fetched_at, cached = _library_cache
    if cached and time.monotonic() - fetched_at < _LIBRARY_CACHE_SECONDS:
        return cached
    try:
        fresh = bunny.list_stream_videos()
    except bunny.BunnyError:
        return cached
    _library_cache = (time.monotonic(), fresh)
    return fresh


def _reconcile_forever() -> None:
    while True:
        time.sleep(_RECONCILE_INTERVAL_SECONDS)
        if not bunny.live():
            continue
        session = make_session_factory(get_engine())()
        try:
            pending = session.scalar(
                select(func.count()).select_from(Episode).where(Episode.status == "transcoding")
            )
            if not pending:
                continue
            result = reconcile_transcoding(session, bunny.list_stream_videos())
            if result["advanced"] or result["failed"]:
                _reconcile_log.info("transcode reconcile %s", result)
        except Exception:
            session.rollback()
            _reconcile_log.exception("transcode reconcile failed")
        finally:
            session.close()


def start_transcode_reconciler() -> None:
    """Poll Bunny for finished encodes. The library webhook is not configured."""
    global _reconcile_started
    if _reconcile_started or not bunny.live():
        return
    _reconcile_started = True
    threading.Thread(target=_reconcile_forever, name="transcode-reconcile", daemon=True).start()


def _require_fake_mode() -> None:
    if bunny.live():
        raise HTTPException(status_code=404, detail="dev upload receiver is disabled in real mode")


# ---- media assets: covers and subtitles (Bunny Storage + image CDN) ----
COVER_MAX_BYTES = 5 * 1024 * 1024


COVER_EXTS = ("jpg", "png", "webp")


COVER_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}


SUBTITLE_MAX_BYTES = 2 * 1024 * 1024


def _sniff_cover_ext(data: bytes) -> str | None:
    """Trust file bytes, not the browser-supplied Content-Type."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _cover_remote(drama_id: int, ext: str) -> str:
    return f"covers/{drama_id}/cover.{ext}"


def _best_effort_storage(action, *args) -> None:
    if not bunny.live():
        return
    try:
        action(*args)
    except bunny.BunnyError:
        return


def _purge_cover(drama_id: int, ext: str) -> None:
    """Drop the canonical object and any ?v= cache variant."""
    if not bunny.live():
        return
    try:
        url = bunny.image_url(_cover_remote(drama_id, ext))
    except bunny.BunnyError:
        return
    _best_effort_storage(bunny.purge_url, url)
    _best_effort_storage(bunny.purge_url, url + "*")


def _forget_other_covers(drama_id: int, keep_ext: str | None) -> None:
    for ext in COVER_EXTS:
        if ext == keep_ext:
            continue
        _best_effort_storage(bunny.storage_delete, _cover_remote(drama_id, ext))
        _purge_cover(drama_id, ext)
