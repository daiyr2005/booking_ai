"""LLM-классификатор на Ollama: текст пользователя → ParsedIntent.

Модель только разбирает запрос. Права, валидация и запись в БД выполняются на сервере (граф и сервисы).
Для небольших моделей (llama3.2) схема ответа плоская: так надёжнее, чем вложенный JSON.
Результат всегда проходит проверку Pydantic; при мусоре от модели — один повтор, затем intent=unknown."""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from mysite.schemas.search import ParsedIntent, SearchFilters

log = logging.getLogger("booking_ai.ollama")

FILTER_KEYS = tuple(SearchFilters.model_fields)
LISTING_KEYS = ("category", "country", "city", "title", "description", "property_type", "address", "price",
                "price_min", "price_max", "currency", "price_period", "rooms", "guests", "area",
                "check_in", "check_out")
EMPTY = {None, "", "null", "none", "None", "unknown", "нет", "-", 0, 0.0}


class OllamaUnavailable(RuntimeError):
    """Ollama не отвечает или вернула ошибку."""


class LLMIntent(BaseModel):
    """Плоская схема ответа модели (передаётся в Ollama как `format`)."""
    model_config = ConfigDict(extra="ignore")

    intent: Literal["search", "create", "update", "delete", "subscribe", "unsubscribe", "list_mine", "unknown"] = "unknown"
    listing_id: int | None = None
    category: str | None = None
    country: str | None = None
    city: str | None = None
    district: str | None = None
    property_type: str | None = None
    title: str | None = None
    description: str | None = None
    address: str | None = None
    price: float | None = None
    price_min: float | None = None
    price_max: float | None = None
    currency: str | None = None
    price_period: str | None = None
    rooms: int | None = None
    guests: int | None = None
    area: float | None = None
    area_min: float | None = None
    area_max: float | None = None
    check_in: str | None = None
    check_out: str | None = None
    rental_type: Literal["short_term", "long_term"] | None = None


def _present(v: Any) -> bool:
    return not (isinstance(v, str) and v.strip() in EMPTY) and v not in EMPTY


def to_parsed_intent(raw: LLMIntent) -> ParsedIntent:
    """LLMIntent → ParsedIntent. Пустые/нулевые значения модели считаются отсутствующими."""
    d = {k: v for k, v in raw.model_dump().items() if _present(v)}
    intent = d.pop("intent", "unknown") if "intent" in d else "unknown"
    listing_id = d.pop("listing_id", None)
    if intent in ("search", "subscribe", "unsubscribe", "list_mine"):
        f = {k: d[k] for k in FILTER_KEYS if k in d}
        if "price" in d and "price_max" not in f and "price_min" not in f:
            f["price_max"] = d["price"]            # «до 30000» модель часто кладёт в price
        return ParsedIntent(intent=intent, filters=SearchFilters.model_validate(f), listing_id=listing_id)
    if intent == "create":
        draft = {k: d[k] for k in LISTING_KEYS if k in d}
        for k in ("area_min", "area_max"):          # на случай, если модель перепутала поля
            if k in d and "area" not in draft:
                draft["area"] = d[k]
        return ParsedIntent(intent=intent, draft=draft)
    if intent == "update":
        changes = {k: d[k] for k in LISTING_KEYS if k in d and k not in ("category", "country")}
        return ParsedIntent(intent=intent, changes=changes, listing_id=listing_id)
    if intent == "delete":
        return ParsedIntent(intent=intent, listing_id=listing_id)
    return ParsedIntent(intent="unknown")


SYSTEM_PROMPT = """Ты разбираешь сообщения пользователя сервиса объявлений (аренда жилья, отели и т.п.).
Верни ТОЛЬКО JSON по схеме. Сегодня {today}. Ничего не выдумывай: поле, которого нет в сообщении, оставь null.

intent:
- search — найти объявления; create — добавить своё; update — изменить своё (нужен listing_id);
- delete — удалить своё (нужен listing_id); subscribe — «сообщай о новых»; unsubscribe — отменить подписку
  (listing_id = номер подписки); list_mine — показать мои объявления; unknown — всё остальное.
category — только slug из списка: {categories}. Если ничего не подходит — null.
city, country — названия как в сообщении, в именительном падеже (не «в Бишкеке», а «Бишкек»).
currency — код (KGS, KZT, RUB, USD, EUR). price — цена для create/update. price_min/price_max — границы для search/subscribe.
price_period — night, day, week, month, year или total. Даты — YYYY-MM-DD.
Числа без единиц: «30 тыс» → 30000.
{context}
Примеры:
«найди отель в Бишкеке до 5000 сом за ночь» → {{"intent":"search","category":"hotel","city":"Бишкек","price_max":5000,"currency":"KGS","price_period":"night"}}
«сдаю 2-комнатную квартиру в Алматы, 250000 тенге в месяц, заголовок Уютная двушка» → {{"intent":"create","category":"apartment_rent","city":"Алматы","price":250000,"currency":"KZT","price_period":"month","rooms":2,"title":"Уютная двушка"}}
«измени цену объявления 12 на 30000» → {{"intent":"update","listing_id":12,"price":30000}}
«удали объявление 7» → {{"intent":"delete","listing_id":7}}
«мои объявления» → {{"intent":"list_mine"}}
«привет» → {{"intent":"unknown"}}"""


class OllamaClassifier:
    def __init__(self, base_url: str, model: str, *, timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None, today=date.today):
        self.base_url, self.model, self.today = base_url.rstrip("/"), model, today
        self._http = httpx.Client(timeout=timeout, transport=transport)

    def _chat(self, system: str, user: str) -> str:
        body = {"model": self.model, "stream": False, "format": LLMIntent.model_json_schema(),
                "options": {"temperature": 0},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            r = self._http.post(f"{self.base_url}/api/chat", json=body)
            r.raise_for_status()
            return r.json()["message"]["content"]
        except (httpx.HTTPError, KeyError, ValueError) as e:
            raise OllamaUnavailable(f"Ollama недоступна ({type(e).__name__})") from e

    def classify(self, text: str, *, categories: list[str] | None = None,
                 previous: dict | None = None) -> ParsedIntent:
        """previous = {"intent", "missing_fields", "known": {...}} из прошлого хода (если он закончился уточнением)."""
        context = ""
        if previous and previous.get("missing_fields"):
            context = (f"\nПредыдущий вопрос к пользователю касался: {', '.join(previous['missing_fields'])}. "
                       f"Намерение было {previous['intent']}. Если новое сообщение отвечает на этот вопрос, "
                       f"верни то же intent и заполни только то, что в нём сказано.\n")
        system = SYSTEM_PROMPT.format(today=self.today().isoformat(), context=context,
                                      categories=", ".join(categories) if categories else "(список пуст)")
        last_err: Exception | None = None
        for _ in range(2):
            content = self._chat(system, text)
            try:
                parsed = to_parsed_intent(LLMIntent.model_validate_json(content))
                break
            except (ValidationError, ValueError, json.JSONDecodeError) as e:
                last_err = e
                log.warning("Ollama вернула невалидный разбор: %s", type(e).__name__)
        else:
            return ParsedIntent(intent="unknown")
        if parsed.intent == "unknown" and previous and previous.get("missing_fields"):
            # ответ вроде «Бишкек» без явного намерения продолжает прошлый диалог
            try:
                raw = LLMIntent.model_validate_json(content)
                raw.intent = previous["intent"]
                return to_parsed_intent(raw)
            except (ValidationError, ValueError):
                pass
        return parsed
