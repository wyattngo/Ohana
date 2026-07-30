"""Foundation gate — spec 06 Phase F0 (data model lõi).

Viết TRƯỚC khi `Conversation` / `Customer` / `OrderDraft` tồn tại trong db/models.py —
kỳ vọng RED (ImportError) cho tới khi bước impl land. Đây là gate của phase RISK:high.

Failure mode đang canh (theo thứ tự nghiêm trọng):

  1. **Cross-shop reference** (R1.22 analog, tầng cấu trúc) — một `Conversation` của shop A
     trỏ vào `Customer` của shop B. FK đơn trên `customer_id` KHÔNG chặn được điều này:
     nó chỉ kiểm tra "customer tồn tại", không kiểm tra "customer thuộc đúng shop".
     Cách chặn duy nhất ở tầng DB là **composite FK** `(shop_id, customer_id)` →
     `customers(shop_id, id)`, kèm UNIQUE `(shop_id, id)` ở bảng được trỏ.
     Convention/code review KHÔNG đủ — phải để Postgres từ chối.

  2. **Row-scope leak** — query theo scope shop A trả về row shop B.

  3. **Cột mồ côi** — `pending_reply.conversation_id` / `.customer_id` hiện là Text trần,
     không FK, nên có thể trỏ vào id không tồn tại mà DB vẫn nhận (spec 06 §1 finding #2).

Cần Postgres sống ở DATABASE_URL (CI cấp qua service pgvector/pgvector:pg16).
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana"
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_core_entities_exist_and_are_tenant_scoped(fresh_db) -> None:
    """3 thực thể lõi tồn tại, mang shop_id NOT NULL, và query scope shop A không thấy shop B."""
    from sqlalchemy import select

    from db.models import Conversation, Customer, OrderDraft

    engine, session_factory = await fresh_db()
    cust_a, cust_b = _uid("cust"), _uid("cust")
    conv_a, conv_b = _uid("conv"), _uid("conv")

    # Flush theo đúng thứ tự phụ thuộc (customer → conversation → order_draft). Composite FK
    # được kiểm ngay lúc INSERT, nên gom hết vào một flush là không hợp lệ — và cũng không
    # phản ánh cách code thật chạy (phải có khách trước rồi mới có hội thoại).
    async with session_factory() as s:
        s.add(Customer(id=cust_a, shop_id="shop_a", channel="zalo", external_id="zalo_u1"))
        s.add(Customer(id=cust_b, shop_id="shop_b", channel="zalo", external_id="zalo_u1"))
        await s.flush()
        s.add(Conversation(id=conv_a, shop_id="shop_a", customer_id=cust_a, channel="zalo"))
        s.add(Conversation(id=conv_b, shop_id="shop_b", customer_id=cust_b, channel="zalo"))
        await s.flush()
        s.add(
            OrderDraft(
                id=_uid("od"),
                shop_id="shop_a",
                conversation_id=conv_a,
                customer_id=cust_a,
                items=[{"sku": "A1", "qty": 2}],
            )
        )
        s.add(
            OrderDraft(
                id=_uid("od"),
                shop_id="shop_b",
                conversation_id=conv_b,
                customer_id=cust_b,
                items=[{"sku": "B1", "qty": 1}],
            )
        )
        await s.commit()

    async with session_factory() as s:
        for model in (Customer, Conversation, OrderDraft):
            rows = (await s.execute(select(model).where(model.shop_id == "shop_a"))).scalars().all()
            assert len(rows) == 1, f"{model.__name__}: scope shop_a trả {len(rows)} row (mong 1)"
            assert rows[0].shop_id == "shop_a", f"{model.__name__}: rò row shop khác"

    await engine.dispose()

    # Cùng external_id ở 2 shop phải cùng tồn tại được — tenant tách nhau, không đụng unique.
    assert cust_a != cust_b


@pytest.mark.asyncio
async def test_cross_shop_reference_rejected_by_database(fresh_db) -> None:
    """Conversation của shop A trỏ Customer của shop B ⇒ DB PHẢI từ chối (composite FK).

    Đây là test quan trọng nhất của F0: nó chứng minh tenant integrity được cưỡng chế ở tầng
    lưu trữ, không phải bằng thiện chí của caller. FK đơn `customer_id` sẽ PASS sai ở đây.
    """
    from db.models import Conversation, Customer

    engine, session_factory = await fresh_db()
    cust_b = _uid("cust")

    async with session_factory() as s:
        s.add(Customer(id=cust_b, shop_id="shop_b", channel="zalo", external_id="zalo_u9"))
        await s.commit()

    with pytest.raises(IntegrityError):
        async with session_factory() as s:
            # shop_a mượn customer của shop_b — phải vỡ ở constraint, không được lọt.
            s.add(
                Conversation(id=_uid("conv"), shop_id="shop_a", customer_id=cust_b, channel="zalo")
            )
            await s.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_pending_reply_orphan_columns_now_have_fk(fresh_db) -> None:
    """`pending_reply.conversation_id` / `.customer_id` không còn là Text mồ côi.

    Trước F0, hai cột này trỏ vào hư không mà DB vẫn nhận. Sau F0, id không tồn tại ⇒ reject.
    """
    from db.models import PendingReply

    engine, session_factory = await fresh_db()

    with pytest.raises(IntegrityError):
        async with session_factory() as s:
            s.add(
                PendingReply(
                    reply_id=_uid("rep"),
                    shop_id="shop_a",
                    conversation_id="conversation-khong-ton-tai",
                    customer_id="customer-khong-ton-tai",
                    draft_text="xin chào",
                    intent="greeting",
                    confidence=0.9,
                )
            )
            await s.commit()

    await engine.dispose()


@pytest.mark.asyncio
async def test_conversation_repo_scopes_by_shop(fresh_db) -> None:
    """Repo mới theo pattern `_shop_scope` của PendingReplyRepo: lọc shop_id ở tầng SQL."""
    from db.models import Conversation, Customer
    from db.repos import ConversationRepo

    engine, session_factory = await fresh_db()
    cust_a, cust_b = _uid("cust"), _uid("cust")
    conv_a = _uid("conv")

    async with session_factory() as s:
        s.add(Customer(id=cust_a, shop_id="shop_a", channel="zalo", external_id="u1"))
        s.add(Customer(id=cust_b, shop_id="shop_b", channel="zalo", external_id="u2"))
        await s.flush()
        s.add(Conversation(id=conv_a, shop_id="shop_a", customer_id=cust_a, channel="zalo"))
        s.add(Conversation(id=_uid("conv"), shop_id="shop_b", customer_id=cust_b, channel="zalo"))
        await s.commit()

    # Cùng shape với PendingReplyRepo: repo nhận AsyncSession + shop_scope keyword-only.
    async with session_factory() as s:
        repo_a = ConversationRepo(s, shop_scope="shop_a")
        rows = await repo_a.list_recent(limit=10)
        assert len(rows) == 1, f"ConversationRepo(shop_a) trả {len(rows)} row (mong 1)"
        assert rows[0].shop_id == "shop_a"

        # Truy cập trực tiếp bằng id của shop khác cũng phải trượt (ownership seam).
        repo_b = ConversationRepo(s, shop_scope="shop_b")
        assert await repo_b.get(conv_a) is None, "repo shop_b đọc được conversation của shop_a"

    # Scope rỗng phải bị từ chối ngay ở constructor (không có default cross-tenant).
    async with session_factory() as s:
        with pytest.raises(ValueError, match=r"(?i)shop_scope"):
            ConversationRepo(s, shop_scope="")

    await engine.dispose()


@pytest.mark.asyncio
async def test_order_draft_status_defaults_to_draft(fresh_db) -> None:
    """OrderDraft là chỗ chứa đơn AI trích chờ seller duyệt — mặc định phải là 'draft',
    KHÔNG phải trạng thái nào hàm ý đã xác nhận (guardrail §1.3: AI không tự chốt đơn)."""
    from sqlalchemy import select

    from db.models import Conversation, Customer, OrderDraft

    engine, session_factory = await fresh_db()
    cust, conv, od = _uid("cust"), _uid("conv"), _uid("od")

    async with session_factory() as s:
        s.add(Customer(id=cust, shop_id="shop_a", channel="zalo", external_id="u1"))
        await s.flush()
        s.add(Conversation(id=conv, shop_id="shop_a", customer_id=cust, channel="zalo"))
        await s.flush()
        s.add(
            OrderDraft(
                id=od,
                shop_id="shop_a",
                conversation_id=conv,
                customer_id=cust,
                items=[{"sku": "A1", "qty": 1}],
            )
        )
        await s.commit()

    async with session_factory() as s:
        row = (await s.execute(select(OrderDraft).where(OrderDraft.id == od))).scalar_one()
        assert row.status == "draft", f"OrderDraft.status mặc định = {row.status!r}, mong 'draft'"

    await engine.dispose()
