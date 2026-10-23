"""Настройки приложения из переменных окружения / .env (секреты только там)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DEV_JWT_SECRET = "dev-only-secret-change-me-0000000000"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore",
                                      populate_by_name=True)

    app_name: str = "Booking AI"
    app_env: str = "development"
    # Либо готовый DATABASE_URL, либо набор POSTGRES_* (тогда URL собирается автоматически)
    database_url: str = ""
    postgres_db: str | None = None
    postgres_user: str | None = None
    postgres_password: str = ""
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    jwt_secret: str = DEV_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60 * 24
    bcrypt_rounds: int = 12

    blocked_words_path: Path = BASE_DIR / "data" / "blocked_words.txt"

    # первый администратор создаётся командой `python -m mysite.seed`, если заданы оба значения
    admin_email: str | None = None
    admin_password: str | None = None

    # следующие этапы
    # OLLAMA_URL — короткое имя переменной, OLLAMA_BASE_URL — прежнее; работают оба
    ollama_base_url: str = Field("http://127.0.0.1:11434", validation_alias=AliasChoices("OLLAMA_BASE_URL", "OLLAMA_URL"))
    ollama_model: str = "llama3.2:latest"
    ollama_timeout_seconds: float = 120.0
    ai_pending_ttl_seconds: int = 300      # сколько ждём ответ «да/нет» на подтверждение удаления
    search_provider: str = "tavily"
    tavily_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_bot_username: str | None = None      # без @, нужен для ссылки привязки аккаунта
    telegram_webhook_secret: str | None = None    # для webhook-режима; в polling не нужен
    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_verify_token: str | None = None
    whatsapp_app_secret: str | None = None
    redis_url: str = "redis://localhost:6379/0"

    @model_validator(mode="after")
    def _build_database_url(self):
        if not self.database_url:
            if self.postgres_db and self.postgres_user:
                self.database_url = (f"postgresql+asyncpg://{quote_plus(self.postgres_user)}:"
                                     f"{quote_plus(self.postgres_password)}@{self.postgres_host}:"
                                     f"{self.postgres_port}/{self.postgres_db}")
            else:
                self.database_url = "postgresql+asyncpg://user:password@localhost:5432/booking_ai"
        return self


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.jwt_secret:  # пустое значение из .env не должно становиться ключом подписи
        s.jwt_secret = DEV_JWT_SECRET
    if s.app_env.lower() == "production" and (s.jwt_secret == DEV_JWT_SECRET or len(s.jwt_secret) < 32):
        raise RuntimeError("В production задайте JWT_SECRET длиной не менее 32 символов.")
    return s