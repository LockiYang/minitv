"""Finance order queries shared by billing and the operations dashboard."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.modules.account.models import User
from app.modules.billing.models import Order, Plan, PlanVersion


ORDER_STATUSES = ("created", "paid", "failed", "refunded")


ORDER_BILLING_MODES = ("subscription", "one_time")


ORDER_LEDGER_JOINS = (
    (User, User.id == Order.user_id),
    (PlanVersion, PlanVersion.id == Order.plan_version_id),
    (Plan, Plan.id == PlanVersion.plan_id),
)


def _order_payload(order: Order, account: User | None, version: PlanVersion | None, plan: Plan | None) -> dict:
    """One order row: legacy flat fields for the dashboard + nested user/plan."""
    return {
        "order_no": order.order_no,
        "user_id": order.user_id,
        "amount_cents": order.amount_cents,
        "currency": order.currency,
        "billing_mode": order.billing_mode,
        "status": order.status,
        "created_at": order.created_at.isoformat(),
        "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        "external_txn_id": order.external_txn_id,
        "user": None
        if account is None
        else {
            "id": account.id,
            "public_id": account.public_id,
            "display_name": account.display_name,
            "email": account.email,
            "avatar_url": account.avatar_url,
            "region": account.region,
            "locale": account.locale,
            "created_at": account.created_at.isoformat(),
        },
        "plan": None
        if plan is None
        else {
            "code": plan.code,
            "name_en": plan.name_en,
            "name_zh": plan.name_zh,
            "version": None if version is None else version.version,
            "price_cents": None if version is None else version.price_cents,
            "currency": None if version is None else version.currency,
            "benefits": None if version is None else (version.benefits or {}),
        },
    }


def list_finance_orders(
    db: Session,
    *,
    q: str | None = None,
    status: str | None = None,
    billing_mode: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    conditions = []
    needle = (q or "").strip()
    if needle:
        like = f"%{needle}%"
        conditions.append(
            or_(
                Order.order_no.ilike(like),
                Order.external_txn_id.ilike(like),
                User.email.ilike(like),
                User.display_name.ilike(like),
                User.public_id == needle,
            )
        )
    if status in ORDER_STATUSES:
        conditions.append(Order.status == status)
    if billing_mode in ORDER_BILLING_MODES:
        conditions.append(Order.billing_mode == billing_mode)

    joins = ORDER_LEDGER_JOINS
    count_stmt = select(func.count(Order.id))
    for target, onclause in joins:
        count_stmt = count_stmt.outerjoin(target, onclause)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = db.scalar(count_stmt) or 0

    stmt = select(Order, User, PlanVersion, Plan)
    for target, onclause in joins:
        stmt = stmt.outerjoin(target, onclause)
    if conditions:
        stmt = stmt.where(*conditions)
    rows = db.execute(
        stmt.order_by(Order.id.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()

    items = [_order_payload(*row) for row in rows]

    grouped = db.execute(
        select(
            Order.status,
            func.count(Order.id),
            func.coalesce(func.sum(Order.amount_cents), 0),
        ).group_by(Order.status)
    ).all()
    counts = {row[0]: {"orders": int(row[1]), "amount_cents": int(row[2])} for row in grouped}
    paid = counts.get("paid", {"orders": 0, "amount_cents": 0})
    refunded = counts.get("refunded", {"orders": 0, "amount_cents": 0})
    created = counts.get("created", {"orders": 0, "amount_cents": 0})
    failed = counts.get("failed", {"orders": 0, "amount_cents": 0})
    total_orders = sum(item["orders"] for item in counts.values()) or 0

    # 今日成交按后台统一业务日（洛杉矶时区）统计，与数据看板口径一致。
    from app.modules.account.service import LA, la_today

    day_start = datetime.combine(la_today(), datetime.min.time(), tzinfo=LA).astimezone(
        timezone.utc
    )
    today_paid_cents = (
        db.scalar(
            select(func.coalesce(func.sum(Order.amount_cents), 0)).where(
                Order.status == "paid", Order.paid_at >= day_start
            )
        )
        or 0
    )
    today_orders = (
        db.scalar(select(func.count(Order.id)).where(Order.created_at >= day_start)) or 0
    )

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "summary": {
            "total_orders": total_orders,
            "paid_orders": paid["orders"],
            "refunded_orders": refunded["orders"],
            "created_orders": created["orders"],
            "failed_orders": failed["orders"],
            "paid_cents": paid["amount_cents"],
            "refunded_cents": refunded["amount_cents"],
            "pending_cents": created["amount_cents"],
            # 口径：gross = 曾经收到的钱（含后来退掉的），net = 退款后真正留下的。
            "gross_cents": paid["amount_cents"] + refunded["amount_cents"],
            "net_cents": paid["amount_cents"],
            "today_paid_cents": int(today_paid_cents),
            "today_orders": int(today_orders),
            "currency": "USD",
        },
    }
