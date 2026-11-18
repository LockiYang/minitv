"""Supplier authentication and supplier status dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.ops import api_keys as api_key_service, service as ops_service
from app.modules.ops.models import AdminApiKey, AdminUser, ROLE_PARTNER, STATUS_ACTIVE, Supplier


# ---- auth ----
def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"code": "missing_credential", "message": "缺少 Authorization 头"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth[len("Bearer "):]


def _resolve(request: Request, db: Session) -> tuple[AdminUser, AdminApiKey | None]:
    credential = _bearer_token(request)
    if api_key_service.looks_like_key(credential):
        user, key = api_key_service.resolve_credential(db, credential)
        # Defence in depth: even if a future dependency forgets a role check,
        # a partner key can never leave its own surface.
        if user.role == ROLE_PARTNER and not request.url.path.startswith("/partner/v1/"):
            raise HTTPException(
                status_code=403,
                detail={"code": "partner_scope_denied", "message": "上剧方密钥只能访问合作方接口"},
            )
        request.state.api_key_id = key.id
        request.state.api_key_staff_id = user.id
        return user, key
    try:
        user = ops_service.resolve_session_token(db, credential)
    except ops_service.SessionStoreUnavailable as exc:
        raise HTTPException(status_code=503, detail="会话存储不可用") from exc
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"code": "invalid_session", "message": "会话无效或已过期"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user, None


def _require_supplier(db: Session, user: AdminUser) -> Supplier:
    if user.role != ROLE_PARTNER or user.supplier_id is None:
        raise HTTPException(
            status_code=403,
            detail={"code": "partner_only", "message": "该接口仅对上剧方开放"},
        )
    supplier = db.get(Supplier, user.supplier_id)
    if supplier is None or supplier.status != STATUS_ACTIVE:
        raise HTTPException(
            status_code=403,
            detail={"code": "supplier_disabled", "message": "供应商已停用"},
        )
    return supplier


def require_partner(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    user, key = _resolve(request, db)
    _require_supplier(db, user)
    if key is not None:
        api_key_service.authorize(key, request.method, request.url.path, user.role)
    return user


def require_partner_session(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    """Credential management must never be reachable with a key."""
    if api_key_service.looks_like_key(_bearer_token(request)):
        raise HTTPException(
            status_code=403,
            detail={"code": "session_required", "message": "该接口只能用登录会话调用"},
        )
    user, _ = _resolve(request, db)
    _require_supplier(db, user)
    return user
