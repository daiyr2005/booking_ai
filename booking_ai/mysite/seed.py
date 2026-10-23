"""Начальные справочники и (по желанию) первый администратор: `python -m mysite.seed`.
Идемпотентно: повторный запуск ничего не дублирует."""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.config import get_settings
from mysite.database.ai_models import Category, City, Country, User
from mysite.services.security import hash_password

COUNTRIES = {
    "KG": ("Кыргызстан", ["Бишкек", "Ош", "Каракол", "Чолпон-Ата", "Джалал-Абад"]),
    "KZ": ("Казахстан", ["Алматы", "Астана", "Шымкент"]),
    "RU": ("Россия", ["Москва", "Санкт-Петербург"]),
    "UZ": ("Узбекистан", ["Ташкент", "Самарканд"]),
    "TR": ("Турция", ["Стамбул", "Анталья"]),
}

# (slug, название, slug родителя)
CATEGORIES = [
    ("real_estate", "Недвижимость", None),
    ("real_estate_rent", "Аренда", "real_estate"),
    ("apartment_rent", "Квартиры", "real_estate_rent"),
    ("room_rent", "Комнаты", "real_estate_rent"),
    ("house_rent", "Дома и коттеджи", "real_estate_rent"),
    ("real_estate_sale", "Продажа", "real_estate"),
    ("apartment_sale", "Квартиры на продажу", "real_estate_sale"),
    ("house_sale", "Дома и коттеджи на продажу", "real_estate_sale"),
    ("hotel", "Отели и гостиницы", None),
    ("tourist_services", "Туристические услуги", None),
]


async def seed_catalog(session: AsyncSession) -> None:
    for code, (name, cities) in COUNTRIES.items():
        country = await session.scalar(select(Country).where(Country.code == code))
        if country is None:
            country = Country(name=name, code=code)
            session.add(country)
            await session.flush()
        have = set((await session.execute(select(City.name).where(City.country_id == country.id))).scalars())
        session.add_all(City(name=c, country_id=country.id) for c in cities if c not in have)
    by_slug: dict[str, Category] = {}
    for slug, name, parent in CATEGORIES:
        cat = await session.scalar(select(Category).where(Category.slug == slug))
        if cat is None:
            cat = Category(name=name, slug=slug, parent_id=by_slug[parent].id if parent else None)
            session.add(cat)
            await session.flush()
        by_slug[slug] = cat
    await session.commit()


async def seed_admin(session: AsyncSession) -> bool:
    s = get_settings()
    if not (s.admin_email and s.admin_password):
        return False
    email = s.admin_email.lower()
    if await session.scalar(select(User.id).where(User.email == email)):
        return False
    session.add(User(full_name="Administrator", email=email, role="admin",
                     password_hash=await asyncio.to_thread(hash_password, s.admin_password)))
    await session.commit()
    return True


async def main() -> None:
    from mysite.database.session import get_sessionmaker
    async with get_sessionmaker()() as session:
        await seed_catalog(session)
        created = await seed_admin(session)
    print("Справочники заполнены." + (" Администратор создан." if created else ""))


if __name__ == "__main__":
    asyncio.run(main())
