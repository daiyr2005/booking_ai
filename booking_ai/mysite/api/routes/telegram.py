from __future__ import annotations

import hmac

from fastapi import APIRouter, BackgroundTasks, Header, Request
from fastapi.responses import JSONResponse

from mysite.config import get_settings
from mysite.database.session import get_sessionmaker
from mysite.integrations.telegram import handle_update
from mysite.services.errors import ServiceError

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/webhook", include_in_schema=False)
async def webhook(request: Request, background: BackgroundTasks,
                  x_telegram_bot_api_secret_token: str | None = Header(None)):
    """Webhook Telegram. Включён, только если заданы TELEGRAM_BOT_TOKEN и TELEGRAM_WEBHOOK_SECRET.
    Регистрация: Bot API setWebhook с тем же secret_token."""
    secret = get_settings().telegram_webhook_secret
    client, runtime = getattr(request.app.state, "telegram", None), getattr(request.app.state, "ai", None)
    if not secret or client is None or runtime is None:
        raise ServiceError(404, "Not found")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(x_telegram_bot_api_secret_token, secret):
        raise ServiceError(403, "Forbidden")
    try:
        update = await request.json()
    except ValueError:
        return JSONResponse({"ok": False}, status_code=400)
    # отвечаем сразу (Telegram повторяет доставку при долгом ответе), обработка идёт в фоне
    background.add_task(handle_update, update, runtime=runtime, client=client, maker=get_sessionmaker())
    return {"ok": True}
