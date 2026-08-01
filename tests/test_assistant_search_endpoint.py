"""R3 · GET /api/assistant/search (ADR round2).

Test scope:
- Happy: title match ⇒ conversations[]; content match ⇒ messages[] + snippet có <em>.
- Cross-user isolation: user A search KHÔNG thấy conversation/message của Bob.
- Soft-deleted conversation loại khỏi kết quả.
- Empty q ⇒ 422 (Query min_length=1).
- Q too long (> 200) ⇒ 422.
- 401 missing cookie.
- Rate limit: gọi > 30 lần/phút (free) ⇒ 429 với reason `search_rate_limit_exceeded`.
- Không ăn quota chat (rate bucket riêng).

Cần Redis (fakeredis) + DB thật (FTS index đã migrate).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator

import fakeredis.aioredis
import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from auth.identity import SESSION_COOKIE_NAME

_SECRET = "test-secret-r3-search"
_USER_A = "r3-search-alice"
_USER_B = "r3-search-bob"


def _mint(user_id: str, tier: str = "free") -> str:
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
            await s.execute(text("DELETE FROM assistant.messages WHERE user_id LIKE 'r3-search-%'"))
            await s.execute(
                text("DELETE FROM assistant.conversations WHERE user_id LIKE 'r3-search-%'")
            )
            await s.commit()

    await _purge()
    yield
    await _purge()


@pytest.fixture
def app(
    monkeypatch: pytest.MonkeyPatch,
    real_redis: fakeredis.aioredis.FakeRedis,
) -> Iterator[FastAPI]:
    monkeypatch.setenv("OHANA_JWT_SECRET", _SECRET)
    monkeypatch.setenv("OHANA_ENV", "dev")

    from api.assistant_search import build_router, get_redis_from_app_state
    from db.session import make_session_factory

    sf = make_session_factory()

    @contextlib.asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.redis_pool = object()
        yield

    app = FastAPI(lifespan=_lifespan)
    app.include_router(build_router(sf), prefix="/api")
    app.dependency_overrides[get_redis_from_app_state] = lambda: real_redis
    yield app


async def _seed_conv_with_messages(
    user_id: str,
    title: str,
    messages: list[tuple[str, str]],
) -> int:
    """Insert conversation + messages theo (role, content) pairs. Trả conv_id."""
    from db.session import make_session_factory

    sf = make_session_factory()
    async with sf() as s:
        row = (
            await s.execute(
                text(
                    "INSERT INTO assistant.conversations (user_id, title) "
                    "VALUES (:u, :t) RETURNING conversation_id"
                ),
                {"u": user_id, "t": title},
            )
        ).first()
        assert row is not None
        conv_id = int(row[0])
        for role, content in messages:
            await s.execute(
                text(
                    "INSERT INTO assistant.messages (conversation_id, user_id, role, content) "
                    "VALUES (:c, :u, :r, :b)"
                ),
                {"c": conv_id, "u": user_id, "r": role, "b": content},
            )
        await s.commit()
        return conv_id


def test_search_title_match(app: FastAPI, cleanup_db: None) -> None:
    import asyncio

    conv_id = asyncio.run(
        _seed_conv_with_messages(
            _USER_A,
            "Chiến lược tăng trưởng Q4",
            [("user", "hello"), ("assistant", "hi")],
        )
    )
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.get("/api/assistant/search", params={"q": "chiến lược"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    conv_ids = {c["conversation_id"] for c in body["conversations"]}
    assert conv_id in conv_ids


def test_search_content_match_snippet_has_em(app: FastAPI, cleanup_db: None) -> None:
    """FTS match + snippet chứa <em>...</em> highlight từ ts_headline."""
    import asyncio

    conv_id = asyncio.run(
        _seed_conv_with_messages(
            _USER_A,
            "Notes",
            [
                ("user", "how do I bake a chocolate cake"),
                ("assistant", "you need flour, sugar, cocoa and butter"),
            ],
        )
    )
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.get("/api/assistant/search", params={"q": "chocolate"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["messages"]) >= 1
    hit = body["messages"][0]
    assert hit["conversation_id"] == conv_id
    assert "<em>" in hit["snippet"] and "</em>" in hit["snippet"]


def test_search_cross_user_isolated(app: FastAPI, cleanup_db: None) -> None:
    """User A search từ khoá của Bob ⇒ không thấy."""
    import asyncio

    asyncio.run(
        _seed_conv_with_messages(
            _USER_B,
            "bob-secret-topic",
            [("user", "some unique bob content xylophone")],
        )
    )
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.get("/api/assistant/search", params={"q": "xylophone"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversations"] == []
    assert body["messages"] == []


def test_search_soft_deleted_conversation_hidden(app: FastAPI, cleanup_db: None) -> None:
    """Soft-delete conversation ⇒ title + message match đều biến mất khỏi search."""
    import asyncio

    conv_id = asyncio.run(
        _seed_conv_with_messages(
            _USER_A,
            "orphan-title-marker",
            [("user", "unique_deleted_marker_word")],
        )
    )
    # Soft delete
    from db.session import make_session_factory

    sf = make_session_factory()

    async def _delete() -> None:
        async with sf() as s:
            await s.execute(
                text(
                    "UPDATE assistant.conversations SET deleted_at = now() "
                    "WHERE conversation_id = :c"
                ),
                {"c": conv_id},
            )
            await s.commit()

    asyncio.run(_delete())

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        r1 = client.get("/api/assistant/search", params={"q": "orphan-title-marker"})
        r2 = client.get("/api/assistant/search", params={"q": "unique_deleted_marker_word"})
    assert r1.json() == {"conversations": [], "messages": []}
    assert r2.json() == {"conversations": [], "messages": []}


def test_search_empty_q_422(app: FastAPI) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.get("/api/assistant/search", params={"q": ""})
    assert resp.status_code == 422


def test_search_q_too_long_422(app: FastAPI) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.get("/api/assistant/search", params={"q": "x" * 201})
    assert resp.status_code == 422


def test_search_401_missing_cookie(app: FastAPI) -> None:
    with TestClient(app) as client:
        resp = client.get("/api/assistant/search", params={"q": "hi"})
    assert resp.status_code == 401


def test_search_rate_limit_free_after_30_reqs(app: FastAPI, cleanup_db: None) -> None:
    """Free tier: 30/min → 31st ⇒ 429 `search_rate_limit_exceeded`."""
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A, tier="free"))
        for _ in range(30):
            r = client.get("/api/assistant/search", params={"q": "hi"})
            assert r.status_code == 200
        rate_limited = client.get("/api/assistant/search", params={"q": "hi"})
    assert rate_limited.status_code == 429
    body = rate_limited.json()
    assert body["detail"]["reason"] == "search_rate_limit_exceeded"


def test_search_bucket_separate_from_chat(
    app: FastAPI, real_redis: fakeredis.aioredis.FakeRedis
) -> None:
    """Search rate key namespace `rl:search:` — không đụng `rl:user:` (chat).

    Reach 30 search reqs ⇒ khoá `rl:search:...:qpm:...` tồn tại nhưng khoá `rl:user:...`
    KHÔNG tồn tại (chưa gọi chat). Gate ranh giới namespace."""
    import asyncio

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A, tier="free"))
        for _ in range(5):
            client.get("/api/assistant/search", params={"q": "hi"})

    async def _inspect() -> tuple[list[bytes], list[bytes]]:
        search_keys = [k async for k in real_redis.scan_iter(match="rl:search:*")]
        chat_keys = [k async for k in real_redis.scan_iter(match="rl:user:*")]
        return search_keys, chat_keys

    search_keys, chat_keys = asyncio.run(_inspect())
    assert len(search_keys) >= 1
    assert chat_keys == []
