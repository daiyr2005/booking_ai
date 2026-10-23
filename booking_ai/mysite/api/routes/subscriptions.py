from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_current_user
from mysite.database.ai_models import Category, ChannelAccount, City, Country, SearchSubscription, User
from mysite.database.session import get_session
from mysite.schemas.search import SearchFilters
from mysite.schemas.subscription import SubscriptionCreate, SubscriptionOut, SubscriptionUpdate
from mysite.services.errors import ServiceError

router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


def _clean_filters(raw: dict | None) -> dict:
    """filters_json проверяется той же схемой, что и поиск: противоречия и неизвестные поля отклоняются."""
    if not raw:
        return {}
    try:
        return SearchFilters.model_validate(raw).model_dump(mode="json", exclude_none=True)
    except ValueError as e:
        raise ServiceError(422, "Некорректные фильтры подписки", [str(e)])


async def _check_refs(session: AsyncSession, data: dict) -> None:
    for key, model, label in (("category_id", Category, "Категория"), ("country_id", Country, "Страна"),
                              ("city_id", City, "Город")):
        if data.get(key) is not None and await session.get(model, data[key]) is None:
            raise ServiceError(422, f"{label} не найдена")


async def _own_or_404(session: AsyncSession, sub_id: int, user: User) -> SearchSubscription:
    sub = await session.get(SearchSubscription, sub_id)
    if sub is None or sub.user_id != user.id:
        raise ServiceError(404, "Подписка не найдена")  # чужие подписки не раскрываем
    return sub


@router.post("/", response_model=SubscriptionOut, status_code=201)
async def create_subscription(body: SubscriptionCreate, user: User = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    data = body.model_dump()
    if body.channel != "api":
        linked = await session.scalar(select(ChannelAccount.id).where(
            ChannelAccount.user_id == user.id, ChannelAccount.channel == body.channel,
            ChannelAccount.is_verified.is_(True), ChannelAccount.is_active.is_(True)))
        if not linked:
            raise ServiceError(400, f"Сначала привяжите и подтвердите канал {body.channel}")
    await _check_refs(session, data)
    data["filters_json"] = _clean_filters(data["filters_json"])
    sub = SearchSubscription(user_id=user.id, **data)
    session.add(sub)
    await session.commit()
    return sub


@router.get("/", response_model=list[SubscriptionOut])
async def list_subscriptions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                             user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    q = (select(SearchSubscription).where(SearchSubscription.user_id == user.id)
         .order_by(SearchSubscription.id).limit(limit).offset(offset))
    return (await session.execute(q)).scalars().all()


@router.patch("/{subscription_id}", response_model=SubscriptionOut)
async def update_subscription(subscription_id: int, body: SubscriptionUpdate, user: User = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    sub = await _own_or_404(session, subscription_id, user)
    changes = body.model_dump(exclude_unset=True)
    for non_null in ("frequency_minutes", "is_active"):
        if non_null in changes and changes[non_null] is None:
            del changes[non_null]
    await _check_refs(session, changes)
    if "filters_json" in changes:
        changes["filters_json"] = _clean_filters(changes["filters_json"])
    pmin = changes.get("price_min", sub.price_min)
    pmax = changes.get("price_max", sub.price_max)
    cur = changes.get("currency", sub.currency)
    if pmin is not None and pmax is not None and pmin > pmax:
        raise ServiceError(422, "Минимальная цена больше максимальной")
    if (pmin is not None or pmax is not None) and not cur:
        raise ServiceError(422, "Укажите валюту для фильтра по цене")
    for k, v in changes.items():
        setattr(sub, k, v)
    await session.commit()
    return sub


@router.delete("/{subscription_id}", status_code=204)
async def delete_subscription(subscription_id: int, user: User = Depends(get_current_user),
                              session: AsyncSession = Depends(get_session)):
    sub = await _own_or_404(session, subscription_id, user)
    await session.delete(sub)
    await session.commit()
    return Response(status_code=204)
