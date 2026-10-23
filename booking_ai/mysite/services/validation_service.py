"""Детерминированная валидация объявлений и фильтров (основа Validation Agent).
LLM не принимает решение о допустимости — только эти проверки."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from mysite.schemas.search import CURRENCIES, PERIODS
from mysite.services.moderation_service import ModerationService, normalize_text

REQUIRED_FOR_CREATE = ("category", "city", "price")
TEXT_FIELDS = ("title", "description", "address")


def _dec(v):
    try:
        return None if v in (None, "") else Decimal(str(v))
    except InvalidOperation:
        return "bad"


def is_safe_url(url: str, *, resolve: bool = False) -> bool:
    """Защита от SSRF: только http(s), без credentials, без локальных/приватных адресов."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return False
    host = p.hostname.lower()
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return False
    addrs: list[str] = []
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        if resolve:
            try:
                addrs = [i[4][0] for i in socket.getaddrinfo(host, None)]
            except socket.gaierror:
                return False
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


def validate_listing_data(data: dict, *, partial: bool = False, today: date | None = None,
                          moderation: ModerationService | None = None) -> list[str]:
    """Возвращает список понятных ошибок. partial=True — для PATCH (обязательные поля не требуются)."""
    errors: list[str] = []
    if not partial:
        for f in REQUIRED_FOR_CREATE:
            if data.get(f) in (None, "") and not (f == "price" and data.get("price_min") not in (None, "")):
                errors.append(f"Не заполнено обязательное поле: {f}")

    price, pmin, pmax = _dec(data.get("price")), _dec(data.get("price_min")), _dec(data.get("price_max"))
    for name, val in (("price", price), ("price_min", pmin), ("price_max", pmax)):
        if val == "bad":
            errors.append(f"Поле {name} должно быть числом")
        elif val is not None and val < 0:
            errors.append(f"Поле {name} не может быть отрицательным")
    nums = [x for x in (pmin, pmax) if isinstance(x, Decimal)]
    if len(nums) == 2 and pmin > pmax:
        errors.append("Минимальная цена больше максимальной")

    cur = data.get("currency")
    if cur and str(cur).upper() not in CURRENCIES:
        errors.append(f"Неподдерживаемая валюта: {cur}")
    per = data.get("price_period")
    if per and per not in PERIODS:
        errors.append(f"Неподдерживаемый период оплаты: {per}")

    ci, co = data.get("check_in"), data.get("check_out")
    if isinstance(ci, date) and isinstance(co, date):
        if co <= ci:
            errors.append("Дата выезда должна быть позже даты заезда")
    if isinstance(ci, date) and today and ci < today:
        errors.append("Дата заезда уже прошла")

    for f in ("rooms", "guests"):
        v = data.get(f)
        if v is not None and (not isinstance(v, int) or v < 0):
            errors.append(f"Поле {f} должно быть неотрицательным целым числом")

    url = data.get("source_url")
    if url and not is_safe_url(url):
        errors.append("Некорректная или небезопасная ссылка")

    if moderation:
        bad = moderation.check_fields({f: data.get(f) for f in TEXT_FIELDS})
        if bad:
            errors.append(ModerationService.user_message(bad))
    return errors


def fingerprint(data: dict) -> str:
    key = "|".join(normalize_text(str(data.get(k) or "")) for k in
                   ("category", "city", "title", "price", "rooms", "address"))
    return hashlib.sha256(key.encode()).hexdigest()


def find_duplicates(candidate: dict, existing: list[dict]) -> list[dict]:
    """Дубликат: тот же source_url / (source_name, external_id) / тот же отпечаток."""
    fp = fingerprint(candidate)
    out = []
    for e in existing:
        same_url = candidate.get("source_url") and candidate.get("source_url") == e.get("source_url")
        same_ext = (candidate.get("external_id") and candidate.get("external_id") == e.get("external_id")
                    and candidate.get("source_name") == e.get("source_name"))
        if same_url or same_ext or fingerprint(e) == fp:
            out.append(e)
    return out
