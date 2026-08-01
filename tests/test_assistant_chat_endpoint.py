"""Phase 2.4b · POST /api/assistant/chat gate — luồng full 4 primitive consume.

Cover:
- Happy path (allow → recall → LLM → record → save → 200).
- 401 no cookie / seller token trong cookie.
- 429 rate limit exceeded (reason=rate_limit_exceeded, used=0).
- 429 daily cost cap exceeded (reason=daily_cost_cap_exceeded, used=cap).
- Memory recall augments prompt (verify `<past_statement>` block).
- Memory recall failure không chặn chat (log warning, prompt clean).
- User message auto-saved (recall after chat trả 1 hit).
- record_tokens updates counter (before/after diff = LLM usage).
- Fail-open Redis down → 200 (D4 giữ).
- Empty LLM reply → 502.
- Wrapped user message reaches LLM (`<user_question>` tag).
- Response body reports tier + daily_tokens_used.

Test app tự dựng (KHÁC test_chat_endpoint dùng real main.app) — không cần verify
StaticFiles trap của Tầng 3 SPA; test app đơn giản hơn với deps override.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import fakeredis.aioredis
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.assistant_cost import _key as cost_key
from agent.assistant_memory import AssistantMemory
from agent.assistant_tier import TIER_LIMITS
from agent.embedder import Embedder
from agent.llm_client import AssistantStep
from app.config import EMBED_DIM
from auth.identity import SESSION_COOKIE_NAME

_SECRET = "test-secret-p24b"
_USER_ID = "p24b-alice"


class _FakeLLM:
    """Ghi lại messages, trả reply cố định. Không mạng."""

    _default_model = "fake-llm-p24b"

    def __init__(
        self,
        reply: str = "Chào bạn, tôi có thể giúp gì?",
        usage: dict[str, int] | None = None,
    ) -> None:
        self.reply = reply
        self.usage = usage or {
            "prompt_tokens": 30,
            "completion_tokens": 20,
            "total_tokens": 50,
        }
        self.seen: list[list[dict[str, Any]]] = []
        self.last_hits: dict[str, int] = {}

    async def step(self, messages: list[dict[str, Any]], **kwargs: Any) -> AssistantStep:
        self.seen.append(messages)
        return AssistantStep(content=self.reply, tool_calls=[], usage=self.usage)


class _FakeEmbedder(Embedder):
    """Deterministic sparse — cùng bài test_wiki_rag / test_assistant_memory."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * EMBED_DIM
            for tok in t.lower().split():
                slot = hash(tok) % EMBED_DIM
                vec[slot] = 1.0
            out.append(vec)
        return out


def _mint_user_token(tier: str = "free", user_id: str = _USER_ID) -> str:
    return jwt.encode({"sub": user_id, "role": "user", "tier": tier}, _SECRET, algorithm="HS256")


def _mint_seller_token() -> str:
    return jwt.encode(
        {"sub": "seller-x", "role": "seller", "shop_id": "shop-1"},
        _SECRET,
        algorithm="HS256",
    )


@pytest.fixture
def real_redis() -> Iterator[fakeredis.aioredis.FakeRedis]:
    """Fakeredis shared cho test — 1 instance per test."""
    yield fakeredis.aioredis.FakeRedis()


@pytest.fixture
async def cleanup_memory() -> AsyncIterator[None]:
    """Xoá memory test trước/sau — prefix `p24b-` để không chạm DB dùng chung."""
    from sqlalchemy import text

    from db.session import make_session_factory

    sf = make_session_factory()
    async with sf() as s:
        await s.execute(text("DELETE FROM assistant.user_memory WHERE user_id LIKE 'p24b-%'"))
        await s.commit()
    yield
    async with sf() as s:
        await s.execute(text("DELETE FROM assistant.user_memory WHERE user_id LIKE 'p24b-%'"))
        await s.commit()


@pytest.fixture
def app_and_fakes(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
) -> Iterator[tuple[FastAPI, _FakeLLM]]:
    """Test app: mount assistant router với fake LLM + fake embedder + fake redis pool.
    Lifespan giả set `app.state.redis_pool` = mock pool trả `real_redis`."""
    monkeypatch.setenv("OHANA_JWT_SECRET", _SECRET)
    monkeypatch.setenv("OHANA_ENV", "dev")

    from api.assistant_chat import build_router
    from db.session import make_session_factory

    # `make_session_factory` đọc DATABASE_URL env — cùng nguồn với test_wiki_rag /
    # test_assistant_memory. Hardcode DSN ở fixture = drift với env test setup, đã cháy
    # một lần vì password mismatch.
    session_factory = make_session_factory()

    fake_llm = _FakeLLM()

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Pool giả trả real_redis khi `get_redis(pool)` gọi. Cách rẻ nhất là mock
        # `get_redis` để bypass ConnectionPool → chỉ trả real_redis.
        app.state.redis_pool = object()  # placeholder
        yield

    app = FastAPI(lifespan=_lifespan)
    app.include_router(
        build_router(
            session_factory,
            embedder_factory=_FakeEmbedder,
            llm_dep=lambda: fake_llm,
        ),
        prefix="/api",
    )

    # Override get_redis_from_app_state để trả real_redis (bypass pool).
    from api.assistant_chat import get_redis_from_app_state

    app.dependency_overrides[get_redis_from_app_state] = lambda: real_redis

    try:
        yield app, fake_llm
    finally:
        # Cleanup engine (giữ session mở qua await; TestClient __exit__ sẽ chạy lifespan
        # shutdown — không cần dispose ở đây vì async).
        pass


def test_happy_path_returns_reply_with_tier_and_used(app_and_fakes, cleanup_memory) -> None:
    """Allow + LLM trả reply + counter cập nhật + response body có tier/used."""
    app, fake_llm = app_and_fakes
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        resp = client.post("/api/assistant/chat", json={"message": "Chào bạn"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == fake_llm.reply
    assert body["model"] == "fake-llm-p24b"
    assert body["grounded"] is False
    assert body["tier"] == "free"
    # Sau record_tokens(usage.total_tokens=50), get_daily_tokens = 50.
    assert body["daily_tokens_used"] == 50
    assert body["usage"]["total_tokens"] == 50


def test_401_when_no_cookie(app_and_fakes) -> None:
    """No cookie ⇒ 401 (từ user_identity_from_cookie)."""
    app, _ = app_and_fakes
    with TestClient(app) as client:
        resp = client.post("/api/assistant/chat", json={"message": "x"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "missing_session_cookie"}


def test_401_when_seller_token_in_cookie(app_and_fakes) -> None:
    """Seller token ⇒ 401 (route Tầng 2 chỉ nhận role `user`)."""
    app, _ = app_and_fakes
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_seller_token())
        resp = client.post("/api/assistant/chat", json={"message": "x"})
    assert resp.status_code == 401
    assert resp.json() == {"detail": "invalid_session_cookie"}


def test_429_when_rate_limit_exceeded(app_and_fakes, real_redis, cleanup_memory) -> None:
    """Free tier qpm=10; sau 10 call, lần 11 ⇒ 429 rate_limit_exceeded."""
    app, _ = app_and_fakes
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        for _ in range(10):
            r = client.post("/api/assistant/chat", json={"message": "hi"})
            assert r.status_code == 200
        r = client.post("/api/assistant/chat", json={"message": "hi"})
    assert r.status_code == 429
    detail = r.json()["detail"]
    assert detail["reason"] == "rate_limit_exceeded"
    assert detail["daily_tokens_used"] == 0


def test_429_when_daily_cost_cap_exceeded(app_and_fakes, real_redis, cleanup_memory) -> None:
    """Pre-set cost counter = cap → next call 429 daily_cost_cap_exceeded."""
    app, _ = app_and_fakes
    # Pre-set trực tiếp vào fakeredis (dùng cost_key module để tránh drift format).
    import asyncio

    async def _seed():
        await real_redis.set(cost_key(_USER_ID), TIER_LIMITS["free"].daily_tokens)

    asyncio.run(_seed())

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        resp = client.post("/api/assistant/chat", json={"message": "hi"})

    assert resp.status_code == 429
    detail = resp.json()["detail"]
    assert detail["reason"] == "daily_cost_cap_exceeded"
    assert detail["daily_tokens_used"] == TIER_LIMITS["free"].daily_tokens


def test_memory_recall_augments_prompt(app_and_fakes, cleanup_memory) -> None:
    """Pre-save 1 memory qua module trực tiếp → chat kế tiếp có `<past_statement>`
    block trong system prompt."""
    app, fake_llm = app_and_fakes

    # Pre-save memory qua AssistantMemory trực tiếp (không qua endpoint).
    import asyncio

    from db.session import make_session_factory

    sf = make_session_factory()

    async def _seed_memory():
        mem = AssistantMemory(sf, _FakeEmbedder(), user_scope=_USER_ID)
        await mem.save_text("Tôi thích ăn phở bò")

    asyncio.run(_seed_memory())

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        resp = client.post("/api/assistant/chat", json={"message": "Món ngon Hà Nội là gì"})
    assert resp.status_code == 200

    # Verify LLM nhận system prompt có `<past_statement>` block.
    assert len(fake_llm.seen) == 1
    system_msg = fake_llm.seen[0][0]
    assert system_msg["role"] == "system"
    assert "<past_statement>" in system_msg["content"]
    assert "phở bò" in system_msg["content"]


def test_memory_recall_failure_does_not_block_chat(
    app_and_fakes, monkeypatch, cleanup_memory
) -> None:
    """Mock `AssistantMemory.recall_text` raise → chat vẫn 200, log warning."""
    app, fake_llm = app_and_fakes

    # Patch recall_text để raise.
    from agent import assistant_memory

    async def _raise(self, query, k):
        raise RuntimeError("memory backend down")

    monkeypatch.setattr(assistant_memory.AssistantMemory, "recall_text", _raise)

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        resp = client.post("/api/assistant/chat", json={"message": "hi"})
    assert resp.status_code == 200
    # System prompt KHÔNG có memory block khi recall lỗi. Không check tag literal
    # `<past_statement>` vì injection directive nói VỀ tag đó (false positive); check
    # header của block "Bạn nhớ các thông tin sau về người dùng" chỉ xuất hiện khi có hits.
    system_msg = fake_llm.seen[0][0]
    assert "Bạn nhớ các thông tin sau về người dùng" not in system_msg["content"]


def test_user_message_auto_saved_to_memory(app_and_fakes, cleanup_memory) -> None:
    """Sau chat, memory recall query gần với message → ≥1 hit (auto-save)."""
    app, _ = app_and_fakes
    unique_msg = "Tôi là kỹ sư điện tử ở Đà Nẵng"
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        resp = client.post("/api/assistant/chat", json={"message": unique_msg})
    assert resp.status_code == 200

    # Verify auto-save qua recall trực tiếp.
    import asyncio

    from db.session import make_session_factory

    sf = make_session_factory()

    async def _check():
        mem = AssistantMemory(sf, _FakeEmbedder(), user_scope=_USER_ID)
        hits = await mem.recall_text(unique_msg, k=5)
        assert len(hits) >= 1
        assert any(unique_msg in h.content for h in hits)

    asyncio.run(_check())


def test_record_tokens_updates_daily_counter(app_and_fakes, real_redis, cleanup_memory) -> None:
    """Trước chat: counter=0. Sau: counter=usage.total_tokens (50 từ fake)."""
    app, _ = app_and_fakes
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        resp = client.post("/api/assistant/chat", json={"message": "x"})
    assert resp.status_code == 200

    import asyncio

    async def _get():
        raw = await real_redis.get(cost_key(_USER_ID))
        assert int(raw) == 50

    asyncio.run(_get())


def test_wrapped_user_message_reaches_llm(app_and_fakes, cleanup_memory) -> None:
    """I3-analog · user message phải wrap `<user_question>` trước khi vào LLM."""
    app, fake_llm = app_and_fakes
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        client.post("/api/assistant/chat", json={"message": "câu hỏi thử"})

    user_msg = fake_llm.seen[0][1]
    assert user_msg["role"] == "user"
    assert "<user_question>" in user_msg["content"]
    assert "câu hỏi thử" in user_msg["content"]
    assert "</user_question>" in user_msg["content"]


def test_empty_llm_reply_returns_502(app_and_fakes, cleanup_memory) -> None:
    """LLM trả `content=""` ⇒ 502 (cùng bài Tầng 3, không bong bóng rỗng)."""
    app, fake_llm = app_and_fakes
    fake_llm.reply = "   "  # trắng — sau strip = ""
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        resp = client.post("/api/assistant/chat", json={"message": "hi"})
    assert resp.status_code == 502
    assert resp.json() == {"detail": "llm_empty_response"}


def test_fail_open_when_redis_down(app_and_fakes, monkeypatch, cleanup_memory) -> None:
    """D4 · Redis pool raise ở mọi op ⇒ chat vẫn 200 (tier gate fail-open True,
    record_tokens no-op). `daily_tokens_used=0` (get_daily_tokens fail-open)."""
    app, _ = app_and_fakes

    from redis.exceptions import RedisError

    # Override lại dep với mock raise.
    from api.assistant_chat import get_redis_from_app_state

    broken = AsyncMock()
    broken.pipeline = lambda: _broken_pipeline()
    broken.get = AsyncMock(side_effect=RedisError("down"))

    def _broken_pipeline():
        pipe = AsyncMock()
        pipe.execute = AsyncMock(side_effect=RedisError("down"))
        pipe.incrby = lambda *a, **kw: None
        pipe.expire = lambda *a, **kw: None
        pipe.incr = lambda *a, **kw: None
        return pipe

    app.dependency_overrides[get_redis_from_app_state] = lambda: broken

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_user_token(tier="free"))
        resp = client.post("/api/assistant/chat", json={"message": "hi"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["daily_tokens_used"] == 0
