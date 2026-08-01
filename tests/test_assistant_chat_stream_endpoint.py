"""R1 · POST /api/assistant/chat/stream (ADR round2).

SSE streaming variant của /chat. Test scope:
- Happy: N token frame + 1 done frame; done.data có conversation_id + message_id + usage.
- Tier deny (rate/cost) ⇒ 429 raise TRƯỚC khi mở stream (không phải error frame).
- LLM raise mid-stream ⇒ event: error frame, KHÔNG persist reply dở.
- Empty LLM reply ⇒ event: error `llm_empty_response`.
- Reply được persist vào assistant.messages sau done.
- record_tokens INCRBY vào cost bucket sau done.
- Empty content mid-stream (LLM emit no tokens then done) ⇒ error, no persist.
- 401 missing cookie.

Reuse fixture pattern từ test_assistant_chat_endpoint.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from typing import Any

import fakeredis.aioredis
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent.assistant_cost import _key as cost_key
from agent.embedder import Embedder
from agent.llm_client import StreamDone, StreamEvent, StreamTokenDelta
from app.config import EMBED_DIM
from auth.identity import SESSION_COOKIE_NAME

_SECRET = "test-secret-r1-stream"
_USER_ID = "r1-stream-alice"


class _FakeStreamLLM:
    """LLM stream tokens theo config. `chunks` = list token gửi sequential, `usage` gắn
    vào StreamDone. Raise nếu `raise_on_start`. Buffer messages caller."""

    _default_model = "fake-stream-llm"

    def __init__(
        self,
        chunks: list[str] | None = None,
        usage: dict[str, int] | None = None,
        raise_on_start: bool = False,
        raise_mid_stream: bool = False,
    ) -> None:
        self.chunks = chunks if chunks is not None else ["Chào ", "bạn ", "nhé"]
        self.usage = usage or {"prompt_tokens": 30, "completion_tokens": 20, "total_tokens": 50}
        self.raise_on_start = raise_on_start
        self.raise_mid_stream = raise_mid_stream
        self.seen: list[list[dict[str, Any]]] = []
        self.last_hits: dict[str, int] = {}

    async def step_stream(
        self, messages: list[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[StreamEvent]:
        self.seen.append(messages)
        if self.raise_on_start:
            raise RuntimeError("simulated_provider_error")
        for i, ch in enumerate(self.chunks):
            if self.raise_mid_stream and i == 1:
                raise RuntimeError("simulated_mid_stream")
            yield StreamTokenDelta(delta=ch)
        yield StreamDone(finish_reason="stop", accumulated_tool_calls=[], usage=self.usage)


class _FakeEmbedder(Embedder):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * EMBED_DIM for _ in texts]


def _mint(user_id: str = _USER_ID, tier: str = "free") -> str:
    return jwt.encode({"sub": user_id, "role": "user", "tier": tier}, _SECRET, algorithm="HS256")


@pytest.fixture
def real_redis() -> Iterator[fakeredis.aioredis.FakeRedis]:
    yield fakeredis.aioredis.FakeRedis()


@pytest.fixture
async def cleanup_db() -> AsyncIterator[None]:
    from db.session import make_session_factory

    sf = make_session_factory()

    async def _purge() -> None:
        async with sf() as s:
            await s.execute(text("DELETE FROM assistant.messages WHERE user_id LIKE 'r1-stream-%'"))
            await s.execute(
                text("DELETE FROM assistant.conversations WHERE user_id LIKE 'r1-stream-%'")
            )
            await s.execute(
                text("DELETE FROM assistant.user_memory WHERE user_id LIKE 'r1-stream-%'")
            )
            await s.commit()

    await _purge()
    yield
    await _purge()


def _make_app(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
    fake_llm: _FakeStreamLLM,
) -> FastAPI:
    monkeypatch.setenv("OHANA_JWT_SECRET", _SECRET)
    monkeypatch.setenv("OHANA_ENV", "dev")

    from api.assistant_chat import build_router, get_redis_from_app_state
    from db.session import make_session_factory

    sf = make_session_factory()

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.redis_pool = object()
        yield

    app = FastAPI(lifespan=_lifespan)
    app.include_router(
        build_router(sf, embedder_factory=_FakeEmbedder, llm_dep=lambda: fake_llm),
        prefix="/api",
    )
    app.dependency_overrides[get_redis_from_app_state] = lambda: real_redis
    return app


def _parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    """Parse SSE stream text ⇒ list of (event_name, parsed_data)."""
    events: list[tuple[str, dict[str, Any]]] = []
    for block in raw.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        name = ""
        data = ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        if name and data:
            events.append((name, json.loads(data)))
    return events


def test_stream_happy_emits_tokens_then_done(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
    cleanup_db: None,
) -> None:
    fake = _FakeStreamLLM(chunks=["Xin ", "chào ", "bạn"])
    app = _make_app(monkeypatch, real_redis, fake)
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint())
        resp = client.post("/api/assistant/chat/stream", json={"message": "hi"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(resp.text)
    token_events = [e for e in events if e[0] == "token"]
    done_events = [e for e in events if e[0] == "done"]
    assert len(token_events) == 3
    assert [e[1]["text"] for e in token_events] == ["Xin ", "chào ", "bạn"]
    assert len(done_events) == 1
    done = done_events[0][1]
    assert done["conversation_id"] > 0
    assert done["message_id"] > 0
    assert done["usage"]["total_tokens"] == 50


def test_stream_persists_reply_after_done(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
    cleanup_db: None,
) -> None:
    """Sau done event, DB có 2 message (user + assistant)."""
    import asyncio

    fake = _FakeStreamLLM(chunks=["ok"])
    app = _make_app(monkeypatch, real_redis, fake)
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint())
        resp = client.post("/api/assistant/chat/stream", json={"message": "test-persist"})
    events = _parse_sse(resp.text)
    done_data = next(e[1] for e in events if e[0] == "done")
    conv_id = done_data["conversation_id"]

    from db.session import make_session_factory

    sf = make_session_factory()

    async def _read() -> list[tuple[str, str]]:
        async with sf() as s:
            rows = (
                await s.execute(
                    text(
                        "SELECT role, content FROM assistant.messages "
                        "WHERE conversation_id = :c ORDER BY message_id"
                    ),
                    {"c": conv_id},
                )
            ).all()
        return [(r[0], r[1]) for r in rows]

    pair = asyncio.run(_read())
    assert pair == [("user", "test-persist"), ("assistant", "ok")]


def test_stream_records_cost_after_done(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
    cleanup_db: None,
) -> None:
    """cost bucket = usage.total_tokens sau done."""
    import asyncio

    fake = _FakeStreamLLM(
        chunks=["a"], usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    )
    app = _make_app(monkeypatch, real_redis, fake)
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint())
        client.post("/api/assistant/chat/stream", json={"message": "hi"})

    async def _read_cost() -> int:
        val = await real_redis.get(cost_key(_USER_ID))
        return int(val) if val else 0

    assert asyncio.run(_read_cost()) == 15


def test_stream_llm_error_mid_stream_emits_error_frame(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
    cleanup_db: None,
) -> None:
    """LLM raise ở token thứ 2 ⇒ frame `token` cho token 1 + `event: error`,
    KHÔNG persist reply dở, KHÔNG record cost."""
    import asyncio

    fake = _FakeStreamLLM(chunks=["good ", "then ", "boom"], raise_mid_stream=True)
    app = _make_app(monkeypatch, real_redis, fake)
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint())
        resp = client.post("/api/assistant/chat/stream", json={"message": "midfail"})
    # Status vẫn 200 (SSE mở rồi), error frame là terminal signal.
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    assert events[0][0] == "token"
    assert events[0][1]["text"] == "good "
    error_frames = [e for e in events if e[0] == "error"]
    assert len(error_frames) == 1
    assert error_frames[0][1]["code"] == "llm_error"
    assert not any(e[0] == "done" for e in events)

    # DB không có message assistant nào (reply dở không persist). User message
    # cũng KHÔNG persist (append_pair là atomic — persist chỉ ở happy path).
    from db.session import make_session_factory

    sf = make_session_factory()

    async def _count() -> int:
        async with sf() as s:
            row = (
                await s.execute(
                    text("SELECT COUNT(*) FROM assistant.messages WHERE user_id = :u"),
                    {"u": _USER_ID},
                )
            ).first()
            return int(row[0]) if row else 0

    assert asyncio.run(_count()) == 0

    # Cost bucket không tăng.
    async def _cost() -> int:
        val = await real_redis.get(cost_key(_USER_ID))
        return int(val) if val else 0

    assert asyncio.run(_cost()) == 0


def test_stream_rate_limit_429_before_stream_opens(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
    cleanup_db: None,
) -> None:
    """Rate deny ⇒ HTTP 429, KHÔNG mở SSE. Client thấy status code trực tiếp."""
    import asyncio

    # Pre-fill rate bucket đầy cho user (free limit 10 qpm)
    from agent.assistant_rate_limit import _key as rate_key

    async def _fill() -> None:
        k = rate_key(_USER_ID)
        await real_redis.set(k, "9999")
        await real_redis.expire(k, 60)

    asyncio.run(_fill())

    fake = _FakeStreamLLM()
    app = _make_app(monkeypatch, real_redis, fake)
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(tier="free"))
        resp = client.post("/api/assistant/chat/stream", json={"message": "hi"})
    assert resp.status_code == 429
    assert resp.json()["detail"]["reason"] == "rate_limit_exceeded"


def test_stream_empty_reply_emits_error_frame(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
    cleanup_db: None,
) -> None:
    """LLM emit 0 token → StreamDone ⇒ reply rỗng ⇒ error frame `llm_empty_response`."""
    fake = _FakeStreamLLM(chunks=[])
    app = _make_app(monkeypatch, real_redis, fake)
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint())
        resp = client.post("/api/assistant/chat/stream", json={"message": "hi"})
    events = _parse_sse(resp.text)
    error_frames = [e for e in events if e[0] == "error"]
    assert len(error_frames) == 1
    assert error_frames[0][1]["code"] == "llm_empty_response"


def test_stream_401_missing_cookie(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    fake = _FakeStreamLLM()
    app = _make_app(monkeypatch, real_redis, fake)
    with TestClient(app) as client:
        resp = client.post("/api/assistant/chat/stream", json={"message": "hi"})
    assert resp.status_code == 401


def test_stream_sets_no_buffer_headers(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
    cleanup_db: None,
) -> None:
    """X-Accel-Buffering: no + Cache-Control: no-cache — chống proxy buffer chunk."""
    fake = _FakeStreamLLM(chunks=["hi"])
    app = _make_app(monkeypatch, real_redis, fake)
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint())
        resp = client.post("/api/assistant/chat/stream", json={"message": "hi"})
    assert resp.headers.get("x-accel-buffering") == "no"
    assert "no-cache" in resp.headers.get("cache-control", "")
