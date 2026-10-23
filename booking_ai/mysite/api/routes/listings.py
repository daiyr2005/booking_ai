from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_current_user, get_current_user_optional, get_moderation
from mysite.database.ai_models import ListingStatus, User
from mysite.database.session import get_session
from mysite.schemas.listing import ListingCreate, ListingOut, ListingUpdate
from mysite.schemas.search import normalize_currency
from mysite.services import listing_service as svc
from mysite.services.errors import ServiceError
from mysite.services.moderation_service import ModerationService

router = APIRouter(prefix="/listings", tags=["listings"])


@router.post("/", response_model=ListingOut, status_code=201)
async def create_listing(body: ListingCreate, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session),
                         moderation: ModerationService = Depends(get_moderation)):
    return await svc.create_listing(session, user, body.model_dump(), moderation)


@router.get("/", response_model=list[ListingOut])
async def list_listings(
    category_id: int | None = None, country_id: int | None = None, city_id: int | None = None,
    price_min: Decimal | None = Query(None, ge=0), price_max: Decimal | None = Query(None, ge=0),
    currency: str | None = None, rooms: int | None = Query(None, ge=0),
    mine: bool = False, status: str | None = None,
    limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
    user: User | None = Depends(get_current_user_optional), session: AsyncSession = Depends(get_session),
):
    if price_min is not None and price_max is not None and price_min > price_max:
        raise ServiceError(422, "Минимальная цена больше максимальной")
    if currency:
        try:
            currency = normalize_currency(currency)
        except ValueError as e:
            raise ServiceError(422, str(e))
    owner_id, statuses = None, svc.PUBLIC_STATUSES
    if mine:
        if user is None:
            raise ServiceError(401, "Требуется авторизация")
        if status and status not in ListingStatus.ALL:
            raise ServiceError(422, "Неизвестный статус")
        owner_id, statuses = user.id, ([status] if status else None)
    category_ids = await svc.category_subtree_ids(session, category_id) if category_id is not None else None
    return await svc.query_listings(
        session, owner_id=owner_id, statuses=statuses, category_ids=category_ids,
        country_ids=[country_id] if country_id is not None else None,
        city_ids=[city_id] if city_id is not None else None,
        price_min=price_min, price_max=price_max, currency=currency, rooms=rooms, limit=limit, offset=offset)


@router.get("/{listing_id}", response_model=ListingOut)
async def get_listing(listing_id: int, user: User | None = Depends(get_current_user_optional),
                      session: AsyncSession = Depends(get_session)):
    listing = await svc.get_listing_or_404(session, listing_id)
    svc.ensure_visible(listing, user)
    return listing


@router.patch("/{listing_id}", response_model=ListingOut)
async def update_listing(listing_id: int, body: ListingUpdate, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session),
                         moderation: ModerationService = Depends(get_moderation)):
    listing = await svc.get_listing_or_404(session, listing_id)
    svc.ensure_visible(listing, user)      # чужой черновик — 404, чужое публичное — 403
    svc.ensure_can_modify(listing, user)
    return await svc.update_listing(session, listing, body.model_dump(exclude_unset=True), moderation)


@router.delete("/{listing_id}", status_code=204)
async def delete_listing(listing_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    listing = await svc.get_listing_or_404(session, listing_id)
    svc.ensure_visible(listing, user)
    svc.ensure_can_modify(listing, user)
    await svc.delete_listing(session, listing)
    return Response(status_code=204)


@router.post("/{listing_id}/publish", response_model=ListingOut)
async def publish_listing(listing_id: int, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session),
                          moderation: ModerationService = Depends(get_moderation)):
    listing = await svc.get_listing_or_404(session, listing_id)
    svc.ensure_visible(listing, user)
    svc.ensure_can_modify(listing, user)
    return await svc.publish_listing(session, listing, moderation)


@router.post("/{listing_id}/deactivate", response_model=ListingOut)
async def deactivate_listing(listing_id: int, user: User = Depends(get_current_user),
                             session: AsyncSession = Depends(get_session)):
    listing = await svc.get_listing_or_404(session, listing_id)
    svc.ensure_visible(listing, user)
    svc.ensure_can_modify(listing, user)
    return await svc.deactivate_listing(session, listing)
