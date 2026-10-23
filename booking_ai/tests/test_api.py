"""Интеграционные тесты REST API (SQLite in-memory, без сети и токенов)."""
from datetime import date, timedelta

from tests.conftest import PASSWORD, listing_payload

API = "/api/v1"


# ---------- авторизация ----------
async def test_register_login_me(ctx):
    h = await ctx.register("a@example.com")
    r = await ctx.client.get(f"{API}/users/me", headers=h)
    assert r.status_code == 200 and r.json()["email"] == "a@example.com" and r.json()["role"] == "user"
    assert "password" not in r.text


async def test_duplicate_email_and_wrong_password(ctx):
    await ctx.register("a@example.com")
    r = await ctx.client.post(f"{API}/auth/register",
                              json={"email": "A@Example.com", "password": PASSWORD, "full_name": "x"})
    assert r.status_code == 409
    r = await ctx.client.post(f"{API}/auth/login", json={"email": "a@example.com", "password": "wrong-password"})
    assert r.status_code == 401
    r = await ctx.client.post(f"{API}/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert r.status_code == 401


async def test_registration_cannot_set_role(ctx):
    r = await ctx.client.post(f"{API}/auth/register", json={
        "email": "b@example.com", "password": PASSWORD, "full_name": "b", "role": "admin"})
    assert r.status_code == 422


async def test_protected_routes_need_token(ctx):
    ids = await ctx.seed_catalog()
    assert (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids))).status_code == 401
    assert (await ctx.client.get(f"{API}/users/me")).status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert (await ctx.client.get(f"{API}/users/me", headers=bad)).status_code == 401


# ---------- объявления ----------
async def test_create_publish_and_visibility(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    r = await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=h)
    assert r.status_code == 201, r.text
    lst = r.json()
    assert lst["status"] == "draft" and lst["currency"] == "KGS" and lst["country_id"] == ids["kg"]
    # черновик не виден в публичном списке и анониму
    assert (await ctx.client.get(f"{API}/listings/")).json() == []
    assert (await ctx.client.get(f"{API}/listings/{lst['id']}")).status_code == 404
    r = await ctx.client.post(f"{API}/listings/{lst['id']}/publish", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "active" and r.json()["published_at"]
    assert len((await ctx.client.get(f"{API}/listings/")).json()) == 1
    assert (await ctx.client.get(f"{API}/listings/{lst['id']}")).status_code == 200
    r = await ctx.client.post(f"{API}/listings/{lst['id']}/deactivate", headers=h)
    assert r.json()["status"] == "inactive"
    assert (await ctx.client.get(f"{API}/listings/")).json() == []


async def test_my_listings(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=h)
    r = await ctx.client.get(f"{API}/listings/", params={"mine": True}, headers=h)
    assert len(r.json()) == 1
    assert (await ctx.client.get(f"{API}/listings/", params={"mine": True})).status_code == 401


async def test_owner_can_edit_only_changed_fields(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    lid = (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=h)).json()["id"]
    r = await ctx.client.patch(f"{API}/listings/{lid}", json={"price": "28000"}, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["price"] in ("28000", "28000.00") and body["title"] == "Квартира у парка" and body["rooms"] == 2


async def test_foreign_listing_cannot_be_changed_or_deleted(ctx):
    ids = await ctx.seed_catalog()
    owner = await ctx.register("owner@example.com")
    other = await ctx.register("other@example.com")
    lid = (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=owner)).json()["id"]
    await ctx.client.post(f"{API}/listings/{lid}/publish", headers=owner)
    assert (await ctx.client.patch(f"{API}/listings/{lid}", json={"price": "1"}, headers=other)).status_code == 403
    assert (await ctx.client.delete(f"{API}/listings/{lid}", headers=other)).status_code == 403
    assert (await ctx.client.post(f"{API}/listings/{lid}/deactivate", headers=other)).status_code == 403
    # чужой черновик вообще не раскрывается
    d = (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids, title="Другая квартира"),
                               headers=owner)).json()["id"]
    assert (await ctx.client.patch(f"{API}/listings/{d}", json={"price": "1"}, headers=other)).status_code == 404
    # цена не изменилась
    assert (await ctx.client.get(f"{API}/listings/{lid}")).json()["price"] in ("25000", "25000.00")


async def test_admin_can_edit_foreign_listing(ctx):
    ids = await ctx.seed_catalog()
    owner = await ctx.register("owner@example.com")
    admin = await ctx.register("admin@example.com", admin=True)
    lid = (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=owner)).json()["id"]
    assert (await ctx.client.patch(f"{API}/listings/{lid}", json={"rooms": 3}, headers=admin)).status_code == 200


async def test_delete_listing(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    lid = (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=h)).json()["id"]
    assert (await ctx.client.delete(f"{API}/listings/{lid}", headers=h)).status_code == 204
    assert (await ctx.client.get(f"{API}/listings/{lid}", headers=h)).status_code == 404


# ---------- валидация ----------
async def test_price_range_and_dates_rejected(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    r = await ctx.client.post(f"{API}/listings/", headers=h,
                              json=listing_payload(ids, price_min="50000", price_max="20000"))
    assert r.status_code == 422 and any("больше максимальной" in e for e in r.json()["errors"])
    d = date.today() + timedelta(days=10)
    r = await ctx.client.post(f"{API}/listings/", headers=h, json=listing_payload(
        ids, check_in=d.isoformat(), check_out=(d - timedelta(days=3)).isoformat()))
    assert r.status_code == 422 and any("выезда" in e for e in r.json()["errors"])
    r = await ctx.client.post(f"{API}/listings/", headers=h, json=listing_payload(
        ids, check_in=(date.today() - timedelta(days=1)).isoformat()))
    assert r.status_code == 422 and any("уже прошла" in e for e in r.json()["errors"])


async def test_currency_required_with_price_and_unknown_refs(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    r = await ctx.client.post(f"{API}/listings/", json=listing_payload(ids, currency=None), headers=h)
    assert r.status_code == 422 and any("валют" in e.lower() for e in r.json()["errors"])
    r = await ctx.client.post(f"{API}/listings/", json=listing_payload(ids, category_id=9999), headers=h)
    assert r.status_code == 422
    # город из другой страны
    r = await ctx.client.post(f"{API}/listings/", headers=h,
                              json=listing_payload(ids, city_id=ids["almaty"], country_id=ids["kg"]))
    assert r.status_code == 422


async def test_unsafe_source_url_rejected(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    r = await ctx.client.post(f"{API}/listings/", headers=h,
                              json=listing_payload(ids, source_url="http://169.254.169.254/latest"))
    assert r.status_code == 422


async def test_patch_cannot_break_invariants(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    lid = (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=h)).json()["id"]
    r = await ctx.client.patch(f"{API}/listings/{lid}", json={"price_min": "9", "price_max": "1"}, headers=h)
    assert r.status_code == 422
    r = await ctx.client.patch(f"{API}/listings/{lid}", json={"status": "active"}, headers=h)
    assert r.status_code == 422  # статус и владельца клиент менять не может
    r = await ctx.client.patch(f"{API}/listings/{lid}", json={"owner_id": 999}, headers=h)
    assert r.status_code == 422


async def test_duplicate_listing_rejected(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    assert (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=h)).status_code == 201
    assert (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=h)).status_code == 409


# ---------- модерация ----------
async def test_blocked_words_admin_only_and_enforced(ctx):
    ids = await ctx.seed_catalog()
    user = await ctx.register("user@example.com")
    admin = await ctx.register("admin@example.com", admin=True)
    assert (await ctx.client.get(f"{API}/blocked-words/", headers=user)).status_code == 403
    assert (await ctx.client.post(f"{API}/blocked-words/", json={"word": "плохое"}, headers=user)).status_code == 403
    r = await ctx.client.post(f"{API}/blocked-words/", json={"word": "Плохое", "reason": "тест"}, headers=admin)
    assert r.status_code == 201 and r.json()["normalized_word"] == "плохое"
    assert (await ctx.client.post(f"{API}/blocked-words/", json={"word": "ПЛОХОЕ"}, headers=admin)).status_code == 409
    for title in ("Это плохое жильё", "п л о х о е жильё"):
        r = await ctx.client.post(f"{API}/listings/", json=listing_payload(ids, title=title), headers=user)
        assert r.status_code == 422 and any("не прошло проверку" in e for e in r.json()["errors"]), title
    # не подстрока
    r = await ctx.client.post(f"{API}/listings/", json=listing_payload(ids, title="Неплохое жильё"), headers=user)
    assert r.status_code == 201
    # отключили слово — снова проходит
    wid = (await ctx.client.get(f"{API}/blocked-words/", headers=admin)).json()[0]["id"]
    assert (await ctx.client.patch(f"{API}/blocked-words/{wid}", json={"is_active": False},
                                   headers=admin)).status_code == 200
    r = await ctx.client.post(f"{API}/listings/", json=listing_payload(ids, title="Это плохое жильё"), headers=user)
    assert r.status_code == 201
    assert (await ctx.client.delete(f"{API}/blocked-words/{wid}", headers=admin)).status_code == 204


async def test_edit_with_blocked_word_rejected(ctx):
    ids = await ctx.seed_catalog()
    user = await ctx.register("user@example.com")
    admin = await ctx.register("admin@example.com", admin=True)
    lid = (await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=user)).json()["id"]
    await ctx.client.post(f"{API}/blocked-words/", json={"word": "мошенник"}, headers=admin)
    r = await ctx.client.patch(f"{API}/listings/{lid}", json={"description": "Хозяин МОШЕННИК"}, headers=user)
    assert r.status_code == 422
    r = await ctx.client.post(f"{API}/listings/{lid}/publish", headers=user)
    assert r.status_code == 200  # текущий текст чистый


# ---------- поиск ----------
async def _publish(ctx, h, ids, **over):
    r = await ctx.client.post(f"{API}/listings/", json=listing_payload(ids, **over), headers=h)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    assert (await ctx.client.post(f"{API}/listings/{lid}/publish", headers=h)).status_code == 200
    return lid


async def test_search_by_category_city_country_and_price(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    a = await _publish(ctx, h, ids, title="Квартира дешёвая", price="20000")
    b = await _publish(ctx, h, ids, title="Квартира дорогая", price="45000", rooms=3)
    c = await _publish(ctx, h, ids, title="Квартира в Алматы", city_id=ids["almaty"], price="30000")
    d = await _publish(ctx, h, ids, title="Отель", category_id=ids["hotel"], price="3000", price_period="night")

    async def found(**f):
        r = await ctx.client.post(f"{API}/search/", json=f)
        assert r.status_code == 200, r.text
        return {x["id"] for x in r.json()["items"]}

    assert await found(category="apartment_rent", city="Бишкек") == {a, b}
    assert await found(category="real_estate_rent") == {a, b, c}          # включая вложенные категории
    assert await found(category="apartment_rent", country="Казахстан") == {c}
    assert await found(category="apartment_rent", city="Бишкек", price_max=30000, currency="сом") == {a}
    assert await found(category="apartment_rent", price_min=25000, price_max=40000) == {c}
    assert await found(category="apartment_rent", city="Бишкек", rooms=3) == {b}
    assert await found(category="hotel") == {d}


async def test_search_unknown_city_returns_empty_with_warning(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    await _publish(ctx, h, ids)
    r = await ctx.client.post(f"{API}/search/", json={"city": "Атлантида"})
    assert r.json()["items"] == [] and r.json()["warnings"]


async def test_search_rejects_contradictions(ctx):
    r = await ctx.client.post(f"{API}/search/", json={"price_min": 50000, "price_max": 20000})
    assert r.status_code == 422
    r = await ctx.client.post(f"{API}/search/", json={"check_in": "2026-11-15", "check_out": "2026-11-10"})
    assert r.status_code == 422


async def test_search_does_not_show_drafts(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=h)  # черновик
    assert (await ctx.client.post(f"{API}/search/", json={"category": "apartment_rent"})).json()["items"] == []


# ---------- справочники ----------
async def test_catalog_write_requires_admin(ctx):
    user = await ctx.register("user@example.com")
    admin = await ctx.register("admin@example.com", admin=True)
    body = {"name": "Узбекистан", "code": "uz"}
    assert (await ctx.client.post(f"{API}/countries/", json=body)).status_code == 401
    assert (await ctx.client.post(f"{API}/countries/", json=body, headers=user)).status_code == 403
    r = await ctx.client.post(f"{API}/countries/", json=body, headers=admin)
    assert r.status_code == 201 and r.json()["code"] == "UZ"
    assert (await ctx.client.post(f"{API}/countries/", json=body, headers=admin)).status_code == 409
    assert len((await ctx.client.get(f"{API}/countries/")).json()) == 1
    assert (await ctx.client.get(f"{API}/countries/", params={"include_inactive": True})).status_code == 403


async def test_same_city_name_in_two_countries_but_not_twice_in_one(ctx):
    admin = await ctx.register("admin@example.com", admin=True)
    k = (await ctx.client.post(f"{API}/countries/", json={"name": "КР", "code": "KG"}, headers=admin)).json()["id"]
    r = (await ctx.client.post(f"{API}/countries/", json={"name": "РФ", "code": "RU"}, headers=admin)).json()["id"]
    assert (await ctx.client.post(f"{API}/cities/", json={"name": "Ош", "country_id": k}, headers=admin)).status_code == 201
    assert (await ctx.client.post(f"{API}/cities/", json={"name": "Ош", "country_id": r}, headers=admin)).status_code == 201
    assert (await ctx.client.post(f"{API}/cities/", json={"name": "Ош", "country_id": k}, headers=admin)).status_code == 409
    assert (await ctx.client.post(f"{API}/cities/", json={"name": "X", "country_id": 999}, headers=admin)).status_code == 422


async def test_categories_tree_and_cycle_protection(ctx):
    admin = await ctx.register("admin@example.com", admin=True)
    mk = lambda **b: ctx.client.post(f"{API}/categories/", json=b, headers=admin)  # noqa: E731
    root = (await mk(name="Недвижимость", slug="real_estate")).json()
    mid = (await mk(name="Аренда", slug="rent", parent_id=root["id"])).json()
    leaf = (await mk(name="Квартиры", slug="flats", parent_id=mid["id"])).json()
    assert (await mk(name="Дубль", slug="flats")).status_code == 409
    tree = (await ctx.client.get(f"{API}/categories/tree")).json()
    assert tree[0]["slug"] == "real_estate" and tree[0]["children"][0]["children"][0]["slug"] == "flats"
    r = await ctx.client.patch(f"{API}/categories/{root['id']}", json={"parent_id": leaf["id"]}, headers=admin)
    assert r.status_code == 422  # цикл
    assert (await ctx.client.delete(f"{API}/categories/{root['id']}", headers=admin)).status_code == 409


async def test_cannot_delete_catalog_items_in_use(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("owner@example.com")
    admin = await ctx.register("admin@example.com", admin=True)
    await ctx.client.post(f"{API}/listings/", json=listing_payload(ids), headers=h)
    assert (await ctx.client.delete(f"{API}/countries/{ids['kg']}", headers=admin)).status_code == 409
    assert (await ctx.client.delete(f"{API}/cities/{ids['bishkek']}", headers=admin)).status_code == 409
    assert (await ctx.client.delete(f"{API}/categories/{ids['apt']}", headers=admin)).status_code == 409
    assert (await ctx.client.delete(f"{API}/cities/{ids['almaty']}", headers=admin)).status_code == 204


# ---------- подписки и уведомления ----------
async def test_subscription_lifecycle_and_isolation(ctx):
    ids = await ctx.seed_catalog()
    u1 = await ctx.register("u1@example.com")
    u2 = await ctx.register("u2@example.com")
    body = {"category_id": ids["apt"], "city_id": ids["bishkek"], "price_max": "30000", "currency": "сом",
            "filters_json": {"rooms": 2}, "frequency_minutes": 30}
    r = await ctx.client.post(f"{API}/subscriptions/", json=body, headers=u1)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["currency"] == "KGS" and r.json()["filters_json"] == {"rooms": 2}
    assert len((await ctx.client.get(f"{API}/subscriptions/", headers=u1)).json()) == 1
    assert (await ctx.client.get(f"{API}/subscriptions/", headers=u2)).json() == []
    assert (await ctx.client.patch(f"{API}/subscriptions/{sid}", json={"is_active": False}, headers=u2)).status_code == 404
    assert (await ctx.client.delete(f"{API}/subscriptions/{sid}", headers=u2)).status_code == 404
    r = await ctx.client.patch(f"{API}/subscriptions/{sid}", json={"is_active": False}, headers=u1)
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (await ctx.client.patch(f"{API}/subscriptions/{sid}", json={"price_min": "40000"}, headers=u1)).status_code == 422
    assert (await ctx.client.delete(f"{API}/subscriptions/{sid}", headers=u1)).status_code == 204


async def test_subscription_validation(ctx):
    ids = await ctx.seed_catalog()
    h = await ctx.register("u1@example.com")
    post = lambda **b: ctx.client.post(f"{API}/subscriptions/", json=b, headers=h)  # noqa: E731
    assert (await post(price_min="50000", price_max="20000", currency="KGS")).status_code == 422
    assert (await post(price_max="20000")).status_code == 422                      # нет валюты
    assert (await post(frequency_minutes=1)).status_code == 422
    assert (await post(city_id=9999)).status_code == 422
    assert (await post(filters_json={"unknown": 1})).status_code == 422
    assert (await post(filters_json={"price_min": 9, "price_max": 1})).status_code == 422
    assert (await post(channel="telegram", city_id=ids["bishkek"])).status_code == 400  # канал не привязан


async def test_subscription_telegram_requires_verified_account(ctx):
    from mysite.database.ai_models import ChannelAccount
    h = await ctx.register("u1@example.com")
    me = (await ctx.client.get(f"{API}/users/me", headers=h)).json()
    async with ctx.maker() as s:
        s.add(ChannelAccount(user_id=me["id"], channel="telegram", external_user_id="123", is_verified=True))
        await s.commit()
    r = await ctx.client.post(f"{API}/subscriptions/", json={"channel": "telegram"}, headers=h)
    assert r.status_code == 201


async def test_notifications_only_own(ctx):
    from mysite.database.ai_models import Listing, Notification, SearchSubscription
    ids = await ctx.seed_catalog()
    h1, h2 = await ctx.register("u1@example.com"), await ctx.register("u2@example.com")
    me = (await ctx.client.get(f"{API}/users/me", headers=h1)).json()
    lid = await _publish(ctx, h1, ids)
    sid = (await ctx.client.post(f"{API}/subscriptions/", json={}, headers=h1)).json()["id"]
    async with ctx.maker() as s:
        s.add(Notification(user_id=me["id"], listing_id=lid, subscription_id=sid, channel="api", status="sent"))
        await s.commit()
    assert len((await ctx.client.get(f"{API}/notifications/", headers=h1)).json()) == 1
    assert (await ctx.client.get(f"{API}/notifications/", headers=h2)).json() == []
    assert (await ctx.client.get(f"{API}/notifications/")).status_code == 401


# ---------- пользователи ----------
async def test_admin_user_management(ctx):
    user = await ctx.register("user@example.com")
    admin = await ctx.register("admin@example.com", admin=True)
    uid = (await ctx.client.get(f"{API}/users/me", headers=user)).json()["id"]
    aid = (await ctx.client.get(f"{API}/users/me", headers=admin)).json()["id"]
    assert (await ctx.client.get(f"{API}/users/", headers=user)).status_code == 403
    assert len((await ctx.client.get(f"{API}/users/", headers=admin)).json()) == 2
    assert (await ctx.client.patch(f"{API}/users/{uid}", json={"is_active": False}, headers=admin)).status_code == 200
    # отключённый пользователь больше не входит и его токен перестаёт работать
    assert (await ctx.client.get(f"{API}/users/me", headers=user)).status_code == 401
    r = await ctx.client.post(f"{API}/auth/login", json={"email": "user@example.com", "password": PASSWORD})
    assert r.status_code == 401
    assert (await ctx.client.patch(f"{API}/users/{aid}", json={"role": "user"}, headers=admin)).status_code == 409
