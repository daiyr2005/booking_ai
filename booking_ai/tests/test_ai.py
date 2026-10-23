"""Тесты этапа 3: классификатор Ollama (мок HTTP), чат /ai/chat, Telegram. Без сети, токенов и реальной Ollama."""
import json

import httpx
import pytest
from sqlalchemy import select

from mysite.clients.ollama import LLMIntent, OllamaClassifier, OllamaUnavailable, to_parsed_intent
from mysite.database.ai_models import ChannelAccount, Listing, SearchSubscription
from mysite.integrations.telegram import TelegramClient, TelegramError, format_reply, handle_update
from mysite.schemas.ai import ChatOut
from mysite.schemas.search import ParsedIntent, SearchFilters
from mysite.services.ai_chat import AIRuntime, parse_yes_no
from mysite.services.moderation_service import ModerationService
from tests.conftest import listing_payload

API = "/api/v1"


# ---------- разбор ответа модели ----------
def test_to_parsed_intent_search_price_goes_to_max_and_zero_is_missing():
    p = to_parsed_intent(LLMIntent(intent="search", category="hotel", city="Бишкек", price=5000, currency="KGS",
                                   rooms=0, district="null"))
    assert p.intent == "search" and p.filters.price_max == 5000 and p.filters.rooms is None
    assert p.filters.district is None and p.filters.currency == "KGS"


def test_to_parsed_intent_create_and_update_and_delete():
    c = to_parsed_intent(LLMIntent(intent="create", category="apartment_rent", city="Бишкек", price=100, title="Т"))
    assert c.draft == {"category": "apartment_rent", "city": "Бишкек", "price": 100.0, "title": "Т"}
    u = to_parsed_intent(LLMIntent(intent="update", listing_id=3, price=50, category="hotel"))
    assert u.changes == {"price": 50.0} and u.listing_id == 3      # категорию через ассистента не меняем
    assert to_parsed_intent(LLMIntent(intent="delete", listing_id=9)).listing_id == 9
    assert to_parsed_intent(LLMIntent(intent="unknown")).intent == "unknown"


def test_to_parsed_intent_contradiction_raises():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        to_parsed_intent(LLMIntent(intent="search", price_min=10, price_max=5))


def _ollama(handler):
    return OllamaClassifier("http://ollama.test", "llama3.2:latest", transport=httpx.MockTransport(handler))


def _reply(obj):
    return httpx.Response(200, json={"message": {"content": obj if isinstance(obj, str) else json.dumps(obj)}})


def test_classifier_sends_schema_model_and_context():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["url"] = str(req.url)
        return _reply({"intent": "search", "category": "hotel", "city": "Бишкек"})

    out = _ollama(handler).classify("найди отель", categories=["hotel", "apartment_rent"])
    b = seen["body"]
    assert seen["url"] == "http://ollama.test/api/chat" and b["model"] == "llama3.2:latest" and b["stream"] is False
    assert b["format"]["type"] == "object" and "intent" in b["format"]["properties"]
    assert "hotel, apartment_rent" in b["messages"][0]["content"] and b["options"]["temperature"] == 0
    assert out.filters.category == "hotel"


def test_classifier_retries_once_then_unknown():
    calls = []

    def handler(req):
        calls.append(1)
        return _reply("это не json")

    assert _ollama(handler).classify("???").intent == "unknown" and len(calls) == 2


def test_classifier_recovers_on_second_try():
    answers = iter(["мусор", {"intent": "list_mine"}])
    assert _ollama(lambda r: _reply(next(answers))).classify("мои").intent == "list_mine"


def test_classifier_ollama_down_raises():
    def handler(req):
        raise httpx.ConnectError("refused")

    with pytest.raises(OllamaUnavailable):
        _ollama(handler).classify("привет")
    with pytest.raises(OllamaUnavailable):
        _ollama(lambda r: httpx.Response(500, json={"error": "x"})).classify("привет")


def test_classifier_continues_previous_intent_for_bare_answer():
    c = _ollama(lambda r: _reply({"intent": "unknown", "city": "Бишкек"}))
    out = c.classify("Бишкек", previous={"intent": "search", "missing_fields": ["city"]})
    assert out.intent == "search" and out.filters.city == "Бишкек"


@pytest.mark.parametrize("text,expected", [("да", True), ("Да, удалить!", True), ("нет", False), ("Отмена", False),
                                           ("да вообще-то я хотел другое сказать тебе", None), ("найди отель", None)])
def test_parse_yes_no(text, expected):
    assert parse_yes_no(text) is expected


# ---------- /ai/chat ----------
class FakeClassifier:
    def __init__(self, *intents):
        self.queue, self.calls = list(intents), []

    def classify(self, text, *, categories=None, previous=None):
        self.calls.append({"text": text, "categories": categories, "previous": previous})
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def use_ai(ctx, *intents) -> FakeClassifier:
    fake = FakeClassifier(*intents)
    ctx.app.state.ai = AIRuntime(fake, ModerationService())
    return fake


async def chat(ctx, message, headers=None, conversation_id="c1"):
    return await ctx.client.post(f"{API}/ai/chat", json={"message": message, "conversation_id": conversation_id},
                                 headers=headers or {})


async def make_active_listing(ctx, h, ids, **over):
    r = await ctx.client.post(f"{API}/listings/", json=listing_payload(ids, **over), headers=h)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    assert (await ctx.client.post(f"{API}/listings/{lid}/publish", headers=h)).status_code == 200
    return lid


async def test_chat_anonymous_search_finds_published_listing(ctx):
    ids = await ctx.seed_catalog()
    lid = await make_active_listing(ctx, await ctx.register("a@example.com"), ids)
    fake = use_ai(ctx, ParsedIntent(intent="search", filters=SearchFilters(category="apartment_rent", city="Бишкек")))
    r = await chat(ctx, "найди квартиру в Бишкеке")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [x["id"] for x in body["results"]] == [lid] and "1" in body["response"]
    assert "apartment_rent" in fake.calls[0]["categories"]


async def test_chat_search_unknown_city_returns_nothing(ctx):
    ids = await ctx.seed_catalog()
    await make_active_listing(ctx, await ctx.register("a@example.com"), ids)
    use_ai(ctx, ParsedIntent(intent="search", filters=SearchFilters(category="apartment_rent", city="Атлантида")))
    assert (await chat(ctx, "q")).json()["results"] == []


async def test_chat_clarification_then_answer_merges_filters(ctx):
    ids = await ctx.seed_catalog()
    lid = await make_active_listing(ctx, await ctx.register("a@example.com"), ids)
    fake = use_ai(ctx,
                  ParsedIntent(intent="search", filters=SearchFilters(category="apartment_rent")),
                  ParsedIntent(intent="search", filters=SearchFilters(city="Бишкек")))
    first = (await chat(ctx, "квартира")).json()
    assert first["missing_fields"] == ["city"] and "Уточните" in first["response"]
    second = (await chat(ctx, "Бишкек")).json()
    assert [x["id"] for x in second["results"]] == [lid]
    assert fake.calls[1]["previous"]["missing_fields"] == ["city"]


async def test_chat_old_filters_do_not_stick_to_new_request(ctx):
    ids = await ctx.seed_catalog()
    await make_active_listing(ctx, await ctx.register("a@example.com"), ids, price="25000")
    use_ai(ctx,
           ParsedIntent(intent="search", filters=SearchFilters(category="apartment_rent", city="Бишкек", price_max=1)),
           ParsedIntent(intent="search", filters=SearchFilters(category="apartment_rent", city="Бишкек")))
    assert (await chat(ctx, "дёшево")).json()["results"] == []
    assert len((await chat(ctx, "любые")).json()["results"]) == 1   # price_max=1 из прошлого запроса не прилип


async def test_chat_create_requires_login(ctx):
    await ctx.seed_catalog()
    use_ai(ctx, ParsedIntent(intent="create", draft={"category": "apartment_rent", "city": "Бишкек", "price": 1, "title": "Т"}))
    r = await chat(ctx, "добавь")
    assert "войти" in r.json()["response"]


DRAFT = {"category": "apartment_rent", "city": "Бишкек", "price": 25000, "currency": "KGS", "price_period": "month",
         "rooms": 2, "title": "Квартира у парка"}


async def test_chat_create_makes_draft_owned_by_user(ctx):
    await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    use_ai(ctx, ParsedIntent(intent="create", draft=DRAFT))
    body = (await chat(ctx, "сдаю квартиру", h)).json()
    assert body["operation"]["action"] == "created"
    lst = body["operation"]["listing"]
    assert lst["status"] == "draft" and lst["category_id"] and lst["city_id"] and lst["country_id"]
    me = (await ctx.client.get(f"{API}/users/me", headers=h)).json()
    assert lst["owner_id"] == me["id"]


async def test_chat_create_asks_for_missing_title(ctx):
    await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    use_ai(ctx, ParsedIntent(intent="create", draft={k: v for k, v in DRAFT.items() if k != "title"}))
    body = (await chat(ctx, "сдаю", h)).json()
    assert body["missing_fields"] == ["title"] and body["operation"] is None


async def test_chat_create_unknown_city_is_reported_not_crashed(ctx):
    await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    use_ai(ctx, ParsedIntent(intent="create", draft={**DRAFT, "city": "Атлантида"}))
    r = await chat(ctx, "сдаю", h)
    assert r.status_code == 200 and "Город не найден" in r.json()["response"]


async def test_chat_create_duplicate_is_reported(ctx):
    await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    use_ai(ctx, ParsedIntent(intent="create", draft=DRAFT), ParsedIntent(intent="create", draft=DRAFT))
    await chat(ctx, "1", h)
    body = (await chat(ctx, "2", h)).json()
    assert body["operation"] is None and "Не удалось выполнить" in body["response"]
    async with ctx.maker() as s:
        assert len((await s.execute(select(Listing))).scalars().all()) == 1


async def test_chat_blocked_word_rejected(ctx):
    await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    ctx.app.state.ai = AIRuntime(FakeClassifier(ParsedIntent(intent="create", draft={**DRAFT, "title": "плохое"})),
                                 ModerationService(ModerationService.parse_lines(["плохое"])))
    body = (await chat(ctx, "x", h)).json()
    assert body["operation"] is None and body["response"].startswith("Не удалось")


async def test_chat_update_own_listing(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    lid = await make_active_listing(ctx, h, ids)
    use_ai(ctx, ParsedIntent(intent="update", listing_id=lid, changes={"price": 30000}))
    assert (await chat(ctx, "цена 30000", h)).json()["operation"]["action"] == "updated"
    assert float((await ctx.client.get(f"{API}/listings/{lid}", headers=h)).json()["price"]) == 30000


async def test_chat_cannot_touch_foreign_listing(ctx):
    ids = await ctx.seed_catalog()
    owner = await ctx.register("a@example.com")
    other = await ctx.register("b@example.com")
    lid = await make_active_listing(ctx, owner, ids)
    use_ai(ctx, ParsedIntent(intent="update", listing_id=lid, changes={"price": 1}),
           ParsedIntent(intent="delete", listing_id=lid))
    assert (await chat(ctx, "x", other)).json()["operation"] is None
    assert (await chat(ctx, "y", other)).json()["operation"] is None
    assert (await ctx.client.get(f"{API}/listings/{lid}", headers=owner)).status_code == 200


async def test_chat_foreign_draft_looks_nonexistent(ctx):
    ids = await ctx.seed_catalog()
    owner, other = await ctx.register("a@example.com"), await ctx.register("b@example.com")
    r = await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=owner)
    use_ai(ctx, ParsedIntent(intent="delete", listing_id=r.json()["id"]))
    assert "не найдено" in (await chat(ctx, "x", other)).json()["response"]


async def test_chat_delete_needs_yes(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    lid = await make_active_listing(ctx, h, ids)
    fake = use_ai(ctx, ParsedIntent(intent="delete", listing_id=lid))
    first = (await chat(ctx, "удали", h)).json()
    assert first["requires_confirmation"] and (await ctx.client.get(f"{API}/listings/{lid}", headers=h)).status_code == 200
    second = (await chat(ctx, "да", h)).json()
    assert second["operation"]["action"] == "deleted" and len(fake.calls) == 1   # «да» не отправляется в LLM
    assert (await ctx.client.get(f"{API}/listings/{lid}", headers=h)).status_code == 404


async def test_chat_delete_cancelled_by_no_and_by_other_message(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    lid = await make_active_listing(ctx, h, ids)
    use_ai(ctx, ParsedIntent(intent="delete", listing_id=lid), ParsedIntent(intent="delete", listing_id=lid),
           ParsedIntent(intent="unknown"), ParsedIntent(intent="unknown"))
    await chat(ctx, "удали", h)
    assert "отменил" in (await chat(ctx, "нет", h)).json()["response"]
    await chat(ctx, "удали", h)                       # ожидание подтверждения заново
    await chat(ctx, "расскажи анекдот", h)            # другое сообщение сбрасывает ожидание
    assert (await chat(ctx, "да", h)).json()["operation"] is None   # «да» вне контекста ничего не удаляет
    assert (await ctx.client.get(f"{API}/listings/{lid}", headers=h)).status_code == 200


async def test_chat_confirmation_expires(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    lid = await make_active_listing(ctx, h, ids)
    now = [0.0]
    ctx.app.state.ai = AIRuntime(FakeClassifier(ParsedIntent(intent="delete", listing_id=lid), ParsedIntent(intent="unknown")),
                                 ModerationService(), pending_ttl=60, clock=lambda: now[0])
    await chat(ctx, "удали", h)
    now[0] = 61
    assert (await chat(ctx, "да", h)).json()["operation"] is None
    assert (await ctx.client.get(f"{API}/listings/{lid}", headers=h)).status_code == 200


async def test_chat_confirmation_not_shared_between_users_or_conversations(ctx):
    ids = await ctx.seed_catalog()
    a, b = await ctx.register("a@example.com"), await ctx.register("b@example.com")
    lid = await make_active_listing(ctx, a, ids)
    use_ai(ctx, ParsedIntent(intent="delete", listing_id=lid), ParsedIntent(intent="unknown"),
           ParsedIntent(intent="unknown"))
    await chat(ctx, "удали", a, conversation_id="c1")
    await chat(ctx, "да", b, conversation_id="c1")      # другой пользователь, тот же conversation_id
    await chat(ctx, "да", a, conversation_id="other")   # тот же пользователь, другой диалог
    assert (await ctx.client.get(f"{API}/listings/{lid}", headers=a)).status_code == 200


async def test_chat_list_mine_and_subscribe_and_unsubscribe(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    lid = await make_active_listing(ctx, h, ids)
    use_ai(ctx, ParsedIntent(intent="list_mine"),
           ParsedIntent(intent="subscribe", filters=SearchFilters(category="apartment_rent", city="Бишкек",
                                                                  price_max=30000, currency="KGS")))
    assert [x["id"] for x in (await chat(ctx, "мои", h)).json()["results"]] == [lid]
    sub = (await chat(ctx, "сообщай", h)).json()["operation"]
    assert sub["action"] == "subscribed"
    subs = (await ctx.client.get(f"{API}/subscriptions/", headers=h)).json()
    assert len(subs) == 1 and subs[0]["city_id"] == ids["bishkek"] and subs[0]["channel"] == "api"
    use_ai(ctx, ParsedIntent(intent="unsubscribe", listing_id=subs[0]["id"]))
    assert (await chat(ctx, "отпиши", h)).json()["operation"]["ok"] is True
    assert (await ctx.client.get(f"{API}/subscriptions/", headers=h)).json() == []


async def test_chat_subscribe_price_without_currency_rejected(ctx):
    await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    use_ai(ctx, ParsedIntent(intent="subscribe", filters=SearchFilters(category="apartment_rent", city="Бишкек", price_max=1)))
    assert "валюту" in (await chat(ctx, "x", h)).json()["response"]


async def test_chat_cannot_unsubscribe_foreign_subscription(ctx):
    await ctx.seed_catalog()
    a, b = await ctx.register("a@example.com"), await ctx.register("b@example.com")
    use_ai(ctx, ParsedIntent(intent="subscribe", filters=SearchFilters(category="apartment_rent", city="Бишкек")))
    await chat(ctx, "x", a)
    sid = (await ctx.client.get(f"{API}/subscriptions/", headers=a)).json()[0]["id"]
    use_ai(ctx, ParsedIntent(intent="unsubscribe", listing_id=sid))
    assert (await chat(ctx, "y", b)).json()["operation"]["ok"] is False
    assert len((await ctx.client.get(f"{API}/subscriptions/", headers=a)).json()) == 1


async def test_chat_ollama_down_is_503_and_invalid_input_422(ctx):
    use_ai(ctx, OllamaUnavailable("down"))
    r = await chat(ctx, "привет")
    assert r.status_code == 503 and "Ollama" in r.json()["detail"]
    assert (await ctx.client.post(f"{API}/ai/chat", json={"message": ""})).status_code == 422
    assert (await ctx.client.post(f"{API}/ai/chat", json={"message": "x", "conversation_id": "bad id!"})).status_code == 422
    assert (await ctx.client.post(f"{API}/ai/chat", json={"message": "x", "confirmed": True})).status_code == 422


async def test_chat_without_conversation_id_generates_one(ctx):
    use_ai(ctx, ParsedIntent(intent="unknown"))
    r = await ctx.client.post(f"{API}/ai/chat", json={"message": "привет"})
    assert r.status_code == 200 and r.json()["conversation_id"]


# ---------- привязка канала ----------
async def test_link_code_requires_auth_and_is_short_and_telegram_safe(ctx):
    assert (await ctx.client.post(f"{API}/channels/telegram/link")).status_code == 401
    h = await ctx.register("a@example.com")
    r = await ctx.client.post(f"{API}/channels/telegram/link", headers=h)
    assert r.status_code == 201
    code = r.json()["code"]
    import re
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,64}", code) and code in r.json()["instruction"]


async def test_link_code_is_not_a_login_token(ctx):
    h = await ctx.register("a@example.com")
    code = (await ctx.client.post(f"{API}/channels/telegram/link", headers=h)).json()["code"]
    r = await ctx.client.get(f"{API}/users/me", headers={"Authorization": f"Bearer {code}"})
    assert r.status_code == 401


# ---------- Telegram ----------
class TgBox:
    def __init__(self):
        self.sent = []
        self.client = TelegramClient("TEST:TOKEN", transport=httpx.MockTransport(self._handler))

    def _handler(self, req):
        body = json.loads(req.content)
        self.sent.append(body["text"])
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


def upd(text, *, mid=1, chat=555, sender=555, chat_type="private", is_bot=False):
    return {"update_id": mid, "message": {"message_id": mid, "chat": {"id": chat, "type": chat_type},
                                          "from": {"id": sender, "is_bot": is_bot}, "text": text}}


async def tg(ctx, box, runtime, update):
    await handle_update(update, runtime=runtime, client=box.client, maker=ctx.maker)


async def test_telegram_unlinked_user_can_search_but_not_create(ctx):
    ids = await ctx.seed_catalog()
    await make_active_listing(ctx, await ctx.register("a@example.com"), ids, title="Квартира у парка")
    box = TgBox()
    rt = AIRuntime(FakeClassifier(
        ParsedIntent(intent="search", filters=SearchFilters(category="apartment_rent", city="Бишкек")),
        ParsedIntent(intent="create", draft=DRAFT)), ModerationService())
    await tg(ctx, box, rt, upd("найди", mid=1))
    assert "Квартира у парка" in box.sent[0] and "25000" in box.sent[0]
    await tg(ctx, box, rt, upd("добавь", mid=2))
    assert "войти" in box.sent[1]


async def test_telegram_link_then_act_as_user_and_dedup(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    lid = await make_active_listing(ctx, h, ids)
    code = (await ctx.client.post(f"{API}/channels/telegram/link", headers=h)).json()["code"]
    box = TgBox()
    rt = AIRuntime(FakeClassifier(ParsedIntent(intent="list_mine")), ModerationService())
    start = upd(f"/start {code}", mid=1)
    await tg(ctx, box, rt, start)
    assert "привязан" in box.sent[0]
    await tg(ctx, box, rt, start)                       # повторная доставка того же сообщения
    assert len(box.sent) == 1
    async with ctx.maker() as s:
        acc = (await s.execute(select(ChannelAccount))).scalar_one()
        assert acc.channel == "telegram" and acc.external_user_id == "555" and acc.is_verified
    await tg(ctx, box, rt, upd("мои объявления", mid=2))
    assert f"№{lid}" in box.sent[1] and "войти" not in box.sent[1]
    await tg(ctx, box, rt, upd("/start " + code, mid=3, chat=777, sender=777))   # код одноразовый
    assert "недействителен" in box.sent[2]
    await tg(ctx, box, rt, upd("/unlink", mid=4))
    assert "отвязан" in box.sent[3]
    async with ctx.maker() as s:
        assert (await s.execute(select(ChannelAccount))).scalars().all() == []


async def test_telegram_subscription_from_linked_chat_uses_telegram_channel(ctx):
    await ctx.seed_catalog()
    h = await ctx.register("a@example.com")
    code = (await ctx.client.post(f"{API}/channels/telegram/link", headers=h)).json()["code"]
    box = TgBox()
    rt = AIRuntime(FakeClassifier(ParsedIntent(
        intent="subscribe", filters=SearchFilters(category="apartment_rent", city="Бишкек"))), ModerationService())
    await tg(ctx, box, rt, upd(f"/start {code}", mid=1))
    await tg(ctx, box, rt, upd("сообщай", mid=2))
    async with ctx.maker() as s:
        sub = (await s.execute(select(SearchSubscription))).scalar_one()
        assert sub.channel == "telegram"


async def test_telegram_foreign_account_cannot_be_taken_over(ctx):
    await ctx.seed_catalog()
    a, b = await ctx.register("a@example.com"), await ctx.register("b@example.com")
    code_a = (await ctx.client.post(f"{API}/channels/telegram/link", headers=a)).json()["code"]
    code_b = (await ctx.client.post(f"{API}/channels/telegram/link", headers=b)).json()["code"]
    box, rt = TgBox(), AIRuntime(FakeClassifier(), ModerationService())
    await tg(ctx, box, rt, upd(f"/start {code_a}", mid=1))
    await tg(ctx, box, rt, upd(f"/start {code_b}", mid=2))     # тот же Telegram пытается привязаться к другому аккаунту
    assert "уже привязан" in box.sent[1]


async def test_telegram_bad_code_help_and_ignored_updates(ctx):
    box, rt = TgBox(), AIRuntime(FakeClassifier(), ModerationService())
    await tg(ctx, box, rt, upd("/start nonsense", mid=1))
    assert "недействителен" in box.sent[0]
    await tg(ctx, box, rt, upd("/start", mid=2))
    assert "объявлен" in box.sent[1]
    n = len(box.sent)
    await tg(ctx, box, rt, upd("привет", mid=3, chat_type="group", chat=-100))   # группы игнорируются
    await tg(ctx, box, rt, upd("привет", mid=4, is_bot=True))
    await tg(ctx, box, rt, {"update_id": 9, "edited_message": {}})
    await tg(ctx, box, rt, {"update_id": 10, "message": {"message_id": 1, "chat": {"id": 1, "type": "private"},
                                                         "from": {"id": 1}, "photo": []}})
    assert len(box.sent) == n


async def test_telegram_ollama_down_gives_neutral_reply(ctx):
    box = TgBox()
    rt = AIRuntime(FakeClassifier(OllamaUnavailable("down")), ModerationService())
    await tg(ctx, box, rt, upd("привет", mid=1))
    assert "недоступен" in box.sent[0] and "Ollama" not in box.sent[0]


async def test_telegram_unexpected_error_does_not_leak_details(ctx):
    box = TgBox()
    rt = AIRuntime(FakeClassifier(RuntimeError("secret internals")), ModerationService())
    await tg(ctx, box, rt, upd("привет", mid=1))
    assert "secret" not in box.sent[0] and "ещё раз" in box.sent[0]


async def test_telegram_client_error_never_contains_token():
    def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")

    c = TelegramClient("123:SECRET", transport=httpx.MockTransport(boom))
    with pytest.raises(TelegramError) as e:
        await c.send_message(1, "x")
    assert "SECRET" not in str(e.value)

    c2 = TelegramClient("123:SECRET", transport=httpx.MockTransport(
        lambda r: httpx.Response(401, json={"ok": False, "description": "Unauthorized"})))
    with pytest.raises(TelegramError, match="Unauthorized"):
        await c2.send_message(1, "x")


async def test_telegram_long_message_is_split():
    box = TgBox()
    await box.client.send_message(1, "я" * 9000)
    assert [len(x) for x in box.sent] == [4000, 4000, 1000]


def test_format_reply_limits_results_and_shows_source():
    out = ChatOut(conversation_id="c", response="Найдено результатов: 7.",
                  results=[{"id": i, "title": f"T{i}", "price": "10.00", "currency": "KGS",
                            "source_url": "https://s.kg/1" if i == 1 else None} for i in range(1, 8)])
    text = format_reply(out)
    assert text.count("№") == 5 and "https://s.kg/1" in text and "ещё 2" in text


async def test_webhook_disabled_without_secret(ctx):
    r = await ctx.client.post(f"{API}/telegram/webhook", json={"update_id": 1})
    assert r.status_code == 404
