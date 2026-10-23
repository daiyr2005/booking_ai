"""Запуск бота через long polling: `python -m mysite.integrations.telegram_poll`.
Не требует публичного адреса, подходит для локальной разработки рядом с Ollama."""
from __future__ import annotations

import asyncio
import logging

from mysite.config import get_settings
from mysite.database.session import get_sessionmaker
from mysite.integrations.telegram import TelegramClient, TelegramError, handle_update
from mysite.services.ai_chat import build_runtime
from mysite.services.moderation_service import ModerationService, load_terms_from_db

log = logging.getLogger("booking_ai.telegram")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("Задайте TELEGRAM_BOT_TOKEN в .env")
    moderation = ModerationService(path=settings.blocked_words_path)
    moderation.load_file()
    maker = get_sessionmaker()
    try:
        async with maker() as session:
            moderation.set_terms(await load_terms_from_db(session))
    except Exception:
        log.exception("Запрещённые слова из БД не загружены, используется TXT-файл")
    runtime = build_runtime(settings, moderation)
    client = TelegramClient(settings.telegram_bot_token)
    await client.delete_webhook()  # polling и webhook взаимоисключающие
    log.info("Бот запущен (long polling). Остановка: Ctrl+C")
    offset: int | None = None
    delay = 1.0
    try:
        while True:
            try:
                updates = await client.get_updates(offset)
                delay = 1.0
            except TelegramError as e:
                log.warning("%s; повтор через %.0f с", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                await handle_update(u, runtime=runtime, client=client, maker=maker)
    finally:
        await client.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
