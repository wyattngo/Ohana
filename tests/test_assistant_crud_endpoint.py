"""Phase 2.4c · CRUD endpoints (/api/assistant/conversations, /api/assistant/memories).

Cover HTTP contract:
- 401 khi missing cookie / seller token trong cookie (từ user_identity_from_cookie).
- 201 create conversation, 200 list, 200 get, 200 patch, 204 delete.
- 404 khi cross-user (không 403 — không leak existence).
- 404 khi soft-deleted (get sau delete).
- 422 khi body invalid (title empty, quá dài).
- List sort DESC + cursor pagination.
- Memory list + delete (hard) tương đương.

Không cần Redis (CRUD không tier gate). Không cần LLM. Chỉ FakeEmbedder cho constructor
`AssistantMemory` (embed KHÔNG bị gọi trong list/delete — nhưng constructor cần object).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.embedder import Embedder
from app.config import EMBED_DIM
from auth.identity import SESSION_COOKIE_NAME

_SECRET = "test-secret-p24c"
_USER_A = "p24c-endpoint-alice"
_USER_B = "p24c-endpoint-bob"


class _FakeEmbedder(Embedder):
    """Không được gọi trong CRUD path (list/delete), nhưng constructor cần object."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * EMBED_DIM for _ in texts]


def _mint(user_id: str, tier: str = "free") -> str:
    return jwt.encode({"sub": user_id, "role": "user", "tier": tier}, _SECRET, algorithm="HS256")


def _mint_seller() -> str:
    return jwt.encode(
        {"sub": "seller-x", "role": "seller", "shop_id": "shop-1"},
        _SECRET,
        algorithm="HS256",
    )


@pytest.fixture
async def cleanup_db() -> AsyncIterator[None]:
    """Xoá conversations + memories test prefix p24c-endpoint-. Chạy trước + sau."""
    from sqlalchemy import text

    from db.session import make_session_factory

    sf = make_session_factory()
    async with sf() as s:
        await s.execute(
            text("DELETE FROM assistant.conversations WHERE user_id LIKE 'p24c-endpoint-%'")
        )
        await s.execute(
            text("DELETE FROM assistant.user_memory WHERE user_id LIKE 'p24c-endpoint-%'")
        )
        await s.commit()
    yield
    async with sf() as s:
        await s.execute(
            text("DELETE FROM assistant.conversations WHERE user_id LIKE 'p24c-endpoint-%'")
        )
        await s.execute(
            text("DELETE FROM assistant.user_memory WHERE user_id LIKE 'p24c-endpoint-%'")
        )
        await s.commit()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    """Test app mount CRUD router. Không cần Redis, không cần LLM."""
    monkeypatch.setenv("OHANA_JWT_SECRET", _SECRET)
    monkeypatch.setenv("OHANA_ENV", "dev")

    from api.assistant_crud import build_router
    from db.session import make_session_factory

    session_factory = make_session_factory()
    app = FastAPI()
    app.include_router(
        build_router(session_factory, embedder_factory=_FakeEmbedder),
        prefix="/api",
    )
    yield app


# ── Conversations ────────────────────────────────────────────────────────────────


def test_create_conversation_401_when_missing_cookie(app: FastAPI) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/assistant/conversations", json={"title": "X"})
    assert resp.status_code == 401


def test_create_conversation_401_when_seller_token(app: FastAPI) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint_seller())
        resp = client.post("/api/assistant/conversations", json={"title": "X"})
    assert resp.status_code == 401


def test_create_conversation_happy_201(app: FastAPI, cleanup_db: None) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.post("/api/assistant/conversations", json={"title": "Chat 1"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Chat 1"
    assert body["conversation_id"] > 0
    assert "created_at" in body and "updated_at" in body


def test_create_conversation_null_title_ok(app: FastAPI, cleanup_db: None) -> None:
    """title=null OK (user tạo trước, đặt tên sau)."""
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.post("/api/assistant/conversations", json={})
    assert resp.status_code == 201
    assert resp.json()["title"] is None


def test_create_conversation_title_too_long_422(app: FastAPI, cleanup_db: None) -> None:
    """title > 200 chars ⇒ 422 (Pydantic validation)."""
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.post("/api/assistant/conversations", json={"title": "X" * 201})
    assert resp.status_code == 422


def test_list_conversations_returns_only_own(app: FastAPI, cleanup_db: None) -> None:
    """User A list KHÔNG thấy conversation của user B."""
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        client.post("/api/assistant/conversations", json={"title": "alice-1"})
        client.post("/api/assistant/conversations", json={"title": "alice-2"})
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_B))
        client.post("/api/assistant/conversations", json={"title": "bob-1"})

        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.get("/api/assistant/conversations")

    assert resp.status_code == 200
    body = resp.json()
    titles = {item["title"] for item in body["items"]}
    assert titles == {"alice-1", "alice-2"}
    assert body["next_cursor"] is None  # ít hơn limit


def test_list_conversations_pagination(app: FastAPI, cleanup_db: None) -> None:
    """Page 1 (limit=2), page 2 dùng cursor."""
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        for i in range(3):
            client.post("/api/assistant/conversations", json={"title": f"C{i}"})

        page1 = client.get("/api/assistant/conversations?limit=2").json()
        assert len(page1["items"]) == 2
        assert page1["next_cursor"] is not None
        cursor = page1["next_cursor"]

        page2 = client.get(f"/api/assistant/conversations?limit=2&before={cursor}").json()
        assert len(page2["items"]) == 1
        assert page2["next_cursor"] is None  # hết trang


def test_get_conversation_404_cross_user(app: FastAPI, cleanup_db: None) -> None:
    """User A get id của user B ⇒ 404 (không 403 — không leak existence)."""
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_B))
        bob = client.post("/api/assistant/conversations", json={"title": "bob"}).json()

        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.get(f"/api/assistant/conversations/{bob['conversation_id']}")
    assert resp.status_code == 404
    assert resp.json() == {"detail": "conversation_not_found"}


def test_get_conversation_happy(app: FastAPI, cleanup_db: None) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        created = client.post("/api/assistant/conversations", json={"title": "T"}).json()
        resp = client.get(f"/api/assistant/conversations/{created['conversation_id']}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "T"


def test_patch_conversation_updates_title(app: FastAPI, cleanup_db: None) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        created = client.post("/api/assistant/conversations", json={"title": "old"}).json()
        resp = client.patch(
            f"/api/assistant/conversations/{created['conversation_id']}",
            json={"title": "new"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "new"
    assert body["updated_at"] >= created["updated_at"]


def test_patch_conversation_404_cross_user(app: FastAPI, cleanup_db: None) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_B))
        bob = client.post("/api/assistant/conversations", json={"title": "bob"}).json()

        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.patch(
            f"/api/assistant/conversations/{bob['conversation_id']}",
            json={"title": "hacked"},
        )
    assert resp.status_code == 404


def test_patch_conversation_empty_title_422(app: FastAPI, cleanup_db: None) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        created = client.post("/api/assistant/conversations", json={"title": "T"}).json()
        resp = client.patch(
            f"/api/assistant/conversations/{created['conversation_id']}",
            json={"title": ""},
        )
    assert resp.status_code == 422


def test_delete_conversation_204_then_get_404(app: FastAPI, cleanup_db: None) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        created = client.post("/api/assistant/conversations", json={"title": "ephemeral"}).json()
        del_resp = client.delete(f"/api/assistant/conversations/{created['conversation_id']}")
        assert del_resp.status_code == 204
        assert del_resp.content == b""

        get_resp = client.get(f"/api/assistant/conversations/{created['conversation_id']}")
    assert get_resp.status_code == 404


def test_delete_conversation_second_time_404(app: FastAPI, cleanup_db: None) -> None:
    """Delete lần 2 ⇒ 404 (soft-delete không idempotent theo bài Repo)."""
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        created = client.post("/api/assistant/conversations", json={"title": "X"}).json()
        client.delete(f"/api/assistant/conversations/{created['conversation_id']}")
        resp = client.delete(f"/api/assistant/conversations/{created['conversation_id']}")
    assert resp.status_code == 404


def test_delete_conversation_404_cross_user(app: FastAPI, cleanup_db: None) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_B))
        bob = client.post("/api/assistant/conversations", json={"title": "bob"}).json()

        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.delete(f"/api/assistant/conversations/{bob['conversation_id']}")
    assert resp.status_code == 404
    # Bob vẫn thấy conversation của mình.
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_B))
        get_resp = client.get(f"/api/assistant/conversations/{bob['conversation_id']}")
    assert get_resp.status_code == 200


# ── Memories ────────────────────────────────────────────────────────────────────


def test_list_memories_401_when_missing_cookie(app: FastAPI) -> None:
    with TestClient(app) as client:
        resp = client.get("/api/assistant/memories")
    assert resp.status_code == 401


async def _seed_memory(user_scope: str, content: str) -> int:
    """Seed memory bằng repo trực tiếp. Engine mới per-call (`asyncio.run` mở loop mới —
    engine cũ bind loop cũ sẽ nổ). Return memory_id để test dùng làm ref delete."""
    from agent.assistant_memory import AssistantMemory
    from db.session import make_session_factory

    sf = make_session_factory()
    mem = AssistantMemory(sf, _FakeEmbedder(), user_scope=user_scope)
    return await mem.save_text(content)


def test_list_memories_returns_only_own(app: FastAPI, cleanup_db: None) -> None:
    """Save qua repo trực tiếp (endpoint POST /memories chưa có ở P2.4c), rồi list qua endpoint."""
    import asyncio

    asyncio.run(_seed_memory(_USER_A, "alice-remember-this"))
    asyncio.run(_seed_memory(_USER_B, "bob-secret"))

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.get("/api/assistant/memories")
    assert resp.status_code == 200
    body = resp.json()
    contents = [item["content"] for item in body["items"]]
    assert "alice-remember-this" in contents
    assert "bob-secret" not in contents


def test_delete_memory_204_then_list_missing(app: FastAPI, cleanup_db: None) -> None:
    import asyncio

    mid = asyncio.run(_seed_memory(_USER_A, "forget-me"))

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        del_resp = client.delete(f"/api/assistant/memories/{mid}")
        assert del_resp.status_code == 204
        list_resp = client.get("/api/assistant/memories")
    assert list_resp.status_code == 200
    contents = [item["content"] for item in list_resp.json()["items"]]
    assert "forget-me" not in contents


def test_delete_memory_404_cross_user(app: FastAPI, cleanup_db: None) -> None:
    import asyncio

    b_mid = asyncio.run(_seed_memory(_USER_B, "bob-secret"))

    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.delete(f"/api/assistant/memories/{b_mid}")
    assert resp.status_code == 404


def test_delete_memory_404_missing_id(app: FastAPI, cleanup_db: None) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.delete("/api/assistant/memories/999999999")
    assert resp.status_code == 404
