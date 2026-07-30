"""Admin wiki ingest UI gate (spec 04 `04-Task-OhanaAISeller-GD0_5-InboxUI.md` §7 Phase P2).

Written BEFORE `auth.identity.require_admin` and `api/admin.py`'s guard/mount land — expected
RED until step 6. `api/admin.py` is NOT mounted in `app/main.py` as of P1 (that module's own
docstring: "api/admin.py mounting is spec 04 Phase P2's concern"), so a request to
`POST /api/admin/wiki/ingest` should genuinely fail to reach the real handler right now.

**Spec bug (flagged in the P2 ANCHOR report, not silently worked around):** §7 P2 step 1
literally reads "GET `/api/admin/wiki/ingest` với non-admin cookie → 403" — but the guarded
route is POST-only (`api/admin.py` only declares `@router.post("/wiki/ingest", ...)`); a GET
there 405s (Method Not Allowed) regardless of cookie, it never reaches `require_admin`. This
file tests the route the spec actually means: POST with a non-admin session → 403.

**CSRF interacts with this route too.** `/api/admin/wiki/ingest` is a state-mutating POST and
is NOT in `app/main.py`'s `_CSRF_EXEMPT_PATHS`, so the double-submit CSRF middleware
(pre-existing since Phase P0, unmodified here) runs before routing on every request. Both core
tests below authorize first and echo the `ohana_csrf` cookie as `X-CSRF-Token` so the 403/200
they assert is unambiguously about `require_admin` / the ingest handler — not an incidental
CSRF rejection (a CSRF-driven 403 and a role-driven 403 are indistinguishable on the wire).

Note on RED integrity: an unauthenticated POST with NO cookies at all already 403s from the
CSRF middleware alone, regardless of whether `api/admin.py` is mounted — that's an existing
P0 behavior, not something P2 adds, so a test shaped that way would not actually gate this
phase's code (would "pass" identically before and after this patch). It is deliberately NOT
included as a P2 gate; see `test_admin_ingest_admin_cookie_without_csrf_header_is_rejected`
below, which is kept as a regression/documentation check with that same caveat stated inline.

Requires the same live Postgres as `test_tenant_isolation.py` — the admin-cookie ingest test
round-trips through real `Embedding` rows (CLAUDE.md §7: no mocked DB in integration tests).
The embedder behind the route is NOT a real OpenAI call — see `api/admin.py`
`DeterministicDevEmbedder`'s docstring for why (no `app/config.py` in this repo, no
`OPENAI_API_KEY` in this dev env). Chunk/vector *quality* is out of scope here; only the HTTP
contract + chunk COUNT is asserted.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models import Embedding
from db.session import make_engine


@pytest.fixture
def dev_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OHANA_ENV", "dev")
    from app.main import app

    return TestClient(app)


@pytest.fixture
async def seeded_source_refs():
    """Same teardown shape as `test_inbox_ui_e2e.py`'s `seeded_replies` (ISSUE-014: no central
    `conftest.py` cleanup in this repo yet) — tracks every `source_ref` this file ingests into
    the shared `_platform` wiki namespace and hard-deletes those rows on teardown so repeat
    runs don't accumulate stray `Embedding` rows."""
    created: list[str] = []
    yield created

    if not created:
        return
    engine = make_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await session.execute(delete(Embedding).where(Embedding.source_ref.in_(created)))
            await session.commit()
    finally:
        await engine.dispose()


def _authorize_with_csrf(client: TestClient, role: str) -> dict[str, str]:
    """Bootstrap a dev session cookie for `role` AND return the CSRF header the caller must
    echo on state-mutating requests — mirrors `test_inbox_ui_e2e.py`'s `_authorize_with_csrf`,
    extended with the `?role=` query param `api/mock_auth.py` already supports."""
    resp = client.post(f"/api/mock/authorize?role={role}")
    assert resp.status_code == 200
    csrf_token = client.cookies.get("ohana_csrf")
    assert csrf_token, "mock authorize must mint the ohana_csrf cookie"
    return {"X-CSRF-Token": csrf_token}


def test_admin_ingest_with_seller_cookie_is_403(dev_client: TestClient) -> None:
    """The corrected version of spec 04 §7 P2 step 1's test (POST, not GET — see module
    docstring). A seller (non-admin) session with a VALID CSRF header still gets 403 from
    `require_admin`."""
    csrf_headers = _authorize_with_csrf(dev_client, "seller")

    resp = dev_client.post(
        "/api/admin/wiki/ingest",
        json={"text": "x" * 120, "source_ref": "unused-seller-403-test"},
        headers=csrf_headers,
    )
    assert resp.status_code == 403


def test_admin_ingest_with_admin_cookie_returns_chunks(
    dev_client: TestClient, seeded_source_refs: list[str]
) -> None:
    csrf_headers = _authorize_with_csrf(dev_client, "admin")
    source_ref = "test-admin-ui-policy-v1"
    seeded_source_refs.append(source_ref)

    resp = dev_client.post(
        "/api/admin/wiki/ingest",
        json={
            "text": (
                "Chinh sach doi tra: khach duoc doi tra trong 7 ngay ke tu ngay nhan hang, "
                "voi dieu kien san pham con nguyen tem mac va bao bi. "
            )
            * 3,
            "source_ref": source_ref,
        },
        headers=csrf_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["chunks"] > 0


def test_admin_ingest_admin_cookie_without_csrf_header_is_rejected(
    dev_client: TestClient,
) -> None:
    """Adversarial mirror of `test_inbox_ui_e2e.py`'s `test_approve_without_csrf_header_is_rejected`
    — admin role alone isn't enough; the double-submit CSRF contract applies to this route too
    (it is not in `_CSRF_EXEMPT_PATHS`). NOTE: unlike the two tests above, this one is not a
    P2-specific RED gate — the CSRF middleware (Phase P0, unmodified here) already rejects any
    cookie-less-CSRF POST regardless of whether this route is mounted, so it returns 403 both
    before and after this patch. Kept anyway as a regression/documentation check that P2's
    mount didn't accidentally add this path to `_CSRF_EXEMPT_PATHS`."""
    dev_client.post("/api/mock/authorize?role=admin")  # session cookie set, CSRF withheld

    resp = dev_client.post(
        "/api/admin/wiki/ingest",
        json={"text": "x" * 120, "source_ref": "unused-csrf-test"},
    )
    assert resp.status_code == 403


async def test_dev_embedder_refuses_to_run_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adversarial: the GD0.5 placeholder embedder produces hash vectors, not embeddings. Outside
    dev it must REFUSE rather than let ingest answer {"success": true, "chunks": N} while writing
    semantically meaningless rows — that failure is silent-wrong, not loud: `search_wiki` would
    feed near-random chunks to the drafter and the AI would answer a customer confidently from the
    wrong source. Mirrors `test_web_scaffold.py`'s `test_jwt_secret_refuses_public_fallback`
    — same "a dev fallback must be gated on the dev signal, not on a docstring" invariant.
    """
    # Xoá CẢ key lẫn env-signal: ca đang test là "không key + không dev". Shell dev local
    # giờ CÓ key thật trong .env (nạp 2026-07-30) — thiếu hai dòng delenv này, factory
    # chọn embedder THẬT và test đo nhầm sang network thay vì đo cái guard.
    monkeypatch.delenv("OHANA_ENV", raising=False)
    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from agent.embedder import default_embedder
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="embedder"):
            await default_embedder().embed(["chính sách đổi trả trong 3 ngày"])
    finally:
        get_settings.cache_clear()  # đừng để settings không-key cache lại cho test sau
