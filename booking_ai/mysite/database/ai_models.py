"""Модели Booking AI (SQLAlchemy 2.x). Если в проекте уже есть свой Base,
замените AIBase на него и подключите metadata в Alembic env.py."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
                        Numeric, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AIBase(DeclarativeBase):
    pass


class ListingStatus:
    DRAFT = "draft"
    PENDING = "pending"
    ACTIVE = "active"
    INACTIVE = "inactive"
    REJECTED = "rejected"
    EXTERNAL = "external"  # найдено в интернете, не создано на платформе
    ALL = {DRAFT, PENDING, ACTIVE, INACTIVE, REJECTED, EXTERNAL}


class User(AIBase):
    __tablename__ = "ai_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default="user")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    listings: Mapped[list["Listing"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Country(AIBase):
    __tablename__ = "ai_countries"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    code: Mapped[str] = mapped_column(String(3), unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    cities: Mapped[list["City"]] = relationship(back_populates="country", cascade="all, delete-orphan")


class City(AIBase):
    __tablename__ = "ai_cities"
    __table_args__ = (UniqueConstraint("country_id", "name", name="uq_city_country_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    country_id: Mapped[int] = mapped_column(ForeignKey("ai_countries.id", ondelete="CASCADE"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    country: Mapped[Country] = relationship(back_populates="cities")


class Category(AIBase):
    __tablename__ = "ai_categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    slug: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("ai_categories.id", ondelete="SET NULL"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    parent: Mapped["Category | None"] = relationship(remote_side="Category.id", back_populates="children")
    children: Mapped[list["Category"]] = relationship(back_populates="parent")


class Listing(AIBase):
    __tablename__ = "ai_listings"
    __table_args__ = (
        CheckConstraint("price_min IS NULL OR price_max IS NULL OR price_min <= price_max", name="ck_price_range"),
        CheckConstraint("price IS NULL OR price >= 0", name="ck_price_nonneg"),
        CheckConstraint("check_in IS NULL OR check_out IS NULL OR check_out > check_in", name="ck_dates"),
        UniqueConstraint("source_name", "external_id", name="uq_listing_external"),
        Index("ix_listing_search", "category_id", "city_id", "status", "price"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("ai_users.id", ondelete="CASCADE"), index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("ai_categories.id"))
    country_id: Mapped[int | None] = mapped_column(ForeignKey("ai_countries.id"))
    city_id: Mapped[int | None] = mapped_column(ForeignKey("ai_cities.id"))
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    property_type: Mapped[str | None] = mapped_column(String(64))
    address: Mapped[str | None] = mapped_column(String(255))
    price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    price_period: Mapped[str | None] = mapped_column(String(16))  # night/day/month/total
    rooms: Mapped[int | None] = mapped_column(Integer)
    guests: Mapped[int | None] = mapped_column(Integer)
    area: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    check_in: Mapped[date | None] = mapped_column(Date)
    check_out: Mapped[date | None] = mapped_column(Date)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    source_name: Mapped[str | None] = mapped_column(String(128))
    external_id: Mapped[str | None] = mapped_column(String(255))
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # дата публикации в источнике
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # когда проверяли мы
    status: Mapped[str] = mapped_column(String(16), default=ListingStatus.DRAFT, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    owner: Mapped[User | None] = relationship(back_populates="listings")


class SearchSubscription(AIBase):
    __tablename__ = "ai_search_subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("ai_users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    category_id: Mapped[int | None] = mapped_column(ForeignKey("ai_categories.id"))
    country_id: Mapped[int | None] = mapped_column(ForeignKey("ai_countries.id"))
    city_id: Mapped[int | None] = mapped_column(ForeignKey("ai_cities.id"))
    price_min: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    price_max: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    filters_json: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    frequency_minutes: Mapped[int] = mapped_column(Integer, default=60)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Notification(AIBase):
    __tablename__ = "ai_notifications"
    # дедупликация: одно объявление — одно уведомление на подписку и канал
    __table_args__ = (UniqueConstraint("subscription_id", "listing_id", "channel", name="uq_notification_dedup"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("ai_users.id", ondelete="CASCADE"), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("ai_listings.id", ondelete="CASCADE"))
    subscription_id: Mapped[int] = mapped_column(ForeignKey("ai_search_subscriptions.id", ondelete="CASCADE"))
    channel: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/sent/failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)


class BlockedWord(AIBase):
    __tablename__ = "ai_blocked_words"
    id: Mapped[int] = mapped_column(primary_key=True)
    word: Mapped[str] = mapped_column(String(255))
    normalized_word: Mapped[str] = mapped_column(String(255), unique=True)
    match_type: Mapped[str] = mapped_column(String(16), default="word")  # exact/word/phrase
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChannelAccount(AIBase):
    __tablename__ = "ai_channel_accounts"
    __table_args__ = (UniqueConstraint("channel", "external_user_id", name="uq_channel_external"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("ai_users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    external_user_id: Mapped[str] = mapped_column(String(128))
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ProcessedMessage(AIBase):
    """Защита от повторной доставки webhook/сообщений."""
    __tablename__ = "ai_processed_messages"
    __table_args__ = (UniqueConstraint("channel", "external_message_id", name="uq_processed_msg"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(16))
    external_message_id: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class ChannelLinkCode(AIBase):
    """Одноразовый короткий код привязки мессенджера: пользователь получает его в API и отправляет боту."""
    __tablename__ = "ai_channel_link_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("ai_users.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
