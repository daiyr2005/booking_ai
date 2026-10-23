"""Telegram Bot API: клиент, привязка аккаунта и обработка входящих сообщений.

Режимы: long polling (`python -m mysite.integrations.telegram_poll`, не требует публичного адреса) и webhook
(`POST /api/v1/telegram/webhook`). Токен бота берётся только из настроек и нигде не логируется."""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mysite.clients.ollama import OllamaUnavailable
from mysite.database.ai_models import ChannelAccount, ChannelLinkCode, ProcessedMessage, User
from mysite.schemas.ai import ChatOut
from mysite.services.ai_chat import AIRuntime

# httpx пишет в лог полный URL запроса, а в нём токен бота: глушим INFO-логи библиотеки
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("booking_ai.telegram")

CHANNEL = "telegram"
LINK_TTL = timedelta(minutes=15)
MAX_TEXT = 2000
TG_LIMIT = 4000

HELP = ("Я помогаю искать, добавлять, менять и удалять объявления и оформлять подписки.\n"
        "Примеры: «найди отель в Бишкеке до 5000 сом за ночь», «мои объявления», «удали объявление 7».\n"
        "Чтобы управлять объявлениями, привяжите аккаунт: получите код в API (POST /api/v1/channels/telegram/link) "
        "и отправьте мне «/start КОД».\nОтвязать: /unlink")


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 base_url: str = "https://api.telegram.org"):
        self._url = f"{base_url}/bot{token}"
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=10.0), transport=transport)

    async def _call(self, method: str, payload: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        try:
            r = await self._http.post(f"{self._url}/{method}", json=payload or {},
                                      timeout=timeout if timeout else httpx.USE_CLIENT_DEFAULT)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"Telegram API недоступен ({type(e).__name__})") from None  # без URL с токеном
        if not data.get("ok"):
            raise TelegramError(f"Telegram API: {data.get('description', 'ошибка')} ({method})")
        return data["result"]

    async def send_message(self, chat_id: int | str, text: str) -> None:
        for i in range(0, max(len(text), 1), TG_LIMIT):
            await self._call("sendMessage", {"chat_id": chat_id, "text": text[i:i + TG_LIMIT] or "…"})

    async def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._call("getUpdates", payload, timeout=timeout + 15)

    async def delete_webhook(self) -> None:
        await self._call("deleteWebhook", {"drop_pending_updates": False})

    async def set_webhook(self, url: str, secret: str) -> None:
        await self._call("setWebhook", {"url": url, "secret_token": secret, "allowed_updates": ["message"]})

    async def aclose(self) -> None:
        await self._http.aclose()


def format_reply(out: ChatOut) -> str:
    """Обычный текст без разметки (parse_mode не используется, чтобы пользовательский текст ничего не ломал)."""
    parts = [out.response]
    for r in out.results[:5]:
        price = f" — {r['price']} {r.get('currency') or ''}".rstrip() if r.get("price") else ""
        line = f"№{r.get('id')} {r.get('title', '')}{price}"
        if r.get("source_url"):
            line += f"\n{r['source_url']}"
        parts.append(line)
    if len(out.results) > 5:
        parts.append(f"…и ещё {len(out.results) - 5}")
    return "\n".join(p for p in parts if p)


async def create_link_code(session: AsyncSession, user_id: int, channel: str = CHANNEL) -> tuple[str, int]:
    """Короткий одноразовый код (символы A-Z a-z 0-9 _ -: подходит для t.me/bot?start=КОД)."""
    code = secrets.token_urlsafe(16)
    session.add(ChannelLinkCode(code=code, user_id=user_id, channel=channel,
                                expires_at=datetime.now(timezone.utc) + LINK_TTL))
    await session.commit()
    return code, int(LINK_TTL.total_seconds())


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # SQLite отдаёт naive-даты


async def _link(session: AsyncSession, code: str, tg_user_id: int) -> str:
    row = await session.scalar(select(ChannelLinkCode).where(
        ChannelLinkCode.code == code, ChannelLinkCode.channel == CHANNEL))
    if row is None or _aware(row.expires_at) < datetime.now(timezone.utc):
        if row is not None:
            await session.delete(row)
            await session.commit()
        return "Код недействителен или истёк. Получите новый в API."
    existing = await session.scalar(select(ChannelAccount).where(
        ChannelAccount.channel == CHANNEL, ChannelAccount.external_user_id == str(tg_user_id)))
    if existing is not None and existing.user_id != row.user_id:
        return "Этот Telegram уже привязан к другому аккаунту. Сначала отправьте /unlink с него."
    if existing is None:
        session.add(ChannelAccount(user_id=row.user_id, channel=CHANNEL, external_user_id=str(tg_user_id),
                                   is_verified=True, is_active=True))
    else:
        existing.is_verified = existing.is_active = True
    await session.delete(row)  # код одноразовый
    await session.commit()
    return "Готово: аккаунт привязан. Теперь можно управлять объявлениями и подписками."


async def _unlink(session: AsyncSession, tg_user_id: int) -> str:
    acc = await session.scalar(select(ChannelAccount).where(
        ChannelAccount.channel == CHANNEL, ChannelAccount.external_user_id == str(tg_user_id)))
    if acc is None:
        return "Этот Telegram ни к чему не привязан."
    await session.delete(acc)
    await session.commit()
    return "Аккаунт отвязан."


async def _claim_message(session: AsyncSession, external_id: str) -> bool:
    """Дедупликация повторной доставки. True — сообщение новое."""
    session.add(ProcessedMessage(channel=CHANNEL, external_message_id=external_id))
    try:
        await session.commit()
        return True
    except IntegrityError:
        await session.rollback()
        return False


async def _linked_user(session: AsyncSession, tg_user_id: int) -> User | None:
    acc = await session.scalar(select(ChannelAccount).where(
        ChannelAccount.channel == CHANNEL, ChannelAccount.external_user_id == str(tg_user_id),
        ChannelAccount.is_verified.is_(True), ChannelAccount.is_active.is_(True)))
    user = await session.get(User, acc.user_id) if acc else None
    return user if user is not None and user.is_active else None


async def process_message(session: AsyncSession, runtime: AIRuntime, *, chat_id: int, tg_user_id: int,
                          text: str) -> str:
    cmd, _, arg = text.partition(" ")
    cmd = cmd.split("@")[0].lower()
    if cmd == "/start":
        return await _link(session, arg.strip(), tg_user_id) if arg.strip() else HELP
    if cmd == "/help":
        return HELP
    if cmd == "/unlink":
        return await _unlink(session, tg_user_id)
    user = await _linked_user(session, tg_user_id)
    out = await runtime.handle(session, user, CHANNEL, str(chat_id), text)
    return format_reply(out)


async def handle_update(update: dict, *, runtime: AIRuntime, client: TelegramClient,
                        maker: async_sessionmaker[AsyncSession]) -> None:
    """Обрабатывает один update. Ошибки не пробрасываются: пользователю уходит нейтральный ответ, подробности в лог."""
    msg = update.get("message")
    if not isinstance(msg, dict) or not isinstance(msg.get("text"), str):
        return
    chat, sender = msg.get("chat") or {}, msg.get("from") or {}
    if chat.get("type") != "private" or "id" not in chat or "id" not in sender or sender.get("is_bot"):
        return  # только личные чаты: в группах чужие люди увидели бы ответы с данными аккаунта
    chat_id, text = chat["id"], msg["text"].strip()[:MAX_TEXT]
    if not text:
        return
    try:
        async with maker() as session:
            if not await _claim_message(session, f"{chat_id}:{msg.get('message_id')}"):
                return
            reply = await process_message(session, runtime, chat_id=chat_id, tg_user_id=sender["id"], text=text)
    except OllamaUnavailable:
        reply = "Ассистент временно недоступен, попробуйте позже."
    except Exception:
        log.exception("Ошибка обработки сообщения Telegram")
        reply = "Что-то пошло не так, попробуйте ещё раз позже."
    try:
        await client.send_message(chat_id, reply)
    except TelegramError as e:
        log.warning("Не удалось отправить ответ: %s", e)
