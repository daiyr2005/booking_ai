from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_current_user
from mysite.config import get_settings
from mysite.database.ai_models import ChannelAccount, User
from mysite.database.session import get_session
from mysite.integrations.telegram import create_link_code
from mysite.services.errors import ServiceError

router = APIRouter(prefix="/channels", tags=["channels"])


class LinkCodeOut(BaseModel):
    code: str
    expires_in_seconds: int
    deep_link: str | None
    instruction: str


class ChannelAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    channel: str
    external_user_id: str
    is_verified: bool
    is_active: bool
    created_at: datetime


@router.post("/telegram/link", response_model=LinkCodeOut, status_code=201)
async def telegram_link(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Одноразовый код (15 минут): отправьте боту `/start КОД`, и этот Telegram привяжется к вашему аккаунту."""
    code, ttl = await create_link_code(session, user.id)
    username = get_settings().telegram_bot_username
    return LinkCodeOut(code=code, expires_in_seconds=ttl,
                       deep_link=f"https://t.me/{username}?start={code}" if username else None,
                       instruction=f"Отправьте боту сообщение: /start {code}")


@router.get("/", response_model=list[ChannelAccountOut])
async def my_channels(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    q = select(ChannelAccount).where(ChannelAccount.user_id == user.id).order_by(ChannelAccount.id)
    return (await session.execute(q)).scalars().all()


@router.delete("/{account_id}", status_code=204)
async def unlink_channel(account_id: int, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    acc = await session.get(ChannelAccount, account_id)
    if acc is None or acc.user_id != user.id:
        raise ServiceError(404, "Привязка не найдена")
    await session.delete(acc)
    await session.commit()
    return Response(status_code=204)
