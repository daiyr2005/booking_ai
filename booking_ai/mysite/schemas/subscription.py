from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mysite.schemas.search import normalize_currency

Channel = Literal["api", "telegram", "whatsapp"]


class _Price(BaseModel):
    @field_validator("currency", mode="before", check_fields=False)
    @classmethod
    def _currency(cls, v):
        return None if v in (None, "") else normalize_currency(str(v))


class SubscriptionCreate(_Price):
    model_config = ConfigDict(extra="forbid")
    channel: Channel = "api"
    category_id: int | None = None
    country_id: int | None = None
    city_id: int | None = None
    price_min: Decimal | None = Field(None, ge=0)
    price_max: Decimal | None = Field(None, ge=0)
    currency: str | None = None
    filters_json: dict[str, Any] | None = None   # проверяется через SearchFilters
    frequency_minutes: int = Field(60, ge=15, le=10080)

    @model_validator(mode="after")
    def _check(self):
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            raise ValueError("Минимальная цена больше максимальной")
        if (self.price_min is not None or self.price_max is not None) and not self.currency:
            raise ValueError("Укажите валюту для фильтра по цене")
        return self


class SubscriptionUpdate(_Price):
    model_config = ConfigDict(extra="forbid")
    category_id: int | None = None
    country_id: int | None = None
    city_id: int | None = None
    price_min: Decimal | None = Field(None, ge=0)
    price_max: Decimal | None = Field(None, ge=0)
    currency: str | None = None
    filters_json: dict[str, Any] | None = None
    frequency_minutes: int | None = Field(None, ge=15, le=10080)
    is_active: bool | None = None


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    channel: str
    category_id: int | None
    country_id: int | None
    city_id: int | None
    price_min: Decimal | None
    price_max: Decimal | None
    currency: str | None
    filters_json: dict[str, Any]
    is_active: bool
    frequency_minutes: int
    last_checked_at: datetime | None
    created_at: datetime


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    listing_id: int
    subscription_id: int
    channel: str
    status: str
    attempts: int
    sent_at: datetime | None
    error_message: str | None


class BlockedWordCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    word: str = Field(min_length=1, max_length=255)
    match_type: Literal["exact", "word", "phrase"] = "word"
    reason: str | None = Field(None, max_length=255)


class BlockedWordUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    word: str | None = Field(None, min_length=1, max_length=255)
    match_type: Literal["exact", "word", "phrase"] | None = None
    reason: str | None = Field(None, max_length=255)
    is_active: bool | None = None


class BlockedWordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    word: str
    normalized_word: str
    match_type: str
    is_active: bool
    reason: str | None
    created_at: datetime
