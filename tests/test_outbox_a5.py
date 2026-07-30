"""Gate A5 — outbox queue: CTE §6.1 (I7), claim §6.2 (SKIP LOCKED), worker, reaper (I13).

Khoá bốn thứ:
1. I7: duplicate `(channel, msg_id)` ⇒ CTE trả None, KHÔNG có row outbox thứ hai.
2. §6.2: hai claimer ĐỒNG THỜI (transaction chồng nhau) không lấy trùng row.
3. Worker end-to-end: outbox row → `receive_and_draft` → PendingReply; done ⇒ payload
   NULL (O8 trim); drafter nổ ⇒ retry rồi `failed` sau MAX_ATTEMPTS.
4. I13: claim kẹt (worker chết sau claim) ⇒ reaper trả về `pending`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select

from app.worker_seller import MAX_ATTEMPTS, OutboxWorker
from bridge.zalo_sender import MockZaloSender
from db.models import Conversation, Customer, Outbox, PendingReply, Shop
from db.repos import OutboxRepo

_SHOP = "shop_a5"
_CUS = "cus_a5"
_CONV = "conv_a5"


async def _seed(session_factory) -> None:
    async with session_factory() as s:
        s.add(Shop(id=_SHOP, name="Shop A5"))
        s.add(Customer(id=_CUS, shop_id=_SHOP, channel="zalo", external_id="z-1"))
        await s.flush()
        s.add(Conversation(id=_CONV, shop_id=_SHOP, customer_id=_CUS, channel="zalo"))
        await s.commit()


async def _enqueue(session_factory, msg_id: str, text: str = "còn hàng ko") -> int:
    async with session_factory() as s:
        outbox_id = await OutboxRepo(s).record_and_enqueue(
            channel="zalo",
            platform_msg_id=msg_id,
            shop_id=_SHOP,
            payload={"customer_id": _CUS, "conversation_id": _CONV, "text": text},
            trace_id=uuid.uuid4(),
        )
    assert outbox_id is not None
    return outbox_id


@dataclass
class _D:
    text: str
    intent: str
    confidence: float


class _LowConfDrafter:
    async def draft(self, *, shop_id, customer_id, message, history) -> _D:
        return _D(text="draft trả lời", intent="general_qa", confidence=0.2)  # → park


class _BoomDrafter:
    async def draft(self, *, shop_id, customer_id, message, history) -> _D:
        raise RuntimeError("LLM nổ")


def _worker(session_factory, drafter=None) -> OutboxWorker:
    return OutboxWorker(
        drafter=drafter or _LowConfDrafter(),
        senders={"zalo": MockZaloSender()},
        session_factory=session_factory,
        shop_auto_enabled={},
    )


async def _row(session_factory, outbox_id: int) -> Outbox:
    async with session_factory() as s:
        return (await s.execute(select(Outbox).where(Outbox.outbox_id == outbox_id))).scalar_one()


# ── 1 · I7: CTE idempotent ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_event_enqueues_exactly_once(fresh_db) -> None:
    _, session_factory = await fresh_db()
    await _seed(session_factory)

    await _enqueue(session_factory, "dup-1")
    async with session_factory() as s:
        second = await OutboxRepo(s).record_and_enqueue(
            channel="zalo",
            platform_msg_id="dup-1",
            shop_id=_SHOP,
            payload={"customer_id": _CUS, "conversation_id": _CONV, "text": "retry"},
            trace_id=uuid.uuid4(),
        )
    assert second is None, "retry cùng khoá phải trả None (CTE 0 row), không enqueue lại"

    async with session_factory() as s:
        n = (await s.execute(select(func.count()).select_from(Outbox))).scalar_one()
    assert n == 1


# ── 2 · §6.2: claim không trùng dưới concurrency thật ──────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_claimers_never_take_the_same_row(fresh_db) -> None:
    """Transaction A claim (CHƯA commit) rồi B claim: SKIP LOCKED bắt B nhảy qua row A
    đang giữ — không chờ, không trùng. Đây là nội dung C2 áp cho outbox."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    id1 = await _enqueue(session_factory, "cc-1")
    id2 = await _enqueue(session_factory, "cc-2")

    async with session_factory() as sa, session_factory() as sb:
        rows_a = await OutboxRepo(sa).claim_batch(limit=1)  # giữ lock, KHÔNG commit vội
        rows_b = await OutboxRepo(sb).claim_batch(limit=1)
        await sa.commit()
        await sb.commit()

    got_a = {r["outbox_id"] for r in rows_a}
    got_b = {r["outbox_id"] for r in rows_b}
    assert got_a and got_b, "cả hai claimer đều phải lấy được việc (còn 2 row pending)"
    assert not (got_a & got_b), "hai claimer lấy TRÙNG row — SKIP LOCKED không hoạt động"
    assert got_a | got_b == {id1, id2}


# ── 3 · worker end-to-end ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_worker_drains_row_into_pending_reply(fresh_db) -> None:
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    outbox_id = await _enqueue(session_factory, "e2e-1")

    claimed = await _worker(session_factory).run_dispatch_once()
    assert claimed == 1

    async with session_factory() as s:
        drafts = (await s.execute(select(func.count()).select_from(PendingReply))).scalar_one()
    assert drafts == 1, "worker phải đưa row qua receive_and_draft → park"

    row = await _row(session_factory, outbox_id)
    assert row.status == "done"
    assert row.payload is None, "done phải NULL-out payload (O8 trim)"


@pytest.mark.asyncio
async def test_poison_row_retries_then_fails_without_killing_worker(fresh_db) -> None:
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    outbox_id = await _enqueue(session_factory, "poison-1")

    worker = _worker(session_factory, drafter=_BoomDrafter())
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claimed = await worker.run_dispatch_once()
        assert claimed == 1, f"lần thử {attempt}: row phải quay lại pending để claim được"

    row = await _row(session_factory, outbox_id)
    assert row.status == "failed", "hết MAX_ATTEMPTS phải là failed, không retry vô hạn"
    assert row.attempts == MAX_ATTEMPTS
    assert row.last_error and "LLM nổ" in row.last_error
    assert row.payload is not None, "failed GIỮ payload cho ops (khác done)"

    assert await worker.run_dispatch_once() == 0, "failed không được claim lại"


# ── 4 · I13: reaper gỡ claim kẹt ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reaper_releases_stuck_claim(fresh_db) -> None:
    """Mô phỏng worker chết SAU claim: row nằm `processing` với `claimed_at` cũ.
    Reaper phải trả nó về `pending`; vòng dispatch sau xử lý được bình thường."""
    from sqlalchemy import text

    _, session_factory = await fresh_db()
    await _seed(session_factory)
    outbox_id = await _enqueue(session_factory, "stuck-1")

    async with session_factory() as s:
        await OutboxRepo(s).claim_batch(limit=1)
        await s.commit()  # claim xong, "worker chết" — không settle
        await s.execute(
            text("UPDATE outbox SET claimed_at = now() - interval '10 minutes' WHERE outbox_id=:i"),
            {"i": outbox_id},
        )
        await s.commit()

    worker = _worker(session_factory)
    assert await worker.run_reaper_once() == 1

    row = await _row(session_factory, outbox_id)
    assert row.status == "pending"
    assert row.claimed_at is None

    assert await worker.run_dispatch_once() == 1, "row sống lại phải xử lý được"


@pytest.mark.asyncio
async def test_reaper_leaves_fresh_claims_alone(fresh_db) -> None:
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    await _enqueue(session_factory, "fresh-1")

    async with session_factory() as s:
        await OutboxRepo(s).claim_batch(limit=1)
        await s.commit()

    assert await _worker(session_factory).run_reaper_once() == 0, (
        "claim mới (trong hạn) không được reap — reaper chỉ gỡ claim KẸT"
    )
