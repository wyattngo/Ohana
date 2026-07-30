"""Spec 14 — schema-shaping: draft TTL/snapshot/label (A0) + webhook idempotency (B0).

A0 chỉ dựng SCHEMA + repo surface (workflow §2.3/§2.5, §7 bước 3). KHÔNG capture snapshot
lúc draft, KHÔNG tính TTL, KHÔNG edit-path — cột nullable sẵn để runtime sau ghi vào mà
không phải migrate dữ liệu. `label` set tất định từ `new_status` NGAY (nuôi §8.1) — đây là
tín hiệu train auto-send GĐ1, không ghi từ đầu = phải tích lại từ đầu.

`label` ≠ `status`: status là lifecycle gửi (pending→approved→sent|rejected), label là tín
hiệu train (approved|rejected|edited). Trùng cho approve/reject, LỆCH cho edited (sửa text
rồi duyệt = status approved nhưng label edited). Cột riêng — gộp là mất `edited` mãi mãi.

B0 — webhook_event_log: idempotency ở tầng DB (workflow §2.1 ràng buộc #2). PK
`(channel, platform_msg_id)` + `record_event` on_conflict_do_nothing ⇒ retry Zalo cùng
payload KHÔNG nhân đôi. Repo KHÔNG shop-scoped: idempotency là biên giới nền-tảng, không
phải dữ liệu tenant. Race-safe bằng MỘT câu insert (KHÔNG select-then-insert = ISSUE-017).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

from channels.identity import resolve_conversation
from db.models import PendingReply
from db.repos import PendingReplyRepo

pytestmark = pytest.mark.asyncio

_SHOP = "shop_a"


async def _seed_parents(session_factory) -> tuple[str, str]:  # type: ignore[no-untyped-def]
    """Tạo Customer + Conversation thật (composite FK của PendingReply đòi cha cùng shop)."""
    async with session_factory() as s:
        return await resolve_conversation(
            s, shop_id=_SHOP, channel="zalo", external_user_id=f"u-{uuid.uuid4().hex[:8]}"
        )


async def _make_reply(session_factory, **kw):  # type: ignore[no-untyped-def]
    customer_id, conversation_id = await _seed_parents(session_factory)
    async with session_factory() as s:
        repo = PendingReplyRepo(s, shop_scope=_SHOP)
        return await repo.create(
            reply_id=uuid.uuid4().hex,
            conversation_id=conversation_id,
            customer_id=customer_id,
            draft_text="chào bạn",
            intent="general_qa",
            confidence=0.5,
            **kw,
        )


async def test_new_columns_exist_and_default_null(fresh_db) -> None:  # type: ignore[no-untyped-def]
    """create() không truyền snapshot/expires ⇒ hàng cũ vẫn tạo được, 3 cột = None."""
    _, session_factory = await fresh_db()
    row = await _make_reply(session_factory)
    assert row.snapshot is None
    assert row.expires_at is None
    assert row.label is None


async def test_create_persists_snapshot_and_expires(fresh_db) -> None:  # type: ignore[no-untyped-def]
    """create() truyền snapshot (JSONB) + expires_at ⇒ lưu đúng, đọc lại khớp."""
    _, session_factory = await fresh_db()
    snap = {"price": 199000, "in_stock": True, "captured_at": "2026-07-21T00:00:00Z"}
    exp = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    row = await _make_reply(session_factory, snapshot=snap, expires_at=exp)

    async with session_factory() as s:
        got = await PendingReplyRepo(s, shop_scope=_SHOP).get(row.reply_id)
    assert got is not None
    assert got.snapshot == snap
    assert got.expires_at == exp


async def test_mark_decided_approved_sets_label_approved(fresh_db) -> None:  # type: ignore[no-untyped-def]
    """mark_decided(new_status='approved') ⇒ label='approved' (derive trong repo)."""
    _, session_factory = await fresh_db()
    row = await _make_reply(session_factory)
    async with session_factory() as s:
        n = await PendingReplyRepo(s, shop_scope=_SHOP).mark_decided(
            row.reply_id, new_status="approved", decided_by="seller-1"
        )
    assert n == 1
    async with session_factory() as s:
        got = await PendingReplyRepo(s, shop_scope=_SHOP).get(row.reply_id)
    assert got is not None
    assert got.status == "approved"
    assert got.label == "approved"


async def test_mark_decided_rejected_sets_label_rejected(fresh_db) -> None:  # type: ignore[no-untyped-def]
    """mark_decided(new_status='rejected') ⇒ label='rejected'."""
    _, session_factory = await fresh_db()
    row = await _make_reply(session_factory)
    async with session_factory() as s:
        await PendingReplyRepo(s, shop_scope=_SHOP).mark_decided(
            row.reply_id, new_status="rejected", decided_by="seller-1"
        )
    async with session_factory() as s:
        got = await PendingReplyRepo(s, shop_scope=_SHOP).get(row.reply_id)
    assert got is not None
    assert got.label == "rejected"


async def test_mark_decided_sent_does_not_set_label(fresh_db) -> None:  # type: ignore[no-untyped-def]
    """`sent` là bước lifecycle nội bộ (worker gửi), KHÔNG phải quyết định của seller ⇒
    KHÔNG là tín hiệu train. Label chỉ ghi ở approve/reject (và edited sau). Một reply
    approved→sent phải GIỮ label='approved', không bị 'sent' đè lên."""
    _, session_factory = await fresh_db()
    row = await _make_reply(session_factory)
    async with session_factory() as s:
        repo = PendingReplyRepo(s, shop_scope=_SHOP)
        await repo.mark_decided(row.reply_id, new_status="approved", decided_by="seller-1")
    async with session_factory() as s:
        repo = PendingReplyRepo(s, shop_scope=_SHOP)
        await repo.mark_decided(row.reply_id, new_status="sent", decided_by="worker")
    async with session_factory() as s:
        got = await PendingReplyRepo(s, shop_scope=_SHOP).get(row.reply_id)
    assert got is not None
    assert got.status == "sent"
    assert got.label == "approved", "label train-signal KHÔNG được 'sent' đè"


async def test_label_check_constraint_rejects_bad_value(fresh_db) -> None:  # type: ignore[no-untyped-def]
    """CHECK ở tầng DB: label ngoài {approved,rejected,edited} bị từ chối — hàng rào không
    ai bypass được (raw SQL cũng chịu), khác Pydantic bị bypass bởi raw SQL."""
    _, session_factory = await fresh_db()
    customer_id, conversation_id = await _seed_parents(session_factory)
    with pytest.raises(IntegrityError):
        async with session_factory() as s:
            s.add(
                PendingReply(
                    reply_id=uuid.uuid4().hex,
                    shop_id=_SHOP,
                    conversation_id=conversation_id,
                    customer_id=customer_id,
                    draft_text="x",
                    intent="general_qa",
                    confidence=0.5,
                    status="pending",
                    label="garbage",
                )
            )
            await s.commit()


# ======================================================================================
# B0 — webhook_event_log idempotency
# ======================================================================================


async def _count_events(session_factory) -> int:  # type: ignore[no-untyped-def]
    from sqlalchemy import func, select

    from db.models import WebhookEventLog

    async with session_factory() as s:
        return (await s.execute(select(func.count()).select_from(WebhookEventLog))).scalar_one()


async def test_record_event_first_time_returns_true_and_creates_row(fresh_db) -> None:  # type: ignore[no-untyped-def]
    from db.repos import WebhookEventRepo

    _, session_factory = await fresh_db()
    async with session_factory() as s:
        first = await WebhookEventRepo(s).record_event(
            channel="zalo", platform_msg_id="msg-1", shop_id=_SHOP
        )
    assert first is True
    assert await _count_events(session_factory) == 1


async def test_record_event_duplicate_returns_false_single_row(fresh_db) -> None:  # type: ignore[no-untyped-def]
    """Retry cùng key ⇒ False + vẫn 1 row. Đây là bất biến chống-nhân-đôi của webhook."""
    from db.repos import WebhookEventRepo

    _, session_factory = await fresh_db()
    async with session_factory() as s:
        assert await WebhookEventRepo(s).record_event(
            channel="zalo", platform_msg_id="msg-1", shop_id=_SHOP
        )
    async with session_factory() as s:
        second = await WebhookEventRepo(s).record_event(
            channel="zalo", platform_msg_id="msg-1", shop_id=_SHOP
        )
    assert second is False
    assert await _count_events(session_factory) == 1


async def test_record_event_key_is_channel_plus_msg_id(fresh_db) -> None:  # type: ignore[no-untyped-def]
    """Cùng platform_msg_id nhưng KHÁC channel ⇒ 2 row (key là cặp). Và khác msg_id ⇒ 2 row."""
    from db.repos import WebhookEventRepo

    _, session_factory = await fresh_db()
    async with session_factory() as s:
        repo = WebhookEventRepo(s)
        assert await repo.record_event(channel="zalo", platform_msg_id="m", shop_id=_SHOP)
        assert await repo.record_event(channel="fb", platform_msg_id="m", shop_id=_SHOP)
        assert await repo.record_event(channel="zalo", platform_msg_id="m2", shop_id=_SHOP)
    assert await _count_events(session_factory) == 3


async def test_record_event_concurrent_same_key_yields_one_row(fresh_db) -> None:  # type: ignore[no-untyped-def]
    """HAI record_event ĐỒNG THỜI cùng key (session riêng) ⇒ đúng MỘT row, đúng MỘT True.

    Đây là ca thật của webhook: Zalo retry trong khi request đầu chưa commit. Chống trùng
    phải ở tầng DB (on_conflict), KHÔNG select-then-insert — select-then-insert cho cả hai
    thấy 'chưa có' rồi insert cả hai (ISSUE-017 spec 09 vừa đóng cho Conversation)."""
    from db.repos import WebhookEventRepo

    _, session_factory = await fresh_db()

    async def _one() -> bool:
        async with session_factory() as s:
            return await WebhookEventRepo(s).record_event(
                channel="zalo", platform_msg_id="race", shop_id=_SHOP
            )

    results = await asyncio.gather(_one(), _one())
    assert sum(results) == 1, f"đúng một True mong đợi, được {results}"
    assert await _count_events(session_factory) == 1
