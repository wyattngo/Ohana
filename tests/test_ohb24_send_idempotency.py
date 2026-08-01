"""OHB-24 · Send idempotency — dedup log chặn double-send khi crash-reap-resend.

**Gap trước OHB-24** (verify main `8aba78b`, comment tự-thú ở worker_seller.py:399):
send_one flow là send() → mark_sent. Crash sau send() thành công nhưng trước mark_sent
⇒ R5 (`_REAP_R5_STUCK_SEND_CLAIM`) NULL sent_claimed_at ⇒ worker khác re-claim ⇒
send() lần hai. Vô hại GĐ0 (MockZaloSender), nhưng ZaloSender thật ⇒ khách nhận 2 tin.

**Sau OHB-24:** flow ba pha reserve_send → send() → mark_sent. Reserve là INSERT ON
CONFLICT DO NOTHING RETURNING trên `public.sent_log` (PK reply_id) — lần hai re-claim,
reserve_send trả False ⇒ skip sender, mark_sent clear claim. Sender.send() gọi ĐÚNG 1
lần dù crash + re-claim.

Test scenario mô phỏng crash bằng monkey-patch `PendingReplyRepo.mark_sent` throw ở
lần gọi đầu; lần hai gọi tự nhiên (không patch) — chứng minh dedup chặn double-send.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text as sa_text

from app.worker_seller import WorkerDeps, run_send_loop
from db.models import Conversation, Customer, PendingReply, Shop

_SHOP = "shop_ohb24"
_CUS = "cus_ohb24"
_CONV = "conv_ohb24"


@dataclass
class _CountingSender:
    """Sender đếm số lần được gọi — gate exactly-once (well, dedup) của test này."""

    call_count: int = 0

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
        self.call_count += 1


async def _seed(session_factory) -> None:
    async with session_factory() as s:
        s.add(Shop(id=_SHOP, name="OHB24 Shop"))
        s.add(Customer(id=_CUS, shop_id=_SHOP, channel="zalo", external_id="z-ohb24"))
        await s.flush()
        s.add(Conversation(id=_CONV, shop_id=_SHOP, customer_id=_CUS, channel="zalo"))
        await s.commit()


async def _park_approved(session_factory) -> str:
    reply_id = uuid.uuid4().hex
    async with session_factory() as s:
        s.add(
            PendingReply(
                reply_id=reply_id,
                shop_id=_SHOP,
                conversation_id=_CONV,
                customer_id=_CUS,
                draft_text="draft đã duyệt",
                intent="general_qa",
                confidence=0.7,
                status="approved",
                trace_id=uuid.uuid4(),
                decided_by="seller_test",
                decided_at=datetime.now(UTC),
            )
        )
        await s.commit()
    return reply_id


async def _reap_r5(session_factory) -> None:
    """Backdate `sent_claimed_at` > 5' để R5 hoặc worker khác coi là stuck và re-claim.
    Ở đây mô phỏng trực tiếp bằng NULL cột (như R5 sẽ làm) — nhanh và tất định."""
    async with session_factory() as s:
        await s.execute(sa_text("UPDATE pending_reply SET sent_claimed_at = NULL"))
        await s.commit()


class _NoDraft:
    """Drafter placeholder — send loop không gọi drafter."""

    async def draft(self, *, shop_id, customer_id, message, history):
        raise AssertionError("send loop không được gọi drafter")


@pytest.mark.asyncio
async def test_dedup_log_prevents_double_send_after_crash(fresh_db, monkeypatch):
    """Gate OHB-24 · crash sau send() → re-claim → dedup log lock reply_id → sender
    KHÔNG gọi lần hai. Status cuối = 'sent' (lần re-claim mark_sent thành công qua
    đường dedup-hit)."""
    import db.repos as repos_module

    _, session_factory = await fresh_db()
    await _seed(session_factory)
    reply_id = await _park_approved(session_factory)

    sender = _CountingSender()

    # Monkey-patch mark_sent: lần đầu throw (giả lập crash sau khi send() đã thành
    # công), lần sau chạy tự nhiên.
    real_mark_sent = repos_module.PendingReplyRepo.mark_sent
    calls = {"n": 0}

    async def flaky_mark_sent(self, reply_id_arg, *, claimed_at):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash mid-mark_sent (post-send)")
        return await real_mark_sent(self, reply_id_arg, claimed_at=claimed_at)

    monkeypatch.setattr(repos_module.PendingReplyRepo, "mark_sent", flaky_mark_sent)

    deps = WorkerDeps(session_factory=session_factory, drafter=_NoDraft(), sender=sender)

    # Lượt 1: sender.send() OK → sent_log reserved → mark_sent throw → send_loop catch
    # exception → release_send_claim → job không mark 'sent'
    await run_send_loop(deps, run_once=True)
    assert sender.call_count == 1, "sender.send() phải chạy đúng 1 lần ở lượt claim đầu"
    async with session_factory() as s:
        row = await s.get(PendingReply, reply_id)
    assert row is not None
    assert row.status == "approved", (
        "sau crash mid-mark, status vẫn 'approved' — release_send_claim clear claim"
    )
    assert row.sent_claimed_at is None, "claim đã released cho lượt kế"

    # R5 mô phỏng (thực ra release đã NULL claim, backdate là belt-and-braces)
    await _reap_r5(session_factory)

    # Lượt 2: SendQueue re-claim → send_one gọi reserve_send → sent_log ĐÃ có reply_id
    # ⇒ False ⇒ SKIP sender ⇒ mark_sent chạy đường dedup-hit
    await run_send_loop(deps, run_once=True)

    assert sender.call_count == 1, (
        "GATE OHB-24 · sender.send() phải VẪN đúng 1 lần dù re-claim — dedup log chặn "
        f"double-send. Thấy {sender.call_count} lần."
    )
    async with session_factory() as s:
        row = await s.get(PendingReply, reply_id)
    assert row is not None
    assert row.status == "sent", "lượt re-claim mark_sent thành công qua đường dedup-hit"
    assert row.sent_claimed_at is None, "claim clear sau mark_sent"


@pytest.mark.asyncio
async def test_send_failure_rollback_dedup_allows_retry(fresh_db):
    """Send exception ⇒ rollback dedup ⇒ lượt kế retry được (không im lặng vĩnh viễn
    khi network chớp)."""

    class _BoomSender:
        async def send(self, *, shop_id, customer_id, text) -> None:
            raise RuntimeError("simulated network blip")

    _, session_factory = await fresh_db()
    await _seed(session_factory)
    reply_id = await _park_approved(session_factory)

    deps_boom = WorkerDeps(
        session_factory=session_factory, drafter=_NoDraft(), sender=_BoomSender()
    )
    await run_send_loop(deps_boom, run_once=True)
    async with session_factory() as s:
        row = await s.get(PendingReply, reply_id)
    assert row is not None
    assert row.status == "approved", "send lỗi ⇒ status giữ approved (rollback)"
    assert row.sent_claimed_at is None, "claim release cho lượt kế"

    # Lượt 2 với sender OK
    ok_sender = _CountingSender()
    deps_ok = WorkerDeps(session_factory=session_factory, drafter=_NoDraft(), sender=ok_sender)
    await run_send_loop(deps_ok, run_once=True)
    assert ok_sender.call_count == 1, "retry sau rollback: sender gọi được 1 lần"
    async with session_factory() as s:
        row = await s.get(PendingReply, reply_id)
    assert row is not None
    assert row.status == "sent", "retry thành công"
