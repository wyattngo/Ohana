"""Spec 17 P3 — ZaloChannel.parse_inbound real envelope (RISK: medium, RED first).

Envelope Zalo THẬT (docs 2026-07-24): `{app_id, sender:{id}, recipient:{id}, event_name,
message:{text, msg_id, attachments?}, timestamp}`. KHÔNG có `oa_id` top-level.

parse_inbound:
- `user_send_text` → InboundMessage (external_user_id=sender.id, platform_msg_id=message.msg_id)
- event khác (image/sticker/oa_send_*/unknown) → None (skip, log INFO — KHÔNG raise, crash =
  webhook 500 = Zalo retry storm)
- thiếu sender.id / message.text → ValueError (fail loud, không attribute nhầm khách)
"""

from __future__ import annotations

import logging

import pytest

from bridge.zalo_sender import MockZaloSender
from channels.base import InboundMessage
from channels.zalo import ZaloChannel

_OA_ID = "2074138120372622547"
_USER_ID = "3742389367648617405"


def _channel() -> ZaloChannel:
    return ZaloChannel(sender=MockZaloSender())


def _envelope(
    *,
    event_name: str = "user_send_text",
    sender_id: str = _USER_ID,
    recipient_id: str = _OA_ID,
    text: str | None = "còn hàng size L không anh?",
    msg_id: str = "43b59c025aebfb5e6fa",
    timestamp: str = "1602560967477",
    extra_message: dict | None = None,
) -> dict:
    message: dict = {"msg_id": msg_id}
    if text is not None:
        message["text"] = text
    if extra_message:
        message.update(extra_message)
    return {
        "app_id": "2074138120372622546",
        "sender": {"id": sender_id},
        "recipient": {"id": recipient_id},
        "event_name": event_name,
        "message": message,
        "timestamp": timestamp,
    }


def test_valid_user_send_text_minimal():
    msg = _channel().parse_inbound(_envelope())
    assert isinstance(msg, InboundMessage)
    assert msg.external_user_id == _USER_ID, "sender.id = customer external id"
    assert msg.text == "còn hàng size L không anh?"
    assert msg.platform_msg_id == "43b59c025aebfb5e6fa"


def test_valid_user_send_text_ignores_unknown_message_field():
    """Zalo thêm field lạ (vd `attachments`) không được làm crash — TypedDict + get() mềm."""
    env = _envelope(extra_message={"attachments": [{"type": "sticker", "payload": {}}]})
    msg = _channel().parse_inbound(env)
    assert isinstance(msg, InboundMessage)
    assert msg.text == "còn hàng size L không anh?"


def test_missing_sender_id_raises():
    env = _envelope()
    del env["sender"]["id"]
    with pytest.raises(ValueError):
        _channel().parse_inbound(env)


def test_missing_message_text_raises():
    env = _envelope(text=None)  # message có msg_id nhưng không text
    with pytest.raises(ValueError):
        _channel().parse_inbound(env)


def test_missing_message_msg_id_raises():
    """msg_id BẮT BUỘC (P3 review HIGH #2): là khoá idempotency. Thiếu ⇒ raise, KHÔNG default
    None — None làm webhook bỏ qua dedup → payload thiếu msg_id → mỗi retry xử lý lại
    (amplification). Zalo LUÔN gửi msg_id cho user_send_text (docs 2026-07-24)."""
    env = _envelope()
    del env["message"]["msg_id"]
    with pytest.raises(ValueError):
        _channel().parse_inbound(env)


def test_unknown_event_name_returns_none(caplog):
    """Event Zalo thêm mới (chưa doc) → None + log INFO, KHÔNG raise (chống retry storm)."""
    env = _envelope(event_name="user_send_hologram_2027")
    with caplog.at_level(logging.INFO):
        result = _channel().parse_inbound(env)
    assert result is None
    assert any(
        "skip" in r.message.lower() or "hologram" in r.message.lower() for r in caplog.records
    )


def test_known_non_text_event_returns_none():
    """user_send_image = known event nhưng spec 17 chỉ text ⇒ skip (None), không raise."""
    env = _envelope(event_name="user_send_image", text=None)
    result = _channel().parse_inbound(env)
    assert result is None


def test_oa_send_echo_event_returns_none():
    """oa_send_text (OA echo tin mình gửi) → skip, không xử lý như tin khách."""
    env = _envelope(event_name="oa_send_text", sender_id=_OA_ID, recipient_id=_USER_ID)
    result = _channel().parse_inbound(env)
    assert result is None


def test_malformed_missing_message_object_raises():
    """message không phải dict / thiếu hẳn ⇒ ValueError (không silent None cho text event)."""
    env = _envelope()
    del env["message"]
    with pytest.raises(ValueError):
        _channel().parse_inbound(env)
