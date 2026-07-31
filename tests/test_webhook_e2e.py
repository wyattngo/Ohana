"""Spec 17 P3 → A5 — webhook E2E: signed payload → verify → parse → QUEUE → worker → park.

Đường đầy đủ trong test env sau A5: POST signed Zalo envelope → verify_signature (P1) →
parse_inbound (P3, real envelope) → resolve_conversation → §6.1 record_and_enqueue → ACK
"queued". Webhook KHÔNG ghi `messages`, KHÔNG draft (CTE §6.1) — phần đó là của
`app/worker_seller.py`: outbox loop ghi tin khách (C1 dedup) + set debounce, debounce loop
compose → PendingReply. KHÔNG mount app thật (P4 blocked) — build_router + TestClient.

Idempotency: 2 webhook cùng `msg_id` → 1 event-log row + 1 outbox row + (sau worker) 1
`messages` row (qua `webhook_event_log` PK `(channel, platform_msg_id)` — I7 event ⟺ outbox
một câu).

Test cũ `test_e2e_append_failure_does_not_lose_message_on_retry` (atomic record_event +
append TRONG webhook) đã VIẾT LẠI theo luồng mới: webhook không append nữa, nên "không mất
tin khi append lỗi" giờ nghĩa là outbox job lỗi quay về `pending` và retry ghi được message.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy import text as sa_text

from api.webhook import build_router
from app.worker_seller import WorkerDeps, run_debounce_loop, run_outbox_loop, run_send_loop
from bridge.zalo_sender import MockZaloSender
from channels.zalo import ZaloChannel
from db.models import Conversation, Customer, Message, Outbox, PendingReply, Shop
from db.repos import PendingReplyRepo, ZaloOATokenRepo

_APP_ID = "2074138120372622546"
_OA_ID = "2074138120372622547"
_USER_ID = "3742389367648617405"
_OA_SECRET = "e2e-oa-secret-per-oa"


def _sig(app_id: str, raw: bytes, ts: str, secret: str) -> str:
    return hashlib.sha256((app_id + raw.decode("utf-8") + ts + secret).encode("utf-8")).hexdigest()


def _envelope(text: str = "còn hàng ko", msg_id: str = "e2e-msg-1") -> tuple[bytes, str]:
    ts = str(int(datetime.now(UTC).timestamp() * 1000))
    payload = {
        "app_id": _APP_ID,
        "sender": {"id": _USER_ID},
        "recipient": {"id": _OA_ID},
        "event_name": "user_send_text",
        "message": {"text": text, "msg_id": msg_id},
        "timestamp": ts,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8"), ts


@dataclass
class _D:
    text: str
    intent: str
    confidence: float


class _LowConfDrafter:
    async def draft(self, *, shop_id, customer_id, message, history) -> _D:
        return _D(text="draft trả lời", intent="general_qa", confidence=0.2)  # → park


async def _seed(session_factory) -> None:
    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(Shop(id="shop_a", name="Shop A"))
        await s.commit()
        await ZaloOATokenRepo(s).update_tokens_locked(
            shop_id="shop_a",
            oa_id=_OA_ID,
            access_token="a",
            refresh_token="r",
            access_expires_at=now + timedelta(hours=1),
            refresh_expires_at=now + timedelta(days=90),
            oa_secret_key=_OA_SECRET,
        )


def _client(session_factory) -> TestClient:
    router = build_router(
        session_factory,
        channels={"zalo": ZaloChannel(sender=MockZaloSender())},  # type: ignore[dict-item]
        endpoint_to_shop={("zalo", "EP1"): "shop_a"},
        enabled=True,
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


async def _count(session_factory, model) -> int:
    async with session_factory() as s:
        return (await s.execute(select(func.count()).select_from(model))).scalar_one()


async def _backdate_debounce(session_factory) -> None:
    """Timer debounce mặc định là tương lai (DEBOUNCE_DELAY_SECONDS) — kéo về quá khứ để
    `due_conversations` (SchedulerRepo, cột `next_debounce_at`) thấy ngay trong test."""
    async with session_factory() as s:
        await s.execute(
            sa_text("UPDATE conversations SET next_debounce_at = now() - interval '1 second'")
        )
        await s.commit()


@pytest.mark.asyncio
async def test_e2e_signed_payload_queues_then_worker_parks(fresh_db):
    """Full path A5: signed envelope → verify → parse → QUEUED (webhook không draft inline
    nữa — ACK ≤2s, design §7) → worker outbox ghi tin → worker debounce compose → park.
    Message VÀ PendingReply đều chỉ có SAU worker."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)

    raw, ts = _envelope()
    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"X-ZEvent-Signature": _sig(_APP_ID, raw, ts, _OA_SECRET)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "queued"
    assert resp.headers.get("X-Trace-Id"), "trace_id phải ra header (design §9)"

    # Webhook xong: identity + queue row — CHƯA có message, CHƯA có draft (CTE §6.1)
    assert await _count(session_factory, Customer) == 1
    assert await _count(session_factory, Conversation) == 1
    assert await _count(session_factory, Outbox) == 1
    assert await _count(session_factory, Message) == 0, "ghi messages là việc của worker"
    assert await _count(session_factory, PendingReply) == 0, "draft là việc của worker"

    # Worker loop 1 (outbox §6.2): ghi tin khách + set debounce — chưa compose
    deps = WorkerDeps(
        session_factory=session_factory, drafter=_LowConfDrafter(), sender=MockZaloSender()
    )
    await run_outbox_loop(deps, run_once=True)
    assert await _count(session_factory, Message) == 1  # tin khách
    assert await _count(session_factory, PendingReply) == 0, "compose thuộc loop debounce"

    # Worker loop 2 (debounce §6.3): backdate timer rồi compose → park
    await _backdate_debounce(session_factory)
    await run_debounce_loop(deps, run_once=True)
    assert await _count(session_factory, PendingReply) == 1


@pytest.mark.asyncio
async def test_e2e_duplicate_msg_id_idempotent(fresh_db):
    """2 webhook cùng msg_id → 1 outbox row, và sau worker chỉ 1 messages row
    (webhook_event_log dedup — I7 event ⟺ outbox một câu)."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)

    raw, ts = _envelope(msg_id="dup-msg")
    headers = {"X-ZEvent-Signature": _sig(_APP_ID, raw, ts, _OA_SECRET)}

    r1 = client.post("/webhook/zalo/EP1", content=raw, headers=headers)
    r2 = client.post("/webhook/zalo/EP1", content=raw, headers=headers)
    assert r1.status_code == 200
    assert r1.json()["action"] == "queued"
    assert r2.status_code == 200, "retry cùng msg_id vẫn ACK 200 (không lỗi)"
    assert r2.json()["action"] == "duplicate"
    assert r2.headers.get("X-Trace-Id"), "nhánh duplicate cũng phải mang X-Trace-Id"

    # Idempotent: KHÔNG nhân đôi row trong queue (I7)
    assert await _count(session_factory, Outbox) == 1, "duplicate không được enqueue lần hai"

    # Và sau khi worker drain: đúng 1 tin khách
    deps = WorkerDeps(
        session_factory=session_factory, drafter=_LowConfDrafter(), sender=MockZaloSender()
    )
    await run_outbox_loop(deps, run_once=True)
    assert await _count(session_factory, Message) == 1, "duplicate msg_id không được tạo 2 messages"


@pytest.mark.asyncio
async def test_e2e_append_failure_does_not_lose_message_on_retry(fresh_db, monkeypatch):
    """Viết lại cho A5 (chủ thể cũ — atomic record_event + append TRONG webhook — đã xoá):
    giờ "không mất tin" nghĩa là job outbox lỗi lúc `append_inbound` quay về `pending`
    (mark_failed, backoff) chứ KHÔNG done/dead, và lượt claim sau ghi được tin khách.

    Mô phỏng: lần chạy outbox loop đầu, `MessageRepo.append_inbound` throw (DB blip) →
    job failed → messages trống nhưng outbox row còn nguyên pending. Backdate
    `next_retry_at` (backoff 2^attempts giây) rồi chạy loop lần hai → tin khách recover.
    """
    import db.repos as repos_module

    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)

    raw, ts = _envelope(msg_id="flaky-msg")
    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"X-ZEvent-Signature": _sig(_APP_ID, raw, ts, _OA_SECRET)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "queued"

    real_append = repos_module.MessageRepo.append_inbound
    calls = {"n": 0}

    async def flaky_append(self, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated DB blip during append_inbound")
        return await real_append(self, **kwargs)

    monkeypatch.setattr(repos_module.MessageRepo, "append_inbound", flaky_append)

    # Lần 1: append throw → job failed → về pending (KHÔNG mất, KHÔNG dead)
    deps = WorkerDeps(
        session_factory=session_factory, drafter=_LowConfDrafter(), sender=MockZaloSender()
    )
    await run_outbox_loop(deps, run_once=True)
    assert await _count(session_factory, Message) == 0
    async with session_factory() as s:
        status = (await s.execute(select(Outbox.status))).scalar_one()
    assert status == "pending", f"job lỗi phải quay về pending để retry, thấy {status!r}"

    # Backoff đẩy next_retry_at về tương lai — kéo về quá khứ để claim lại ngay trong test
    async with session_factory() as s:
        await s.execute(sa_text("UPDATE outbox SET next_retry_at = now() - interval '1 second'"))
        await s.commit()

    # Lần 2: append OK → tin khách reprocess, KHÔNG mất
    await run_outbox_loop(deps, run_once=True)
    assert await _count(session_factory, Message) == 1, "tin khách phải recover ở retry, không mất"


async def _deliver(client, *, text: str, msg_id: str) -> None:
    """Webhook một tin khách vào (đã ký) — dừng ở outbox, chưa chạy worker."""
    raw, ts = _envelope(text=text, msg_id=msg_id)
    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"X-ZEvent-Signature": _sig(_APP_ID, raw, ts, _OA_SECRET)},
    )
    assert resp.status_code == 200, resp.text


async def _n_pending(session_factory) -> int:
    async with session_factory() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(PendingReply)
                .where(PendingReply.status == "pending")
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_o9_second_message_while_pending_yields_single_draft(fresh_db):
    """O9=B (OHB-7 · gate của bước): tin thứ hai tới KHI draft đầu vẫn `pending` ⇒ vẫn ĐÚNG
    1 draft. Guard `has_pending` (trước claim_debounce) bỏ lượt compose thứ hai; tin mới
    KHÔNG mất — vẫn bền trong `messages` (chứng minh no-drop ở test kế)."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)
    deps = WorkerDeps(
        session_factory=session_factory, drafter=_LowConfDrafter(), sender=MockZaloSender()
    )

    # Tin 1 → outbox → debounce → 1 draft pending
    await _deliver(client, text="tin 1", msg_id="o9-msg-1")
    await run_outbox_loop(deps, run_once=True)
    await _backdate_debounce(session_factory)
    await run_debounce_loop(deps, run_once=True)
    assert await _n_pending(session_factory) == 1

    # Tin 2 tới KHI draft 1 CHƯA được quyết → outbox ghi tin + arm timer → debounce loop
    await _deliver(client, text="tin 2", msg_id="o9-msg-2")
    await run_outbox_loop(deps, run_once=True)
    assert await _count(session_factory, Message) == 2, "tin 2 phải bền trong messages (no-drop)"
    await _backdate_debounce(session_factory)
    await run_debounce_loop(deps, run_once=True)

    # Guard chặn compose lượt hai — vẫn đúng 1 draft đang chờ seller
    assert await _n_pending(session_factory) == 1, "O9=B: pending đang mở ⇒ không draft thứ hai"
    assert await _count(session_factory, PendingReply) == 1, "không có row draft thứ hai nào"


@pytest.mark.asyncio
async def test_o9_accumulated_message_swept_after_pending_resolved(fresh_db):
    """No-drop (OHB-7): tin tới lúc pending KHÔNG bị nuốt. Sau khi seller quyết draft đầu
    (reject → rời `pending`), timer vẫn due (guard không clear) nên lượt compose kế cuốn
    tin tích luỹ thành draft MỚI."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)
    deps = WorkerDeps(
        session_factory=session_factory, drafter=_LowConfDrafter(), sender=MockZaloSender()
    )

    # Draft 1 pending, rồi tin 2 tới bị guard chặn (đúng 1 draft) — như test trên
    await _deliver(client, text="tin 1", msg_id="o9d-msg-1")
    await run_outbox_loop(deps, run_once=True)
    await _backdate_debounce(session_factory)
    await run_debounce_loop(deps, run_once=True)
    await _deliver(client, text="tin 2", msg_id="o9d-msg-2")
    await run_outbox_loop(deps, run_once=True)
    await _backdate_debounce(session_factory)
    await run_debounce_loop(deps, run_once=True)
    assert await _n_pending(session_factory) == 1

    # Seller quyết draft 1 (reject) → rời trạng thái pending
    async with session_factory() as s:
        reply_id = (
            await s.execute(select(PendingReply.reply_id).where(PendingReply.status == "pending"))
        ).scalar_one()
    async with session_factory() as s:
        n = await PendingReplyRepo(s, shop_scope="shop_a").mark_decided(
            reply_id, new_status="rejected", decided_by="seller_x"
        )
    assert n == 1

    # Timer vẫn due (guard chưa từng clear) → compose kế cuốn tin 2 thành draft mới
    await _backdate_debounce(session_factory)
    await run_debounce_loop(deps, run_once=True)
    assert await _n_pending(session_factory) == 1, (
        "tin tích luỹ được compose sau khi draft cũ quyết"
    )
    assert await _count(session_factory, PendingReply) == 2, (
        "draft cũ (rejected) + draft mới (pending)"
    )


async def _approve_only_pending(session_factory) -> str:
    """Seller-approve nhánh test: đọc reply pending duy nhất, mark_decided → 'approved'."""
    async with session_factory() as s:
        reply_id = (
            await s.execute(select(PendingReply.reply_id).where(PendingReply.status == "pending"))
        ).scalar_one()
    async with session_factory() as s:
        n = await PendingReplyRepo(s, shop_scope="shop_a").mark_decided(
            reply_id, new_status="approved", decided_by="seller_e2e"
        )
    assert n == 1, "mark_decided phải update đúng 1 row"
    return reply_id


@pytest.mark.asyncio
async def test_ohb23_send_worker_drains_approved_and_marks_sent(fresh_db):
    """Gate OHB-23: seed 1 draft `approved` → `run_send_loop` (Mock) → status='sent' +
    `MockZaloSender.send()` gọi đúng payload (shop_id/customer_id/text)."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)
    sender = MockZaloSender()
    deps = WorkerDeps(session_factory=session_factory, drafter=_LowConfDrafter(), sender=sender)

    # Đầy đủ đường: webhook → outbox → debounce → park → seller-approve
    await _deliver(client, text="ship uy tín?", msg_id="ohb23-msg-1")
    await run_outbox_loop(deps, run_once=True)
    await _backdate_debounce(session_factory)
    await run_debounce_loop(deps, run_once=True)
    reply_id = await _approve_only_pending(session_factory)

    # Send-worker drain
    await run_send_loop(deps, run_once=True)

    # Payload: shop_id + customer_id + text đúng draft đã ghi (draft_text = "draft trả lời")
    assert len(sender.sends) == 1, f"gọi Mock đúng 1 lần, thấy {len(sender.sends)}"
    call = sender.sends[0]
    assert call["shop_id"] == "shop_a"
    assert call["text"] == "draft trả lời"
    assert call["customer_id"], "customer_id phải có (từ row PendingReply)"

    # Status → 'sent', claim đã clear (mark_sent set sent_claimed_at = NULL)
    async with session_factory() as s:
        row = await s.get(PendingReply, reply_id)
    assert row is not None
    assert row.status == "sent"
    assert row.sent_claimed_at is None, "mark_sent phải clear claim"


@pytest.mark.asyncio
async def test_ohb23_send_error_releases_claim_no_status_flip(fresh_db):
    """OHB-23 no-mark-oan: sender raise ⇒ release claim, status GIỮ 'approved' cho lượt kế
    thử lại. KHÔNG chuyển 'sent' oan khi sender chưa xác nhận (I13 semantics: release là
    thả token sở hữu, không phải quyết định trạng thái)."""

    class _BoomSender:
        async def send(self, *, shop_id, customer_id, text) -> None:
            raise RuntimeError("simulated Zalo network blip")

    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)
    # Compose bằng deps có Mock; send loop dùng deps có Boom
    deps_compose = WorkerDeps(
        session_factory=session_factory, drafter=_LowConfDrafter(), sender=MockZaloSender()
    )
    deps_send = WorkerDeps(
        session_factory=session_factory, drafter=_LowConfDrafter(), sender=_BoomSender()
    )

    await _deliver(client, text="tin lỗi mạng", msg_id="ohb23-boom-1")
    await run_outbox_loop(deps_compose, run_once=True)
    await _backdate_debounce(session_factory)
    await run_debounce_loop(deps_compose, run_once=True)
    reply_id = await _approve_only_pending(session_factory)

    # Send lỗi — loop bắt exception, release, tiếp tục (không chết loop)
    await run_send_loop(deps_send, run_once=True)

    async with session_factory() as s:
        row = await s.get(PendingReply, reply_id)
    assert row is not None
    assert row.status == "approved", "sender fail ⇒ status GIỮ approved, không mark sent oan"
    assert row.sent_claimed_at is None, "release_send_claim phải clear claim để retry ngay"

    # Lượt kế với Mock OK → sent thành công (chứng minh retry-ngay không cần R5)
    sender = MockZaloSender()
    deps_retry = WorkerDeps(
        session_factory=session_factory, drafter=_LowConfDrafter(), sender=sender
    )
    await run_send_loop(deps_retry, run_once=True)
    assert len(sender.sends) == 1
    async with session_factory() as s:
        row = await s.get(PendingReply, reply_id)
    assert row is not None and row.status == "sent"


@pytest.mark.asyncio
async def test_e2e_bad_signature_401_no_processing(fresh_db):
    """Verify fail → 401, KHÔNG parse, KHÔNG ghi row nào (chốt chặn P1 giữ trên đường E2E)."""
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    client = _client(session_factory)

    raw, _ = _envelope()
    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"X-ZEvent-Signature": "f" * 64},
    )
    assert resp.status_code == 401
    assert await _count(session_factory, Customer) == 0
    assert await _count(session_factory, Message) == 0
    assert await _count(session_factory, Outbox) == 0
    assert await _count(session_factory, PendingReply) == 0
