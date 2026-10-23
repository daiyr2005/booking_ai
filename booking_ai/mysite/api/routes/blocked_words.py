from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_moderation, require_admin
from mysite.database.ai_models import BlockedWord, User
from mysite.database.session import get_session
from mysite.schemas.subscription import BlockedWordCreate, BlockedWordOut, BlockedWordUpdate
from mysite.services import moderation_admin as admin_svc
from mysite.services.errors import ServiceError
from mysite.services.moderation_service import ModerationService

router = APIRouter(prefix="/blocked-words", tags=["blocked-words"])


async def _get_or_404(session: AsyncSession, word_id: int) -> BlockedWord:
    row = await session.get(BlockedWord, word_id)
    if row is None:
        raise ServiceError(404, "Слово не найдено")
    return row


@router.get("/", response_model=list[BlockedWordOut])
async def list_words(limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                     _: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    q = select(BlockedWord).order_by(BlockedWord.id).limit(limit).offset(offset)
    return (await session.execute(q)).scalars().all()


@router.post("/", response_model=BlockedWordOut, status_code=201)
async def add_word(body: BlockedWordCreate, _: User = Depends(require_admin),
                   session: AsyncSession = Depends(get_session),
                   moderation: ModerationService = Depends(get_moderation)):
    return await admin_svc.add_word(session, moderation, word=body.word, match_type=body.match_type, reason=body.reason)


@router.patch("/{word_id}", response_model=BlockedWordOut)
async def update_word(word_id: int, body: BlockedWordUpdate, _: User = Depends(require_admin),
                      session: AsyncSession = Depends(get_session),
                      moderation: ModerationService = Depends(get_moderation)):
    row = await _get_or_404(session, word_id)
    return await admin_svc.update_word(session, moderation, row, body.model_dump(exclude_unset=True))


@router.delete("/{word_id}", status_code=204)
async def delete_word(word_id: int, _: User = Depends(require_admin), session: AsyncSession = Depends(get_session),
                      moderation: ModerationService = Depends(get_moderation)):
    row = await _get_or_404(session, word_id)
    await admin_svc.delete_word(session, moderation, row)
    return Response(status_code=204)
