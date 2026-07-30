"""Orchestrator draft-and-decide flow (spec 01 Phase 5 step 14).

Exercises the F3 glue end-to-end WITHOUT hitting Zalo or a live LLM: the draft LLM and
Zalo sender are Protocol-based fakes injected at call time. The gate proves two invariants:

  1. Sensitive intent → park (row lands in `pending_reply`, sender NEVER called).
  2. Safe + high-confidence + shop opted-in → send (sender called exactly once with the draft;
     no `pending_reply` row created).

The park row's `shop_id` MUST match the identity supplied by the caller — a caller for
shop A cannot cause a park row for shop B (S4 ownership seam).

Written BEFORE `agent/orchestrator.py`, `db/repos.py`, `bridge/zalo_sender.py`, and the
`PendingReply` model — expected RED until steps 15/16/17/18 land.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana"
)


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


class _FakeSender:
    """Records send calls without any network. In prod this is bridge/zalo_sender.py."""

    def __init__(self) -> None:
        self.sends: list[dict[str, Any]] = []

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
        self.sends.append({"shop_id": shop_id, "customer_id": customer_id, "text": text})


async def _seed_parents(session_factory, *, shop_id: str, customer_id: str, conversation_id: str):
    """Create the Customer + Conversation a parked reply now references.

    Spec 06 F0 gave `pending_reply.conversation_id` / `.customer_id` composite foreign keys
    `(shop_id, …)`. These tests used to pass invented ids straight through because the columns
    referenced nothing; now the parents must exist, same as production.

    Note these tests pass `conversation_id` EXPLICITLY from here on. `agent/orchestrator.py`
    still falls back to `conversation_id or customer_id` — a shim from before `conversations`
    existed, which would now FK-violate at runtime. That shim is KNOWN UNCOVERED in spec 06
    §7 F0 and must be fixed before the webhook is mounted; these tests deliberately do not
    exercise it rather than papering over it.
    """
    from db.models import Conversation, Customer

    async with session_factory() as s:
        s.add(Customer(id=customer_id, shop_id=shop_id, channel="zalo", external_id=customer_id))
        await s.flush()
        s.add(
            Conversation(
                id=conversation_id, shop_id=shop_id, customer_id=customer_id, channel="zalo"
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_sensitive_intent_parks_and_never_sends() -> None:
    """Adversarial: high confidence, shop opted into auto — the sensitive-intent blocklist
    still forces a park. Zalo sender is NEVER called."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from agent.orchestrator import receive_and_draft
    from db.models import Base, PendingReply
    from db.repos import PendingReplyRepo

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    drafter = _FakeDrafter(
        {"refund": _FakeDraft(text="Draft: refund reply.", intent="refund", confidence=0.99)}
    )
    sender = _FakeSender()

    await _seed_parents(
        session_factory, shop_id="shop_a", customer_id="cust1", conversation_id="conv1"
    )

    outcome = await receive_and_draft(
        shop_id="shop_a",
        customer_id="cust1",
        conversation_id="conv1",
        message="I want a refund on order O1.",
        drafter=drafter,
        sender=sender,
        session_factory=session_factory,
        shop_auto_enabled_intents=frozenset(),  # unused for sensitive — blocklist wins
    )

    assert outcome.action == "park"
    assert outcome.reply_id is not None
    assert sender.sends == [], "sensitive intent must NEVER call the sender"

    async with session_factory() as s:
        rows = (
            (await s.execute(select(PendingReply).where(PendingReply.shop_id == "shop_a")))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].status == "pending"
    assert rows[0].intent == "refund"
    assert rows[0].shop_id == "shop_a"

    # Sanity: repo scoped to a DIFFERENT shop sees nothing (S4 ownership seam holds).
    async with session_factory() as s:
        other = await PendingReplyRepo(s, shop_scope="shop_b").list_pending()
    assert other == [], "cross-shop repo must not see shop_a's row"

    await engine.dispose()


@pytest.mark.asyncio
async def test_safe_high_confidence_auto_enabled_sends() -> None:
    """The one path that reaches the customer without a seller in the loop."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from agent.orchestrator import receive_and_draft
    from db.models import Base, PendingReply

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    drafter = _FakeDrafter(
        {"hours": _FakeDraft(text="We are open 9-6.", intent="general_qa", confidence=0.95)}
    )
    sender = _FakeSender()

    # `_seed_parents` LÀ bắt buộc kể từ spec 10 H1. Comment cũ ở đây nói auto_send "writes
    # NO row" nên không đụng FK nào — điều đó đúng cho tới H1, khi nhánh auto_send bắt đầu
    # ghi `messages` (role=assistant) sau khi gửi thành công. Composite FK của H0 lập tức
    # từ chối id giả `conv1`/`cust1`, và đó là FK làm đúng việc của nó.
    await _seed_parents(
        session_factory, shop_id="shop_a", customer_id="cust1", conversation_id="conv1"
    )
    outcome = await receive_and_draft(
        shop_id="shop_a",
        customer_id="cust1",
        conversation_id="conv1",
        message="What are your hours?",
        drafter=drafter,
        sender=sender,
        session_factory=session_factory,
        shop_auto_enabled_intents=frozenset({"general_qa"}),
    )

    assert outcome.action == "auto_send"
    assert outcome.reply_id is None
    assert len(sender.sends) == 1
    assert sender.sends[0] == {
        "shop_id": "shop_a",
        "customer_id": "cust1",
        "text": "We are open 9-6.",
    }

    async with session_factory() as s:
        rows = (await s.execute(select(PendingReply))).scalars().all()
    assert rows == [], "auto_send path must NOT create a pending_reply row"

    await engine.dispose()


@pytest.mark.asyncio
async def test_low_confidence_parks_even_for_safe_intent() -> None:
    """Draft's own confidence is below threshold → park regardless of auto opt-in."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from agent.orchestrator import receive_and_draft
    from db.models import Base, PendingReply

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    drafter = _FakeDrafter(
        {"unclear": _FakeDraft(text="I'm not sure.", intent="general_qa", confidence=0.3)}
    )
    sender = _FakeSender()

    await _seed_parents(
        session_factory, shop_id="shop_a", customer_id="cust1", conversation_id="conv1"
    )

    outcome = await receive_and_draft(
        shop_id="shop_a",
        customer_id="cust1",
        conversation_id="conv1",
        message="Something unclear",
        drafter=drafter,
        sender=sender,
        session_factory=session_factory,
        shop_auto_enabled_intents=frozenset({"general_qa"}),
    )

    assert outcome.action == "park"
    assert sender.sends == []
    async with session_factory() as s:
        rows = (await s.execute(select(PendingReply))).scalars().all()
    assert len(rows) == 1

    await engine.dispose()
