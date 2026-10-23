from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.database.ai_models import User
from mysite.database.session import get_session
from mysite.services.ai_chat import AIRuntime
from mysite.services.errors import ServiceError
from mysite.services.moderation_service import ModerationService
from mysite.services.security import decode_access_token

bearer = HTTPBearer(auto_error=False, description="JWT из POST /api/v1/auth/login")


async def get_current_user_optional(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> User | None:
    if creds is None:
        return None
    user_id = decode_access_token(creds.credentials)
    user = await session.get(User, user_id) if user_id is not None else None
    if user is None or not user.is_active:
        raise ServiceError(401, "Недействительный или просроченный токен")
    return user


async def get_current_user(user: User | None = Depends(get_current_user_optional)) -> User:
    if user is None:
        raise ServiceError(401, "Требуется авторизация")
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise ServiceError(403, "Недостаточно прав")
    return user


def get_moderation(request: Request) -> ModerationService:
    return request.app.state.moderation


def get_ai_runtime(request: Request) -> AIRuntime:
    runtime = getattr(request.app.state, "ai", None)
    if runtime is None:
        raise ServiceError(503, "ИИ-ассистент не настроен")
    return runtime
