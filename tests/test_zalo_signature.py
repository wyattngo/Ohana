"""Spec 17 P1 — Zalo signature verification (RISK: high, RED first).

Vì sao module này tồn tại — signature verify là chốt chặn giữa "webhook mở" và "code đọc
body". Sai formula, sai key, hoặc verify sau parse đều làm attacker inject tin khách giả
mạo vào draft engine (khi P4 mount). Docs Zalo canonical (2026-07-24):
`X-ZEvent-Signature: mac = sha256(appId + data + timeStamp + OAsecretKey)`.

Key lookup KHÔNG lookup theo `oa_id` (envelope không có top-level `oa_id`). `oa_id` phải
SUY từ candidate IDs trong body: `sender.id` (event `oa_send_*` echo — OA là sender) hoặc
`recipient.id` (event `user_send_*` — user gửi TỚI OA, recipient là OA). Verify thử cả 2
candidate với `oa_secret_lookup` — 1 match ⇒ dùng secret đó tính hash và compare; 0 match
hoặc hash lệch ⇒ 401.

Replay window ±5 phút chống replay attack. Cửa sổ hẹp vì Zalo push realtime, không cần lỏng.

Đo bằng `hmac.compare_digest` (constant-time) — `==` chuỗi sẽ leak thông tin về prefix
match qua timing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable

import pytest
from fastapi import HTTPException

# Import target - sẽ ImportError trong RED phase (module chưa tồn tại)
from channels.zalo.signature import verify_zalo_signature

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_APP_ID = "2074138120372622546"
_OA_ID = "2074138120372622547"
_USER_ID = "3742389367648617405"
_OA_SECRET = "the-real-oa-secret-key-per-oa"


def _compute_signature(app_id: str, raw_body: bytes, timestamp: str, oa_secret: str) -> str:
    """Formula canonical từ docs Zalo: sha256(appId + data + timeStamp + OAsecretKey)."""
    base = app_id + raw_body.decode("utf-8") + timestamp + oa_secret
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def _build_body(
    *,
    app_id: str = _APP_ID,
    sender_id: str = _USER_ID,
    recipient_id: str = _OA_ID,
    event_name: str = "user_send_text",
    text: str = "còn hàng size L không anh?",
    msg_id: str = "43b59c025aebfb5e6fa",
    timestamp: str | None = None,
) -> tuple[bytes, str]:
    """Trả (raw_body_bytes, timestamp) — timestamp default = now."""
    import time

    ts = timestamp if timestamp is not None else str(int(time.time() * 1000))
    payload = {
        "app_id": app_id,
        "sender": {"id": sender_id},
        "recipient": {"id": recipient_id},
        "event_name": event_name,
        "message": {"text": text, "msg_id": msg_id},
        "timestamp": ts,
    }
    # `separators=(",",":")` để mô phỏng cách gateway push — không thêm space thừa.
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return raw, ts


class _FakeRequest:
    """Test double cho FastAPI Request — chỉ cần `body()` async + `headers` dict."""

    def __init__(self, body: bytes, headers: dict[str, str]) -> None:
        self._body = body
        self.headers = headers

    async def body(self) -> bytes:
        return self._body


def _make_lookup(secrets: dict[str, str]) -> Callable[[str], Awaitable[str | None]]:
    """oa_id → oa_secret, hoặc None nếu không có."""

    async def _lookup(oa_id: str) -> str | None:
        return secrets.get(oa_id)

    return _lookup


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_signature_via_recipient_id_returns_raw_body():
    """user_send_text: recipient.id = OA id ⇒ lookup thấy secret ⇒ hash match ⇒ raw body ra."""
    raw, ts = _build_body()
    sig = _compute_signature(_APP_ID, raw, ts, _OA_SECRET)
    req = _FakeRequest(raw, {"x-zevent-signature": sig})
    lookup = _make_lookup({_OA_ID: _OA_SECRET})

    result = await verify_zalo_signature(req, lookup)
    assert result == raw, "verify PASS phải trả nguyên raw body để downstream re-parse"


@pytest.mark.asyncio
async def test_valid_signature_via_sender_id_when_echo_event():
    """oa_send_text echo: sender.id = OA id ⇒ recipient lookup miss, sender lookup match ⇒ PASS."""
    # Echo event: OA gửi tin, sender=OA, recipient=user
    raw, ts = _build_body(sender_id=_OA_ID, recipient_id=_USER_ID, event_name="oa_send_text")
    sig = _compute_signature(_APP_ID, raw, ts, _OA_SECRET)
    req = _FakeRequest(raw, {"x-zevent-signature": sig})
    lookup = _make_lookup({_OA_ID: _OA_SECRET})

    result = await verify_zalo_signature(req, lookup)
    assert result == raw


@pytest.mark.asyncio
async def test_wrong_signature_raises_401():
    raw, ts = _build_body()
    correct_sig = _compute_signature(_APP_ID, raw, ts, _OA_SECRET)
    # Đảo 1 hex char ⇒ hash sai nhưng cùng length ⇒ compare_digest false
    wrong_sig = ("f" if correct_sig[0] != "f" else "0") + correct_sig[1:]
    req = _FakeRequest(raw, {"x-zevent-signature": wrong_sig})
    lookup = _make_lookup({_OA_ID: _OA_SECRET})

    with pytest.raises(HTTPException) as exc:
        await verify_zalo_signature(req, lookup)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_missing_signature_header_raises_401():
    raw, _ = _build_body()
    req = _FakeRequest(raw, {})  # KHÔNG có X-ZEvent-Signature
    lookup = _make_lookup({_OA_ID: _OA_SECRET})

    with pytest.raises(HTTPException) as exc:
        await verify_zalo_signature(req, lookup)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_malformed_json_body_raises_400():
    """Body không parse được ⇒ 400, không phải 401 — phân biệt để oncall biết bug ở đâu."""
    raw = b"{not valid json at all"
    req = _FakeRequest(raw, {"x-zevent-signature": "0" * 64})
    lookup = _make_lookup({_OA_ID: _OA_SECRET})

    with pytest.raises(HTTPException) as exc:
        await verify_zalo_signature(req, lookup)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_replay_old_timestamp_raises_401():
    """timestamp cũ hơn 5 phút ⇒ 401 (replay defense).

    Attack: capture 1 payload thật, replay lại 1 tuần sau. Cùng hash (secret không đổi) nên
    signature verify PASS nếu không check timestamp — phải chặn ở đây.
    """
    import time

    old_ts = str(int((time.time() - 600) * 1000))  # 10 phút trước
    raw, ts = _build_body(timestamp=old_ts)
    sig = _compute_signature(_APP_ID, raw, ts, _OA_SECRET)
    req = _FakeRequest(raw, {"x-zevent-signature": sig})
    lookup = _make_lookup({_OA_ID: _OA_SECRET})

    with pytest.raises(HTTPException) as exc:
        await verify_zalo_signature(req, lookup)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_replay_future_timestamp_raises_401():
    """timestamp TƯƠNG LAI hơn 5 phút ⇒ 401.

    Test bi-directional replay window (P1 review LOW 5): `abs(now - ts) > MAX_SKEW_MS` cover
    cả past và future. Future-timestamp attack ít phổ biến nhưng có thật: attacker clock skew
    server, hoặc gửi payload thu trước để "pre-book" verify sau này. `abs()` semantic đảm
    bảo cả 2 hướng đều chặn.
    """
    import time

    future_ts = str(int((time.time() + 600) * 1000))  # 10 phút sau
    raw, ts = _build_body(timestamp=future_ts)
    sig = _compute_signature(_APP_ID, raw, ts, _OA_SECRET)
    req = _FakeRequest(raw, {"x-zevent-signature": sig})
    lookup = _make_lookup({_OA_ID: _OA_SECRET})

    with pytest.raises(HTTPException) as exc:
        await verify_zalo_signature(req, lookup)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_tampered_body_same_timestamp_raises_401():
    """Attacker sửa body (đổi text) nhưng giữ nguyên timestamp + signature cũ ⇒ hash lệch ⇒ 401.

    Đây là attack thật: MITM sửa nội dung message giữa Zalo và server ta. Không có test này
    thì tampered body vẫn PASS nếu signature = hash của body GỐC (không phải body sửa).
    """
    original_raw, ts = _build_body(text="giá bao nhiêu")
    original_sig = _compute_signature(_APP_ID, original_raw, ts, _OA_SECRET)
    # Attacker sửa text
    tampered_raw, _ = _build_body(text="CHUYỂN 10 TRIỆU", timestamp=ts)
    # Nhưng gửi kèm signature CỦA body gốc
    req = _FakeRequest(tampered_raw, {"x-zevent-signature": original_sig})
    lookup = _make_lookup({_OA_ID: _OA_SECRET})

    with pytest.raises(HTTPException) as exc:
        await verify_zalo_signature(req, lookup)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_unknown_oa_id_both_candidates_raises_401():
    """Neither sender.id nor recipient.id có trong DB ⇒ không tra được secret ⇒ 401.

    Trường hợp: attacker gửi payload với random oa_id không thuộc shop nào. Ta không thể tính
    hash để verify, nên phải REJECT — KHÔNG được im lặng pass hoặc lộ thông tin "oa_id nào
    trong DB".
    """
    raw, ts = _build_body()
    # Signature không quan trọng — verify fail ở bước lookup trước khi hash
    sig = _compute_signature(_APP_ID, raw, ts, _OA_SECRET)
    req = _FakeRequest(raw, {"x-zevent-signature": sig})
    # DB rỗng — cả sender.id lẫn recipient.id đều lookup ra None
    lookup = _make_lookup({})

    with pytest.raises(HTTPException) as exc:
        await verify_zalo_signature(req, lookup)
    assert exc.value.status_code == 401
