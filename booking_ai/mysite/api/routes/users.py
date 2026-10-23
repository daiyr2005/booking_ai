from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_current_user, require_admin
from mysite.database.ai_models import User
from mysite.database.session import get_session
from mysite.schemas.auth import UserAdminUpdate, UserOut, UserSelfUpdate
from mysite.services.errors import ServiceError

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


@router.patch("/me", response_model=UserOut)
async def update_me(body: UserSelfUpdate, user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    if body.full_name is not None:
        user.full_name = body.full_name
        await session.commit()
    return user


@router.get("/", response_model=list[UserOut])
async def list_users(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                     _: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    rows = await session.execute(select(User).order_by(User.id).limit(limit).offset(offset))
    return rows.scalars().all()


@router.patch("/{user_id}", response_model=UserOut)
async def admin_update_user(user_id: int, body: UserAdminUpdate, admin: User = Depends(require_admin),
                            session: AsyncSession = Depends(get_session)):
    target = await session.get(User, user_id)
    if target is None:
        raise ServiceError(404, "Пользователь не найден")
    if target.id == admin.id and (body.role == "user" or body.is_active is False):
        raise ServiceError(409, "Нельзя понизить или отключить самого себя")
    if body.role is not None:
        target.role = body.role
    if body.is_active is not None:
        target.is_active = body.is_active
    await session.commit()
    return target
