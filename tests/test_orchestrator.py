"""Orchestrator receive-and-draft flow (spec 01 Sub-task E → A8 · I10).

Exercises the F3 glue end-to-end WITHOUT Zalo or a live LLM: the drafter is a
Protocol-based fake injected at call time.

A8: auto_send + threshold đã xoá cùng nhánh code — I10. `receive_and_draft` không còn
nhận `sender` / `shop_auto_enabled_intents`, không trả `ReceiveOutcome`: PARK là đường ra
DUY NHẤT, hàm trả thẳng `reply_id`. Test cũ đổi nghĩa theo:
  - test_safe_high_confidence_auto_enabled_sends → safe intent giờ CŨNG park, chỉ khác
    là `escalation_reasons` rỗng (không có nhánh nào chạm khách).
  - test_low_confidence_parks_even_for_safe_intent → confidence không gate gì nữa; nó
    chỉ được persist nguyên trạng lên row (tín hiệu train/hiển thị w§8.1).

Invariants còn lại:
  1. Sensitive intent → park row mang `escalation_reasons == ["sensitive_intent"]`.
  2. Park row's `shop_id` MUST match the caller identity — repo scoped to shop B never
     sees shop A's row (S4 ownership seam).
  3. `trace_id` truyền vào bền nguyên trạng trên row (A5/G6 — draft đối chiếu được §9).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select

from agent.orchestrator import receive_and_draft
from db.models import Conversation, Customer, PendingReply, Shop
from db.repos import PendingReplyRepo


@dataclass
class _FakeDraft:
    """What a real draft LLM would return — text + classified intent + confidence."""

    text: str
    intent: str
    confidence: float


class _FakeDrafter:
    """Deterministic draft generator — no LLM. Returns whatever preloaded response matches
    the incoming message text (fallthrough → generic reply)."""

    def __init__(self, responses: dict[str, _FakeDraft]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def draft(
        self, *, shop_id: str, customer_id: str, message: str, history: list[Any]
    ) -> _FakeDraft:
        self.calls.append({"shop_id": shop_id, "customer_id": customer_id, "message": message})
        for key, val in self._responses.items():
            if key.lower() in message.lower():
                return val
        return _FakeDraft(text="…", intent="general_qa", confidence=0.4)


async def _seed_parents(session_factory, *, shop_id: str, customer_id: str, conversation_id: str):
    """Create the Shop + Customer + Conversation a parked reply references.

    Spec 06 F0 gave `pending_reply.conversation_id` / `.customer_id` composite foreign keys
    `(shop_id, …)`; spec 11 S0 made `shops` the parent of `shop_id`. The parents must exist,
    same as production — invented ids straight through would FK-violate at INSERT.
    """
    async with session_factory() as s:
        s.add(Shop(id=shop_id, name=f"Shop {shop_id}", status="active"))
        await s.flush()
        s.add(Customer(id=customer_id, shop_id=shop_id, channel="zalo", external_id=customer_id))
        await s.flush()
        s.add(
            Conversation(
                id=conversation_id, shop_id=shop_id, customer_id=customer_id, channel="zalo"
            )
        )
        await s.commit()


async def _pending_rows(session_factory, *, shop_id: str) -> list[PendingReply]:
    async with session_factory() as s:
        return list(
            (await s.execute(select(PendingReply).where(PendingReply.shop_id == shop_id)))
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_sensitive_intent_parks_with_sensitive_reason(fresh_db) -> None:
    """Adversarial: confidence cao cỡ nào thì blocklist vẫn thắng — row park mang
    `escalation_reasons == ["sensitive_intent"]` để inbox sort nó lên đầu (§7)."""
    _, session_factory = await fresh_db()
    drafter = _FakeDrafter(
        {"refund": _FakeDraft(text="Draft: refund reply.", intent="refund", confidence=0.99)}
    )
    await _seed_parents(
        session_factory, shop_id="shop_a", customer_id="cust1", conversation_id="conv1"
    )

    trace = uuid.uuid4()
    reply_id = (
        await receive_and_draft(
            shop_id="shop_a",
            customer_id="cust1",
            conversation_id="conv1",
            message="I want a refund on order O1.",
            drafter=drafter,
            session_factory=session_factory,
            trace_id=trace,
        )
    ).reply_id

    rows = await _pending_rows(session_factory, shop_id="shop_a")
    assert len(rows) == 1
    row = rows[0]
    assert row.reply_id == reply_id
    assert row.status == "pending"
    assert row.intent == "refund"
    assert row.shop_id == "shop_a"
    assert row.escalation_reasons == ["sensitive_intent"]
    assert row.trace_id == trace, "trace_id phải bền nguyên trạng trên row (A5/G6)"

    # Sanity: repo scoped to a DIFFERENT shop sees nothing (S4 ownership seam holds).
    async with session_factory() as s:
        other = await PendingReplyRepo(s, shop_scope="shop_b").list_pending()
    assert other == [], "cross-shop repo must not see shop_a's row"


@pytest.mark.asyncio
async def test_safe_intent_still_parks_with_empty_reasons(fresh_db) -> None:
    """A8 · I10: KHÔNG có nhánh nào chạm khách — safe intent + confidence cao vẫn park.
    Khác biệt duy nhất với sensitive: `escalation_reasons` rỗng (draft thường)."""
    _, session_factory = await fresh_db()
    drafter = _FakeDrafter(
        {"hours": _FakeDraft(text="We are open 9-6.", intent="general_qa", confidence=0.95)}
    )
    await _seed_parents(
        session_factory, shop_id="shop_a", customer_id="cust1", conversation_id="conv1"
    )

    reply_id = (
        await receive_and_draft(
            shop_id="shop_a",
            customer_id="cust1",
            conversation_id="conv1",
            message="What are your hours?",
            drafter=drafter,
            session_factory=session_factory,
            trace_id=uuid.uuid4(),
        )
    ).reply_id

    rows = await _pending_rows(session_factory, shop_id="shop_a")
    assert len(rows) == 1
    assert rows[0].reply_id == reply_id
    assert rows[0].status == "pending"
    assert rows[0].draft_text == "We are open 9-6."
    assert rows[0].escalation_reasons == []


@pytest.mark.asyncio
async def test_low_confidence_persists_verbatim_and_parks(fresh_db) -> None:
    """Confidence không còn gate gì (A8) — draft mù mờ vẫn park như mọi draft, và con số
    được persist NGUYÊN TRẠNG lên row cho seller/tầng train nhìn thấy, không bị chặn/sửa."""
    _, session_factory = await fresh_db()
    drafter = _FakeDrafter(
        {"unclear": _FakeDraft(text="I'm not sure.", intent="general_qa", confidence=0.3)}
    )
    await _seed_parents(
        session_factory, shop_id="shop_a", customer_id="cust1", conversation_id="conv1"
    )

    reply_id = (
        await receive_and_draft(
            shop_id="shop_a",
            customer_id="cust1",
            conversation_id="conv1",
            message="Something unclear",
            drafter=drafter,
            session_factory=session_factory,
            trace_id=uuid.uuid4(),
        )
    ).reply_id

    rows = await _pending_rows(session_factory, shop_id="shop_a")
    assert len(rows) == 1
    assert rows[0].reply_id == reply_id
    assert rows[0].confidence == pytest.approx(0.3)
    assert rows[0].escalation_reasons == []
