"""Типизированное состояние графа LangGraph (поля из раздела 5 ТЗ + служебные)."""
from __future__ import annotations

from typing import Any, TypedDict


class BookingState(TypedDict, total=False):
    user_id: int | None
    channel: str
    conversation_id: str
    intent: str
    user_query: str
    search_filters: dict[str, Any]
    draft: dict[str, Any]
    changes: dict[str, Any]
    missing_fields: list[str]
    search_results: list[dict[str, Any]]
    selected_listing_id: int | None
    validation_errors: list[str]
    warnings: list[str]
    operation_result: dict[str, Any] | None
    notification_targets: list[dict[str, Any]]
    requires_confirmation: bool
    confirmed: bool
    response: str
