from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from mysite.api.dependencies import get_ai_runtime, get_current_user_optional
from mysite.clients.ollama import OllamaUnavailable
from mysite.database.ai_models import User
from mysite.database.session import get_session
from mysite.schemas.ai import ChatIn, ChatOut
from mysite.services.ai_chat import AIRuntime
from mysite.services.errors import ServiceError

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/chat", response_model=ChatOut)
async def chat(body: ChatIn, user: User | None = Depends(get_current_user_optional),
               session: AsyncSession = Depends(get_session), runtime: AIRuntime = Depends(get_ai_runtime)):
    """Сообщение ассистенту. Без токена доступен только поиск; остальные действия требуют входа.
    Удаление объявления ассистент выполняет только после ответа «да» в том же conversation_id."""
    try:
        return await runtime.handle(session, user, "api", body.conversation_id, body.message)
    except OllamaUnavailable:
        raise ServiceError(503, "Языковая модель (Ollama) недоступна, попробуйте позже")
