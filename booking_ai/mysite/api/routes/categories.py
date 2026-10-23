from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_current_user_optional, require_admin
from mysite.database.ai_models import Category, Listing, User
from mysite.database.session import get_session
from mysite.schemas.catalog import CategoryCreate, CategoryOut, CategoryTree, CategoryUpdate
from mysite.services.errors import ServiceError

router = APIRouter(prefix="/categories", tags=["categories"])


async def _get_or_404(session: AsyncSession, category_id: int) -> Category:
    cat = await session.get(Category, category_id)
    if cat is None:
        raise ServiceError(404, "Категория не найдена")
    return cat


async def _ensure_slug_free(session: AsyncSession, slug: str, exclude_id: int | None = None) -> None:
    q = select(Category.id).where(Category.slug == slug)
    if exclude_id is not None:
        q = q.where(Category.id != exclude_id)
    if await session.scalar(q):
        raise ServiceError(409, "Категория с таким slug уже есть")


async def _ensure_parent_ok(session: AsyncSession, parent_id: int | None, self_id: int | None = None) -> None:
    """Родитель должен существовать, иерархия не должна замыкаться в цикл."""
    if parent_id is None:
        return
    if await session.get(Category, parent_id) is None:
        raise ServiceError(422, "Родительская категория не найдена")
    cur, seen = parent_id, set()
    while cur is not None and cur not in seen:
        if self_id is not None and cur == self_id:
            raise ServiceError(422, "Категория не может быть вложена сама в себя")
        seen.add(cur)
        cur = await session.scalar(select(Category.parent_id).where(Category.id == cur))


@router.get("/", response_model=list[CategoryOut])
async def list_categories(parent_id: int | None = None, include_inactive: bool = False,
                          user: User | None = Depends(get_current_user_optional),
                          session: AsyncSession = Depends(get_session)):
    if include_inactive and (user is None or user.role != "admin"):
        raise ServiceError(403, "Недостаточно прав")
    q = select(Category).order_by(Category.name, Category.id)
    if parent_id is not None:
        q = q.where(Category.parent_id == parent_id)
    if not include_inactive:
        q = q.where(Category.is_active.is_(True))
    return (await session.execute(q)).scalars().all()


@router.get("/tree", response_model=list[CategoryTree])
async def category_tree(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(select(Category).where(Category.is_active.is_(True))
                                  .order_by(Category.name, Category.id))).scalars().all()
    nodes = {c.id: {"id": c.id, "name": c.name, "slug": c.slug, "description": c.description,
                    "is_active": c.is_active, "children": []} for c in rows}
    roots = []
    for c in rows:
        parent = nodes.get(c.parent_id) if c.parent_id is not None else None
        (parent["children"] if parent else roots).append(nodes[c.id])
    return roots


@router.get("/{category_id}", response_model=CategoryOut)
async def get_category(category_id: int, session: AsyncSession = Depends(get_session)):
    return await _get_or_404(session, category_id)


@router.post("/", response_model=CategoryOut, status_code=201)
async def create_category(body: CategoryCreate, _: User = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    await _ensure_slug_free(session, body.slug)
    await _ensure_parent_ok(session, body.parent_id)
    cat = Category(**body.model_dump())
    session.add(cat)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ServiceError(409, "Категория уже существует")
    return cat


@router.patch("/{category_id}", response_model=CategoryOut)
async def update_category(category_id: int, body: CategoryUpdate, _: User = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    cat = await _get_or_404(session, category_id)
    changes = body.model_dump(exclude_unset=True)
    for non_null in ("name", "slug", "is_active"):
        if non_null in changes and changes[non_null] is None:
            del changes[non_null]
    if "slug" in changes:
        await _ensure_slug_free(session, changes["slug"], exclude_id=cat.id)
    if "parent_id" in changes:
        await _ensure_parent_ok(session, changes["parent_id"], self_id=cat.id)
    for k, v in changes.items():
        setattr(cat, k, v)
    await session.commit()
    return cat


@router.delete("/{category_id}", status_code=204)
async def delete_category(category_id: int, _: User = Depends(require_admin),
                          session: AsyncSession = Depends(get_session)):
    cat = await _get_or_404(session, category_id)
    has_children = await session.scalar(select(func.count()).select_from(Category).where(Category.parent_id == cat.id))
    used = await session.scalar(select(func.count()).select_from(Listing).where(Listing.category_id == cat.id))
    if has_children or used:
        raise ServiceError(409, "Категория используется (есть вложенные категории или объявления) — отключите её")
    await session.delete(cat)
    await session.commit()
    return Response(status_code=204)
