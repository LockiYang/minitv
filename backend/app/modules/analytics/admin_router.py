"""Operations dashboard."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.analytics.service import dashboard as build_dashboard
from app.modules.billing.admin_queries import list_finance_orders
from app.modules.ops.auth import require_admin
from app.modules.ops.models import AdminUser

router = APIRouter(tags=["admin-flow"])


@router.get("/dashboard")
def dashboard(
    day: date | None = None,
    days: int = Query(default=7, ge=1, le=90),
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """Aggregates for one business day (dashboard timezone) plus the trend window."""
    from app.modules.account.service import la_today

    target = day or la_today()
    data = build_dashboard(db, target, days=days)
    data["recent_orders"] = list_finance_orders(db, page=1, page_size=5)["items"]
    return data
