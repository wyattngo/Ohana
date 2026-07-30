"""P0 scaffold gate (spec 04 `04-Task-OhanaAISeller-GD0_5-InboxUI.md` §7 Phase P0 step 1).

Written BEFORE `web/dist` is built and BEFORE `app/main.py` mounts anything — expected RED
until step 6 lands. Locks the browser-facing contract for GD0.5:
  1. `GET /` serves the built Vite SPA shell (static bundle, not a template render).
  2. `GET /api/inbox` requires a verified identity — no cookie => 401 (never a default
     identity; same invariant `auth.identity.verify_token` already enforces for the header
     path, extended here to the cookie path via `identity_from_cookie`).
  3. `POST /api/mock/authorize` (dev-only) mints the fixture identity and sets it as an
     httpOnly session cookie so a browser session can call the JWT-gated `/api/*` routes
     without a real login flow (DEC-OHANA-01 U4).
  4. Cookie round-trip: authorize -> GET /api/inbox returns 200 + [] (no rows exist yet for
     the fixture shop_id — tenant-scoped list, not a stub).

`OHANA_ENV` defaults to "production" (fail-closed) in `api/mock_auth.py` so a misconfigured
deploy can never accidentally expose the mock-authorize bootstrap route. Tests opt IN via
monkeypatch, which exercises the SAME code path a real dev `.env` would (no test-only bypass).

Requires the same live Postgres as `test_tenant_isolation.py` (`DATABASE_URL`, default
postgresql+psycopg://ohana:ohana@localhost:5432/ohana) — `GET /api/inbox` round-trips through
`db.repos.PendingReplyRepo`, it is not mocked here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def dev_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OHANA_ENV", "dev")
    from app.main import app

    return TestClient(app)


@pytest.mark.skipif(
    not (Path(__file__).resolve().parent.parent / "web" / "dist").is_dir(),
    reason="web/dist chưa có (frontend chưa adopt/build) — mount ở app/main.py là có điều kiện",
)
def test_root_serves_html_shell(dev_client: TestClient) -> None:
    resp = dev_client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert '<div id="root">' in resp.text


def test_inbox_without_cookie_is_401(dev_client: TestClient) -> None:
    resp = dev_client.get("/api/inbox")
    assert resp.status_code == 401


def test_mock_authorize_sets_session_cookie_and_returns_fixture(dev_client: TestClient) -> None:
    resp = dev_client.post("/api/mock/authorize")
    assert resp.status_code == 200
    assert resp.json() == {
        "oa_id": "fixture-oa-001",
        "shop_id": "fixture-shop-001",
        "role": "seller",
    }
    assert "ohana_session" in resp.cookies


def test_mock_authorize_disabled_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adversarial: OHANA_ENV unset (fail-closed default) => the bootstrap route 404s, same
    shape as "route doesn't exist" rather than a 403 that would confirm it's there."""
    monkeypatch.delenv("OHANA_ENV", raising=False)
    from app.main import app

    resp = TestClient(app).post("/api/mock/authorize")
    assert resp.status_code == 404


def test_jwt_secret_refuses_public_fallback_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    """Adversarial: the dev fallback secret is public in git, so it must never sign or verify
    outside dev. Guarding only the mint route is not enough — this secret also feeds
    `identity_from_cookie`, so a forgotten OHANA_JWT_SECRET would 404 the mint route while
    still accepting cookies forged with the public value (self-issued shop_id => R1.22)."""
    monkeypatch.delenv("OHANA_ENV", raising=False)
    monkeypatch.delenv("OHANA_JWT_SECRET", raising=False)
    from auth.identity import get_jwt_secret

    with pytest.raises(RuntimeError, match="OHANA_JWT_SECRET"):
        get_jwt_secret()


def test_inbox_with_dev_cookie_returns_200_empty_list(dev_client: TestClient) -> None:
    auth_resp = dev_client.post("/api/mock/authorize")
    assert auth_resp.status_code == 200

    resp = dev_client.get("/api/inbox")
    assert resp.status_code == 200
    assert resp.json() == []
