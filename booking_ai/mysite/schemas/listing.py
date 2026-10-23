"""Схемы объявлений. Владелец и статус задаются сервером, клиент их не передаёт."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mysite.schemas.search import normalize_currency, normalize_period


class _Validators(BaseModel):
    @field_validator("currency", mode="before", check_fields=False)
    @classmethod
    def _currency(cls, v):
        return None if v in (None, "") else normalize_currency(str(v))

    @field_validator("price_period", mode="before", check_fields=False)
    @classmethod
    def _period(cls, v):
        return None if v in (None, "") else normalize_period(str(v))


class ListingCreate(_Validators):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    category_id: int
    country_id: int | None = None
    city_id: int
    title: str = Field(min_length=3, max_length=255)
    description: str | None = Field(None, max_length=10000)
    property_type: str | None = Field(None, max_length=64)
    address: str | None = Field(None, max_length=255)
    price: Decimal | None = Field(None, ge=0, max_digits=14, decimal_places=2)
    price_min: Decimal | None = Field(None, ge=0, max_digits=14, decimal_places=2)
    price_max: Decimal | None = Field(None, ge=0, max_digits=14, decimal_places=2)
    currency: str | None = None
    price_period: str | None = None
    rooms: int | None = Field(None, ge=0, le=1000)
    guests: int | None = Field(None, ge=0, le=1000)
    area: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)
    check_in: date | None = None
    check_out: date | None = None
    source_url: str | None = Field(None, max_length=2048)


class ListingUpdate(_Validators):
    """PATCH: меняются только переданные поля."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    category_id: int | None = None
    country_id: int | None = None
    city_id: int | None = None
    title: str | None = Field(None, min_length=3, max_length=255)
    description: str | None = Field(None, max_length=10000)
    property_type: str | None = Field(None, max_length=64)
    address: str | None = Field(None, max_length=255)
    price: Decimal | None = Field(None, ge=0, max_digits=14, decimal_places=2)
    price_min: Decimal | None = Field(None, ge=0, max_digits=14, decimal_places=2)
    price_max: Decimal | None = Field(None, ge=0, max_digits=14, decimal_places=2)
    currency: str | None = None
    price_period: str | None = None
    rooms: int | None = Field(None, ge=0, le=1000)
    guests: int | None = Field(None, ge=0, le=1000)
    area: Decimal | None = Field(None, ge=0, max_digits=10, decimal_places=2)
    check_in: date | None = None
    check_out: date | None = None
    source_url: str | None = Field(None, max_length=2048)


class ListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    owner_id: int | None
    category_id: int
    country_id: int | None
    city_id: int | None
    title: str
    description: str | None
    property_type: str | None
    address: str | None
    price: Decimal | None
    price_min: Decimal | None
    price_max: Decimal | None
    currency: str | None
    price_period: str | None
    rooms: int | None
    guests: int | None
    area: Decimal | None
    check_in: date | None
    check_out: date | None
    source_url: str | None
    source_name: str | None
    external_id: str | None
    published_at: datetime | None
    last_seen_at: datetime | None
    status: str
    created_at: datetime
    updated_at: datetime
