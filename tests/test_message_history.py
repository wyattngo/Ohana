"""H0 gate (spec `10-Task-OhanaAISeller-ConversationHistory.md` §7 Phase H0).

Viết TRƯỚC khi `Message` có `conversation_id`/`customer_id`/FK ⇒ expected RED.

**Vì sao `Message` là bảng duy nhất trong repo không có FK.** Spec 06 F0 gắn composite FK
`(shop_id, X) → parent(shop_id, id)` cho `Conversation`, `OrderDraft`, `PendingReply` —
và bỏ sót `Message`. Hệ quả không phải thẩm mỹ: bảng không biết message thuộc conversation
nào, nên câu "load last-N của conversation này" KHÔNG viết được. Cùng lắm lọc theo `shop_id`,
tức trộn chung mọi khách của một shop.

**Vì sao FK phải COMPOSITE, và vì sao test phải chứng minh bằng `IntegrityError`.**
`FOREIGN KEY (conversation_id) → conversations(id)` chỉ khẳng định "conversation này tồn
tại". Nó cho phép message của shop A trỏ conversation của shop B và Postgres vui vẻ nhận —
đúng lỗ R1.22. Chỉ dạng composite mới ghim row được tham chiếu vào CÙNG shop. Một test chỉ
hỏi "FK có tồn tại không" sẽ XANH với cả hai dạng, tức không phân biệt được bản vá thật với
bản vá giả. Nên `test_cross_shop_message_rejected_by_database` mới là bằng chứng, phần còn
lại là canh đường.

**Giới hạn đã biết của các test ở đây — đọc trước khi tin chúng quá mức.** `fresh_db` dựng
schema bằng `Base.metadata.create_all`, KHÔNG qua Alembic (xem docstring `conftest.py`).
Nghĩa là mọi assertion dưới đây kiểm **model**, không kiểm **migration**. Model đúng mà
`0006` viết sai thì suite này vẫn xanh. Tính đúng của migration có gate riêng và phải chạy
thật: `alembic upgrade head && alembic downgrade -1 && alembic upgrade head` trong GATE_FULL.
Đừng gộp hai thứ đó làm một.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

_FK_CONVERSATION = "fk_messages_conversation_same_shop"
_FK_CUSTOMER = "fk_messages_customer_same_shop"
_INDEX = "idx_msg_shop_conv_created"


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


async def _seed_shop(conn: sa.ext.asyncio.AsyncConnection, shop: str) -> tuple[str, str]:
    """Tạo (customer, conversation) hợp lệ cho `shop`. Trả `(customer_id, conversation_id)`."""
    cus, conv = _uid("cus"), _uid("conv")
    await conn.execute(
        sa.text(
            "insert into customers (id, shop_id, channel, external_id) values (:i, :s, 'zalo', :e)"
        ),
        {"i": cus, "s": shop, "e": _uid("ext")},
    )
    await conn.execute(
        sa.text(
            "insert into conversations (id, shop_id, customer_id, channel) "
            "values (:i, :s, :c, 'zalo')"
        ),
        {"i": conv, "s": shop, "c": cus},
    )
    return cus, conv


# --- (a) cột tồn tại và NOT NULL -----------------------------------------------------


@pytest.mark.asyncio
async def test_message_has_conversation_and_customer_columns(fresh_db) -> None:
    """(a) Hỏi `information_schema`, không tin khai báo trong Python.

    NOT NULL là phần có ý nghĩa: cột nullable sẽ cho phép message mồ côi lọt vào, và
    lịch sử thủng một cách âm thầm đúng như ISSUE-020 — cột có trong schema, không ai ghi.
    """
    engine, _ = await fresh_db()
    async with engine.connect() as c:
        rows = (
            await c.execute(
                sa.text(
                    "select column_name, is_nullable from information_schema.columns "
                    "where table_name = 'messages'"
                )
            )
        ).all()
    cols = {r[0]: r[1] for r in rows}

    assert "conversation_id" in cols, f"`messages` thiếu conversation_id — có: {sorted(cols)}"
    assert "customer_id" in cols, f"`messages` thiếu customer_id — có: {sorted(cols)}"
    assert cols["conversation_id"] == "NO", "conversation_id phải NOT NULL"
    assert cols["customer_id"] == "NO", "customer_id phải NOT NULL"


# --- (b) FK là COMPOSITE, không phải FK đơn ------------------------------------------


@pytest.mark.asyncio
async def test_foreign_keys_are_composite_on_shop_id(fresh_db) -> None:
    """(b) Đọc `pg_constraint` và kiểm ĐÚNG cặp cột, không chỉ kiểm tên FK tồn tại.

    Một FK tên đúng nhưng chỉ trên `conversation_id` sẽ qua được bài kiểm "có tồn tại
    không" trong khi không chặn gì cross-tenant. Nên assertion nằm ở DANH SÁCH CỘT.
    """
    engine, _ = await fresh_db()
    async with engine.connect() as c:
        rows = (
            await c.execute(
                sa.text("""
                select con.conname, array_agg(att.attname order by k.ord) as cols
                from pg_constraint con
                join pg_class rel on rel.oid = con.conrelid
                join lateral unnest(con.conkey) with ordinality as k(attnum, ord) on true
                join pg_attribute att
                  on att.attrelid = con.conrelid and att.attnum = k.attnum
                where rel.relname = 'messages' and con.contype = 'f'
                group by con.conname
                """)
            )
        ).all()
    fks = {r[0]: list(r[1]) for r in rows}

    assert _FK_CONVERSATION in fks, f"thiếu {_FK_CONVERSATION} — FK hiện có: {sorted(fks)}"
    assert _FK_CUSTOMER in fks, f"thiếu {_FK_CUSTOMER} — FK hiện có: {sorted(fks)}"
    assert fks[_FK_CONVERSATION] == ["shop_id", "conversation_id"], (
        f"FK conversation phải COMPOSITE trên (shop_id, conversation_id), "
        f"đang là {fks[_FK_CONVERSATION]} — FK đơn KHÔNG chặn cross-tenant"
    )
    assert fks[_FK_CUSTOMER] == ["shop_id", "customer_id"], (
        f"FK customer phải COMPOSITE trên (shop_id, customer_id), đang là {fks[_FK_CUSTOMER]}"
    )


# --- (c) bằng chứng thật: Postgres TỪ CHỐI cross-shop --------------------------------


@pytest.mark.asyncio
async def test_cross_shop_message_rejected_by_database(fresh_db) -> None:
    """(c) **Đây là bằng chứng, không phải (a)/(b).**

    Dựng conversation hợp lệ ở shop B, rồi thử ghi message khai `shop_id='A'` trỏ vào nó.
    Với FK đơn, Postgres nhận (conversation TỒN TẠI). Với FK composite, Postgres từ chối
    (cặp `(A, conv_của_B)` không có trong `conversations(shop_id, id)`).

    Đây đúng là bài kiểm mà `test_cross_shop_reference_rejected_by_database` của spec 06 F0
    đã làm cho `Conversation` — `Message` chưa từng có bản tương ứng.
    """
    engine, _ = await fresh_db()
    shop_a, shop_b = _uid("shopA"), _uid("shopB")
    async with engine.begin() as c:
        cus_a, _ = await _seed_shop(c, shop_a)
        _, conv_b = await _seed_shop(c, shop_b)

    with pytest.raises(IntegrityError):
        async with engine.begin() as c:
            await c.execute(
                sa.text(
                    "insert into messages (shop_id, role, content, conversation_id, customer_id) "
                    "values (:s, 'user', 'xin chào', :conv, :cus)"
                ),
                # shop A + conversation của shop B = cặp không tồn tại ⇒ phải bị từ chối.
                {"s": shop_a, "conv": conv_b, "cus": cus_a},
            )


@pytest.mark.asyncio
async def test_same_shop_message_accepted(fresh_db) -> None:
    """(c-đối chứng) Cùng shop thì PHẢI ghi được.

    Không có test này thì một FK viết sai tới mức chặn cả ghi hợp lệ vẫn làm test (c) xanh —
    "từ chối mọi thứ" cũng thoả `pytest.raises`. Cặp chấp-nhận/từ-chối mới định nghĩa đúng
    hành vi.
    """
    engine, _ = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        cus, conv = await _seed_shop(c, shop)
        await c.execute(
            sa.text(
                "insert into messages (shop_id, role, content, conversation_id, customer_id) "
                "values (:s, 'user', 'còn size M không', :conv, :cus)"
            ),
            {"s": shop, "conv": conv, "cus": cus},
        )
    async with engine.connect() as c:
        n = (
            await c.execute(
                sa.text("select count(*) from messages where shop_id = :s"), {"s": shop}
            )
        ).scalar_one()
    assert n == 1


# --- (d) index cho đường đọc history -------------------------------------------------


@pytest.mark.asyncio
async def test_history_index_exists(fresh_db) -> None:
    """(d) Index `(shop_id, conversation_id, created_at)` — đường đọc của H2.

    Index cũ `idx_msg_shop_created` phục vụ truy vấn theo shop; nó KHÔNG phục vụ
    "last-N của conversation này". Giữ cả hai, không thay thế.
    """
    engine, _ = await fresh_db()
    async with engine.connect() as c:
        names = {
            r[0]
            for r in (
                await c.execute(
                    sa.text("select indexname from pg_indexes where tablename = 'messages'")
                )
            ).all()
        }
    assert _INDEX in names, f"thiếu index {_INDEX} — hiện có: {sorted(names)}"
    assert "idx_msg_shop_created" in names, "index cũ bị xoá — phải GIỮ, không thay thế"


# =====================================================================================
# H1 — write path. Viết TRƯỚC `MessageRepo` + wiring ⇒ expected RED.
#
# **Phạm vi H1 KHÔNG gồm idempotency** (GOAL-AMEND, Wyatt ký 2026-07-20): `messages` không
# có khoá dedup, và cơ chế đó là `webhook_event_log` của spec 03 Phase 2 (BLOCKED). Nghĩa là
# Zalo retry SẼ nhân đôi row. Không có test nào ở đây khẳng định ngược lại — cố ý, để không
# ai đọc suite này thành "đã chống trùng".
# =====================================================================================

from dataclasses import dataclass  # noqa: E402


@dataclass
class _FakeDraft:
    text: str
    intent: str
    confidence: float


@dataclass
class _FakeDrafter:
    """Drafter tất định. `draft()` không đọc history ở H1 — đó là H2."""

    text: str = "dạ còn size M ạ"
    intent: str = "product_info"
    confidence: float = 0.95

    async def draft(
        self, *, shop_id: str, customer_id: str, message: str, history: list[Message]
    ) -> _FakeDraft:
        return _FakeDraft(text=self.text, intent=self.intent, confidence=self.confidence)


@dataclass
class _ExplodingSender:
    """`send()` luôn nổ — dùng cho ca (e). Ghi TRƯỚC khi gửi sẽ lộ ra ở đúng test này."""

    sends: list[dict[str, str]] | None = None

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
        raise RuntimeError("Zalo API down")


async def _messages(engine, shop: str, conv: str) -> list[tuple[str, str]]:
    """Trả [(role, content)] theo thứ tự thời gian cho đúng (shop, conversation)."""
    async with engine.connect() as c:
        return [
            (r[0], r[1])
            for r in (
                await c.execute(
                    sa.text(
                        "select role, content from messages "
                        "where shop_id = :s and conversation_id = :c order by created_at, id"
                    ),
                    {"s": shop, "c": conv},
                )
            ).all()
        ]


@pytest.mark.asyncio
async def test_inbound_message_is_persisted(fresh_db) -> None:
    """(a) Tin khách vào ⇒ ĐÚNG 1 row `role='user'`, gắn đúng conversation.

    Ghi ở webhook TRƯỚC `receive_and_draft` là có chủ ý: drafter/LLM nổ thì tin khách vẫn
    còn. Mất tin khách không phục hồi được; mất draft thì retry được.
    """
    from db.repos import MessageRepo

    engine, sf = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        cus, conv = await _seed_shop(c, shop)

    async with sf() as s:
        await MessageRepo(s, shop_scope=shop).append(
            conversation_id=conv, customer_id=cus, role="user", content="còn size M không"
        )

    assert await _messages(engine, shop, conv) == [("user", "còn size M không")]


@pytest.mark.asyncio
async def test_repo_bakes_shop_scope_and_never_takes_it_as_arg(fresh_db) -> None:
    """(d-phần 1) `shop_id` BAKED từ scope repo — `append()` KHÔNG nhận `shop_id`.

    Đối xứng `PendingReplyRepo.create`. Caller bị chiếm quyền cũng không ghi lệch shop được,
    vì không có tham số nào để bẻ.
    """
    import inspect

    from db.repos import MessageRepo

    params = set(inspect.signature(MessageRepo.append).parameters)
    assert "shop_id" not in params, (
        f"`append()` KHÔNG được nhận shop_id — phải baked từ shop_scope. Đang có: {sorted(params)}"
    )
    with pytest.raises(ValueError, match="shop_scope"):
        MessageRepo(None, shop_scope="")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_cross_shop_read_returns_empty_not_raise(fresh_db) -> None:
    """(d-phần 2) Đọc conversation của shop khác ⇒ **rỗng**, KHÔNG raise.

    Raise sẽ phân biệt được "không tồn tại" với "tồn tại nhưng của shop khác" — tức rò rỉ
    sự TỒN TẠI của dữ liệu shop khác. Cùng hình dạng với `PendingReplyRepo.get` trả None.
    """
    from db.repos import MessageRepo

    engine, sf = await fresh_db()
    shop_a, shop_b = _uid("shopA"), _uid("shopB")
    async with engine.begin() as c:
        await _seed_shop(c, shop_a)
        cus_b, conv_b = await _seed_shop(c, shop_b)

    async with sf() as s:
        await MessageRepo(s, shop_scope=shop_b).append(
            conversation_id=conv_b, customer_id=cus_b, role="user", content="bí mật của shop B"
        )

    async with sf() as s:
        leaked = await MessageRepo(s, shop_scope=shop_a).last_n(conv_b, limit=20)
    assert leaked == [], f"shop A đọc được conversation của shop B — R1.22: {leaked}"


@pytest.mark.asyncio
async def test_auto_send_persists_assistant_message(fresh_db) -> None:
    """(b) Nhánh `auto_send` ⇒ thêm ĐÚNG 1 row `role='assistant'`, SAU khi gửi thành công."""
    from agent.orchestrator import receive_and_draft
    from bridge.zalo_sender import MockZaloSender

    engine, sf = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        cus, conv = await _seed_shop(c, shop)

    sender = MockZaloSender()
    outcome = await receive_and_draft(
        shop_id=shop,
        customer_id=cus,
        conversation_id=conv,
        message="còn size M không",
        drafter=_FakeDrafter(),
        sender=sender,
        session_factory=sf,
        shop_auto_enabled_intents=frozenset({"product_info"}),
    )

    assert outcome.action == "auto_send"
    assert len(sender.sends) == 1
    assert await _messages(engine, shop, conv) == [("assistant", "dạ còn size M ạ")]


@pytest.mark.asyncio
async def test_park_writes_no_assistant_message(fresh_db) -> None:
    """(c) Nhánh `park` ⇒ KHÔNG có row assistant.

    Đây là test khẳng định một QUYẾT ĐỊNH (PRE-1004 / §14 Q2: chưa ghi lúc approve), không
    phải một chi tiết kỹ thuật. Nếu ai đó sau này thêm ghi-khi-approve thì test này ĐỎ —
    đúng ý: quyết định đổi thì phải đổi tường minh, không trôi âm thầm.
    """
    from agent.orchestrator import receive_and_draft
    from bridge.zalo_sender import MockZaloSender

    engine, sf = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        cus, conv = await _seed_shop(c, shop)

    sender = MockZaloSender()
    outcome = await receive_and_draft(
        shop_id=shop,
        customer_id=cus,
        conversation_id=conv,
        message="cho mình hoàn hàng",
        drafter=_FakeDrafter(intent="complaint", confidence=0.4),
        sender=sender,
        session_factory=sf,
        shop_auto_enabled_intents=frozenset(),  # không opt-in ⇒ park
    )

    assert outcome.action == "park"
    assert sender.sends == []
    assert await _messages(engine, shop, conv) == [], (
        "nhánh park KHÔNG được ghi message — PendingReply đã là bản ghi của nó, "
        "và chưa có worker nào thực sự gửi"
    )


@pytest.mark.asyncio
async def test_failed_send_writes_no_assistant_message(fresh_db) -> None:
    """(e) `sender.send` nổ ⇒ KHÔNG có row assistant.

    Đây là test phân biệt "ghi sau khi gửi" với "ghi trước khi gửi". Ghi trước thì lịch sử
    khai một điều chưa từng xảy ra, và AI lượt sau sẽ tưởng nó đã trả lời khách rồi.
    """
    from agent.orchestrator import receive_and_draft

    engine, sf = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        cus, conv = await _seed_shop(c, shop)

    with pytest.raises(RuntimeError, match="Zalo API down"):
        await receive_and_draft(
            shop_id=shop,
            customer_id=cus,
            conversation_id=conv,
            message="còn size M không",
            drafter=_FakeDrafter(),
            sender=_ExplodingSender(),
            session_factory=sf,
            shop_auto_enabled_intents=frozenset({"product_info"}),
        )

    assert await _messages(engine, shop, conv) == [], "gửi THẤT BẠI mà vẫn ghi assistant"


@pytest.mark.asyncio
async def test_inbound_persisted_through_real_webhook_route(fresh_db) -> None:
    """(a-mạnh) HTTP POST thật → `messages` có ĐÚNG 1 row `user`, gắn Conversation THẬT.

    Test `test_inbound_message_is_persisted` chỉ chạm `MessageRepo` — nó xanh kể cả khi
    không ai gọi repo từ webhook. GOAL của H1 nói "một tin nhắn khách **đi qua webhook**",
    nên bằng chứng phải đi hết đường: HTTP → adapter → resolve_conversation → ghi.

    Dùng `park` (confidence thấp) để cô lập ĐÚNG đường ghi inbound: nếu nhánh auto_send
    cũng chạy thì không phân biệt được row `user` đến từ webhook hay từ orchestrator.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    from api.webhook import build_router
    from channels.base import InboundMessage
    from db.models import Conversation

    engine, sf = await fresh_db()

    class FakeChannel:
        name = "fakechan"

        async def verify_signature(self, req, session_factory):  # type: ignore[no-untyped-def]
            # Spec 17 P1: webhook.py raise 501 nếu adapter thiếu verify_signature. Test-only
            # no-op — production adapter (ZaloChannel) verify HMAC thật.
            return await req.body()

        def parse_inbound(self, payload):  # type: ignore[no-untyped-def]
            return InboundMessage(external_user_id=payload["uid"], text=payload["body"])

        async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
            raise AssertionError("park path KHÔNG được gọi sender")

    class LowConfDrafter:
        async def draft(
            self, *, shop_id: str, customer_id: str, message: str, history: list[Message]
        ) -> _FakeDraft:
            return _FakeDraft(text="draft ...", intent="general_qa", confidence=0.2)

    app = FastAPI()
    app.include_router(
        build_router(
            LowConfDrafter(),
            sf,
            channels={"fakechan": FakeChannel()},  # type: ignore[dict-item]
            endpoint_to_shop={("fakechan", "EP1"): "shop_a"},
            shop_auto_enabled={},
            enabled=True,
        )
    )
    resp = TestClient(app).post(
        "/webhook/fakechan/EP1", json={"uid": "ext-user-9", "body": "còn size M không"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["action"] == "park"

    async with sf() as s:
        conv = (await s.execute(select(Conversation))).scalars().one()
    assert await _messages(engine, "shop_a", conv.id) == [("user", "còn size M không")], (
        "tin khách phải được ghi khi đi qua webhook, gắn ĐÚNG conversation vừa resolve"
    )


# =====================================================================================
# H2 — read path: last-N vào `Drafter` + cap kép. Viết TRƯỚC khi `Drafter` nhận `history`
# ⇒ expected RED.
#
# **Các test này KHÔNG đo chất lượng trả lời của LLM.** Chúng đo đúng một điều: history
# ĐẾN được drafter, đúng nội dung, đúng thứ tự, đúng conversation, đã cắt theo cap. Việc
# "AI phân giải được đại từ" cần `-m live` + eval; một test phụ thuộc chất lượng LLM là
# test sẽ đỏ ngẫu nhiên và rồi bị ai đó tắt đi.
# =====================================================================================


from agent.orchestrator import (  # noqa: E402
    HISTORY_MAX_CHARS,
    HISTORY_MAX_MESSAGES,
    receive_and_draft,
)
from db.models import Message  # noqa: E402
from db.repos import MessageRepo  # noqa: E402


@dataclass
class _RecordingSender:
    """Sender im lặng — H2 không đo đường gửi, chỉ đo history tới drafter."""

    sent: list[str] | None = None

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
        if self.sent is None:
            self.sent = []
        self.sent.append(text)


@dataclass
class _HistoryCapturingDrafter:
    """Ghi lại ĐÚNG cái nó nhận. Đây là toàn bộ cơ chế đo của H2.

    Không assert bên trong drafter — bắt được gì thì trả ra ngoài cho test assert, để khi
    đỏ thì thông báo nói rõ nhận được gì thay vì chỉ "False is not True".
    """

    seen: list[tuple[str, str]] | None = None
    text: str = "dạ áo đó còn size M ạ"

    async def draft(
        self,
        *,
        shop_id: str,
        customer_id: str,
        message: str,
        history: list[Message],
    ) -> _FakeDraft:
        if self.seen is None:
            self.seen = []
        self.seen.extend((m.role, m.content) for m in history)
        return _FakeDraft(text=self.text, intent="product_info", confidence=0.95)


@pytest.mark.asyncio
async def test_second_turn_drafter_receives_first_turn_history(fresh_db) -> None:
    """(a) Lượt 2 — drafter thấy lượt 1, thứ tự CŨ→MỚI.

    Đây là ca biện minh cho cả spec 10. Khách nhắn "cái áo đó còn size M không" ở lượt 2;
    không có history thì "cái áo đó" không phân giải được và AI không trả lời nổi. Test
    khẳng định nguyên liệu ĐÃ tới tay drafter — phần dùng nó là việc của LLM.

    Thứ tự phải tăng dần: `last_n` lấy `DESC LIMIT n` rồi đảo. Quên đảo thì LLM đọc hội
    thoại ngược, không crash, chỉ trả lời lệch — đúng họ hỏng-âm-thầm.
    """
    engine, sf = await fresh_db()
    shop = "shop_a"
    async with engine.begin() as c:
        cus, conv = await _seed_shop(c, shop)

    async with sf() as s:
        repo = MessageRepo(s, shop_scope=shop)
        await repo.append(
            conversation_id=conv, customer_id=cus, role="user", content="áo thun trắng bao nhiêu"
        )
        await repo.append(
            conversation_id=conv, customer_id=cus, role="assistant", content="dạ 250k ạ"
        )

    drafter = _HistoryCapturingDrafter()
    await receive_and_draft(
        shop_id=shop,
        customer_id=cus,
        conversation_id=conv,
        message="cái áo đó còn size M không",
        drafter=drafter,
        sender=_RecordingSender(),
        session_factory=sf,
        shop_auto_enabled_intents=frozenset(),
    )

    assert drafter.seen == [
        ("user", "áo thun trắng bao nhiêu"),
        ("assistant", "dạ 250k ạ"),
    ], f"drafter phải nhận history lượt 1 theo thứ tự cũ→mới, nhận được: {drafter.seen}"


@pytest.mark.asyncio
async def test_history_of_other_conversation_does_not_leak(fresh_db) -> None:
    """(b) Conversation KHÁC không lẫn vào — kể cả cùng shop, cùng khách.

    `last_n` lọc theo `conversation_id`; thiếu vế đó thì mọi hội thoại của shop trộn làm
    một và AI trả lời khách này bằng ngữ cảnh của khách kia. Không sai type, không crash.
    """
    engine, sf = await fresh_db()
    shop = "shop_a"
    async with engine.begin() as c:
        cus_1, conv_1 = await _seed_shop(c, shop)
        cus_2, conv_2 = await _seed_shop(c, shop)

    async with sf() as s:
        repo = MessageRepo(s, shop_scope=shop)
        await repo.append(
            conversation_id=conv_1, customer_id=cus_1, role="user", content="KHÁCH MỘT"
        )
        await repo.append(
            conversation_id=conv_2, customer_id=cus_2, role="user", content="KHÁCH HAI"
        )

    drafter = _HistoryCapturingDrafter()
    await receive_and_draft(
        shop_id=shop,
        customer_id=cus_2,
        conversation_id=conv_2,
        message="tiếp",
        drafter=drafter,
        sender=_RecordingSender(),
        session_factory=sf,
        shop_auto_enabled_intents=frozenset(),
    )

    assert drafter.seen == [("user", "KHÁCH HAI")], (
        f"history của conversation khác LỌT vào: {drafter.seen}"
    )


@pytest.mark.asyncio
async def test_history_capped_by_message_count_keeps_newest(fresh_db) -> None:
    """(c) Vượt cap SỐ LƯỢNG ⇒ giữ N mới nhất, cắt TỪ ĐẦU.

    Cắt nhầm đầu-đuôi là lỗi vô hình: vẫn đúng số lượng, vẫn không crash, chỉ là AI đọc
    phần hội thoại đã cũ và bỏ mất tin đang cần trả lời. Nên test khẳng định cả hai đầu —
    tin cũ nhất PHẢI biến mất, tin mới nhất PHẢI còn.
    """
    engine, sf = await fresh_db()
    shop = "shop_a"
    async with engine.begin() as c:
        cus, conv = await _seed_shop(c, shop)

    async with sf() as s:
        repo = MessageRepo(s, shop_scope=shop)
        for i in range(HISTORY_MAX_MESSAGES + 5):
            await repo.append(
                conversation_id=conv, customer_id=cus, role="user", content=f"tin-{i:03d}"
            )

    drafter = _HistoryCapturingDrafter()
    await receive_and_draft(
        shop_id=shop,
        customer_id=cus,
        conversation_id=conv,
        message="tiếp",
        drafter=drafter,
        sender=_RecordingSender(),
        session_factory=sf,
        shop_auto_enabled_intents=frozenset(),
    )

    seen = drafter.seen or []
    assert len(seen) == HISTORY_MAX_MESSAGES, (
        f"phải cắt còn {HISTORY_MAX_MESSAGES}, nhận {len(seen)}"
    )
    newest = f"tin-{HISTORY_MAX_MESSAGES + 4:03d}"
    assert seen[-1] == ("user", newest), f"tin MỚI NHẤT phải còn, cuối danh sách là {seen[-1]}"
    assert ("user", "tin-000") not in seen, "tin CŨ NHẤT phải bị cắt — đang cắt nhầm đầu"


@pytest.mark.asyncio
async def test_history_capped_by_chars_keeps_newest(fresh_db) -> None:
    """(d) Vượt cap KÝ TỰ ⇒ cắt thêm, tin mới nhất luôn còn.

    Vì sao cần cap thứ hai: cap số lượng một mình không chặn được 20 tin mỗi tin 3000 ký
    tự — vẫn "đúng 20 tin" mà ngân sách token đã vỡ. Ở đây mỗi tin 1000 ký tự nên cap
    ký tự phải cắn TRƯỚC cap số lượng.
    """
    engine, sf = await fresh_db()
    shop = "shop_a"
    async with engine.begin() as c:
        cus, conv = await _seed_shop(c, shop)

    big = 1000
    async with sf() as s:
        repo = MessageRepo(s, shop_scope=shop)
        for i in range(10):
            await repo.append(
                conversation_id=conv,
                customer_id=cus,
                role="user",
                content=f"{i:03d}" + "x" * (big - 3),
            )

    drafter = _HistoryCapturingDrafter()
    await receive_and_draft(
        shop_id=shop,
        customer_id=cus,
        conversation_id=conv,
        message="tiếp",
        drafter=drafter,
        sender=_RecordingSender(),
        session_factory=sf,
        shop_auto_enabled_intents=frozenset(),
    )

    seen = drafter.seen or []
    total = sum(len(c) for _, c in seen)
    assert total <= HISTORY_MAX_CHARS, f"tổng {total} ký tự vượt cap {HISTORY_MAX_CHARS}"
    assert len(seen) < 10, "cap ký tự phải cắn trước cap số lượng ở ca này"
    assert seen[-1][1].startswith("009"), (
        f"tin mới nhất phải còn sau khi cắt theo ký tự, cuối là {seen[-1][1][:3]}"
    )


@pytest.mark.asyncio
async def test_new_conversation_has_empty_history(fresh_db) -> None:
    """(e) Conversation mới ⇒ history rỗng, KHÔNG nổ.

    Lượt đầu tiên của mọi khách đều đi qua đường này. Nếu nó raise thì tin nhắn đầu tiên
    của mỗi khách mới sẽ chết — ca phổ biến nhất, không phải ca biên.
    """
    engine, sf = await fresh_db()
    shop = "shop_a"
    async with engine.begin() as c:
        cus, conv = await _seed_shop(c, shop)

    drafter = _HistoryCapturingDrafter()
    outcome = await receive_and_draft(
        shop_id=shop,
        customer_id=cus,
        conversation_id=conv,
        message="alo shop ơi",
        drafter=drafter,
        sender=_RecordingSender(),
        session_factory=sf,
        shop_auto_enabled_intents=frozenset(),
    )

    assert outcome.action == "park"
    assert drafter.seen == [], f"conversation mới phải có history rỗng, nhận {drafter.seen}"
