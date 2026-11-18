"""Operational configuration: tabs, banners, copy and marketing."""

from __future__ import annotations

import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ChannelTab(Base):
    __tablename__ = "channel_tabs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    title_en: Mapped[str] = mapped_column(String(64), nullable=False)
    title_zh: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Discover is the only fixed tab. Every other tab is a homepage group shown
    # as its own page, so it points at one home_groups row.
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("home_groups.id"), nullable=True)


class Banner(Base):
    __tablename__ = "banners"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    slot: Mapped[str] = mapped_column(String(32), nullable=False, default="home")
    drama_id: Mapped[int | None] = mapped_column(ForeignKey("dramas.id"), nullable=True)
    starts_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    ends_on: Mapped[datetime.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="online")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The two lines painted over the hero still: a gold hook, then the title.
    kicker: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    headline: Mapped[str] = mapped_column(String(80), nullable=False, default="")


class HotSearch(Base):
    __tablename__ = "hot_searches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    term: Mapped[str] = mapped_column(String(80), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class CopyEntry(Base):
    __tablename__ = "copy_entries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    en: Mapped[str] = mapped_column(Text, nullable=False)
    zh: Mapped[str] = mapped_column(Text, nullable=False, default="")


class MarketingStat(Base):
    """Configured marketing numbers. Never read from live playback counters."""

    __tablename__ = "marketing_stats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Testimonial(Base):
    __tablename__ = "testimonials"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    author: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class StoreLink(Base):
    __tablename__ = "store_links"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(16), unique=True, nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False, default="")


class LegalPage(Base):
    __tablename__ = "legal_pages"

    slug: Mapped[str] = mapped_column(String(32), primary_key=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
