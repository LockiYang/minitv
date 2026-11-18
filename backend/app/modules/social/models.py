"""Likes, favorites, comments and inbox messages."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Reaction(Base):
    __tablename__ = "reactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)  # drama / episode
    target_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # like / favorite / follow
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("user_id", "target_type", "target_id", "kind", name="uq_reaction_once"),
    )


class Comment(Base):
    __tablename__ = "comments"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    drama_id: Mapped[int] = mapped_column(ForeignKey("dramas.id"), nullable=False)
    episode_id: Mapped[int] = mapped_column(ForeignKey("episodes.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # Auto-moderation audit trail. `status` is the visibility switch; these
    # columns record what the model said, so a human can review or appeal.
    moderation_verdict: Mapped[str | None] = mapped_column(String(16), nullable=True)  # allow / review / block
    moderation_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    moderation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    moderation_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    moderated_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Async job state, separate from visibility: queued -> running -> done/failed/canceled.
    moderation_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    moderation_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    moderation_claimed_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    moderation_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class InboxMessage(Base):
    __tablename__ = "inbox_messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # activity / system
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    read_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PushRecord(Base):
    __tablename__ = "push_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    drama_id: Mapped[int | None] = mapped_column(ForeignKey("dramas.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    delivered_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sent_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ModerationSettings(Base):
    """Single-row config for OpenAI-backed comment pre-moderation.

    Everything operations tune (model, prompt, thresholds, fallback) lives here.
    ``api_key_secret`` is the Fernet-encrypted credential edited from the admin
    page; when it is empty the key falls back to ``OPENAI_API_KEY``.
    """

    __tablename__ = "moderation_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    base_url: Mapped[str] = mapped_column(String(200), nullable=False, default="https://api.openai.com/v1")
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="gpt-4o-mini")
    categories: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False, default="")
    approve_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)
    block_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)
    fallback_action: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=6)
    api_key_secret: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    updated_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
