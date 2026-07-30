"""E2E gate for spec 04 Phase P1 (seller screens live-bind).

Exercises the exact HTTP contract `web/src/lib/api.ts` binds to: list -> approve -> reject,
through the SAME dev-cookie bootstrap `test_web_scaffold.py` already covers
(`POST /api/mock/authorize`). Seeds real rows via `db.repos.PendingReplyRepo` against a live
Postgres (CLAUDE.md §7 anti-pattern "Mock database trong integration test" — no mocking here),
scoped to the fixture shop_id `api/mock_auth.py` mints (`fixture-shop-001`).

CSRF is exercised for real, not assumed: `_authorize()` reads back the `ohana_csrf` cookie the
mock-authorize route mints and the approve/reject tests echo it as `X-CSRF-Token` — the same
double-submit round-trip `web/src/lib/api.ts` must perform in the browser (spec 04 §10 PC8).

Note: `app/main.py` already mounts `api/inbox.py` under `/api` as of Phase P0 (its module
docstring: "spec 01 backend, mounted here for the first time") — spec 04 §7 Phase P1 step 2
anticipated that mount happening in P1, but P0 did it early alongside the static-shell mount.
So the list/approve/reject HTTP contract these tests exercise is already live pre-P1; what P1
adds is the browser client (`web/src/lib/api.ts`) and the 3 screens that call it. These tests
gate the contract those screens bind to, not the screens' rendering (no Playwright browser
harness in this repo yet — see the P1 report for what's exercised only by manual smoke).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from db.models import PendingReply
from db.repos import PendingReplyRepo
from db.session import make_engine

_FIXTURE_SHOP_ID = "fixture-shop-001"  # matches api/mock_auth.py fixture claim


@pytest.fixture
def dev_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OHANA_ENV", "dev")
    from app.main import app

    return TestClient(app)


@pytest.fixture
async def seeded_replies():
    """Tracks every `reply_id` a test seeds and hard-deletes them on teardown, regardless of
    final status. `fixture-shop-001` is a SHARED tenant across this file and
    `test_web_scaffold.py` (whose `test_inbox_with_dev_cookie_returns_200_empty_list` asserts
    an EMPTY list) — without this, a `pending`-status row seeded here (e.g. the list-read test,
    which never decides its row) leaks past this test and breaks that assertion the next time
    the shared suite runs. Real DELETE, not a mock — same "no mocked DB" invariant as the rest
    of this file."""
    created_ids: list[str] = []
    yield created_ids

    if not created_ids:
        return
    engine = make_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await session.execute(
                delete(PendingReply).where(PendingReply.reply_id.in_(created_ids))
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _seed_parents(session, *, conversation_id: str, customer_id: str) -> None:
    """Seed the Customer + Conversation rows a PendingReply now points AT.

    Spec 06 F0 turned `pending_reply.conversation_id` / `.customer_id` from bare Text into
    composite foreign keys `(shop_id, …)`. Before that, this file could invent ids like
    "conv-1" and Postgres accepted them — the columns referenced nothing. They do now, so a
    parked reply requires its parents to exist, exactly as production would.

    `ON CONFLICT DO NOTHING` because `_FIXTURE_SHOP_ID` is shared across tests in this file
    (and with test_web_scaffold.py): re-seeding the same customer/conversation must be a
    no-op, not a unique violation.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from db.models import Conversation, Customer

    await session.execute(
        pg_insert(Customer)
        .values(id=customer_id, shop_id=_FIXTURE_SHOP_ID, channel="zalo", external_id=customer_id)
        .on_conflict_do_nothing()
    )
    await session.execute(
        pg_insert(Conversation)
        .values(
            id=conversation_id,
            shop_id=_FIXTURE_SHOP_ID,
            customer_id=customer_id,
            channel="zalo",
        )
        .on_conflict_do_nothing()
    )
    await session.commit()


async def _seed_pending_reply(
    tracker: list[str],
    *,
    conversation_id: str,
    customer_id: str,
    draft_text: str,
    intent: str,
    confidence: float,
) -> str:
    reply_id = f"reply-{uuid.uuid4().hex[:8]}"
    engine = make_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            await _seed_parents(session, conversation_id=conversation_id, customer_id=customer_id)
            repo = PendingReplyRepo(session, shop_scope=_FIXTURE_SHOP_ID)
            await repo.create(
                reply_id=reply_id,
                conversation_id=conversation_id,
                customer_id=customer_id,
                draft_text=draft_text,
                intent=intent,
                confidence=confidence,
            )
    finally:
        await engine.dispose()
    tracker.append(reply_id)
    return reply_id


async def _fetch_status(reply_id: str) -> str:
    engine = make_engine()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            repo = PendingReplyRepo(session, shop_scope=_FIXTURE_SHOP_ID)
            row = await repo.get(reply_id)
            assert row is not None, f"{reply_id} vanished after decision"
            return row.status
    finally:
        await engine.dispose()


def _authorize_with_csrf(client: TestClient) -> dict[str, str]:
    """Bootstrap the dev session cookie AND return the CSRF header the caller must echo on
    state-mutating requests — mirrors exactly what `web/src/lib/api.ts` has to do."""
    resp = client.post("/api/mock/authorize")
    assert resp.status_code == 200
    csrf_token = client.cookies.get("ohana_csrf")
    assert csrf_token, "mock authorize must mint the ohana_csrf cookie"
    return {"X-CSRF-Token": csrf_token}


async def test_inbox_lists_seeded_pending_reply(
    dev_client: TestClient, seeded_replies: list[str]
) -> None:
    reply_id = await _seed_pending_reply(
        seeded_replies,
        conversation_id="conv-1",
        customer_id="cust-1",
        draft_text="Xin chào anh/chị, đơn hàng #123 dự kiến giao trong 2 ngày tới ạ.",
        intent="order_question",
        confidence=0.91,
    )
    _authorize_with_csrf(dev_client)

    resp = dev_client.get("/api/inbox")
    assert resp.status_code == 200

    rows = resp.json()
    matches = [r for r in rows if r["reply_id"] == reply_id]
    assert len(matches) == 1, f"expected exactly 1 row for {reply_id}, got {len(matches)}"

    row = matches[0]
    assert row["conversation_id"] == "conv-1"
    assert row["customer_id"] == "cust-1"
    assert row["intent"] == "order_question"
    assert row["confidence"] == pytest.approx(0.91)
    assert row["status"] == "pending"
    assert row["draft_text"].startswith("Xin chào anh/chị")


async def test_approve_flips_status_in_db(
    dev_client: TestClient, seeded_replies: list[str]
) -> None:
    reply_id = await _seed_pending_reply(
        seeded_replies,
        conversation_id="conv-2",
        customer_id="cust-2",
        draft_text="Dạ shop xin lỗi vì sự bất tiện này, shop sẽ đổi sản phẩm mới cho mình ạ.",
        intent="complaint",
        confidence=0.7,
    )
    csrf_headers = _authorize_with_csrf(dev_client)

    resp = dev_client.post(f"/api/inbox/{reply_id}/approve", headers=csrf_headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "approved"}

    assert await _fetch_status(reply_id) == "approved"


async def test_reject_flips_status_in_db(dev_client: TestClient, seeded_replies: list[str]) -> None:
    reply_id = await _seed_pending_reply(
        seeded_replies,
        conversation_id="conv-3",
        customer_id="cust-3",
        draft_text="Dạ giá sản phẩm là 199.000đ ạ.",
        intent="general",
        confidence=0.95,
    )
    csrf_headers = _authorize_with_csrf(dev_client)

    resp = dev_client.post(f"/api/inbox/{reply_id}/reject", headers=csrf_headers)
    assert resp.status_code == 200
    assert resp.json() == {"status": "rejected"}

    assert await _fetch_status(reply_id) == "rejected"


async def test_approve_without_csrf_header_is_rejected(
    dev_client: TestClient, seeded_replies: list[str]
) -> None:
    """Adversarial: session cookie alone (no X-CSRF-Token echo) must NOT be enough — that's
    the whole point of double-submit CSRF (spec 04 §10 PC8 / RED FLAG scan §4)."""
    reply_id = await _seed_pending_reply(
        seeded_replies,
        conversation_id="conv-4",
        customer_id="cust-4",
        draft_text="Dạ vâng ạ.",
        intent="general",
        confidence=0.5,
    )
    dev_client.post("/api/mock/authorize")  # session cookie set, CSRF header withheld

    resp = dev_client.post(f"/api/inbox/{reply_id}/approve")
    assert resp.status_code == 403

    assert await _fetch_status(reply_id) == "pending"
