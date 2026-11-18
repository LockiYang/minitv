"""Shared Redis connection for sessions and other transient state."""

from __future__ import annotations

import os

import redis

from app.db import load_env

_redis_client = None


def get_redis():
    global _redis_client
    if _redis_client is None:
        load_env()
        url = os.environ.get("REDIS_URL", "redis://:minitv_local@127.0.0.1:16379/0")
        _redis_client = redis.Redis.from_url(url, decode_responses=True)
    return _redis_client


def reset_redis_for_tests() -> None:
    global _redis_client
    _redis_client = None
