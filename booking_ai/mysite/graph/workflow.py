"""Граф LangGraph: классификация → агенты → валидация → операции БД → ответ.
Все побочные эффекты идут через `Deps` (серверные функции с проверкой прав);
LLM лишь классифицирует и извлекает параметры."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from mysite.graph.state import BookingState
from mysite.schemas.search import ParsedIntent
from mysite.services.moderation_service import ModerationService
from mysite.services.validation_service import validate_listing_data

REQUIRED = {"search": ["category", "city"], "subscribe": ["category", "city"],
            "create": ["category", "city", "price", "title"]}
MAX_SEARCH_RETRIES = 2


def _operation_text(op: dict) -> str:
    a = op.get("action")
    if a == "created":
        lid = (op.get("listing") or {}).get("id")
        return f"Создан черновик объявления №{lid}. Он станет видимым после публикации (POST /api/v1/listings/{lid}/publish)."
    if a == "updated":
        return f"Объявление №{(op.get('listing') or {}).get('id')} обновлено."
    if a == "deleted":
        return f"Объявление №{op.get('listing_id')} удалено."
    if a == "subscribed":
        return f"Подписка оформлена (№{(op.get('subscription') or {}).get('id')})."
    if a == "unsubscribed":
        return "Подписка отменена." if op.get("ok") else "Подписка не найдена."
    return f"Готово: {a}."


@dataclass
class Deps:
    classify: Callable[[str], ParsedIntent]                 # LLM (Ollama) → ParsedIntent
    search_web: Callable[[dict], list[dict]]                # SearchProvider
    search_db: Callable[[dict], list[dict]]
    get_listing: Callable[[int], dict | None]               # {id, owner_id, ...}
    create_listing: Callable[[int, dict], dict]
    update_listing: Callable[[int, dict], dict]
    delete_listing: Callable[[int], None]
    list_user_listings: Callable[[int], list[dict]]
    save_subscription: Callable[[int, str, dict], dict]
    remove_subscription: Callable[[int, int], bool]
    moderation: ModerationService | None = None
    today: Callable[[], date] = date.today


def build_graph(deps: Deps, checkpointer=None):
    def classify_node(s: BookingState) -> BookingState:
        p = deps.classify(s["user_query"])
        # данные прошлых сообщений переносим, только если прошлый ход закончился уточнением того же намерения;
        # иначе фильтры/черновик старого запроса «прилипали» бы к новому
        cont = bool(s.get("missing_fields")) and s.get("intent") == p.intent
        merged_filters = {**(s.get("search_filters", {}) if cont else {}),
                          **p.filters.model_dump(mode="json", exclude_none=True)}
        draft = {**(s.get("draft", {}) if cont else {}), **p.draft}
        changes = {**(s.get("changes", {}) if cont else {}), **p.changes}
        missing = [f for f in REQUIRED.get(p.intent, [])
                   if not (merged_filters.get(f) or draft.get(f))]
        listing_id = p.listing_id or (s.get("selected_listing_id") if cont else None)
        return {"intent": p.intent, "search_filters": merged_filters, "draft": draft, "changes": changes,
                "selected_listing_id": listing_id,
                "missing_fields": sorted(set(missing) | set(p.missing_fields)),
                "validation_errors": [], "warnings": [], "requires_confirmation": False,
                "search_results": [], "operation_result": None,
                "response": ""}  # с постоянным checkpointer ответ прошлого хода не должен «залипать»

    def route_after_classify(s: BookingState) -> str:
        intent = s["intent"]
        if intent == "unknown":
            return "respond"
        if intent != "search" and not s.get("user_id"):
            return "need_auth"
        if s.get("missing_fields"):
            return "clarify"
        return {"search": "search", "create": "validate_draft", "update": "check_owner",
                "delete": "check_owner", "subscribe": "save_subscription",
                "unsubscribe": "remove_subscription", "list_mine": "list_mine"}[intent]

    def need_auth(s):
        return {"response": "Для этого действия нужно войти в аккаунт и привязать этот канал."}

    def clarify(s):
        return {"response": "Уточните, пожалуйста: " + ", ".join(s["missing_fields"]) + "."}

    # --- Search Agent ---
    def search(s):
        filters, warnings, results = s["search_filters"], [], []
        for attempt in range(MAX_SEARCH_RETRIES):
            try:
                results = deps.search_web(filters)
                break
            except Exception:  # внешний сервис: ограниченный повтор
                if attempt == MAX_SEARCH_RETRIES - 1:
                    warnings.append("Поиск в интернете временно недоступен, показаны только локальные результаты.")
        local = deps.search_db(filters)
        return {"search_results": local + results, "warnings": warnings}

    def validate_results(s):
        seen, clean = set(), []
        for r in s["search_results"]:
            key = r.get("source_url") or ("id", r.get("id"))
            if key in seen:
                continue
            seen.add(key)
            if r.get("status") == "external" and not r.get("source_url"):
                continue  # внешний результат без первоисточника не показываем
            clean.append(r)
        return {"search_results": clean}

    # --- Listing Agent + Validation Agent ---
    def validate_draft(s):
        errs = validate_listing_data(s["draft"], today=deps.today(), moderation=deps.moderation)
        return {"validation_errors": errs}

    def after_validate(s):
        return "respond" if s["validation_errors"] else "save_listing"

    def save_listing(s):
        return {"operation_result": {"action": "created", "listing": deps.create_listing(s["user_id"], s["draft"])}}

    def check_owner(s):
        lid = s.get("selected_listing_id")
        if not lid:
            return {"validation_errors": ["Укажите ID объявления."]}
        listing = deps.get_listing(lid)
        if listing is None:
            return {"validation_errors": ["Объявление не найдено."]}
        if listing.get("owner_id") != s["user_id"]:
            return {"validation_errors": ["Нет прав на это объявление."]}  # LLM не может обойти
        if s["intent"] == "update":
            merged = {**listing, **s["changes"]}
            errs = validate_listing_data(merged, partial=True, today=None, moderation=deps.moderation)
            return {"validation_errors": errs}
        if not s.get("confirmed"):
            return {"requires_confirmation": True}
        return {}

    def after_owner(s):
        if s.get("validation_errors") or s.get("requires_confirmation"):
            return "respond"
        return "apply_update" if s["intent"] == "update" else "apply_delete"

    def apply_update(s):
        return {"operation_result": {"action": "updated",
                                     "listing": deps.update_listing(s["selected_listing_id"], s["changes"])}}

    def apply_delete(s):
        deps.delete_listing(s["selected_listing_id"])
        return {"operation_result": {"action": "deleted", "listing_id": s["selected_listing_id"]}}

    def list_mine(s):
        return {"search_results": deps.list_user_listings(s["user_id"])}

    # --- Notification Agent (подписки) ---
    def save_subscription(s):
        errs = []
        f = s["search_filters"]
        if f.get("price_min") and f.get("price_max") and float(f["price_min"]) > float(f["price_max"]):
            errs.append("Минимальная цена больше максимальной")
        if errs:
            return {"validation_errors": errs}
        sub = deps.save_subscription(s["user_id"], s.get("channel", "api"), f)
        return {"operation_result": {"action": "subscribed", "subscription": sub},
                "notification_targets": [{"user_id": s["user_id"], "channel": s.get("channel", "api")}]}

    def remove_subscription(s):
        ok = deps.remove_subscription(s["user_id"], s.get("selected_listing_id") or 0)
        return {"operation_result": {"action": "unsubscribed", "ok": ok}}

    def _response_text(s) -> dict:
        if s.get("response"):
            return {}
        if s.get("validation_errors"):
            return {"response": "Не удалось выполнить: " + "; ".join(s["validation_errors"])}
        if s.get("requires_confirmation"):
            return {"response": f"Подтвердите удаление объявления №{s['selected_listing_id']} (да/нет)."}
        if s.get("intent") == "unknown":
            return {"response": "Не понял запрос. Могу найти, добавить, изменить, удалить объявление или оформить подписку."}
        op = s.get("operation_result")
        if op:
            return {"response": _operation_text(op)}
        n = len(s.get("search_results", []))
        msg = f"Найдено результатов: {n}." if n else "Ничего не найдено."
        return {"response": " ".join([msg, *s.get("warnings", [])])}

    def respond(s):
        # подтверждение одноразовое: сбрасываем, чтобы оно не действовало на следующие сообщения диалога
        return {**_response_text(s), "confirmed": False}

    g = StateGraph(BookingState)
    for name, fn in [("classify", classify_node), ("need_auth", need_auth), ("clarify", clarify),
                     ("search", search), ("validate_results", validate_results),
                     ("validate_draft", validate_draft), ("save_listing", save_listing),
                     ("check_owner", check_owner), ("apply_update", apply_update),
                     ("apply_delete", apply_delete), ("list_mine", list_mine),
                     ("save_subscription", save_subscription), ("remove_subscription", remove_subscription),
                     ("respond", respond)]:
        g.add_node(name, fn)
    g.add_edge(START, "classify")
    g.add_conditional_edges("classify", route_after_classify, {k: k for k in [
        "respond", "need_auth", "clarify", "search", "validate_draft", "check_owner",
        "save_subscription", "remove_subscription", "list_mine"]})
    g.add_edge("search", "validate_results")
    g.add_edge("validate_results", "respond")
    g.add_conditional_edges("validate_draft", after_validate, {"respond": "respond", "save_listing": "save_listing"})
    g.add_conditional_edges("check_owner", after_owner, {"respond": "respond", "apply_update": "apply_update",
                                                          "apply_delete": "apply_delete"})
    for n in ("need_auth", "clarify", "save_listing", "apply_update", "apply_delete", "list_mine",
              "save_subscription", "remove_subscription"):
        g.add_edge(n, "respond")
    g.add_edge("respond", END)
    # Для продакшена передайте AsyncPostgresSaver (langgraph-checkpoint-postgres); MemorySaver — только dev/тесты.
    return g.compile(checkpointer=checkpointer or MemorySaver())
