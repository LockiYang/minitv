"""Supplier accounts, statistics, settlements and credentials."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.common.serialization import iso_or_none
from app.modules.content.models import Drama, Episode
from app.modules.ops import (
    api_keys as api_key_service,
    service as ops_service,
    settlements as settlement_service,
)
from app.modules.ops.models import (
    AdminApiKey,
    AdminUser,
    Supplier,
    SupplierSettlement,
    SupplierShareHistory,
)
from app.modules.ops.partner_auth import _bearer_token, require_partner, require_partner_session
from app.modules.ops.schemas import (
    PartnerKeyCreateIn,
    PartnerKeyRevokeIn,
    PartnerKeyUpdateIn,
    PartnerPasswordIn,
)
from app.modules.playback.models import PlaybackEvent
from app.modules.wallet.models import CoinLedgerEntry

router = APIRouter(tags=["partner"])


# ---- profile / auth ----
@router.get("/me")
def me(user: AdminUser = Depends(require_partner), db: Session = Depends(get_db)):
    supplier = db.get(Supplier, user.supplier_id)
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "supplier": {
            "id": supplier.id,
            "name": supplier.name,
            "status": supplier.status,
            "revenue_share_bps": supplier.revenue_share_bps,
        },
    }


@router.post("/auth/password")
def change_password(
    body: PartnerPasswordIn,
    user: AdminUser = Depends(require_partner_session),
    db: Session = Depends(get_db),
):
    if not ops_service.verify_password(body.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    if body.new_password == body.old_password:
        raise HTTPException(status_code=422, detail="新密码不能与原密码相同")
    killed = ops_service.revoke_user_sessions(user.id)
    user.password_hash = ops_service.hash_password(body.new_password)
    db.commit()
    return {"revoked_sessions": killed}


@router.post("/auth/logout")
def logout(request: Request, user: AdminUser = Depends(require_partner_session)):
    try:
        ops_service.logout(_bearer_token(request))
    except ops_service.SessionStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="会话存储不可用") from exc
    return {"ok": True}


# ---- stats (P4) ----
def _supplier_episode_ids(user: AdminUser):
    return select(Episode.id).join(Drama, Drama.id == Episode.drama_id).where(
        Drama.supplier_id == user.supplier_id
    )


def _play_counts(db: Session, ids, start: datetime) -> tuple[int, int]:
    rows = db.execute(
        select(PlaybackEvent.kind, func.count(PlaybackEvent.id))
        .where(
            PlaybackEvent.episode_id.in_(ids),
            PlaybackEvent.created_at >= start,
            PlaybackEvent.kind.in_(["start", "complete"]),
        )
        .group_by(PlaybackEvent.kind)
    ).all()
    counts = dict(rows)
    return counts.get("start", 0), counts.get("complete", 0)


def _unlock_counts(db: Session, ids, start: datetime) -> tuple[int, int]:
    row = db.execute(
        select(func.count(CoinLedgerEntry.id), func.count(func.distinct(CoinLedgerEntry.user_id)))
        .where(
            CoinLedgerEntry.episode_id.in_(ids),
            CoinLedgerEntry.amount < 0,
            CoinLedgerEntry.biz_type == "unlock",
            CoinLedgerEntry.created_at >= start,
        )
    ).one()
    return int(row[0] or 0), int(row[1] or 0)


@router.get("/stats/overview")
def stats_overview(
    days: int = Query(default=30, ge=1, le=90),
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    start = datetime.now(timezone.utc) - timedelta(days=days)
    supplier = db.get(Supplier, user.supplier_id)
    ids = _supplier_episode_ids(user)
    plays, completes = _play_counts(db, ids, start)
    unlock_total, unlock_users = _unlock_counts(db, ids, start)
    viewers = (
        db.scalar(
            select(func.count(func.distinct(PlaybackEvent.user_id))).where(
                PlaybackEvent.episode_id.in_(ids),
                PlaybackEvent.kind == "start",
                PlaybackEvent.created_at >= start,
                PlaybackEvent.user_id.is_not(None),
            )
        )
        or 0
    )
    orders = settlement_service.first_orders_for_supplier(
        db, supplier.id, start.date(), datetime.now(timezone.utc).date() + timedelta(days=1)
    )
    money = settlement_service.revenue_for_orders(db, supplier, orders)
    return {
        "days": days,
        "plays": plays,
        "completes": completes,
        "completion_rate": round(completes / plays, 4) if plays else 0,
        "viewers": int(viewers),
        "unlocks": unlock_total,
        "unlock_users": unlock_users,
        "first_orders": money["order_count"],
        "gross_cents": money["gross_cents"],
        "revenue_cents": money["revenue_cents"],
        "effective_share_bps": money["effective_share_bps"],
        "currency": money["currency"],
    }


@router.get("/stats/dramas")
def stats_dramas(
    days: int = Query(default=30, ge=1, le=90),
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    start = datetime.now(timezone.utc) - timedelta(days=days)
    supplier = db.get(Supplier, user.supplier_id)
    dramas = db.scalars(
        select(Drama).where(Drama.supplier_id == user.supplier_id).order_by(Drama.id)
    ).all()
    ids = [row.id for row in dramas]
    plays: dict[int, int] = {}
    completes: dict[int, int] = {}
    viewers: dict[int, int] = {}
    unlocks: dict[int, int] = {}
    if ids:
        for drama_id, kind, count in db.execute(
            select(Episode.drama_id, PlaybackEvent.kind, func.count(PlaybackEvent.id))
            .join(Episode, Episode.id == PlaybackEvent.episode_id)
            .where(
                Episode.drama_id.in_(ids),
                PlaybackEvent.created_at >= start,
                PlaybackEvent.kind.in_(["start", "complete"]),
            )
            .group_by(Episode.drama_id, PlaybackEvent.kind)
        ).all():
            (plays if kind == "start" else completes)[drama_id] = count
        for drama_id, count in db.execute(
            select(Episode.drama_id, func.count(func.distinct(PlaybackEvent.user_id)))
            .join(Episode, Episode.id == PlaybackEvent.episode_id)
            .where(
                Episode.drama_id.in_(ids),
                PlaybackEvent.kind == "start",
                PlaybackEvent.created_at >= start,
                PlaybackEvent.user_id.is_not(None),
            )
            .group_by(Episode.drama_id)
        ).all():
            viewers[drama_id] = count
        for drama_id, count in db.execute(
            select(Episode.drama_id, func.count(CoinLedgerEntry.id))
            .join(Episode, Episode.id == CoinLedgerEntry.episode_id)
            .where(
                Episode.drama_id.in_(ids),
                CoinLedgerEntry.amount < 0,
                CoinLedgerEntry.biz_type == "unlock",
                CoinLedgerEntry.created_at >= start,
            )
            .group_by(Episode.drama_id)
        ).all():
            unlocks[drama_id] = count

    orders = settlement_service.first_orders_for_supplier(
        db, supplier.id, start.date(), datetime.now(timezone.utc).date() + timedelta(days=1)
    )
    history = list(
        db.scalars(
            select(SupplierShareHistory)
            .where(SupplierShareHistory.supplier_id == supplier.id)
            .order_by(SupplierShareHistory.effective_from)
        )
    )
    order_stats: dict[int, dict] = {}
    for order in orders:
        if order.source_drama_id is None or order.source_drama_id not in ids:
            continue
        paid_at = order.paid_at
        if paid_at.tzinfo is None:
            paid_at = paid_at.replace(tzinfo=timezone.utc)
        bps = settlement_service._share_at(history, paid_at, supplier.revenue_share_bps)
        item = order_stats.setdefault(order.source_drama_id, {"orders": 0, "gross": 0, "revenue": 0})
        item["orders"] += 1
        item["gross"] += order.amount_cents
        item["revenue"] += order.amount_cents * bps // 10000

    items = []
    for drama in dramas:
        p = plays.get(drama.id, 0)
        c = completes.get(drama.id, 0)
        money = order_stats.get(drama.id, {"orders": 0, "gross": 0, "revenue": 0})
        items.append(
            {
                "id": drama.id,
                "title": drama.title,
                "cover_url": drama.cover_url,
                "status": drama.status,
                "plays": p,
                "completion_rate": round(c / p, 4) if p else 0,
                "viewers": viewers.get(drama.id, 0),
                "unlocks": unlocks.get(drama.id, 0),
                "first_orders": money["orders"],
                "gross_cents": money["gross"],
                "revenue_cents": money["revenue"],
            }
        )
    return {"days": days, "currency": "USD", "items": items}


@router.get("/settlements")
def my_settlements(
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(SupplierSettlement)
        .where(SupplierSettlement.supplier_id == user.supplier_id)
        .order_by(SupplierSettlement.period_start.desc())
        .limit(24)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "month": row.period_start.strftime("%Y-%m"),
                "currency": row.currency,
                "gross_cents": row.gross_cents,
                "revenue_cents": row.revenue_cents,
                "effective_share_bps": row.effective_share_bps,
                "order_count": row.order_count,
                "status": row.status,
                "confirmed_at": iso_or_none(row.confirmed_at),
                "paid_at": iso_or_none(row.paid_at),
            }
            for row in rows
        ]
    }


# ---- api keys (session only) ----
def _own_key(db: Session, user: AdminUser, key_id: int) -> AdminApiKey:
    key = db.get(AdminApiKey, key_id)
    if key is None or key.admin_user_id != user.id:
        raise HTTPException(status_code=404, detail="key not found")
    return key


def _key_out(key: AdminApiKey) -> dict:
    return {
        "id": key.id,
        "name": key.name,
        "key_prefix": key.key_prefix,
        "scopes": list(key.scopes or []),
        "state": api_key_service.key_state(key),
        "status": key.status,
        "expires_at": iso_or_none(key.expires_at),
        "last_used_at": iso_or_none(key.last_used_at),
        "created_at": iso_or_none(key.created_at),
        "revoked_at": iso_or_none(key.revoked_at),
        "revoke_reason": key.revoke_reason,
    }


@router.get("/api-keys")
def list_keys(
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(AdminApiKey)
        .where(AdminApiKey.admin_user_id == user.id)
        .order_by(AdminApiKey.id.desc())
    ).all()
    items = [_key_out(key) for key in rows]
    summary = {
        "active": sum(1 for item in items if item["state"] == "active"),
        "expiring": sum(1 for item in items if item["state"] == "expiring"),
        "expired": sum(1 for item in items if item["state"] == "expired"),
        "revoked": sum(1 for item in items if item["state"] == "revoked"),
        "total": len(items),
    }
    return {"items": items, "summary": summary}


@router.get("/auth/keys")
def list_own_keys(
    user: AdminUser = Depends(require_partner_session),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(AdminApiKey)
        .where(AdminApiKey.admin_user_id == user.id)
        .order_by(AdminApiKey.id.desc())
    ).all()
    return {"items": [_key_out(key) for key in rows]}


@router.get("/auth/key-info")
def current_key_info(
    request: Request,
    user: AdminUser = Depends(require_partner),
    db: Session = Depends(get_db),
):
    """Metadata of the calling key only — what the CLI needs for `whoami`.

    Deliberately narrower than `/auth/keys`: a credential must not be able to
    enumerate its owner's other keys.
    """
    credential = _bearer_token(request)
    if not api_key_service.looks_like_key(credential):
        raise HTTPException(status_code=400, detail="请使用 API Key 调用")
    key = db.scalar(
        select(AdminApiKey).where(
            AdminApiKey.key_prefix == credential[: api_key_service.KEY_PREFIX_LEN]
        )
    )
    if key is None:
        raise HTTPException(status_code=404, detail="key not found")
    return _key_out(key)


@router.post("/api-keys", status_code=201)
def create_key(
    body: PartnerKeyCreateIn,
    user: AdminUser = Depends(require_partner_session),
    db: Session = Depends(get_db),
):
    scopes = api_key_service.normalise_scopes(body.scopes, api_key_service.PARTNER_SCOPES)
    key, plaintext = api_key_service.create_key(
        db,
        admin_user_id=user.id,
        name=body.name,
        scopes=scopes,
        expires_in_days=body.expires_in_days,
        actor_id=user.id,
    )
    return {**_key_out(key), "key": plaintext}


@router.patch("/api-keys/{key_id}")
def update_key(
    key_id: int,
    body: PartnerKeyUpdateIn,
    user: AdminUser = Depends(require_partner_session),
    db: Session = Depends(get_db),
):
    key = _own_key(db, user, key_id)
    scopes = (
        api_key_service.normalise_scopes(body.scopes, api_key_service.PARTNER_SCOPES)
        if body.scopes is not None
        else None
    )
    key = api_key_service.update_key(
        db,
        key,
        name=body.name,
        scopes=scopes,
        expires_at=None,
        expires_at_set=False,
        actor_id=user.id,
    )
    return _key_out(key)


@router.post("/api-keys/{key_id}/rotate", status_code=201)
def rotate_key(
    key_id: int,
    user: AdminUser = Depends(require_partner_session),
    db: Session = Depends(get_db),
):
    key = _own_key(db, user, key_id)
    fresh, plaintext = api_key_service.rotate_key(
        db, key, expires_in_days=api_key_service.DEFAULT_TTL_DAYS, actor_id=user.id
    )
    return {**_key_out(fresh), "key": plaintext}


@router.post("/api-keys/{key_id}/revoke")
def revoke_key(
    key_id: int,
    body: PartnerKeyRevokeIn | None = None,
    user: AdminUser = Depends(require_partner_session),
    db: Session = Depends(get_db),
):
    key = _own_key(db, user, key_id)
    key = api_key_service.revoke_key(
        db, key, reason=body.reason if body else None, actor_id=user.id
    )
    return _key_out(key)
