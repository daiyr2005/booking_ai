from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_current_user_optional, require_admin
from mysite.database.ai_models import City, Country, Listing, User
from mysite.database.session import get_session
from mysite.schemas.catalog import CityCreate, CityOut, CityUpdate
from mysite.services.errors import ServiceError

router = APIRouter(prefix="/cities", tags=["cities"])


async def _get_or_404(session: AsyncSession, city_id: int) -> City:
    city = await session.get(City, city_id)
    if city is None:
        raise ServiceError(404, "Город не найден")
    return city


async def _ensure_country(session: AsyncSession, country_id: int) -> None:
    if await session.get(Country, country_id) is None:
        raise ServiceError(422, "Страна не найдена")


async def _ensure_unique(session: AsyncSession, name: str, country_id: int, exclude_id: int | None = None) -> None:
    q = select(City.id).where(City.country_id == country_id, func.lower(City.name) == func.lower(name))
    if exclude_id is not None:
        q = q.where(City.id != exclude_id)
    if await session.scalar(q):
        raise ServiceError(409, "Город с таким названием уже есть в этой стране")  # в другой стране имя допустимо


@router.get("/", response_model=list[CityOut])
async def list_cities(country_id: int | None = None, q: str | None = Query(None, max_length=128),
                      include_inactive: bool = False, limit: int = Query(100, ge=1, le=500),
                      offset: int = Query(0, ge=0), user: User | None = Depends(get_current_user_optional),
                      session: AsyncSession = Depends(get_session)):
    if include_inactive and (user is None or user.role != "admin"):
        raise ServiceError(403, "Недостаточно прав")
    stmt = select(City).order_by(City.name, City.id)
    if country_id is not None:
        stmt = stmt.where(City.country_id == country_id)
    if q:
        stmt = stmt.where(func.lower(City.name).contains(func.lower(q), autoescape=True))
    if not include_inactive:
        stmt = stmt.where(City.is_active.is_(True))
    return (await session.execute(stmt.limit(limit).offset(offset))).scalars().all()


@router.get("/{city_id}", response_model=CityOut)
async def get_city(city_id: int, session: AsyncSession = Depends(get_session)):
    return await _get_or_404(session, city_id)


@router.post("/", response_model=CityOut, status_code=201)
async def create_city(body: CityCreate, _: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    await _ensure_country(session, body.country_id)
    await _ensure_unique(session, body.name, body.country_id)
    city = City(**body.model_dump())
    session.add(city)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ServiceError(409, "Город уже существует")
    return city


@router.patch("/{city_id}", response_model=CityOut)
async def update_city(city_id: int, body: CityUpdate, _: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session)):
    city = await _get_or_404(session, city_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    if "country_id" in changes:
        await _ensure_country(session, changes["country_id"])
    await _ensure_unique(session, changes.get("name", city.name), changes.get("country_id", city.country_id),
                         exclude_id=city.id)
    for k, v in changes.items():
        setattr(city, k, v)
    await session.commit()
    return city


@router.delete("/{city_id}", status_code=204)
async def delete_city(city_id: int, _: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    city = await _get_or_404(session, city_id)
    used = await session.scalar(select(func.count()).select_from(Listing).where(Listing.city_id == city.id))
    if used:
        raise ServiceError(409, "Город используется в объявлениях — отключите его (is_active=false) вместо удаления")
    await session.delete(city)
    await session.commit()
    return Response(status_code=204)
