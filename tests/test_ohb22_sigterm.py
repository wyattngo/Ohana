"""OHB-22 · SIGTERM graceful shutdown — claim nhường ngay, không chờ reaper 5' (I13).

Mô phỏng SIGTERM bằng `task.cancel()` trên task đang chạy loop. Signal thật (kill -TERM)
qua add_signal_handler khó test trong pytest — nhưng đường "cancel loop task" là NÚT thắt
kỹ thuật: signal handler chỉ set stop_event → run_worker cancel 4 loop task → mỗi loop
bắt CancelledError → release claim. Test cancel loop task trực tiếp đo đúng nhánh đó.

Ba gate:
1. Kill giữa compose (debounce loop) → `debounce_claimed_at` NULL (release_debounce_claim),
   timer + compose_failures GIỮ (cancel ≠ lỗi drafter).
2. Kill giữa send (send loop) → `sent_claimed_at` NULL, `status` giữ 'approved' cho lượt kế.
3. run_worker end-to-end với stop_event.set() → mọi task exit clean (không hang, không
   raise ngoài CancelledError).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from sqlalchemy import text as sa_text

from app.worker_seller import (
    WorkerDeps,
    run_debounce_loop,
    run_send_loop,
    run_worker,
)
from bridge.zalo_sender import MockZaloSender
from db.models import Conversation, Customer, Message, PendingReply, Shop

_SHOP = "shop_ohb22"
_CUS = "cus_ohb22"
_CONV = "conv_ohb22"


@dataclass
class _D:
    text: str
    intent: str
    confidence: float


class _BlockingDrafter:
    """Drafter chặn tới khi test cancel — mô phỏng LLM call dài giữa lúc SIGTERM đến."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def draft(self, *, shop_id, customer_id, message, history) -> _D:
        self.entered.set()
        await asyncio.sleep(60)  # bị cancel giữa chừng — không bao giờ về
        return _D(text="never", intent="general_qa", confidence=0.5)


class _BlockingSender:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def send(self, *, shop_id, customer_id, text) -> None:
        self.entered.set()
        await asyncio.sleep(60)


async def _seed(session_factory) -> None:
    async with session_factory() as s:
        s.add(Shop(id=_SHOP, name="OHB22 Shop"))
        s.add(Customer(id=_CUS, shop_id=_SHOP, channel="zalo", external_id="z-ohb22"))
        await s.flush()
        s.add(Conversation(id=_CONV, shop_id=_SHOP, customer_id=_CUS, channel="zalo"))
        await s.flush()
        s.add(
            Message(
                shop_id=_SHOP,
                conversation_id=_CONV,
                customer_id=_CUS,
                role="user",
                content="tin khách chờ compose",
            )
        )
        await s.commit()


async def _arm_debounce_past(session_factory) -> None:
    async with session_factory() as s:
        await s.execute(
            sa_text(
                "UPDATE conversations SET next_debounce_at = now() - interval '1 second', "
                "debounce_trace_id = :tid WHERE id = :cid"
            ),
            {"tid": uuid.uuid4(), "cid": _CONV},
        )
        await s.commit()


@pytest.mark.asyncio
async def test_ohb22_sigterm_releases_debounce_claim(fresh_db):
    """Kill giữa compose ⇒ debounce_claimed_at NULL, timer GIỮ, compose_failures = 0.
    Cancel ≠ lỗi drafter, worker khác/lượt kế claim lại NGAY tick sau (không chờ R3 5')."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    await _arm_debounce_past(session_factory)

    drafter = _BlockingDrafter()
    deps = WorkerDeps(session_factory=session_factory, drafter=drafter, sender=MockZaloSender())

    task = asyncio.create_task(run_debounce_loop(deps, run_once=True))
    # Đợi drafter thật sự vào (claim đã set trước đó rồi mới gọi drafter)
    await asyncio.wait_for(drafter.entered.wait(), timeout=5.0)

    # SIGTERM equivalent: cancel task đang xử lý
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Verify: claim released, timer giữ, không đếm poison
    async with session_factory() as s:
        conv = await s.get(Conversation, _CONV)
    assert conv is not None
    assert conv.debounce_claimed_at is None, (
        "SIGTERM giữa compose ⇒ release_debounce_claim phải NULL claim ngay (không chờ R3)"
    )
    assert conv.next_debounce_at is not None, "timer PHẢI giữ — tin khách vẫn chờ compose"
    assert conv.compose_failures == 0, "cancel không phải lỗi drafter, không đếm poison"


async def _park_approved(session_factory) -> str:
    """Seed một draft 'approved' để test cancel giữa send."""
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


@pytest.mark.asyncio
async def test_ohb22_sigterm_releases_send_claim(fresh_db):
    """Kill giữa send ⇒ sent_claimed_at NULL, status GIỮ 'approved'. Lượt claim kế lại
    ngay, không chờ R5 5'. KHÔNG mark 'sent' oan khi sender chưa xác nhận."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    reply_id = await _park_approved(session_factory)

    sender = _BlockingSender()

    class _NoDraft:
        async def draft(self, *, shop_id, customer_id, message, history):
            raise AssertionError("send loop không được gọi drafter")

    deps = WorkerDeps(session_factory=session_factory, drafter=_NoDraft(), sender=sender)
    task = asyncio.create_task(run_send_loop(deps, run_once=True))
    await asyncio.wait_for(sender.entered.wait(), timeout=5.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with session_factory() as s:
        row = await s.get(PendingReply, reply_id)
    assert row is not None
    assert row.status == "approved", "SIGTERM giữa send ⇒ status GIỮ approved (chưa xác nhận gửi)"
    assert row.sent_claimed_at is None, "release_send_claim phải clear claim ngay (không chờ R5)"


@pytest.mark.asyncio
async def test_ohb22_run_worker_shuts_down_on_signal(fresh_db, monkeypatch):
    """run_worker end-to-end: giả lập signal bằng cách set stop_event thủ công (không
    cài signal handler thật trong pytest — phần đó phụ thuộc platform, đã guard try/except
    trong run_worker). Gate: mọi loop exit clean, không hang, không raise."""
    _, session_factory = await fresh_db()
    deps = WorkerDeps(
        session_factory=session_factory,
        drafter=_BlockingDrafter(),
        sender=MockZaloSender(),
    )

    # Chạy run_worker và trigger SIGTERM sau 300ms bằng cách gửi SIGINT (asyncio bắt
    # trên Unix qua add_signal_handler đã cài trong run_worker; test này sanity-check
    # đường signal → shutdown clean).
    import os
    import signal as _signal

    async def _kick_signal() -> None:
        await asyncio.sleep(0.3)
        os.kill(os.getpid(), _signal.SIGINT)

    kick = asyncio.create_task(_kick_signal())
    try:
        # run_worker return None trên SIGTERM path (không raise) → asyncio.wait_for chờ.
        await asyncio.wait_for(run_worker(deps), timeout=10.0)
    finally:
        kick.cancel()
