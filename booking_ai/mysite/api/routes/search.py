from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.database.session import get_session
from mysite.schemas.listing import ListingOut
from mysite.schemas.search import SearchFilters
from mysite.services import listing_service as svc

router = APIRouter(prefix="/search", tags=["search"])


class SearchOut(BaseModel):
    items: list[ListingOut]
    warnings: list[str] = []
    applied_filters: dict


@router.post("/", response_model=SearchOut)
async def search(filters: SearchFilters, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                 session: AsyncSession = Depends(get_session)):
    """Поиск по локальной базе. Поиск в интернете подключается отдельным этапом (SearchProvider)."""
    data = filters.model_dump(mode="json", exclude_none=True)
    items, warnings = await svc.search_listings(session, filters, limit=limit, offset=offset)
    return SearchOut(items=items, warnings=warnings, applied_filters=data)
