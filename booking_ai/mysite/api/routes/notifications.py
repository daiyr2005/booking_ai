from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_current_user
from mysite.database.ai_models import Notification, User
from mysite.database.session import get_session
from mysite.schemas.subscription import NotificationOut

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("/", response_model=list[NotificationOut])
async def list_notifications(status: str | None = Query(None, pattern="^(pending|sent|failed)$"),
                             limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                             user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    q = select(Notification).where(Notification.user_id == user.id)
    if status:
        q = q.where(Notification.status == status)
    q = q.order_by(Notification.id.desc()).limit(limit).offset(offset)
    return (await session.execute(q)).scalars().all()
