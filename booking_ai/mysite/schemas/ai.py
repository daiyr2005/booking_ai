"""Схемы чата с ИИ-ассистентом."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=2000)
    # идентификатор диалога; по нему хранятся уточнения и ожидание подтверждения. Если не задан — создаётся новый
    conversation_id: str | None = Field(None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")


class ChatOut(BaseModel):
    conversation_id: str
    response: str
    intent: str = "unknown"
    requires_confirmation: bool = False
    missing_fields: list[str] = []
    warnings: list[str] = []
    results: list[dict[str, Any]] = []
    operation: dict[str, Any] | None = None
