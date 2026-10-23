"""Точка входа: `python main.py` (127.0.0.1:8080) или `uvicorn main:app --reload`."""
from __future__ import annotations
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mysite.api.router import api_router
from mysite.config import get_settings
from mysite.integrations.telegram import TelegramClient
from mysite.services.ai_chat import build_runtime
from mysite.services.errors import ServiceError
from mysite.services.moderation_service import ModerationService, load_terms_from_db, sync_blocked_words
import uvicorn


log = logging.getLogger("booking_ai")


def create_app(*, load_blocked_file: bool = True) -> FastAPI:
    settings = get_settings()
    moderation = ModerationService(path=settings.blocked_words_path)
    if load_blocked_file:
        moderation.load_file()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # TXT -> таблица BlockedWord, дальше источник правды — БД (ею управляют через /api/v1/blocked-words)
        try:
            from mysite.database.session import get_sessionmaker
            async with get_sessionmaker()() as session:
                await sync_blocked_words(session, app.state.moderation)
                app.state.moderation.set_terms(await load_terms_from_db(session))
        except Exception:
            log.exception("Не удалось синхронизировать запрещённые слова с БД, используется TXT-файл")
        yield
        if app.state.telegram is not None:
            await app.state.telegram.aclose()

    app = FastAPI(title=settings.app_name, version="0.3.0", lifespan=lifespan)
    app.state.moderation = moderation
    app.state.ai = build_runtime(settings, moderation)
    app.state.telegram = TelegramClient(settings.telegram_bot_token) if settings.telegram_bot_token else None

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(status_code=exc.status_code, headers=headers,
                            content={"detail": exc.message, "errors": exc.errors})

    @app.get("/health", tags=["service"])
    async def health():
        return {"status": "ok"}

    app.include_router(api_router)
    return app


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)