"""Administration of site and app configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.content.models import Drama, HomeGroup
from app.modules.ops.auth import require_admin, require_content_writer
from app.modules.ops.config_models import (
    Banner,
    ChannelTab,
    CopyEntry,
    HotSearch,
    LegalPage,
    MarketingStat,
    StoreLink,
)
from app.modules.ops.config_schemas import BannerIn, BannerUpdateIn, CopyIn, TabIn, TabUpdateIn
from app.modules.ops.models import AdminUser

router = APIRouter(tags=["admin-flow"])


@router.post("/copies")
def save_copy(body: CopyIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    if not body.en.strip():
        raise HTTPException(status_code=422, detail="en is required")
    row = db.scalar(select(CopyEntry).where(CopyEntry.key == body.key))
    if row is None:
        row = CopyEntry(key=body.key, en=body.en, zh=body.zh)
        db.add(row)
    else:
        row.en = body.en
        row.zh = body.zh
    db.commit()
    return {"key": row.key}


@router.get("/copies")
def list_copies(db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    rows = db.scalars(select(CopyEntry).order_by(CopyEntry.key)).all()
    return {"items": [{"key": row.key, "en": row.en, "zh": row.zh} for row in rows]}


DISCOVER_CODE = "discover"


def _tab_out(row: ChannelTab) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "title_en": row.title_en,
        "title_zh": row.title_zh,
        "position": row.position,
        "enabled": row.enabled,
        "locked": row.locked or row.code == DISCOVER_CODE,
        "group_id": row.group_id,
    }


@router.get("/tabs")
def list_tabs(db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    rows = db.scalars(select(ChannelTab).order_by(ChannelTab.position, ChannelTab.id)).all()
    return {"items": [_tab_out(row) for row in rows]}


@router.post("/tabs")
def save_tab(body: TabIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    if body.group_id is not None and db.get(HomeGroup, body.group_id) is None:
        raise HTTPException(status_code=404, detail="group not found")
    row = db.scalar(select(ChannelTab).where(ChannelTab.code == body.code))
    if row is None:
        row = ChannelTab(code=body.code, title_en=body.title_en, title_zh=body.title_zh, position=body.position)
        row.enabled = body.enabled
        row.locked = body.code == DISCOVER_CODE
        row.group_id = None if row.locked else body.group_id
        db.add(row)
    else:
        row.position = body.position
        row.title_en = body.title_en
        row.title_zh = body.title_zh
        row.enabled = body.enabled
        if not (row.locked or row.code == DISCOVER_CODE):
            row.group_id = body.group_id
    db.commit()
    return _tab_out(row)


@router.patch("/tabs/{tab_id}")
def update_tab(tab_id: int, body: TabUpdateIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = db.get(ChannelTab, tab_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tab not found")
    data = body.model_dump(exclude_unset=True)
    if "group_id" in data and data["group_id"] is not None and db.get(HomeGroup, data["group_id"]) is None:
        raise HTTPException(status_code=404, detail="group not found")
    locked = row.locked or row.code == DISCOVER_CODE
    if locked and ("position" in data or "enabled" in data or "group_id" in data):
        raise HTTPException(status_code=409, detail="discover tab is fixed")
    if "title_en" in data and data["title_en"] is not None:
        row.title_en = data["title_en"]
    if "title_zh" in data and data["title_zh"] is not None:
        row.title_zh = data["title_zh"]
    if "position" in data and data["position"] is not None:
        row.position = data["position"]
    if "enabled" in data and data["enabled"] is not None:
        row.enabled = data["enabled"]
    if "group_id" in data:
        row.group_id = data["group_id"]
    db.commit()
    return _tab_out(row)


@router.delete("/tabs/{tab_id}")
def delete_tab(tab_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = db.get(ChannelTab, tab_id)
    if row is None:
        raise HTTPException(status_code=404, detail="tab not found")
    if row.locked or row.code == DISCOVER_CODE:
        raise HTTPException(status_code=409, detail="discover tab is fixed")
    db.delete(row)
    db.commit()
    return {"deleted": True}


def _banner_out(row: Banner, drama: Drama | None) -> dict:
    return {
        "id": row.id,
        "slot": row.slot,
        "drama_id": row.drama_id,
        "title": None if drama is None else drama.title,
        "cover_url": None if drama is None else drama.cover_url,
        "kicker": row.kicker,
        "headline": row.headline,
        "status": row.status,
        "position": row.position,
    }


@router.get("/banners")
def list_banners(slot: str = "home", db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    rows = db.scalars(select(Banner).where(Banner.slot == slot).order_by(Banner.position, Banner.id)).all()
    return {"items": [_banner_out(row, db.get(Drama, row.drama_id) if row.drama_id else None) for row in rows]}


@router.post("/banners")
def save_banner(body: BannerIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    drama = db.get(Drama, body.drama_id)
    if drama is None:
        raise HTTPException(status_code=404, detail="drama not found")
    count = db.scalar(select(func.count()).select_from(Banner).where(Banner.slot == body.slot, Banner.status == "online")) or 0
    if body.status == "online" and count >= 3:
        raise HTTPException(status_code=409, detail="a banner holds 3 dramas at most")
    row = Banner(
        drama_id=drama.id,
        slot=body.slot,
        status=body.status,
        position=body.position,
        kicker=body.kicker,
        headline=body.headline or drama.title,
    )
    db.add(row)
    db.commit()
    return _banner_out(row, drama)


@router.patch("/banners/{banner_id}")
def update_banner(banner_id: int, body: BannerUpdateIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = db.get(Banner, banner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="banner not found")
    data = body.model_dump(exclude_unset=True)
    drama = db.get(Drama, row.drama_id) if row.drama_id else None
    if "drama_id" in data and data["drama_id"] is not None:
        drama = db.get(Drama, data["drama_id"])
        if drama is None:
            raise HTTPException(status_code=404, detail="drama not found")
        row.drama_id = drama.id
    if "status" in data and data["status"] is not None:
        row.status = data["status"]
    if "position" in data and data["position"] is not None:
        row.position = data["position"]
    if "kicker" in data and data["kicker"] is not None:
        row.kicker = data["kicker"]
    if "headline" in data and data["headline"] is not None:
        row.headline = data["headline"]
    db.commit()
    return _banner_out(row, drama)


@router.delete("/banners/{banner_id}")
def delete_banner(banner_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = db.get(Banner, banner_id)
    if row is None:
        raise HTTPException(status_code=404, detail="banner not found")
    db.delete(row)
    db.commit()
    return {"deleted": True}


@router.post("/hot-searches")
def save_hot(term: str, position: int = 0, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = HotSearch(term=term, position=position)
    db.add(row)
    db.commit()
    return {"id": row.id}


@router.post("/marketing/stats")
def save_stat(label: str, value: str, position: int = 0, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = MarketingStat(label=label, value=value, position=position)
    db.add(row)
    db.commit()
    return {"id": row.id}


@router.post("/legal/{slug}")
def save_legal(slug: str, title: str, body: str = "", db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = db.get(LegalPage, slug)
    if row is None:
        row = LegalPage(slug=slug, title=title, body=body)
        db.add(row)
    else:
        row.title = title
        row.body = body
    db.commit()
    return {"slug": slug}


@router.post("/stores")
def save_store(platform: str, url: str, db: Session = Depends(get_db), user: AdminUser = Depends(require_content_writer)):
    row = db.scalar(select(StoreLink).where(StoreLink.platform == platform))
    if row is None:
        row = StoreLink(platform=platform, url=url)
        db.add(row)
    else:
        row.url = url
    db.commit()
    return {"platform": platform, "url": url}
