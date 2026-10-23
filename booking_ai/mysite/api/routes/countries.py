from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_current_user_optional, require_admin
from mysite.database.ai_models import City, Country, Listing, User
from mysite.database.session import get_session
from mysite.schemas.catalog import CountryCreate, CountryOut, CountryUpdate
from mysite.services.errors import ServiceError

router = APIRouter(prefix="/countries", tags=["countries"])


async def _get_or_404(session: AsyncSession, country_id: int) -> Country:
    country = await session.get(Country, country_id)
    if country is None:
        raise ServiceError(404, "Страна не найдена")
    return country


async def _ensure_unique(session: AsyncSession, name: str | None, code: str | None, exclude_id: int | None = None):
    for col, val, msg in ((Country.name, name, "Страна с таким названием уже есть"),
                          (Country.code, code, "Страна с таким кодом уже есть")):
        if val is None:
            continue
        q = select(Country.id).where(col == val)
        if exclude_id is not None:
            q = q.where(Country.id != exclude_id)
        if await session.scalar(q):
            raise ServiceError(409, msg)


@router.get("/", response_model=list[CountryOut])
async def list_countries(include_inactive: bool = False, user: User | None = Depends(get_current_user_optional),
                         session: AsyncSession = Depends(get_session)):
    if include_inactive and (user is None or user.role != "admin"):
        raise ServiceError(403, "Недостаточно прав")
    q = select(Country).order_by(Country.name)
    if not include_inactive:
        q = q.where(Country.is_active.is_(True))
    return (await session.execute(q)).scalars().all()


@router.get("/{country_id}", response_model=CountryOut)
async def get_country(country_id: int, session: AsyncSession = Depends(get_session)):
    return await _get_or_404(session, country_id)


@router.post("/", response_model=CountryOut, status_code=201)
async def create_country(body: CountryCreate, _: User = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    await _ensure_unique(session, body.name, body.code)
    country = Country(**body.model_dump())
    session.add(country)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ServiceError(409, "Страна уже существует")
    return country


@router.patch("/{country_id}", response_model=CountryOut)
async def update_country(country_id: int, body: CountryUpdate, _: User = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    country = await _get_or_404(session, country_id)
    changes = {k: v for k, v in body.model_dump(exclude_unset=True).items() if v is not None}
    await _ensure_unique(session, changes.get("name"), changes.get("code"), exclude_id=country.id)
    for k, v in changes.items():
        setattr(country, k, v)
    await session.commit()
    return country


@router.delete("/{country_id}", status_code=204)
async def delete_country(country_id: int, _: User = Depends(require_admin),
                         session: AsyncSession = Depends(get_session)):
    country = await _get_or_404(session, country_id)
    used = await session.scalar(select(func.count()).select_from(Listing).where(Listing.country_id == country.id))
    if used:
        raise ServiceError(409, "Страна используется в объявлениях — отключите её (is_active=false) вместо удаления")
    await session.delete(country)  # города удаляются каскадом
    await session.commit()
    return Response(status_code=204)
