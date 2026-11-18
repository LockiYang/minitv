"""Client-facing site and app configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.billing import service as billing
from app.modules.common.models import AuditLog
from app.modules.content import service as content
from app.modules.ops.config_models import (
    ChannelTab,
    CopyEntry,
    HotSearch,
    LegalPage,
    MarketingStat,
    StoreLink,
    Testimonial,
)

router = APIRouter(tags=["public-flow"])


@router.get("/config")
def config(locale: str = "en", db: Session = Depends(get_db)):
    tabs = db.scalars(select(ChannelTab).where(ChannelTab.enabled.is_(True)).order_by(ChannelTab.position)).all()
    copies = {}
    for row in db.scalars(select(CopyEntry)).all():
        if locale == "zh" and row.zh:
            copies[row.key] = row.zh
        else:
            if locale == "zh" and not row.zh:
                db.add(AuditLog(actor_type="system", actor_id=None, action="copy_fallback", target_type="copy", target_id=row.id, payload={"key": row.key}))
            copies[row.key] = row.en
    db.commit()
    return {
        "tabs": [
            {"code": t.code, "title_en": t.title_en, "title_zh": t.title_zh, "position": t.position, "locked": t.locked, "group_id": t.group_id}
            for t in tabs
        ],
        "copies": copies,
    }


@router.get("/marketing")
def marketing(db: Session = Depends(get_db)):
    dramas = content.list_published_dramas(db)[:6]
    return {
        "stats": [
            {"label": s.label, "value": s.value}
            for s in db.scalars(select(MarketingStat).order_by(MarketingStat.position)).all()
        ],
        "testimonials": [
            {"author": t.author, "body": t.body}
            for t in db.scalars(select(Testimonial).order_by(Testimonial.position)).all()
        ],
        "stores": {s.platform: s.url for s in db.scalars(select(StoreLink)).all()},
        "trending": [{"id": d.id, "title": d.title, "cover_url": d.cover_url, "play_count": d.play_count} for d in dramas],
        "plans": billing.list_current_plans(db),
    }


@router.get("/legal/{slug}")
def legal(slug: str, db: Session = Depends(get_db)):
    page = db.get(LegalPage, slug)
    if page is None:
        raise HTTPException(status_code=404, detail="not found")
    return {"slug": page.slug, "title": page.title, "body": page.body}


@router.get("/search/trending")
def trending(db: Session = Depends(get_db)):
    rows = db.scalars(select(HotSearch).where(HotSearch.enabled.is_(True)).order_by(HotSearch.position)).all()
    return {"items": [row.term for row in rows]}
