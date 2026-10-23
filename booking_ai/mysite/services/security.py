"""Пароли (bcrypt) и JWT (PyJWT)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import bcrypt
import jwt

from mysite.config import get_settings

MAX_PASSWORD_BYTES = 72  # ограничение bcrypt


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise ValueError("Пароль слишком длинный")
    return bcrypt.hashpw(raw, bcrypt.gensalt(rounds=get_settings().bcrypt_rounds)).decode("ascii")


def verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False
    raw = password.encode("utf-8")
    if len(raw) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, password_hash.encode("ascii"))
    except ValueError:
        return False


@lru_cache
def dummy_hash() -> str:
    """Для выравнивания времени ответа, когда пользователь не найден."""
    return hash_password("dummy-password-for-timing")


def create_access_token(user_id: int) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "iat": now, "exp": now + timedelta(minutes=s.access_token_expire_minutes)}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> int | None:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["exp", "sub"]})
        if payload.get("purpose"):  # токены с назначением (не доступа) не годятся для входа
            return None
        return int(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        return None
