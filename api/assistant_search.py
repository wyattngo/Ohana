"""R3 search endpoint — GET /api/assistant/search (ADR round2).

Tách file khỏi `assistant_crud.py` vì cần **Redis** (rate-limit `search:{user_id}`) —
CRUD sạch không cần Redis. Cùng bài `assistant_chat.py` tách khỏi CRUD ở P2.4b.

Contract: `?q=&limit=` → `{conversations: [...], messages: [...]}`. Không pagination v1
(cap 20/loại; client vượt cap phải refine query).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.assistant_rate_limit import try_acquire_search
from agent.assistant_search import AssistantSearch
from agent.assistant_tier import TIER_LIMITS
from agent.redis_client import get_redis
from auth.user_identity import UserIdentity, user_identity_from_cookie


class ConversationHitOut(BaseModel):
    conversation_id: int
    title: str
    updated_at: datetime


class MessageHitOut(BaseModel):
    message_id: int
    conversation_id: int
    snippet: str
    created_at: datetime


class SearchOut(BaseModel):
    conversations: list[ConversationHitOut]
    messages: list[MessageHitOut]


def get_redis_from_app_state(request: Request) -> Redis:
    """Cùng bài `api/assistant_chat.py` — pool từ `app.state.redis_pool`."""
    return get_redis(request.app.state.redis_pool)


def build_router(
    session_factory: async_sessionmaker[AsyncSession],
) -> APIRouter:
    """Router search. session_factory cùng svc_ohana_ai (D2 grant assistant schema)."""
    router = APIRouter(prefix="/assistant", tags=["assistant"])

    @router.get("/search", response_model=SearchOut)
    async def search(
        q: str = Query(min_length=1, max_length=200, description="Search query"),
        identity: UserIdentity = Depends(user_identity_from_cookie),
        redis: Redis = Depends(get_redis_from_app_state),
    ) -> SearchOut:
        """R3 · search conversations + messages per-user.

        Rate limit riêng (`search:` namespace, không ăn quota chat). Free 30/min, Pro
        120/min. Vượt ⇒ 429 (cùng shape chat 429).
        """
        limit = TIER_LIMITS.get(identity.tier)
        if limit is None:
            raise HTTPException(status_code=429, detail={"reason": "unknown_tier"})

        ok = await try_acquire_search(redis, identity.user_id, limit_qpm=limit.search_qpm)
        if not ok:
            raise HTTPException(
                status_code=429,
                detail={"reason": "search_rate_limit_exceeded"},
            )

        repo = AssistantSearch(session_factory, user_scope=identity.user_id)
        result = await repo.search(q)
        return SearchOut(
            conversations=[
                ConversationHitOut(
                    conversation_id=h.conversation_id,
                    title=h.title,
                    updated_at=h.updated_at,
                )
                for h in result.conversations
            ],
            messages=[
                MessageHitOut(
                    message_id=h.message_id,
                    conversation_id=h.conversation_id,
                    snippet=h.snippet,
                    created_at=h.created_at,
                )
                for h in result.messages
            ],
        )

    return router
