from __future__ import annotations

from fastapi import APIRouter

from mysite.api.routes import (ai, auth, blocked_words, categories, channels, cities, countries, listings,
                               notifications, search, subscriptions, telegram, users)

api_router = APIRouter(prefix="/api/v1")
for module in (auth, users, countries, cities, categories, listings, search, subscriptions, notifications,
               blocked_words, ai, channels, telegram):
    api_router.include_router(module.router)
