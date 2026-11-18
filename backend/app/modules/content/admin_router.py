"""Administration of episodes, publishing and home groups."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.common import bunny
from app.modules.common.models import AuditLog
from app.modules.content.admin_queries import _drama_or_404
from app.modules.content.media_service import _cached_library, apply_remote_video
from app.modules.content.models import (
    CdnPurge,
    Drama,
    DramaHomeGroup,
    Episode,
    HomeGroup,
    MediaUpload,
)
from app.modules.content.publish_service import (
    GROUP_LIMIT,
    PublishError,
    add_to_group,
    apply_due_schedules,
    change_drama_status,
    delete_drama,
    publish_episode,
    remove_from_group,
    schedule_publish,
)
from app.modules.content.schemas import (
    BulkIn,
    DeleteIn,
    EpisodeBulkStatusIn,
    EpisodeIn,
    EpisodeUpdateIn,
    GroupAddIn,
    GroupIn,
    GroupUpdateIn,
    PositionIn,
    ScheduleIn,
    StatusIn,
)
from app.modules.content.service import InvalidTransition, transition_episode
from app.modules.ops.auth import require_admin, require_admin_role, require_content_writer
from app.modules.ops.models import AdminUser
from app.modules.playback.service import bump_generation

router = APIRouter(tags=["admin-flow"])


@router.post("/dramas/{drama_id}/episodes")
def create_episode(drama_id: int, body: EpisodeIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    _drama_or_404(db, drama_id)
    duplicate = db.scalar(
        select(Episode.id).where(
            Episode.drama_id == drama_id,
            Episode.episode_number == body.episode_number,
        )
    )
    if duplicate is not None:
        raise HTTPException(
            status_code=409,
            detail=f"第 {body.episode_number} 集已存在，请换一个集数",
        )
    title = (body.title or f"Episode {body.episode_number}").strip() or f"Episode {body.episode_number}"
    row = Episode(drama_id=drama_id, episode_number=body.episode_number, title=title[:200])
    db.add(row)
    db.commit()
    return {
        "id": row.id,
        "episode_number": row.episode_number,
        "title": row.title,
        "status": row.status,
    }


@router.post("/dramas/{drama_id}/episodes/bulk")
def bulk_episodes(drama_id: int, body: BulkIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    _drama_or_404(db, drama_id)
    existing = set(db.scalars(select(Episode.episode_number).where(Episode.drama_id == drama_id)))
    created = []
    for number in range(1, body.count + 1):
        if number in existing:
            continue
        row = Episode(drama_id=drama_id, episode_number=number, title=f"Episode {number}")
        db.add(row)
        created.append(number)
    db.commit()
    return {"created": created, "total": body.count}


@router.get("/dramas/{drama_id}/episodes")
def list_episodes(
    drama_id: int,
    status: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    stmt = select(Episode).where(Episode.drama_id == drama_id)
    if status:
        stmt = stmt.where(Episode.status == status)
    if q:
        stmt = stmt.where(or_(Episode.title.ilike(f"%{q}%"), Episode.episode_number == _maybe_int(q)))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Episode.episode_number).offset((page - 1) * page_size).limit(page_size)).all()
    # Opening the episode list is the moment an operator notices a stuck
    # "transcoding" badge. Match this page against the library listing so a
    # finished encode flips without a manual sync click. One listing, not one
    # request per row, and the listing is cached so paging does not refetch.
    if bunny.live() and any(row.status == "transcoding" and row.bunny_video_id for row in rows):
        remote_by_guid = _cached_library()
        changed = False
        for row in rows:
            remote = remote_by_guid.get(row.bunny_video_id)
            if row.status != "transcoding" or remote is None:
                continue
            try:
                if apply_remote_video(db, row, remote)["changed"]:
                    changed = True
            except InvalidTransition:
                continue
        if changed:
            db.commit()
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [
            {
                "id": row.id,
                "episode_number": row.episode_number,
                "title": row.title,
                "status": row.status,
                "duration_seconds": row.duration_seconds,
                "bunny_video_id": row.bunny_video_id,
            }
            for row in rows
        ],
    }


def _maybe_int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return -1


@router.delete("/episodes/{episode_id}", status_code=204)
def delete_episode(
    episode_id: int,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    """Delete an **empty shell** only (`not_uploaded` / `transcode_failed`).

    Anything with actual media (in_review / published / unpublished) must be
    taken down first — deleting it would silently drop a video that viewers may
    still hold an entitlement for.
    """
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if episode.status not in ("not_uploaded", "transcode_failed"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "episode_not_empty",
                "message": f"第 {episode.episode_number} 集是 {episode.status}，只有空壳可以删；请先下架",
            },
        )
    bump_generation(episode.id)
    db.execute(delete(MediaUpload).where(MediaUpload.episode_id == episode.id))
    db.execute(delete(CdnPurge).where(CdnPurge.episode_id == episode.id))
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=user.id,
            action="episode_delete",
            target_type="episode",
            target_id=episode.id,
            payload={
                "drama_id": episode.drama_id,
                "episode_number": episode.episode_number,
                "status": episode.status,
            },
        )
    )
    db.delete(episode)
    db.commit()
    return Response(status_code=204)


@router.patch("/episodes/{episode_id}")
def update_episode(
    episode_id: int,
    body: EpisodeUpdateIn,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    """Edit episode metadata. Video bytes are replaced via upload-session."""
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    if body.title is None and body.episode_number is None:
        raise HTTPException(status_code=422, detail="没有需要更新的字段")
    if body.title is not None:
        title = body.title.strip()
        if not title:
            raise HTTPException(status_code=422, detail="标题不能为空")
        episode.title = title
    if body.episode_number is not None and body.episode_number != episode.episode_number:
        duplicate = db.scalar(
            select(Episode.id).where(
                Episode.drama_id == episode.drama_id,
                Episode.episode_number == body.episode_number,
                Episode.id != episode.id,
            )
        )
        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail=f"第 {body.episode_number} 集已存在，请换一个集数",
            )
        episode.episode_number = body.episode_number
    try:
        db.commit()
    except IntegrityError:
        # Race with a concurrent create/rename of the same number.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"第 {body.episode_number} 集已存在，请换一个集数",
        ) from None
    return {
        "id": episode.id,
        "episode_number": episode.episode_number,
        "title": episode.title,
        "status": episode.status,
    }


@router.post("/episodes/{episode_id}/publish")
def publish_one(episode_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    try:
        publish_episode(db, episode)
    except PublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"status": episode.status}


@router.post("/episodes/{episode_id}/unpublish")
def unpublish_one(episode_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    """Take one episode off the public catalog; the video object stays intact."""
    episode = db.get(Episode, episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="episode not found")
    try:
        transition_episode(db, episode, "unpublished")
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    bump_generation(episode.id)  # invalidate already-signed local URLs
    db.commit()
    return {"status": episode.status}


@router.post("/episodes/bulk-status")
def bulk_episode_status(
    body: EpisodeBulkStatusIn,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_content_writer),
):
    """Publish or take down many episodes in one call.

    Rows that cannot move (wrong source status, missing video, unknown id) are
    reported in ``skipped`` instead of failing the whole batch, so the admin
    selection stays a single click even with a mixed table.
    """
    target = "published" if body.action == "publish" else "unpublished"
    rows = db.scalars(select(Episode).where(Episode.id.in_(body.episode_ids))).all()
    found = {episode.id for episode in rows}
    updated: list[dict] = []
    skipped: list[dict] = []
    for episode in rows:
        if target == "published" and not episode.bunny_video_id:
            skipped.append({"id": episode.id, "status": episode.status, "reason": "no_video"})
            continue
        try:
            transition_episode(db, episode, target)
        except InvalidTransition:
            skipped.append({"id": episode.id, "status": episode.status, "reason": "invalid_transition"})
            continue
        if target == "unpublished":
            bump_generation(episode.id)
        updated.append(
            {"id": episode.id, "episode_number": episode.episode_number, "status": episode.status}
        )
    for missing_id in sorted(set(body.episode_ids) - found):
        skipped.append({"id": missing_id, "status": None, "reason": "not_found"})
    db.commit()
    return {"action": body.action, "updated": updated, "skipped": skipped}


@router.post("/dramas/{drama_id}/status")
def drama_status(drama_id: int, body: StatusIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    drama = _drama_or_404(db, drama_id)
    try:
        change_drama_status(db, drama, body.to_status, actor_id=user.id)
    except PublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"status": drama.status}


@router.post("/dramas/{drama_id}/schedule")
def drama_schedule(drama_id: int, body: ScheduleIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    drama = _drama_or_404(db, drama_id)
    schedule_publish(db, drama, body.at, actor_id=user.id)
    return {"scheduled_publish_at": drama.scheduled_publish_at.isoformat() if drama.scheduled_publish_at else None}


@router.post("/schedules/apply")
def apply_schedules(db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    return {"published": apply_due_schedules(db)}


@router.post("/dramas/{drama_id}/delete")
def remove_drama(drama_id: int, body: DeleteIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_admin_role)):
    drama = _drama_or_404(db, drama_id)
    try:
        return delete_drama(db, drama, confirm=body.confirm, actor_id=user.id)
    except PublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/groups")
def create_group(body: GroupIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = HomeGroup(name=body.name, position=body.position)
    db.add(row)
    db.commit()
    return {"id": row.id}


def _group_counts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(DramaHomeGroup.group_id, func.count()).group_by(DramaHomeGroup.group_id)
    ).all()
    return {group_id: count for group_id, count in rows}


def _group_out(row: HomeGroup, drama_count: int) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "position": row.position,
        "status": row.status,
        "style": row.style,
        "badge": row.badge,
        "drama_count": drama_count,
        "limit": GROUP_LIMIT,
    }


@router.get("/groups")
def list_groups(db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    rows = db.scalars(select(HomeGroup).order_by(HomeGroup.position, HomeGroup.id)).all()
    counts = _group_counts(db)
    return {"items": [_group_out(row, counts.get(row.id, 0)) for row in rows]}


@router.patch("/groups/{group_id}")
def update_group(group_id: int, body: GroupUpdateIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    """Rename or pause a group. A body that only carries position keeps the old contract."""
    group = db.get(HomeGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        name = (data["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="name is required")
        group.name = name
    if "status" in data and data["status"] is not None:
        group.status = data["status"]
    if "position" in data and data["position"] is not None:
        group.position = data["position"]
    if "style" in data and data["style"] is not None:
        group.style = data["style"]
    if "badge" in data and data["badge"] is not None:
        group.badge = data["badge"]
    if not data:
        raise HTTPException(status_code=422, detail="nothing to update")
    db.commit()
    count = db.scalar(select(func.count()).select_from(DramaHomeGroup).where(DramaHomeGroup.group_id == group.id)) or 0
    return _group_out(group, count)


@router.post("/groups/{group_id}/dramas")
def group_add(group_id: int, body: GroupAddIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    group = db.get(HomeGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    try:
        return add_to_group(db, group, body.drama_id, confirm=body.confirm)
    except PublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/groups/{group_id}/dramas")
def group_dramas(group_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    group = db.get(HomeGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="group not found")
    links = db.scalars(
        select(DramaHomeGroup)
        .where(DramaHomeGroup.group_id == group_id)
        .order_by(DramaHomeGroup.position_in_group, DramaHomeGroup.drama_id)
    ).all()
    items = []
    for link in links:
        drama = db.get(Drama, link.drama_id)
        if drama is None:
            continue
        items.append(
            {
                "id": drama.id,
                "title": drama.title,
                "status": drama.status,
                "cover_url": drama.cover_url,
                "position_in_group": link.position_in_group,
            }
        )
    count = len(items)
    return {"group": _group_out(group, count), "items": items}


@router.delete("/groups/{group_id}/dramas/{drama_id}")
def group_remove(group_id: int, drama_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    if db.get(HomeGroup, group_id) is None:
        raise HTTPException(status_code=404, detail="group not found")
    if not remove_from_group(db, group_id, drama_id):
        raise HTTPException(status_code=404, detail="not in group")
    return {"removed": True}


@router.patch("/groups/{group_id}/dramas/{drama_id}")
def group_position(group_id: int, drama_id: int, body: PositionIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    link = db.get(DramaHomeGroup, (group_id, drama_id))
    if link is None:
        raise HTTPException(status_code=404, detail="not in group")
    link.position_in_group = body.position
    db.commit()
    return {"position_in_group": link.position_in_group}


@router.get("/dramas/{drama_id}/groups")
def drama_groups(drama_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    """Groups this drama belongs to, plus every group so the editor can offer the rest."""
    _drama_or_404(db, drama_id)
    rows = db.scalars(select(HomeGroup).order_by(HomeGroup.position, HomeGroup.id)).all()
    counts = _group_counts(db)
    links = {
        link.group_id: link.position_in_group
        for link in db.scalars(select(DramaHomeGroup).where(DramaHomeGroup.drama_id == drama_id)).all()
    }
    items = []
    for row in rows:
        item = _group_out(row, counts.get(row.id, 0))
        item["joined"] = row.id in links
        item["position_in_group"] = links.get(row.id)
        items.append(item)
    return {"items": items}
