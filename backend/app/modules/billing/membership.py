"""Membership status and access windows shared across billing use cases."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.billing.models import Plan, Subscription


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def access_until(session: Session, user_id: int) -> datetime | None:
    """Latest moment this user is entitled: subscription period or pass end."""
    row = session.execute(
        select(Subscription).where(
            Subscription.user_id == user_id,
            Subscription.status == "active",
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    now = datetime.now(timezone.utc)
    candidates = [
        end
        for end in (_as_aware(row.current_period_end), _as_aware(row.pass_end))
        if end is not None and end > now
    ]
    return max(candidates) if candidates else None


def active_membership(session: Session, user_id: int) -> dict:
    """What /v1/me shows: whichever access window ends last."""
    row = session.execute(
        select(Subscription, Plan)
        .join(Plan, Plan.id == Subscription.plan_id)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == "active",
        )
    ).first()
    if row is None:
        return {
            "active": False,
            "plan_code": None,
            "until": None,
            "canceling": False,
            "billing_mode": None,
            "subscription_until": None,
            "pass_until": None,
        }
    subscription, plan = row
    now = datetime.now(timezone.utc)
    sub_end = _as_aware(subscription.current_period_end)
    pass_end = _as_aware(subscription.pass_end)
    live = [end for end in (sub_end, pass_end) if end is not None and end > now]
    if not live:
        return {
            "active": False,
            "plan_code": None,
            "until": None,
            "canceling": False,
            "billing_mode": None,
            "subscription_until": sub_end.isoformat() if sub_end else None,
            "pass_until": pass_end.isoformat() if pass_end else None,
        }
    until = max(live)
    mode = "one_time" if pass_end == until and (sub_end is None or pass_end > sub_end) else "subscription"
    return {
        "active": True,
        "plan_code": plan.code,
        "until": until.isoformat(),
        # "Won't renew" only makes sense while a subscription is the live window.
        "canceling": mode == "subscription" and subscription.canceled_at is not None and sub_end is not None and sub_end > now,
        "billing_mode": mode,
        "subscription_until": sub_end.isoformat() if sub_end else None,
        "pass_until": pass_end.isoformat() if pass_end else None,
    }


def _now() -> datetime:
    return datetime.now(timezone.utc)


def active_subscription(session: Session, user_id: int, *, for_update: bool = False) -> Subscription | None:
    stmt = select(Subscription).where(
        Subscription.user_id == user_id,
        Subscription.status == "active",
    )
    if for_update:
        stmt = stmt.with_for_update()
    return session.scalar(stmt)


def _window_base(row: Subscription) -> datetime:
    """Where a granted period starts when it is *stacked* on what is left."""
    candidates = [end for end in (_as_aware(row.pass_end), _as_aware(row.current_period_end)) if end]
    return max([_now()] + candidates)
