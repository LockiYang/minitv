"""Shared formatting for API responses."""


def iso_or_none(value) -> str | None:
    return value.isoformat() if value else None
