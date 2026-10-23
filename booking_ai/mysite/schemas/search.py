"""Фильтры поиска и результат разбора намерения (Pydantic v2).
Схемы ловят противоречия: цена от > до, выезд не позже заезда, ночи не совпадают с датами."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CURRENCIES = {"KGS", "KZT", "RUB", "USD", "EUR", "UZS", "TRY", "AED", "CNY", "GBP"}
PERIODS = {"night", "day", "week", "month", "year", "total"}

_CURRENCY_ALIASES = {
    "сом": "KGS", "сома": "KGS", "сомов": "KGS", "som": "KGS",
    "тенге": "KZT", "₸": "KZT",
    "руб": "RUB", "рубль": "RUB", "рубля": "RUB", "рублей": "RUB", "₽": "RUB",
    "$": "USD", "доллар": "USD", "доллара": "USD", "долларов": "USD",
    "евро": "EUR", "€": "EUR",
    "сум": "UZS", "лира": "TRY", "лир": "TRY", "дирхам": "AED", "дирхамов": "AED",
    "юань": "CNY", "юаней": "CNY", "фунт": "GBP", "£": "GBP",
}

Intent = Literal["search", "create", "update", "delete", "subscribe", "unsubscribe", "list_mine", "unknown"]


def normalize_currency(value: str) -> str:
    """'сом' -> 'KGS', 'usd' -> 'USD'. Неизвестная валюта — ValueError (не угадываем)."""
    v = value.strip().casefold()
    code = _CURRENCY_ALIASES.get(v, v.upper())
    if code not in CURRENCIES:
        raise ValueError(f"Неподдерживаемая валюта: {value}")
    return code


def normalize_period(value: str) -> str:
    v = value.strip().lower()
    if v not in PERIODS:
        raise ValueError(f"Неподдерживаемый период оплаты: {value}")
    return v


class SearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    category: str | None = None           # slug категории
    country: str | None = None
    city: str | None = None
    district: str | None = None
    property_type: str | None = None
    price_min: Decimal | None = Field(None, ge=0)
    price_max: Decimal | None = Field(None, ge=0)
    currency: str | None = None
    price_period: str | None = None
    rooms: int | None = Field(None, ge=0, le=1000)
    guests: int | None = Field(None, ge=0, le=1000)
    check_in: date | None = None
    check_out: date | None = None
    nights: int | None = Field(None, ge=1, le=365)
    area_min: Decimal | None = Field(None, ge=0)
    area_max: Decimal | None = Field(None, ge=0)
    rental_type: Literal["short_term", "long_term"] | None = None
    extra: list[str] | None = None        # дополнительные требования своими словами

    @field_validator("currency", mode="before")
    @classmethod
    def _currency(cls, v):
        return None if v in (None, "") else normalize_currency(str(v))

    @field_validator("price_period", mode="before")
    @classmethod
    def _period(cls, v):
        return None if v in (None, "") else normalize_period(str(v))

    @model_validator(mode="after")
    def _consistency(self):
        if self.price_min is not None and self.price_max is not None and self.price_min > self.price_max:
            raise ValueError("Минимальная цена больше максимальной")
        if self.area_min is not None and self.area_max is not None and self.area_min > self.area_max:
            raise ValueError("Минимальная площадь больше максимальной")
        if self.check_in and self.check_out:
            if self.check_out <= self.check_in:
                raise ValueError("Дата выезда должна быть позже даты заезда")
            if self.nights is not None and (self.check_out - self.check_in).days != self.nights:
                raise ValueError("Количество ночей не совпадает с датами заезда и выезда")
        return self


class ParsedIntent(BaseModel):
    """Результат классификатора (LLM). Лишние поля от модели игнорируются, значения проверяются."""
    model_config = ConfigDict(extra="ignore")

    intent: Intent = "unknown"
    filters: SearchFilters = Field(default_factory=SearchFilters)
    draft: dict[str, Any] = Field(default_factory=dict)      # поля нового объявления
    changes: dict[str, Any] = Field(default_factory=dict)    # изменяемые поля существующего
    listing_id: int | None = None
    missing_fields: list[str] = Field(default_factory=list)
    needs_clarification: bool = False
