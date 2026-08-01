"""R4 · POST /api/assistant/messages/{id}/feedback (ADR round2).

Test scope:
- Happy: rate assistant msg ⇒ 200, updated_at echo.
- Upsert override: rate lại ⇒ ghi đè (rating khác), 1 row DB.
- Non-assistant msg (user role) ⇒ 404 (không rate được).
- Cross-user msg ⇒ 404.
- Missing msg ⇒ 404.
- Invalid rating (0, 2, -2, "up") ⇒ 422.
- Note too long (> 2000) ⇒ 422.
- 401 missing cookie.

Không cần LLM/Redis (feedback path). Cần DB thật để insert message + conversation
(FK ownership check bằng JOIN trong repo).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from agent.embedder import Embedder
from app.config import EMBED_DIM
from auth.identity import SESSION_COOKIE_NAME

_SECRET = "test-secret-r4-feedback"
_USER_A = "r4-feedback-alice"
_USER_B = "r4-feedback-bob"


class _FakeEmbedder(Embedder):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * EMBED_DIM for _ in texts]


def _mint(user_id: str, tier: str = "free") -> str:
    return jwt.encode({"sub": user_id, "role": "user", "tier": tier}, _SECRET, algorithm="HS256")


@pytest.fixture
async def cleanup_db() -> AsyncIterator[None]:
    from db.session import make_session_factory

    sf = make_session_factory()
    async with sf() as s:
        await s.execute(
            text("DELETE FROM assistant.message_feedback WHERE user_id LIKE 'r4-feedback-%'")
        )
        await s.execute(text("DELETE FROM assistant.messages WHERE user_id LIKE 'r4-feedback-%'"))
        await s.execute(
            text("DELETE FROM assistant.conversations WHERE user_id LIKE 'r4-feedback-%'")
        )
        await s.commit()
    yield
    async with sf() as s:
        await s.execute(
            text("DELETE FROM assistant.message_feedback WHERE user_id LIKE 'r4-feedback-%'")
        )
        await s.execute(text("DELETE FROM assistant.messages WHERE user_id LIKE 'r4-feedback-%'"))
        await s.execute(
            text("DELETE FROM assistant.conversations WHERE user_id LIKE 'r4-feedback-%'")
        )
        await s.commit()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
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


async def _seed_pair(user_id: str) -> tuple[int, int, int]:
    """Insert 1 conversation + 1 user_msg + 1 assistant_msg. Trả (conv_id, user_mid, asst_mid)."""
    from db.session import make_session_factory

    sf = make_session_factory()
    async with sf() as s:
        conv_row = (
            await s.execute(
                text(
                    "INSERT INTO assistant.conversations (user_id, title) "
                    "VALUES (:u, 'r4-test') RETURNING conversation_id"
                ),
                {"u": user_id},
            )
        ).first()
        assert conv_row is not None
        conv_id = int(conv_row[0])
        user_mid_row = (
            await s.execute(
                text(
                    "INSERT INTO assistant.messages (conversation_id, user_id, role, content) "
                    "VALUES (:c, :u, 'user', 'hi') RETURNING message_id"
                ),
                {"c": conv_id, "u": user_id},
            )
        ).first()
        asst_mid_row = (
            await s.execute(
                text(
                    "INSERT INTO assistant.messages (conversation_id, user_id, role, content) "
                    "VALUES (:c, :u, 'assistant', 'hello') RETURNING message_id"
                ),
                {"c": conv_id, "u": user_id},
            )
        ).first()
        await s.commit()
        assert user_mid_row is not None and asst_mid_row is not None
        return conv_id, int(user_mid_row[0]), int(asst_mid_row[0])


def test_feedback_happy_thumbs_up(app: FastAPI, cleanup_db: None) -> None:
    import asyncio

    _, _, asst_mid = asyncio.run(_seed_pair(_USER_A))
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.post(
            f"/api/assistant/messages/{asst_mid}/feedback",
            json={"rating": 1, "note": "helpful"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["message_id"] == asst_mid
    assert "updated_at" in body


def test_feedback_upsert_overrides_rating(app: FastAPI, cleanup_db: None) -> None:
    """Rate lần đầu +1, lần hai -1 ⇒ DB row cuối cùng có rating=-1, note mới."""
    import asyncio

    _, _, asst_mid = asyncio.run(_seed_pair(_USER_A))
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        r1 = client.post(
            f"/api/assistant/messages/{asst_mid}/feedback",
            json={"rating": 1, "note": "first"},
        )
        r2 = client.post(
            f"/api/assistant/messages/{asst_mid}/feedback",
            json={"rating": -1, "note": "second"},
        )
    assert r1.status_code == 200 and r2.status_code == 200
    # Read DB direct
    from db.session import make_session_factory

    sf = make_session_factory()

    async def _read() -> tuple[int, str]:
        async with sf() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT rating, note FROM assistant.message_feedback "
                        "WHERE message_id = :m AND user_id = :u"
                    ),
                    {"m": asst_mid, "u": _USER_A},
                )
            ).first()
            assert row is not None
            return int(row[0]), row[1]

    rating, note = asyncio.run(_read())
    assert rating == -1
    assert note == "second"


def test_feedback_rejects_user_role_msg_404(app: FastAPI, cleanup_db: None) -> None:
    """Rate user message (không phải assistant) ⇒ 404 (repo trả None)."""
    import asyncio

    _, user_mid, _ = asyncio.run(_seed_pair(_USER_A))
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.post(
            f"/api/assistant/messages/{user_mid}/feedback",
            json={"rating": 1},
        )
    assert resp.status_code == 404


def test_feedback_cross_user_404(app: FastAPI, cleanup_db: None) -> None:
    """User A rate assistant msg thuộc conversation của Bob ⇒ 404 (không leak)."""
    import asyncio

    _, _, bob_asst_mid = asyncio.run(_seed_pair(_USER_B))
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.post(
            f"/api/assistant/messages/{bob_asst_mid}/feedback",
            json={"rating": 1},
        )
    assert resp.status_code == 404


def test_feedback_missing_msg_404(app: FastAPI, cleanup_db: None) -> None:
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.post(
            "/api/assistant/messages/99999999/feedback",
            json={"rating": 1},
        )
    assert resp.status_code == 404


def test_feedback_invalid_rating_422(app: FastAPI, cleanup_db: None) -> None:
    """rating không ∈ {-1, 1} ⇒ 422 (endpoint check, DB CHECK cover backup)."""
    import asyncio

    _, _, asst_mid = asyncio.run(_seed_pair(_USER_A))
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        for bad in (0, 2, -2, 5, -100):
            resp = client.post(
                f"/api/assistant/messages/{asst_mid}/feedback",
                json={"rating": bad},
            )
            assert resp.status_code == 422, f"rating={bad} should reject"


def test_feedback_note_too_long_422(app: FastAPI, cleanup_db: None) -> None:
    import asyncio

    _, _, asst_mid = asyncio.run(_seed_pair(_USER_A))
    with TestClient(app) as client:
        client.cookies.set(SESSION_COOKIE_NAME, _mint(_USER_A))
        resp = client.post(
            f"/api/assistant/messages/{asst_mid}/feedback",
            json={"rating": 1, "note": "x" * 2001},
        )
    assert resp.status_code == 422


def test_feedback_401_missing_cookie(app: FastAPI) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/assistant/messages/1/feedback", json={"rating": 1})
    assert resp.status_code == 401
