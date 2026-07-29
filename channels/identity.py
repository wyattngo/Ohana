"""External identity → internal identity (spec 06 Phase F1).

This module exists to kill the shim at `agent/orchestrator.py` that read
`conversation_id or customer_id`. That fallback predates the `conversations` table; after
spec 06 F0 added composite foreign keys it would FK-violate at runtime — a parked reply
would reference a conversation id that is really a customer id and Postgres would reject it.

Placement is deliberate: mapping `(channel, external_user_id)` to our own ids belongs to the
CHANNEL layer, because that is the only layer that knows what a Zalo user id looks like.
The orchestrator should never learn a platform's id format; it receives ids that are already
ours.

Everything here is shop-scoped. The same human messaging two shops produces two customers
and two conversations — see `db.models.Customer` for why that separation is absolute.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Conversation, Customer


async def resolve_conversation(
    session: AsyncSession,
    *,
    shop_id: str,
    channel: str,
    external_user_id: str,
    external_thread_id: str | None = None,
) -> tuple[str, str]:
    """Return `(customer_id, conversation_id)`, creating either if this is a first contact.

    Idempotent on `(shop_id, channel, external_user_id)`: a second message from the same
    person reuses the same rows instead of minting a new customer per inbound message.

    The customer insert uses `ON CONFLICT DO NOTHING` against the
    `uq_customers_shop_chan_ext` constraint, then re-selects — so two concurrent inbound
    messages cannot produce duplicate customers.

    The conversation insert uses the same shape against
    `uq_conversations_shop_cus_chan_thread` (spec 09 C0, closes ISSUE-017) — so neither half
    of this function depends on request timing any more.

    That constraint carries `NULLS NOT DISTINCT`, which is what makes it bite in the common
    case: `external_thread_id` is usually NULL (Zalo does not always send `thread_id`), and
    plain SQL treats NULLs as distinct — a normal UNIQUE would wave both rows through.

    Idempotency key is `(shop_id, customer_id, channel, external_thread_id)`, so two distinct
    threads from one customer stay two conversations. Whether Zalo actually rotates
    `thread_id` mid-conversation is unknown (PRE-004, blocked) — see spec 09 §14 for why the
    recoverable-if-wrong option was chosen.
    """
    if not shop_id:
        raise ValueError("shop_id is required — must come from verified auth, never a payload")
    if not external_user_id:
        raise ValueError("external_user_id is required — refusing to attribute a message to nobody")

    await session.execute(
        pg_insert(Customer)
        .values(
            id=f"cus_{uuid.uuid4().hex[:16]}",
            shop_id=shop_id,
            channel=channel,
            external_id=external_user_id,
        )
        .on_conflict_do_nothing(constraint="uq_customers_shop_chan_ext")
    )
    await session.flush()

    customer_id = (
        await session.execute(
            select(Customer.id)
            .where(Customer.shop_id == shop_id)
            .where(Customer.channel == channel)
            .where(Customer.external_id == external_user_id)
        )
    ).scalar_one()

    # Đối xứng HOÀN TOÀN với nhánh Customer ở trên: insert-on-conflict-do-nothing rồi
    # re-select. Bản trước là select-then-insert — hai tin nhắn đồng thời cùng thấy "chưa
    # có" rồi cùng insert ⇒ 2 conversation (ISSUE-017). Giờ Postgres là trọng tài, không
    # phải thứ tự may rủi của hai request.
    #
    # `order_by(created_at.desc()).limit(1)` của bản cũ đã bỏ: nó tồn tại để CHỌN giữa nhiều
    # conversation trùng, mà giờ trùng là điều constraint cấm. Giữ lại sẽ là dấu vết của một
    # mô hình không còn đúng, và che mất việc `scalar_one()` bây giờ phải luôn tìm thấy đúng
    # một row — nếu không thì có gì đó sai, và ta muốn biết chứ không muốn im lặng lấy cái mới nhất.
    await session.execute(
        pg_insert(Conversation)
        .values(
            id=f"cnv_{uuid.uuid4().hex[:16]}",
            shop_id=shop_id,
            customer_id=customer_id,
            channel=channel,
            external_thread_id=external_thread_id,
        )
        .on_conflict_do_nothing(constraint="uq_conversations_shop_cus_chan_thread")
    )
    await session.flush()

    conversation_id = (
        await session.execute(
            select(Conversation.id)
            .where(Conversation.shop_id == shop_id)
            .where(Conversation.customer_id == customer_id)
            .where(Conversation.channel == channel)
            # `is_(None)` chứ không `== None`: phải khớp ĐÚNG hàng mà upsert vừa nhắm tới.
            # Lọc thiếu cột này thì khách có thread_B sẽ nhận về conversation của thread_A —
            # tức gộp nhầm hai mạch, đúng thứ phương án B sinh ra để tránh.
            .where(
                Conversation.external_thread_id.is_(None)
                if external_thread_id is None
                else Conversation.external_thread_id == external_thread_id
            )
        )
    ).scalar_one()

    await session.commit()
    return customer_id, conversation_id
