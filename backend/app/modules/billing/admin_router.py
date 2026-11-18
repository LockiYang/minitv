"""Administration of billing, membership and orders."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.account.models import User
from app.modules.billing.admin_queries import ORDER_LEDGER_JOINS, _order_payload, list_finance_orders
from app.modules.billing.models import Order, Plan, PlanVersion
from app.modules.billing.schemas import (
    BillingModeIn,
    MembershipGrantIn,
    MembershipRevokeIn,
    PriceIn,
)
from app.modules.billing.service import (
    BillingError,
    active_membership,
    billing_mode,
    change_price,
    get_billing_settings,
    grant_membership as billing_grant_membership,
    list_current_plans,
    membership_detail as billing_membership_detail,
    provision_prices_for_mode,
    revoke_membership as billing_revoke_membership,
    set_billing_mode,
)
from app.modules.common.models import AuditLog
from app.modules.ops.auth import require_admin, require_admin_role
from app.modules.ops.models import AdminUser

router = APIRouter(tags=["admin-flow"])


@router.get("/plans")
def admin_plans(db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    return {"items": list_current_plans(db), "billing_mode": billing_mode(db)}


@router.get("/billing/settings")
def read_billing_settings(db: Session = Depends(get_db), user: AdminUser = Depends(require_admin)):
    row = get_billing_settings(db)
    return {
        "billing_mode": row.billing_mode,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
    }


@router.post("/billing/settings")
def update_billing_settings(
    body: BillingModeIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_admin_role)
):
    """Global switch: affects new purchases only, existing subscriptions keep renewing."""
    try:
        row = set_billing_mode(db, body.billing_mode, user.id)
        provisioned = provision_prices_for_mode(db, row.billing_mode)
    except BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    db.add(
        AuditLog(
            actor_type="admin",
            actor_id=user.id,
            action="billing_mode_change",
            target_type="billing_settings",
            target_id=row.id,
            payload={"billing_mode": row.billing_mode, "prices": provisioned},
        )
    )
    db.commit()
    return {
        "billing_mode": row.billing_mode,
        "prices": provisioned,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.post("/plans/{code}/price")
def set_price(code: str, body: PriceIn, db: Session = Depends(get_db), user: AdminUser = Depends(require_admin_role)):
    try:
        version = change_price(db, code=code, price_cents=body.price_cents, benefits=body.benefits, actor_id=user.id)
    except BillingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {
        "version": version.version,
        "price_cents": version.price_cents,
        "stripe_price_id": version.stripe_price_id,
    }


@router.get("/users/{user_id}/membership")
def user_membership(
    user_id: int,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """会员详情：真实到期时间、来源、最近 6 条记录与上一次后台调整。"""
    account = db.get(User, user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="user not found")
    payload = billing_membership_detail(db, user_id)
    payload["user"] = {
        "id": account.id,
        "public_id": account.public_id,
        "display_name": account.display_name,
        "email": account.email,
    }
    last = payload.get("last_adjust")
    if last and last.get("actor_id"):
        staff = db.get(AdminUser, last["actor_id"])
        if staff is not None:
            last["actor_name"] = staff.display_name or staff.username
    return payload


@router.post("/users/{user_id}/membership")
def grant_user_membership(
    user_id: int,
    body: MembershipGrantIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    """后台开通 / 续期：只叠 `pass_end`，不建订单、不碰 Stripe 价格。"""
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    try:
        return billing_grant_membership(
            db,
            user_id=user_id,
            plan_code=body.plan_code,
            days=body.days,
            mode=body.mode,
            reason=body.reason,
            actor_id=actor.id,
        )
    except BillingError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.delete("/users/{user_id}/membership")
def revoke_user_membership(
    user_id: int,
    body: MembershipRevokeIn,
    db: Session = Depends(get_db),
    actor: AdminUser = Depends(require_admin_role),
):
    """后台取消：Stripe 订阅先打远程，成功后本地两个时间窗一起收回。"""
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    try:
        return billing_revoke_membership(
            db,
            user_id=user_id,
            mode=body.mode,
            reason=body.reason,
            actor_id=actor.id,
        )
    except BillingError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/finance/orders")
def finance_orders(
    q: str | None = None,
    status: str | None = None,
    billing_mode: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """订单明细：下单用户 + 套餐快照 + 渠道流水，支持搜索 / 筛选 / 分页。

    数据看板（/dashboard）复用本函数取最近订单，因此顶层保留 user_id /
    amount_cents / status / created_at 这些旧字段，新增信息放在 user / plan 里。
    """
    return list_finance_orders(
        db, q=q, status=status, billing_mode=billing_mode, page=page, page_size=page_size
    )


@router.get("/finance/orders/{order_no}")
def finance_order_detail(
    order_no: str,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(require_admin),
):
    """单笔订单详情：在列表字段之外补充该用户的累计消费与当前会员状态。"""
    stmt = select(Order, User, PlanVersion, Plan)
    for target, onclause in ORDER_LEDGER_JOINS:
        stmt = stmt.outerjoin(target, onclause)
    row = db.execute(stmt.where(Order.order_no == order_no)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="order not found")

    payload = _order_payload(*row)
    account = row[1]
    if account is None:
        payload["user_stats"] = None
        payload["membership"] = None
        return payload

    paid_orders, paid_cents = db.execute(
        select(func.count(Order.id), func.coalesce(func.sum(Order.amount_cents), 0)).where(
            Order.user_id == account.id, Order.status == "paid"
        )
    ).one()
    refunded_cents = (
        db.scalar(
            select(func.coalesce(func.sum(Order.amount_cents), 0)).where(
                Order.user_id == account.id, Order.status == "refunded"
            )
        )
        or 0
    )
    payload["user_stats"] = {
        "paid_orders": int(paid_orders),
        "paid_cents": int(paid_cents),
        "refunded_cents": int(refunded_cents),
    }
    payload["membership"] = active_membership(db, account.id)
    return payload
