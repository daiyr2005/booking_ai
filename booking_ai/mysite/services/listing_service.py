"""Бизнес-логика объявлений. Права и проверки выполняются здесь, а не в LLM и не в роутах."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.database.ai_models import Category, City, Country, Listing, ListingStatus, User
from mysite.services.errors import ServiceError
from mysite.services.moderation_service import ModerationService
from mysite.services.validation_service import fingerprint, validate_listing_data

PUBLIC_STATUSES = (ListingStatus.ACTIVE, ListingStatus.EXTERNAL)
_PRICE_FIELDS = ("price", "price_min", "price_max")
_CENT = Decimal("0.01")


def listing_to_dict(listing: Listing) -> dict:
    return {c.name: getattr(listing, c.name) for c in Listing.__table__.columns}


def _validation_payload(data: dict) -> dict:
    """Приводит поля объявления к виду, который ждёт validate_listing_data / fingerprint."""
    out = dict(data)
    out["category"] = data.get("category_id")
    out["city"] = data.get("city_id")
    for f in _PRICE_FIELDS:  # одинаковая запись цены у входных данных и у значений из БД
        if out.get(f) is not None:
            out[f] = Decimal(str(out[f])).quantize(_CENT)
    return out


def _full_check(data: dict, moderation: ModerationService | None, *, today: date | None) -> list[str]:
    payload = _validation_payload(data)
    errors = validate_listing_data(payload, partial=False, today=today, moderation=moderation)
    if not (data.get("title") or "").strip():
        errors.append("Не заполнено обязательное поле: title")
    if any(data.get(f) is not None for f in _PRICE_FIELDS) and not data.get("currency"):
        errors.append("Укажите валюту")
    return errors


async def _check_refs(session: AsyncSession, data: dict, *, only: set[str] | None = None) -> list[str]:
    """Проверяет существование и активность категории/города/страны. data['country_id'] выравнивается по городу."""
    errors: list[str] = []
    want = lambda k: only is None or k in only  # noqa: E731
    if want("category_id") and data.get("category_id") is not None:
        cat = await session.get(Category, data["category_id"])
        if cat is None or not cat.is_active:
            errors.append("Категория не найдена или отключена")
    city = None
    if want("city_id") and data.get("city_id") is not None:
        city = await session.get(City, data["city_id"])
        if city is None or not city.is_active:
            errors.append("Город не найден или отключён")
            city = None
    if city is not None:
        if data.get("country_id") not in (None, city.country_id):
            errors.append("Город не принадлежит выбранной стране")
        else:
            data["country_id"] = city.country_id
    elif want("country_id") and data.get("country_id") is not None:
        country = await session.get(Country, data["country_id"])
        if country is None or not country.is_active:
            errors.append("Страна не найдена или отключена")
    return errors


async def _ensure_not_duplicate(session: AsyncSession, owner_id: int, fp: str, *, exclude_id: int | None = None) -> None:
    q = select(Listing.id).where(Listing.owner_id == owner_id, Listing.fingerprint == fp)
    if exclude_id is not None:
        q = q.where(Listing.id != exclude_id)
    if (await session.execute(q.limit(1))).first():
        raise ServiceError(409, "Такое объявление у вас уже есть", ["Дубликат существующего объявления"])


async def create_listing(session: AsyncSession, owner: User, data: dict, moderation: ModerationService | None) -> Listing:
    data = dict(data)
    errors = await _check_refs(session, data)
    errors += _full_check(data, moderation, today=date.today())
    if errors:
        raise ServiceError(422, "Объявление не прошло проверку", errors)
    fp = fingerprint(_validation_payload(data))
    await _ensure_not_duplicate(session, owner.id, fp)
    listing = Listing(owner_id=owner.id, status=ListingStatus.DRAFT, fingerprint=fp, **data)
    session.add(listing)
    await session.commit()
    return listing


async def get_listing_or_404(session: AsyncSession, listing_id: int) -> Listing:
    listing = await session.get(Listing, listing_id)
    if listing is None:
        raise ServiceError(404, "Объявление не найдено")
    return listing


def is_owner_or_admin(listing: Listing, user: User | None) -> bool:
    return user is not None and (user.role == "admin" or listing.owner_id == user.id)


def ensure_visible(listing: Listing, user: User | None) -> None:
    if listing.status in PUBLIC_STATUSES or is_owner_or_admin(listing, user):
        return
    raise ServiceError(404, "Объявление не найдено")  # существование чужих черновиков не раскрываем


def ensure_can_modify(listing: Listing, user: User) -> None:
    if not is_owner_or_admin(listing, user):
        raise ServiceError(403, "Нет прав на это объявление")


async def update_listing(session: AsyncSession, listing: Listing, changes: dict,
                         moderation: ModerationService | None) -> Listing:
    if not changes:
        return listing
    merged = {**listing_to_dict(listing), **changes}
    if "city_id" in changes and "country_id" not in changes:
        merged["country_id"] = None  # страна определится по новому городу
    errors = await _check_refs(session, merged, only={k for k in ("category_id", "city_id", "country_id") if k in changes})
    errors += _full_check(merged, moderation, today=None)  # прошедшие даты не проверяем при правке других полей
    if "check_in" in changes and changes["check_in"] is not None and changes["check_in"] < date.today():
        errors.append("Дата заезда уже прошла")
    if errors:
        raise ServiceError(422, "Изменения не прошли проверку", errors)
    fp = fingerprint(_validation_payload(merged))
    if listing.owner_id is not None:
        await _ensure_not_duplicate(session, listing.owner_id, fp, exclude_id=listing.id)
    for k, v in changes.items():
        setattr(listing, k, v)
    if "country_id" in merged:
        listing.country_id = merged["country_id"]
    listing.fingerprint = fp
    await session.commit()
    return listing


async def publish_listing(session: AsyncSession, listing: Listing, moderation: ModerationService | None) -> Listing:
    if listing.status == ListingStatus.EXTERNAL:
        raise ServiceError(409, "Внешние объявления публиковать нельзя")
    if listing.status == ListingStatus.ACTIVE:
        return listing
    errors = _full_check(listing_to_dict(listing), moderation, today=date.today())
    if errors:
        raise ServiceError(422, "Объявление не прошло проверку", errors)
    listing.status = ListingStatus.ACTIVE
    listing.published_at = listing.published_at or datetime.now(timezone.utc)
    await session.commit()
    return listing


async def deactivate_listing(session: AsyncSession, listing: Listing) -> Listing:
    if listing.status == ListingStatus.EXTERNAL:
        raise ServiceError(409, "Внешние объявления отключать нельзя")
    listing.status = ListingStatus.INACTIVE
    await session.commit()
    return listing


async def delete_listing(session: AsyncSession, listing: Listing) -> None:
    await session.delete(listing)
    await session.commit()


async def category_subtree_ids(session: AsyncSession, root_id: int) -> list[int]:
    ids = [root_id]
    frontier = [root_id]
    while frontier:
        rows = (await session.execute(select(Category.id).where(Category.parent_id.in_(frontier)))).scalars().all()
        frontier = [r for r in rows if r not in ids]
        ids.extend(frontier)
    return ids


async def query_listings(
    session: AsyncSession, *, owner_id: int | None = None, statuses: Sequence[str] | None = PUBLIC_STATUSES,
    category_ids: Sequence[int] | None = None, country_ids: Sequence[int] | None = None,
    city_ids: Sequence[int] | None = None, price_min: Decimal | None = None, price_max: Decimal | None = None,
    currency: str | None = None, price_period: str | None = None, rooms: int | None = None,
    guests: int | None = None, area_min: Decimal | None = None, area_max: Decimal | None = None,
    property_type: str | None = None, limit: int = 20, offset: int = 0,
) -> list[Listing]:
    q = select(Listing)
    if owner_id is not None:
        q = q.where(Listing.owner_id == owner_id)
    if statuses:
        q = q.where(Listing.status.in_(list(statuses)))
    if category_ids is not None:
        q = q.where(Listing.category_id.in_(list(category_ids)))
    if country_ids is not None:
        q = q.where(Listing.country_id.in_(list(country_ids)))
    if city_ids is not None:
        q = q.where(Listing.city_id.in_(list(city_ids)))
    if price_min is not None:
        q = q.where(Listing.price >= price_min)
    if price_max is not None:
        q = q.where(Listing.price <= price_max)
    if currency:
        q = q.where(Listing.currency == currency)
    if price_period:
        q = q.where(Listing.price_period == price_period)
    if rooms is not None:
        q = q.where(Listing.rooms == rooms)
    if guests is not None:
        q = q.where(Listing.guests >= guests)
    if area_min is not None:
        q = q.where(Listing.area >= area_min)
    if area_max is not None:
        q = q.where(Listing.area <= area_max)
    if property_type:
        q = q.where(func.lower(Listing.property_type) == func.lower(property_type))
    q = q.order_by(Listing.created_at.desc(), Listing.id.desc()).limit(limit).offset(offset)
    return list((await session.execute(q)).scalars().all())


async def resolve_filters(session: AsyncSession, filters: dict) -> tuple[dict, list[str]]:
    """Названия из фильтров поиска -> id справочников. Если что-то не нашлось, результат пуст и есть пояснение."""
    kw: dict = {}
    warnings: list[str] = []
    if filters.get("category"):
        cat_id = await session.scalar(select(Category.id).where(Category.slug == filters["category"]))
        if cat_id is None:
            warnings.append(f"Категория не найдена: {filters['category']}")
            kw["category_ids"] = []
        else:
            kw["category_ids"] = await category_subtree_ids(session, cat_id)
    if filters.get("country"):
        ids = (await session.execute(select(Country.id).where(
            func.lower(Country.name) == func.lower(filters["country"])))).scalars().all()
        if not ids:
            warnings.append(f"Страна не найдена: {filters['country']}")
        kw["country_ids"] = list(ids)
    if filters.get("city"):
        q = select(City.id).where(func.lower(City.name) == func.lower(filters["city"]))
        if kw.get("country_ids"):
            q = q.where(City.country_id.in_(kw["country_ids"]))
        ids = (await session.execute(q)).scalars().all()
        if not ids:
            warnings.append(f"Город не найден: {filters['city']}")
        kw["city_ids"] = list(ids)
    return kw, warnings


async def search_listings(session: AsyncSession, filters, *, limit: int = 20, offset: int = 0) -> tuple[list[Listing], list[str]]:
    """Поиск по локальной БД по SearchFilters. Единая точка для REST-поиска и для графа."""
    data = filters.model_dump(mode="json", exclude_none=True)
    kw, warnings = await resolve_filters(session, data)
    # если справочное значение не найдено — честно возвращаем пустой результат, а не «все объявления»
    if any(kw.get(k) == [] for k in ("category_ids", "country_ids", "city_ids")):
        return [], warnings
    items = await query_listings(
        session, price_min=filters.price_min, price_max=filters.price_max, currency=filters.currency,
        price_period=filters.price_period, rooms=filters.rooms, guests=filters.guests,
        area_min=filters.area_min, area_max=filters.area_max, property_type=filters.property_type,
        limit=limit, offset=offset, **kw)
    return items, warnings
