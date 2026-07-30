"""Channel-abstraction gate — spec 06 Phase F1.

Viết TRƯỚC khi `channels/` tồn tại — kỳ vọng RED (ImportError).

Hai thứ phase này mua:

  1. **Thêm kênh không phải mổ core.** Hôm nay chỉ có Zalo, nhưng `api/webhook.py` đang
     `from bridge.zalo_sender import ZaloSender` + route `/webhook/zalo/{oa_id}` hardcode.
     Roadmap §5.2.1 cảnh báo refactor tax 3–5× nếu để tới GĐ2 mới tách. Test ở đây KHÔNG
     kiểm "Messenger chạy được" (chưa có) mà kiểm **core không còn biết tên kênh cụ thể**:
     một adapter giả tên "fakechan" phải chạy qua đúng luồng mà không sửa dòng nào trong core.

  2. **Gỡ shim `conversation_id or customer_id`** (`agent/orchestrator.py:89`). Shim đó có từ
     thời chưa có bảng `conversations`; sau F0 nó sẽ FK-violate lúc runtime. Việc map
     `(channel, external_user_id) → (customer_id, conversation_id)` thuộc về channel layer —
     nơi DUY NHẤT biết id phía kênh. Orchestrator từ đây đòi `conversation_id` thật.

Cần Postgres sống ở DATABASE_URL.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana"
)


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:8]}"


def test_zalo_adapter_satisfies_channel_protocol() -> None:
    """Adapter Zalo phải thoả Protocol chung — và `bridge/zalo_sender.ZaloSender` giữ nguyên
    chữ ký (Spec 03c sẽ thay MockZaloSender bằng sender thật, không được vỡ contract)."""
    import inspect

    from bridge.zalo_sender import MockZaloSender, ZaloSender
    from channels.base import InboundChannel, OutboundChannel
    from channels.zalo import ZaloChannel

    ch = ZaloChannel(sender=MockZaloSender())
    assert isinstance(ch, InboundChannel), "ZaloChannel phải thoả InboundChannel"
    assert isinstance(ch, OutboundChannel), "ZaloChannel phải thoả OutboundChannel"
    assert ch.name == "zalo"

    # Contract của sender KHÔNG được đổi — Spec 03c dựa vào nó.
    sig = inspect.signature(ZaloSender.send)
    assert set(sig.parameters) >= {"shop_id", "customer_id", "text"}, (
        "ZaloSender.send đổi chữ ký — phá contract Spec 03c"
    )


def test_core_does_not_hardcode_channel_names() -> None:
    """Core (webhook router) không được nhắc tên kênh cụ thể — đó là dấu hiệu hardcode còn sót.

    Đây là assertion thật sự của phase này: nếu `api/webhook.py` còn chuỗi "zalo", thì thêm
    Messenger ở GĐ2 vẫn phải mổ core, tức là abstraction chưa land.
    """
    from pathlib import Path

    src = Path("api/webhook.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    # Cho phép xuất hiện trong docstring/comment (lịch sử), nhưng KHÔNG trong code thực thi.
    body = code.split('"""')
    executable = "".join(body[::2])  # bỏ phần trong docstring
    assert "zalo" not in executable.lower(), (
        "api/webhook.py còn hardcode 'zalo' trong code thực thi — abstraction chưa xong"
    )


@pytest.mark.asyncio
async def test_resolve_conversation_creates_and_reuses_tenant_scoped_rows(fresh_db) -> None:
    """`(channel, external_user_id)` → Customer + Conversation THẬT, scoped theo shop.

    Gọi 2 lần cùng external_user_id phải trả CÙNG id (idempotent), không đẻ row trùng —
    nếu không, mỗi tin nhắn đến sẽ tạo một khách mới.
    """
    from sqlalchemy import func, select

    from channels.identity import resolve_conversation
    from db.models import Conversation, Customer

    engine, session_factory = await fresh_db()
    ext = _uid("zalo_user")

    async with session_factory() as s:
        cust1, conv1 = await resolve_conversation(
            s, shop_id="shop_a", channel="zalo", external_user_id=ext
        )
        cust2, conv2 = await resolve_conversation(
            s, shop_id="shop_a", channel="zalo", external_user_id=ext
        )

    assert cust1 == cust2, "cùng external_user_id phải tái dùng Customer, không tạo mới"
    assert conv1 == conv2, "cùng external_user_id phải tái dùng Conversation"

    async with session_factory() as s:
        n_cust = (await s.execute(select(func.count()).select_from(Customer))).scalar_one()
        n_conv = (await s.execute(select(func.count()).select_from(Conversation))).scalar_one()
    assert n_cust == 1, f"tạo {n_cust} Customer cho 1 external id (mong 1)"
    assert n_conv == 1, f"tạo {n_conv} Conversation cho 1 external id (mong 1)"

    # Cùng external id ở shop KHÁC phải là thực thể riêng — tenant tách tuyệt đối.
    async with session_factory() as s:
        cust_b, conv_b = await resolve_conversation(
            s, shop_id="shop_b", channel="zalo", external_user_id=ext
        )
    assert cust_b != cust1, "external id giống nhau ở 2 shop KHÔNG được dùng chung Customer"
    assert conv_b != conv1

    await engine.dispose()


@pytest.mark.asyncio
async def test_brand_new_channel_routes_end_to_end_without_touching_core(fresh_db) -> None:
    """Bằng chứng MẠNH của phase này (test grep source ở trên chỉ là bằng chứng yếu).

    Dựng một kênh mà core chưa từng nghe tên — "fakechan" — rồi chạy hết luồng:
    HTTP → chọn adapter → parse → resolve identity → orchestrator → park.
    KHÔNG sửa một dòng nào trong `api/webhook.py` hay `agent/orchestrator.py` để test này chạy.
    Nếu abstraction chưa land thật thì không thể làm được điều đó.

    Đồng thời đây là runtime coverage DUY NHẤT của `api/webhook.py` (route chưa mount vào
    app/main.py, nên không luồng nào khác chạm tới nó).
    """
    from dataclasses import dataclass

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from api.webhook import build_router
    from channels.base import InboundMessage
    from db.models import Conversation, Customer, PendingReply

    engine, session_factory = await fresh_db()

    class FakeChannel:
        """Một kênh tưởng tượng. Core chưa từng biết nó tồn tại.

        `verify_signature` là no-op — test-only bypass tường minh (spec 17 P1 HIGH 1 fix:
        webhook.py giờ raise 501 nếu adapter thiếu method, thay silent skip). Test chứng minh
        adapter mới KHÔNG BỊ CẤM có verify no-op — nhưng production adapter PHẢI verify thật.
        """

        name = "fakechan"

        def __init__(self) -> None:
            self.sent: list[dict[str, str]] = []

        async def verify_signature(self, req, session_factory):  # type: ignore[no-untyped-def]
            # Test-only: chấp nhận mọi request, chỉ trả raw body. Production adapter (như
            # ZaloChannel) phải verify hash + replay + oa_id lookup.
            return await req.body()

        def parse_inbound(self, payload):  # type: ignore[no-untyped-def]
            return InboundMessage(external_user_id=payload["uid"], text=payload["body"])

        async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
            self.sent.append({"shop_id": shop_id, "customer_id": customer_id, "text": text})

    @dataclass
    class _D:
        text: str
        intent: str
        confidence: float

    class LowConfDrafter:
        async def draft(
            self, *, shop_id: str, customer_id: str, message: str, history: list[Any]
        ) -> _D:
            return _D(text="draft ...", intent="general_qa", confidence=0.2)  # -> park

    ch = FakeChannel()
    router = build_router(
        LowConfDrafter(),
        session_factory,
        channels={"fakechan": ch},  # type: ignore[dict-item]
        endpoint_to_shop={("fakechan", "EP1"): "shop_a"},
        shop_auto_enabled={},
        enabled=True,
    )
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    resp = client.post("/webhook/fakechan/EP1", json={"uid": "ext-user-9", "body": "còn hàng ko"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "park", "low-confidence phải park, không gửi thẳng"
    assert ch.sent == [], "park path KHÔNG được gọi sender"

    # Identity thật đã được tạo, và PendingReply trỏ vào ĐÚNG chúng (FK composite của F0
    # sẽ từ chối nếu resolve_conversation trả id rác).
    async with session_factory() as s:
        cust = (await s.execute(select(Customer))).scalars().all()
        conv = (await s.execute(select(Conversation))).scalars().all()
        pend = (await s.execute(select(PendingReply))).scalars().all()
    assert len(cust) == 1 and cust[0].external_id == "ext-user-9"
    assert len(conv) == 1 and conv[0].channel == "fakechan"
    assert len(pend) == 1
    assert pend[0].conversation_id == conv[0].id, "PendingReply phải trỏ Conversation thật"
    assert pend[0].customer_id == cust[0].id
    assert pend[0].shop_id == "shop_a"

    # Kênh lạ / endpoint lạ → 404, không rò thông tin đăng ký.
    assert client.post("/webhook/khongtontai/EP1", json={}).status_code == 404
    assert client.post("/webhook/fakechan/EP-LA", json={"uid": "u", "body": "x"}).status_code == 404

    await engine.dispose()


@pytest.mark.asyncio
async def test_orchestrator_requires_real_conversation_id(fresh_db) -> None:
    """Shim đã gỡ: `conversation_id` là tham số BẮT BUỘC.

    Trước F1, thiếu nó thì orchestrator âm thầm mượn `customer_id` — sau F0 việc đó
    FK-violate lúc chạy. Giờ thiếu là lỗi ngay ở chữ ký, không phải nổ trong DB.
    """
    import inspect

    from agent.orchestrator import receive_and_draft

    sig = inspect.signature(receive_and_draft)
    param = sig.parameters.get("conversation_id")
    assert param is not None, "receive_and_draft phải còn nhận conversation_id"
    assert param.default is inspect.Parameter.empty, (
        "conversation_id vẫn có default — shim `or customer_id` chưa gỡ hết"
    )

    src = inspect.getsource(receive_and_draft)
    assert "or customer_id" not in src, "shim `conversation_id or customer_id` vẫn còn trong code"
