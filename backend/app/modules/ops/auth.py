"""Platform staff authentication and role dependencies."""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.ops import api_keys as api_key_service, service as ops_service
from app.modules.ops.models import AdminUser, CONTENT_WRITER_ROLES, ROLE_ADMIN, STAFF_ROLES


# ---- auth dependencies ----
def _bearer_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={"code": "missing_credential", "message": "缺少 Authorization 头"},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return auth[len("Bearer "):]


SESSION_STORE_503_DETAIL = (
    "会话存储不可用（Redis 未连接）。请先启动 minitv-redis："
    "在仓库根目录执行 docker compose up -d，然后重试。"
)


def _session_store_503() -> HTTPException:
    return HTTPException(status_code=503, detail=SESSION_STORE_503_DETAIL)


def require_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser:
    """Accept either a staff session token or an API key.

    API keys are bound to an ``admin_users`` row and resolved into that account,
    so every downstream dependency (role checks, audit ``actor_id``) keeps
    working unchanged. The session path is deliberately untouched.
    """
    credential = _bearer_token(request)
    if api_key_service.looks_like_key(credential):
        user, key = api_key_service.resolve_credential(db, credential)
        # Stamp the key before the scope check: a denied write must still be
        # recorded in api_key_events (the middleware reads these after response).
        request.state.api_key_id = key.id
        request.state.api_key_staff_id = user.id
        api_key_service.authorize(key, request.method, request.url.path, user.role)
    else:
        try:
            user = ops_service.resolve_session_token(db, credential)
        except ops_service.SessionStoreUnavailable as exc:
            raise _session_store_503() from exc
        if user is None:
            raise HTTPException(
                status_code=401,
                detail={"code": "invalid_session", "message": "会话无效或已过期"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    # Suppliers live in the same table but are external tenants: they must never
    # reach this surface, even if a future dependency forgets a role check.
    if user.role not in STAFF_ROLES:
        raise HTTPException(
            status_code=403,
            detail={"code": "staff_only", "message": "该接口仅对平台员工开放"},
        )
    return user


def require_content_writer(user: AdminUser = Depends(require_admin)) -> AdminUser:
    if user.role not in CONTENT_WRITER_ROLES:
        raise HTTPException(status_code=403, detail=f"role '{user.role}' cannot write content")
    return user


def require_admin_role(user: AdminUser = Depends(require_admin)) -> AdminUser:
    """Super-admin only: delete drama, price/billing changes, coin adjust, staff."""
    if user.role != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="admin only")
    return user


def _revoke_or_503(user_id: int) -> int:
    try:
        return ops_service.revoke_user_sessions(user_id)
    except ops_service.SessionStoreUnavailable as exc:
        raise _session_store_503() from exc
