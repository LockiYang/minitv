"""Client accounts, profile and rewards."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.modules.account import service as accounts, tasks as account_tasks
from app.modules.account.auth import require_user as _require_user
from app.modules.account.models import User
from app.modules.account.schemas import (
    CodeIn,
    FacebookIn,
    GoogleIn,
    LoginIn,
    ProfileIn,
    RegisterIn,
)
from app.modules.account.service import AccountError
from app.modules.billing import service as billing
from app.modules.wallet.models import WalletAccount

router = APIRouter(tags=["public-flow"])


@router.post("/auth/email/code")
def email_code(body: CodeIn, db: Session = Depends(get_db)):
    try:
        accounts.issue_code(db, email=body.email, purpose=body.purpose)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"sent": True}


@router.get("/auth/providers")
def auth_providers():
    """What this deployment can actually sign in with.

    The client hides anything that is off instead of rendering a button that
    cannot work. The Google client id is served from the environment so a new
    origin does not need an H5 rebuild.

    Only the browser client is published here: the H5 build renders Google's
    GIS button with it, and the Flutter app reuses it as Android's
    `serverClientId`. iOS mints its own audience from the iOS client baked into
    the app, which the backend accepts through `GOOGLE_CLIENT_IDS`.

    Facebook serves its public App ID the same way: the H5 JavaScript SDK and
    the native SDKs all need it, and it is not a secret.
    """
    client_id = next(
        (
            item.strip()
            for name in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_IDS")
            for item in os.environ.get(name, "").split(",")
            if item.strip()
        ),
        "",
    )
    facebook_app_id = os.environ.get("FACEBOOK_APP_ID", "").strip()
    return {
        "email": {"enabled": True},
        "google": {"enabled": bool(client_id), "client_id": client_id or None},
        "facebook": {"enabled": bool(facebook_app_id), "app_id": facebook_app_id or None},
    }


@router.post("/auth/email/authenticate")
def email_authenticate(body: LoginIn, db: Session = Depends(get_db)):
    """Sign in with a code, creating the account on first use."""
    try:
        user, token, created = accounts.login_or_register_with_code(db, email=body.email, code=body.code)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"token": token, "user_id": user.id, "public_id": user.public_id, "created": created}


@router.post("/auth/email/register")
def email_register(body: RegisterIn, db: Session = Depends(get_db)):
    if body.password is not None:
        raise HTTPException(status_code=422, detail="password is not accepted")
    try:
        user, token = accounts.register(db, email=body.email, code=body.code)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"token": token, "user_id": user.id, "public_id": user.public_id}


@router.post("/auth/email/login")
def email_login(body: LoginIn, db: Session = Depends(get_db)):
    try:
        user, token = accounts.login_with_code(db, email=body.email, code=body.code)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"token": token, "user_id": user.id, "public_id": user.public_id}


@router.post("/auth/google")
def google_login(body: GoogleIn, db: Session = Depends(get_db)):
    try:
        user, token, created = accounts.login_with_google(db, id_token=body.id_token)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"token": token, "user_id": user.id, "public_id": user.public_id, "created": created}


@router.post("/auth/facebook")
def facebook_login(body: FacebookIn, db: Session = Depends(get_db)):
    """Sign in with Facebook; the first visit creates the account."""
    if not body.access_token and not body.id_token:
        raise HTTPException(status_code=422, detail="access_token or id_token required")
    try:
        user, token, created = accounts.login_with_facebook(
            db, access_token=body.access_token, id_token=body.id_token
        )
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return {"token": token, "user_id": user.id, "public_id": user.public_id, "created": created}


@router.post("/auth/oauth/{provider}")
def oauth(provider: str):
    try:
        accounts.oauth_login(None, provider=provider)  # type: ignore[arg-type]
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    raise HTTPException(status_code=501, detail="not configured")


@router.get("/me")
def me(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    wallet = db.get(WalletAccount, user.id)
    membership = billing.active_membership(db, user.id)
    return {
        "id": user.id,
        "public_id": user.public_id,
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "gender": user.gender,
        "bio": user.bio,
        "birthday": user.birthday.isoformat() if user.birthday else None,
        "region": user.region,
        "locale": user.locale,
        "following_count": user.following_count,
        "follower_count": user.follower_count,
        "received_likes": user.received_likes,
        "coins": 0 if wallet is None else wallet.balance,
        "reminder_opt_in": user.reminder_opt_in,
        "membership": membership,
    }


@router.patch("/me")
def update_me(body: ProfileIn, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    data = body.model_dump(exclude_none=True)
    if "birthday" in data:
        from datetime import date
        user.birthday = date.fromisoformat(data.pop("birthday"))
    for key, value in data.items():
        setattr(user, key, value)
    db.commit()
    return {"ok": True}


@router.post("/me/checkin")
def checkin(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    return accounts.check_in(db, user)


@router.post("/me/ad-reward")
def ad_reward(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    try:
        return accounts.claim_ad(db, user)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/tasks")
def public_tasks(db: Session = Depends(get_db)):
    """Task catalogue for signed-out visitors.

    Same cards and amounts as /v1/me/tasks, with progress zeroed. The reward tab
    renders this immediately; the client asks for a login only when the visitor
    taps an action that needs an account.
    """
    return account_tasks.tasks_overview(db, None)


@router.get("/me/tasks")
def my_tasks(user: User = Depends(_require_user), db: Session = Depends(get_db)):
    """Daily task centre: progress, amounts and check-in state in one call."""
    return account_tasks.tasks_overview(db, user)


@router.post("/me/tasks/{code}/claim")
def claim_daily_task(
    code: str, user: User = Depends(_require_user), db: Session = Depends(get_db)
):
    try:
        return account_tasks.claim_task(db, user, code)
    except AccountError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/me/reminder")
def reminder(enabled: bool, user: User = Depends(_require_user), db: Session = Depends(get_db)):
    user.reminder_opt_in = enabled
    db.commit()
    return {"enabled": enabled}
