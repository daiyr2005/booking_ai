from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from mysite.database.ai_models import (AIBase, Category, City, Country, Listing, Notification, SearchSubscription, User)
from mysite.graph.workflow import Deps, build_graph
from mysite.schemas.search import ParsedIntent, SearchFilters
from mysite.services.moderation_service import ModerationService
from mysite.services.validation_service import find_duplicates, is_safe_url, validate_listing_data

MOD = ModerationService.parse_lines(["запрещённое_слово_1", "плохое", "# comment", "", "  "])
MS = ModerationService(MOD)


# ---- модерация ----
def test_blocklist_parsing_ignores_blank_and_comments():
    assert len(MOD) == 2


@pytest.mark.parametrize("text", ["Тут ПЛОХОЕ слово", "п л о х о е", "пл\u200bохое", "плoхое", "запрещенное слово 1", "плохое!"])
def test_blocked_detected_despite_evasion(text):
    assert MS.check(text)


@pytest.mark.parametrize("text", ["неплохое жильё", "плохоеслово", "хорошая квартира"])
def test_no_substring_false_positive(text):
    assert not MS.check(text)


def test_file_reload(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("яд\n", encoding="utf-8")
    s = ModerationService(path=p)
    s.load_file()
    assert s.check("это яд")
    p.write_text("яд\nвор\n", encoding="utf-8")
    import os; os.utime(p, (1, 9999999999))
    assert s.reload_if_changed() and s.check("вор")


# ---- фильтры/схемы ----
def test_filters_price_contradiction():
    with pytest.raises(ValidationError):
        SearchFilters(price_min=50000, price_max=20000)


def test_filters_dates_contradiction():
    with pytest.raises(ValidationError):
        SearchFilters(check_in=date(2026, 11, 15), check_out=date(2026, 11, 10))


def test_currency_alias():
    assert SearchFilters(currency="сом").currency == "KGS"


# ---- валидация ----
def test_create_requires_fields():
    errs = validate_listing_data({"category": "apartment_rent"})
    assert any("city" in e for e in errs) and any("price" in e for e in errs)


def test_bad_dates_and_price_range():
    errs = validate_listing_data({"category": "c", "city": "x", "price": 1, "price_min": 5, "price_max": 2,
                                  "check_in": date(2026, 11, 15), "check_out": date(2026, 11, 10)})
    assert len(errs) == 2


def test_blocked_word_in_listing():
    errs = validate_listing_data({"category": "c", "city": "x", "price": 1, "title": "плохое"}, moderation=MS)
    assert any("не прошло проверку" in e for e in errs)


@pytest.mark.parametrize("url,ok", [("https://example.com/a", True), ("http://127.0.0.1/x", False),
                                    ("http://localhost/x", False), ("http://10.0.0.5", False),
                                    ("file:///etc/passwd", False), ("http://169.254.169.254/", False),
                                    ("https://u:p@example.com", False)])
def test_ssrf(url, ok):
    assert is_safe_url(url) is ok


def test_duplicates():
    a = {"source_url": "https://s.kg/1", "title": "A"}
    assert find_duplicates({"source_url": "https://s.kg/1", "title": "B"}, [a])
    assert not find_duplicates({"source_url": "https://s.kg/2", "title": "B"}, [a])


# ---- LangGraph ----
def make_deps(parsed: ParsedIntent, store=None, search_fail=False):
    store = store if store is not None else {}
    calls = {"web": 0, "deleted": []}

    def web(f):
        calls["web"] += 1
        if search_fail:
            raise RuntimeError("down")
        return [{"title": "X", "source_url": "https://s.kg/1", "status": "external"},
                {"title": "dup", "source_url": "https://s.kg/1", "status": "external"},
                {"title": "no source", "status": "external"}]
    d = Deps(classify=lambda q: parsed, search_web=web, search_db=lambda f: [],
             get_listing=lambda i: store.get(i),
             create_listing=lambda u, d_: {"id": 1, **d_}, update_listing=lambda i, c: {**store[i], **c},
             delete_listing=lambda i: calls["deleted"].append(i), list_user_listings=lambda u: [],
             save_subscription=lambda u, ch, f: {"id": 7}, remove_subscription=lambda u, i: True,
             moderation=MS, today=lambda: date(2026, 10, 23))
    return d, calls


def run(deps, **state):
    g = build_graph(deps)
    return g.invoke({"conversation_id": "c1", "channel": "telegram", **state},
                    {"configurable": {"thread_id": state.get("conversation_id", "c1")}})


def test_graph_search_dedup_and_drops_sourceless():
    d, _ = make_deps(ParsedIntent(intent="search", filters=SearchFilters(category="hotel", city="Бишкек")))
    out = run(d, user_query="q", user_id=None)
    assert len(out["search_results"]) == 1


def test_graph_search_missing_fields_asks_clarification():
    d, calls = make_deps(ParsedIntent(intent="search", filters=SearchFilters(city="Бишкек")))
    out = run(d, user_query="q")
    assert out["missing_fields"] == ["category"] and "Уточните" in out["response"] and calls["web"] == 0


def test_graph_search_provider_failure_retries_then_message():
    d, calls = make_deps(ParsedIntent(intent="search", filters=SearchFilters(category="h", city="c")), search_fail=True)
    out = run(d, user_query="q")
    assert calls["web"] == 2 and "недоступен" in out["response"]


def test_graph_create_requires_auth():
    d, _ = make_deps(ParsedIntent(intent="create", draft={"category": "a", "city": "b", "price": 1}))
    assert "войти" in run(d, user_query="q", user_id=None)["response"]


def test_graph_create_ok_and_invalid():
    ok = {"category": "a", "city": "b", "price": 1000, "title": "Квартира"}
    d, _ = make_deps(ParsedIntent(intent="create", draft=ok))
    assert run(d, user_query="q", user_id=1)["operation_result"]["action"] == "created"
    bad = {**ok, "title": "плохое"}
    d, _ = make_deps(ParsedIntent(intent="create", draft=bad))
    out = run(d, user_query="q", user_id=1)
    assert out["operation_result"] is None and out["validation_errors"]


def test_graph_update_foreign_listing_denied():
    d, _ = make_deps(ParsedIntent(intent="update", listing_id=5, changes={"price": 10}), store={5: {"id": 5, "owner_id": 2}})
    out = run(d, user_query="q", user_id=1)
    assert out["operation_result"] is None and "Нет прав" in out["response"]


def test_graph_update_own_listing_ok():
    d, _ = make_deps(ParsedIntent(intent="update", listing_id=5, changes={"price": 28000}), store={5: {"id": 5, "owner_id": 1}})
    assert run(d, user_query="q", user_id=1)["operation_result"]["listing"]["price"] == 28000


def test_graph_delete_needs_confirmation_then_deletes():
    p = ParsedIntent(intent="delete", listing_id=5)
    d, calls = make_deps(p, store={5: {"id": 5, "owner_id": 1}})
    out = run(d, user_query="q", user_id=1)
    assert out["requires_confirmation"] and calls["deleted"] == []
    out = run(d, user_query="да", user_id=1, confirmed=True)
    assert calls["deleted"] == [5]


def test_graph_delete_foreign_denied_even_if_confirmed():
    d, calls = make_deps(ParsedIntent(intent="delete", listing_id=5), store={5: {"id": 5, "owner_id": 2}})
    run(d, user_query="q", user_id=1, confirmed=True)
    assert calls["deleted"] == []


def test_graph_subscribe():
    d, _ = make_deps(ParsedIntent(intent="subscribe", filters=SearchFilters(category="a", city="b", price_max=30000)))
    out = run(d, user_query="q", user_id=1)
    assert out["operation_result"]["action"] == "subscribed" and out["notification_targets"]


# ---- БД ----
@pytest.fixture
async def session():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as c:
        await c.run_sync(AIBase.metadata.create_all)
    async with AsyncSession(eng, expire_on_commit=False) as s:
        yield s
    await eng.dispose()


async def _seed(s):
    u = User(full_name="A"); co = Country(name="КР", code="KG"); s.add_all([u, co]); await s.flush()
    ci = City(name="Бишкек", country_id=co.id); cat = Category(name="Квартиры", slug="apartment_rent"); s.add_all([ci, cat]); await s.flush()
    l = Listing(owner_id=u.id, category_id=cat.id, city_id=ci.id, title="t", price=Decimal("10"))
    sub = SearchSubscription(user_id=u.id, channel="telegram"); s.add_all([l, sub]); await s.flush()
    return u, l, sub


async def test_notification_dedup_unique(session):
    u, l, sub = await _seed(session)
    session.add(Notification(user_id=u.id, listing_id=l.id, subscription_id=sub.id, channel="telegram"))
    await session.commit()
    session.add(Notification(user_id=u.id, listing_id=l.id, subscription_id=sub.id, channel="telegram"))
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_same_city_name_in_different_countries_allowed(session):
    k, r = Country(name="КР", code="KG"), Country(name="РФ", code="RU")
    session.add_all([k, r]); await session.flush()
    session.add_all([City(name="Ош", country_id=k.id), City(name="Ош", country_id=r.id)])
    await session.commit()


async def test_listing_price_range_constraint(session):
    u, l, _ = await _seed(session)
    session.add(Listing(owner_id=u.id, category_id=1, title="x", price_min=Decimal(5), price_max=Decimal(2)))
    with pytest.raises(IntegrityError):
        await session.commit()
