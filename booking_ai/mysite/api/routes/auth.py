from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.database.ai_models import User
from mysite.database.session import get_session
from mysite.schemas.auth import LoginIn, RegisterIn, TokenOut, UserOut
from mysite.services.errors import ServiceError
from mysite.services.security import create_access_token, dummy_hash, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserOut, status_code=201)
async def register(body: RegisterIn, session: AsyncSession = Depends(get_session)):
    email = body.email.lower()
    if await session.scalar(select(User.id).where(func.lower(User.email) == email)):
        raise ServiceError(409, "Пользователь с таким email уже существует")
    # роль всегда "user": администратора нельзя получить через регистрацию
    user = User(full_name=body.full_name, email=email, role="user",
                password_hash=await asyncio.to_thread(hash_password, body.password))
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ServiceError(409, "Пользователь с таким email уже существует")
    return user


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)):
    user = await session.scalar(select(User).where(func.lower(User.email) == body.email.lower()))
    hashed = user.password_hash if user else await asyncio.to_thread(dummy_hash)
    ok = await asyncio.to_thread(verify_password, body.password, hashed)
    if not (user and ok and user.is_active):
        raise ServiceError(401, "Неверный email или пароль")  # одинаковый ответ — не раскрываем, что именно не так
    return TokenOut(access_token=create_access_token(user.id))
