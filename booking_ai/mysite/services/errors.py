from __future__ import annotations


class ServiceError(Exception):
    """Ожидаемая ошибка бизнес-логики; в API превращается в JSON {detail, errors} с нужным кодом."""

    def __init__(self, status_code: int, message: str, errors: list[str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.errors = errors or []
