"""Связка «текст пользователя → граф LangGraph → БД». Один код для REST (/ai/chat) и каналов (Telegram).

Граф синхронный, а БД асинхронная, поэтому граф запускается в рабочем потоке (`asyncio.to_thread`),
а его обращения к БД возвращаются в основной цикл событий через `run_coroutine_threadsafe`.
Права проверяются в графе и сервисах, не в LLM; удаление требует отдельного подтверждения «да»."""
from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any, Callable

from langgraph.checkpoint.memory import MemorySaver
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.database.ai_models import Category, City, Country, Listing, SearchSubscription, User
from mysite.graph.workflow import Deps, build_graph
from mysite.schemas.ai import ChatOut
from mysite.schemas.listing import ListingCreate, ListingOut, ListingUpdate
from mysite.schemas.search import ParsedIntent, SearchFilters
from mysite.schemas.subscription import SubscriptionOut
from mysite.services import listing_service as svc
from mysite.services.errors import ServiceError
from mysite.services.moderation_service import ModerationService

YES = {"да", "yes", "y", "ага", "подтверждаю", "ок", "ok", "удаляй", "удалить", "конечно"}
NO = {"нет", "no", "n", "отмена", "отмени", "отменить", "не", "стоп"}


def parse_yes_no(text: str) -> bool | None:
    words = re.findall(r"[\w]+", text.lower())
    if not words or len(words) > 4:
        return None
    if words[0] in YES:
        return True
    if words[0] in NO:
        return False
    return None


def _out(listing) -> dict:
    return ListingOut.model_validate(listing).model_dump(mode="json")


def _pydantic_errors(e: ValidationError) -> list[str]:
    return [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()]


class SessionBridge:
    """Deps для графа поверх одной AsyncSession. Методы синхронные и вызываются из рабочего потока."""

    def __init__(self, session: AsyncSession, loop: asyncio.AbstractEventLoop, user: User | None,
                 moderation: ModerationService | None, classify: Callable[[str], ParsedIntent],
                 search_web: Callable[[dict], list[dict]]):
        self.session, self.loop, self.user, self.moderation = session, loop, user, moderation
        self._classify, self._search_web = classify, search_web

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=120)

    def deps(self) -> Deps:
        return Deps(
            classify=self._classify, search_web=self._search_web,
            search_db=lambda f: self._run(self._search_db(f)),
            get_listing=lambda i: self._run(self._get_listing(i)),
            create_listing=lambda uid, d: self._run(self._create(uid, d)),
            update_listing=lambda i, c: self._run(self._update(i, c)),
            delete_listing=lambda i: self._run(self._delete(i)),
            list_user_listings=lambda uid: self._run(self._mine(uid)),
            save_subscription=lambda uid, ch, f: self._run(self._subscribe(uid, ch, f)),
            remove_subscription=lambda uid, sid: self._run(self._unsubscribe(uid, sid)),
            moderation=self.moderation)

    # --- справочники: названия из речи → id ---
    async def _category_id(self, slug: str) -> int:
        cid = await self.session.scalar(select(Category.id).where(Category.slug == slug, Category.is_active.is_(True)))
        if cid is None:
            raise ServiceError(422, f"Категория не найдена: {slug}")
        return cid

    async def _city_and_country(self, name: str, country: str | None) -> tuple[int, int]:
        q = select(City.id, City.country_id).where(func.lower(City.name) == func.lower(name), City.is_active.is_(True))
        if country:
            q = q.join(Country, Country.id == City.country_id).where(func.lower(Country.name) == func.lower(country))
        rows = (await self.session.execute(q)).all()
        if not rows:
            raise ServiceError(422, f"Город не найден: {name}")
        if len(rows) > 1:
            raise ServiceError(422, f"Город «{name}» есть в нескольких странах, укажите страну")
        return rows[0][0], rows[0][1]

    async def _map_fields(self, data: dict) -> dict:
        d = dict(data)
        if "category" in d:
            d["category_id"] = await self._category_id(str(d.pop("category")))
        if "city" in d:
            d["city_id"], d["country_id"] = await self._city_and_country(str(d.pop("city")), d.get("country"))
        d.pop("country", None)
        return d

    # --- операции ---
    async def _search_db(self, f: dict) -> list[dict]:
        items, _ = await svc.search_listings(self.session, SearchFilters.model_validate(f), limit=10)
        return [_out(i) for i in items]

    async def _get_listing(self, listing_id: int) -> dict | None:
        listing = await self.session.get(Listing, listing_id)
        # чужие неопубликованные объявления для ассистента «не существуют» (как в REST: ensure_visible)
        if listing is None or not (listing.status in svc.PUBLIC_STATUSES
                                   or (self.user is not None and listing.owner_id == self.user.id)):
            return None
        return svc.listing_to_dict(listing)

    async def _create(self, user_id: int, draft: dict) -> dict:
        user = await self.session.get(User, user_id)
        if user is None or not user.is_active:
            raise ServiceError(401, "Пользователь недоступен")
        try:
            data = ListingCreate.model_validate(await self._map_fields(draft)).model_dump()
        except ValidationError as e:
            raise ServiceError(422, "Объявление не прошло проверку", _pydantic_errors(e))
        return _out(await svc.create_listing(self.session, user, data, self.moderation))

    async def _update(self, listing_id: int, changes: dict) -> dict:
        listing = await svc.get_listing_or_404(self.session, listing_id)
        try:
            data = ListingUpdate.model_validate(await self._map_fields(changes)).model_dump(exclude_unset=True)
        except ValidationError as e:
            raise ServiceError(422, "Изменения не прошли проверку", _pydantic_errors(e))
        return _out(await svc.update_listing(self.session, listing, data, self.moderation))

    async def _delete(self, listing_id: int) -> None:
        await svc.delete_listing(self.session, await svc.get_listing_or_404(self.session, listing_id))

    async def _mine(self, user_id: int) -> list[dict]:
        items = await svc.query_listings(self.session, owner_id=user_id, statuses=None, limit=20)
        return [_out(i) for i in items]

    async def _subscribe(self, user_id: int, channel: str, f: dict) -> dict:
        try:
            clean = SearchFilters.model_validate(f)
        except ValidationError as e:
            raise ServiceError(422, "Некорректные фильтры подписки", _pydantic_errors(e))
        if (clean.price_min is not None or clean.price_max is not None) and not clean.currency:
            raise ServiceError(422, "Укажите валюту для фильтра по цене")
        sub = SearchSubscription(user_id=user_id, channel=channel,
                                 price_min=clean.price_min, price_max=clean.price_max, currency=clean.currency,
                                 filters_json=clean.model_dump(mode="json", exclude_none=True))
        if clean.category:
            sub.category_id = await self._category_id(clean.category)
        if clean.city:
            sub.city_id, sub.country_id = await self._city_and_country(clean.city, clean.country)
        self.session.add(sub)
        await self.session.commit()
        return SubscriptionOut.model_validate(sub).model_dump(mode="json")

    async def _unsubscribe(self, user_id: int, sub_id: int) -> bool:
        sub = await self.session.get(SearchSubscription, sub_id)
        if sub is None or sub.user_id != user_id:
            return False  # чужие подписки не раскрываем
        await self.session.delete(sub)
        await self.session.commit()
        return True


class AIRuntime:
    def __init__(self, classifier, moderation: ModerationService | None = None, *,
                 search_web: Callable[[dict], list[dict]] | None = None, pending_ttl: int = 300,
                 checkpointer=None, clock: Callable[[], float] = time.monotonic):
        self.classifier, self.moderation = classifier, moderation
        self.search_web = search_web or (lambda f: [])   # поиск в интернете подключается на этапе 4
        self.pending_ttl, self.clock = pending_ttl, clock
        self.checkpointer = checkpointer or MemorySaver()  # dev: в памяти процесса; для продакшена нужен Postgres
        self._pending: dict[str, tuple[float, dict]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ожидающее подтверждение хранится на стороне сервера: LLM и клиент не могут «подтвердить» чужое действие
    def _take_pending(self, key: str) -> dict | None:
        item = self._pending.pop(key, None)
        if item and self.clock() - item[0] <= self.pending_ttl:
            return item[1]
        return None

    async def handle(self, session: AsyncSession, user: User | None, channel: str, conversation_id: str | None,
                     text: str) -> ChatOut:
        conv = conversation_id or uuid.uuid4().hex
        key = f"{channel}:{user.id if user else 'anon'}:{conv}"
        async with self._locks.setdefault(key, asyncio.Lock()):
            pending = self._take_pending(key)
            override: ParsedIntent | None = None
            confirmed = False
            if pending:
                decision = parse_yes_no(text)
                if decision is False:
                    return ChatOut(conversation_id=conv, response="Хорошо, отменил.", intent=pending["intent"])
                if decision is True:
                    override = ParsedIntent(intent=pending["intent"], listing_id=pending["listing_id"])
                    confirmed = True
            try:
                state = await self._run_graph(session, user, channel, key, text, override, confirmed)
            except ServiceError as e:
                await session.rollback()
                detail = "; ".join(e.errors) if e.errors else e.message
                return ChatOut(conversation_id=conv, response=f"Не удалось выполнить: {detail}", intent="unknown")
            if state.get("requires_confirmation") and state.get("selected_listing_id"):
                self._pending[key] = (self.clock(), {"intent": state["intent"],
                                                     "listing_id": state["selected_listing_id"]})
            return self._to_out(conv, state)

    async def _run_graph(self, session, user, channel, key, text, override, confirmed) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        catalog = list((await session.execute(select(Category.slug).where(Category.is_active.is_(True)))).scalars())
        holder: dict[str, Any] = {}

        def classify(q: str) -> ParsedIntent:
            if override is not None:
                return override
            return self.classifier.classify(q, categories=catalog, previous=holder.get("previous"))

        bridge = SessionBridge(session, loop, user, self.moderation, classify, self.search_web)
        cfg = {"configurable": {"thread_id": key}}

        def work() -> dict[str, Any]:
            graph = build_graph(bridge.deps(), self.checkpointer)
            snap = graph.get_state(cfg)
            prev = (snap.values if snap is not None else None) or {}
            holder["previous"] = {"intent": prev.get("intent"), "missing_fields": prev.get("missing_fields") or []}
            inp: dict[str, Any] = {"user_query": text, "user_id": user.id if user else None,
                                   "channel": channel, "conversation_id": key}
            if confirmed:
                inp["confirmed"] = True
            return graph.invoke(inp, cfg)

        return await asyncio.to_thread(work)

    @staticmethod
    def _to_out(conv: str, s: dict[str, Any]) -> ChatOut:
        return ChatOut(conversation_id=conv, response=s.get("response") or "", intent=s.get("intent", "unknown"),
                       requires_confirmation=bool(s.get("requires_confirmation")),
                       missing_fields=s.get("missing_fields") or [], warnings=s.get("warnings") or [],
                       results=s.get("search_results") or [], operation=s.get("operation_result"))


def build_runtime(settings, moderation: ModerationService | None = None) -> AIRuntime:
    from mysite.clients.ollama import OllamaClassifier
    classifier = OllamaClassifier(settings.ollama_base_url, settings.ollama_model,
                                  timeout=settings.ollama_timeout_seconds)
    return AIRuntime(classifier, moderation, pending_ttl=settings.ai_pending_ttl_seconds)
