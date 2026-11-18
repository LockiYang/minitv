"""Pydantic request and response models."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.modules.social import moderation
COMMENT_MAX_LENGTH = 500


class CommentIn(BaseModel):
    drama_id: int
    episode_id: int | None = None
    body: str = Field(min_length=1, max_length=1000)


class ReactionIn(BaseModel):
    target_type: str
    target_id: int
    kind: str
    remove: bool = False


class EpisodeCommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=COMMENT_MAX_LENGTH)


class CommentModIn(BaseModel):
    status: str


class ModerationSettingsIn(BaseModel):
    """Admin-editable policy. ``api_key`` is write-only: it is encrypted into the
    row, never echoed back, and left untouched when omitted."""

    enabled: bool
    base_url: str = Field(default="https://api.openai.com/v1", max_length=200)
    model: str = Field(min_length=1, max_length=64)
    categories: list[str] = Field(default_factory=list)
    system_prompt: str = Field(default="", max_length=8000)
    approve_threshold: float = Field(ge=0, le=1)
    block_threshold: float = Field(ge=0, le=1)
    fallback_action: str = "pending"
    timeout_seconds: int = Field(ge=1, le=30)
    api_key: str | None = Field(default=None, max_length=200)
    clear_api_key: bool = False

    @field_validator("api_key")
    @classmethod
    def _blank_key_is_none(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None

    @field_validator("categories")
    @classmethod
    def _clean_categories(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = str(item).strip().lower()[:32]
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned[:20]

    @field_validator("fallback_action")
    @classmethod
    def _known_fallback(cls, value: str) -> str:
        if value not in moderation.FALLBACK_ACTIONS:
            raise ValueError("fallback_action must be pending or approved")
        return value

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        text = value.strip().rstrip("/")
        if not text.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return text


class ModerationTestIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    # Optional draft credential: the admin page verifies a key before saving it.
    api_key: str | None = Field(default=None, max_length=200)

    @field_validator("api_key")
    @classmethod
    def _blank_key_is_none(cls, value: str | None) -> str | None:
        text = (value or "").strip()
        return text or None
