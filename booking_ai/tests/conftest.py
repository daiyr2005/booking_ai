import os

# быстрый bcrypt для тестов; выставляем до импорта настроек приложения
os.environ.setdefault("BCRYPT_ROUNDS", "4")
os.environ.setdefault("APP_ENV", "development")

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from main import create_app  # noqa: E402
from mysite.database.ai_models import AIBase, Category, City, Country, User  # noqa: E402
from mysite.database.session import get_session  # noqa: E402
from mysite.services.moderation_service import ModerationService  # noqa: E402
from mysite.services.security import hash_password  # noqa: E402

PASSWORD = "correct-horse-battery"


class Ctx:
    def __init__(self, client, maker, app=None):
        self.client, self.maker, self.app = client, maker, app

    async def register(self, email: str, *, admin: bool = False) -> dict:
        """Создаёт пользователя и возвращает заголовок Authorization."""
        if admin:
            async with self.maker() as s:
                s.add(User(full_name="Admin", email=email, role="admin", password_hash=hash_password(PASSWORD)))
                await s.commit()
        else:
            r = await self.client.post("/api/v1/auth/register",
                                       json={"email": email, "password": PASSWORD, "full_name": email.split("@")[0]})
            assert r.status_code == 201, r.text
        r = await self.client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    async def seed_catalog(self) -> dict:
        async with self.maker() as s:
            kg, kz = Country(name="Кыргызстан", code="KG"), Country(name="Казахстан", code="KZ")
            s.add_all([kg, kz])
            await s.flush()
            bishkek, almaty = City(name="Бишкек", country_id=kg.id), City(name="Алматы", country_id=kz.id)
            rent = Category(name="Аренда", slug="real_estate_rent")
            s.add_all([bishkek, almaty, rent])
            await s.flush()
            apt = Category(name="Квартиры", slug="apartment_rent", parent_id=rent.id)
            hotel = Category(name="Отели", slug="hotel")
            s.add_all([apt, hotel])
            await s.commit()
            return {"kg": kg.id, "kz": kz.id, "bishkek": bishkek.id, "almaty": almaty.id,
                    "rent": rent.id, "apt": apt.id, "hotel": hotel.id}


def listing_payload(ids: dict, **over) -> dict:
    base = {"category_id": ids["apt"], "city_id": ids["bishkek"], "title": "Квартира у парка",
            "description": "Светлая, рядом метро", "price": "25000", "currency": "сом", "price_period": "month",
            "rooms": 2, "address": "ул. Асанбай"}
    base.update(over)
    return base


@pytest.fixture
async def ctx():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(AIBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    app = create_app(load_blocked_file=False)
    app.state.moderation = ModerationService()

    async def override_session():
        async with maker() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield Ctx(client, maker, app)
    await engine.dispose()
