"""Spec 17 P1 — Webhook signature verify wired vào endpoint (RISK: high, RED first).

Test level cao hơn `test_zalo_signature.py`: mock full HTTP stack qua `TestClient`, chứng
minh signature verify chạy TRƯỚC parse — nếu verify fail, `parse_inbound` KHÔNG chạy (không
side effect, không log gợi ý payload).

Cả 4 case dùng cùng cấu hình router: FakeChannel (fakechan), endpoint_to_shop, MockDrafter.
Chỉ khác signature header và body content.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.webhook import build_router
from channels.base import InboundMessage
from channels.zalo.signature import verify_zalo_signature
from db.models import Shop
from db.repos import ZaloOATokenRepo

_APP_ID = "2074138120372622546"
_OA_ID = "2074138120372622547"
_USER_ID = "3742389367648617405"
_OA_SECRET = "the-oa-secret-per-oa"


def _compute_signature(app_id: str, raw_body: bytes, timestamp: str, oa_secret: str) -> str:
    base = app_id + raw_body.decode("utf-8") + timestamp + oa_secret
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _build_zalo_payload(text: str = "còn hàng ko", ts: str | None = None) -> tuple[bytes, str]:
    import time

    ts = ts if ts is not None else str(int(time.time() * 1000))
    payload = {
        "app_id": _APP_ID,
        "sender": {"id": _USER_ID},
        "recipient": {"id": _OA_ID},
        "event_name": "user_send_text",
        "message": {"text": text, "msg_id": "test-msg-1"},
        "timestamp": ts,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8"), ts


@dataclass
class _D:
    text: str
    intent: str
    confidence: float


class _NoOpDrafter:
    """Drafter luôn park — spec 17 P1 chỉ test verify path, không test draft."""

    async def draft(
        self, *, shop_id: str, customer_id: str, message: str, history: list[Any]
    ) -> _D:
        return _D(text="draft", intent="general_qa", confidence=0.2)


class _ParseTracker:
    """Adapter ghi lại `parse_inbound` được gọi hay chưa — để chứng minh verify chạy TRƯỚC parse.

    KHÔNG dùng ZaloChannel thật vì `parse_inbound` của nó (P3 chưa land) đang là placeholder
    shape `{customer_id, message}`, không match envelope Zalo thật mà test này build. Ghi tay
    `verify_signature` để đi qua đúng đường adapter mới trong `api/webhook.py`.
    """

    name = "zalo"
    parse_count = 0

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []
        type(self).parse_count = 0

    async def verify_signature(
        self, req: Request, session_factory: async_sessionmaker[AsyncSession]
    ) -> bytes:
        async with session_factory() as session:
            repo = ZaloOATokenRepo(session)
            return await verify_zalo_signature(req, repo.get_oa_secret_by_oa_id)

    def parse_inbound(self, payload: dict[str, Any]) -> InboundMessage:
        type(self).parse_count += 1
        return InboundMessage(
            external_user_id=payload["sender"]["id"], text=payload["message"]["text"]
        )

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
        self.sent.append({"shop_id": shop_id, "customer_id": customer_id, "text": text})


async def _seed_zalo_token(session_factory, oa_id: str = _OA_ID) -> None:
    """Insert 1 shop + zalo_oa_tokens row để endpoint verify lookup thấy secret."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    async with session_factory() as s:
        s.add(Shop(id="shop_a", name="Shop A"))
        await s.commit()
        repo = ZaloOATokenRepo(s)
        await repo.update_tokens_locked(
            shop_id="shop_a",
            oa_id=oa_id,
            access_token="a",
            refresh_token="r",
            access_expires_at=now + timedelta(hours=1),
            refresh_expires_at=now + timedelta(days=90),
            oa_secret_key=_OA_SECRET,
        )


def _build_test_client(session_factory) -> tuple[TestClient, _ParseTracker]:
    tracker = _ParseTracker()
    router = build_router(
        _NoOpDrafter(),
        session_factory,
        channels={"zalo": tracker},  # type: ignore[dict-item]
        endpoint_to_shop={("zalo", "EP1"): "shop_a"},
        shop_auto_enabled={},
        enabled=True,
    )
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), tracker


@pytest.mark.asyncio
async def test_endpoint_valid_signature_returns_200_and_parses(fresh_db):
    _, session_factory = await fresh_db()
    await _seed_zalo_token(session_factory)
    client, tracker = _build_test_client(session_factory)

    raw, ts = _build_zalo_payload()
    sig = _compute_signature(_APP_ID, raw, ts, _OA_SECRET)

    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"X-ZEvent-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    assert tracker.parse_count == 1, "verify PASS ⇒ parse_inbound phải chạy đúng 1 lần"


@pytest.mark.asyncio
async def test_endpoint_wrong_signature_returns_401_and_does_not_parse(fresh_db):
    """Verify TRƯỚC parse — hash lệch ⇒ 401 + parse_inbound KHÔNG chạy (không side effect)."""
    _, session_factory = await fresh_db()
    await _seed_zalo_token(session_factory)
    client, tracker = _build_test_client(session_factory)

    raw, _ = _build_zalo_payload()
    wrong_sig = "f" * 64

    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"X-ZEvent-Signature": wrong_sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert tracker.parse_count == 0, "verify FAIL ⇒ parse phải KHÔNG chạy"


@pytest.mark.asyncio
async def test_endpoint_missing_signature_header_returns_401(fresh_db):
    _, session_factory = await fresh_db()
    await _seed_zalo_token(session_factory)
    client, tracker = _build_test_client(session_factory)

    raw, _ = _build_zalo_payload()

    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"Content-Type": "application/json"},  # KHÔNG X-ZEvent-Signature
    )
    assert resp.status_code == 401
    assert tracker.parse_count == 0


@pytest.mark.asyncio
async def test_endpoint_replay_old_timestamp_returns_401(fresh_db):
    """Replay attack: payload cũ (>5min) với signature valid ⇒ vẫn 401."""
    import time

    _, session_factory = await fresh_db()
    await _seed_zalo_token(session_factory)
    client, tracker = _build_test_client(session_factory)

    old_ts = str(int((time.time() - 600) * 1000))  # 10 phút trước
    raw, ts = _build_zalo_payload(ts=old_ts)
    sig = _compute_signature(_APP_ID, raw, ts, _OA_SECRET)  # hash HỢP LỆ với timestamp cũ

    resp = client.post(
        "/webhook/zalo/EP1",
        content=raw,
        headers={"X-ZEvent-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert tracker.parse_count == 0
