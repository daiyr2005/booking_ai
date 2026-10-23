"""Управление списком запрещённых слов через БД + обновление «горячего» ModerationService."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.database.ai_models import BlockedWord
from mysite.services.errors import ServiceError
from mysite.services.moderation_service import ModerationService, load_terms_from_db, normalize_text


async def refresh_moderation(session: AsyncSession, moderation: ModerationService) -> None:
    moderation.set_terms(await load_terms_from_db(session))


def _normalized(word: str) -> str:
    norm = normalize_text(word)
    if not norm:
        raise ServiceError(422, "Слово пусто после нормализации")
    return norm


async def add_word(session: AsyncSession, moderation: ModerationService, *, word: str, match_type: str,
                   reason: str | None) -> BlockedWord:
    norm = _normalized(word)
    if await session.scalar(select(BlockedWord.id).where(BlockedWord.normalized_word == norm)):
        raise ServiceError(409, "Такое слово уже есть в списке")
    row = BlockedWord(word=word, normalized_word=norm, match_type=match_type, reason=reason)
    session.add(row)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ServiceError(409, "Такое слово уже есть в списке")
    await refresh_moderation(session, moderation)
    return row


async def update_word(session: AsyncSession, moderation: ModerationService, row: BlockedWord, changes: dict) -> BlockedWord:
    if changes.get("word") is not None:
        norm = _normalized(changes["word"])
        other = await session.scalar(select(BlockedWord.id).where(
            BlockedWord.normalized_word == norm, BlockedWord.id != row.id))
        if other:
            raise ServiceError(409, "Такое слово уже есть в списке")
        row.word, row.normalized_word = changes["word"], norm
    for k in ("match_type", "is_active"):
        if changes.get(k) is not None:
            setattr(row, k, changes[k])
    if "reason" in changes:
        row.reason = changes["reason"]
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ServiceError(409, "Такое слово уже есть в списке")
    await refresh_moderation(session, moderation)
    return row


async def delete_word(session: AsyncSession, moderation: ModerationService, row: BlockedWord) -> None:
    await session.delete(row)
    await session.commit()
    await refresh_moderation(session, moderation)
