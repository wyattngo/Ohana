"""Gate B6 — cost cap pre-charge wire vào compose (I8 · §6.5 §6.5b · w§2.4).

Bốn chốt (port từ nhánh b6-mainline-salvage, adapt sang worker 3-loop A5–A8):
1. Trần ⇒ drafter KHÔNG được gọi, finish sạch (claim gỡ, timer tắt, KHÔNG đếm poison),
   counter alert tăng, sổ sách không đổi.
2. Đường xanh: reserve 8000 → draft (usage thật 777) → reconcile: reserved 0, actual 777.
3. LLM nổ ⇒ reservation release NGAY (actual=0, không đợi R4); claim TREO cho R3 +
   poison counter +1 — đúng đường retry-có-nhịp của loop debounce.
4. `ensure_today` idempotent + cap chảy từ `shops.daily_token_cap` (a10) vào budget row.

Timer backdate bằng SQL như tests/test_webhook_e2e.py — DEBOUNCE_DELAY_SECONDS là tương lai.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sa_text

from app import alert_service
from app.worker_seller import WorkerDeps, run_debounce_loop
from db.models import Conversation, Customer, Message, PendingReply, Shop
from db.repos import CostRepo, SchedulerRepo

_SHOP = "shop_b6"
_CUS = "cus_b6"
_CONV = "conv_b6"


@dataclass
class _D:
    text: str
    intent: str
    confidence: float
    usage: dict[str, int] | None = None


class _UsageDrafter:
    """Draft park + báo usage thật (777 token) — như LLMDrafter cộng per-step."""

    async def draft(self, *, shop_id, customer_id, message, history) -> _D:
        return _D(
            text="draft",
            intent="general_qa",
            confidence=0.2,
            usage={"prompt_tokens": 700, "completion_tokens": 77, "total_tokens": 777},
        )


class _MustNotBeCalledDrafter:
    def __init__(self) -> None:
        self.called: list[str] = []

    async def draft(self, *, shop_id, customer_id, message, history) -> _D:
        self.called.append(shop_id)
        raise AssertionError("chạm trần mà vẫn gọi LLM — I8/w§2.4 vỡ")


async def _seed_due(session_factory, *, cap: int) -> None:
    """Cây tenant + 1 tin khách + timer debounce đã backdate về quá khứ (đến hạn ngay)."""
    async with session_factory() as s:
        s.add(Shop(id=_SHOP, name="Shop B6", daily_token_cap=cap))
        s.add(Customer(id=_CUS, shop_id=_SHOP, channel="zalo", external_id="z-b6"))
        await s.flush()
        s.add(Conversation(id=_CONV, shop_id=_SHOP, customer_id=_CUS, channel="zalo"))
        await s.flush()
        s.add(
            Message(
                shop_id=_SHOP,
                conversation_id=_CONV,
                customer_id=_CUS,
                role="user",
                content="áo này còn không",
            )
        )
        await s.commit()
        await SchedulerRepo(s).set_debounce(
            conversation_id=_CONV, delay_seconds=0, trace_id=uuid.uuid4()
        )
        await s.execute(
            sa_text("UPDATE conversations SET next_debounce_at = now() - interval '1 second'")
        )
        await s.commit()


async def _budget(session_factory) -> tuple[int, int]:
    async with session_factory() as s:
        row = (
            await s.execute(
                sa_text(
                    "SELECT reserved_tokens, actual_tokens FROM cost_budget "
                    "WHERE shop_id=:sid AND budget_date=CURRENT_DATE"
                ),
                {"sid": _SHOP},
            )
        ).one()
    return int(row[0]), int(row[1])


async def _open_reservations(session_factory) -> int:
    async with session_factory() as s:
        return (
            await s.execute(
                sa_text("SELECT count(*) FROM cost_reservation WHERE released_at IS NULL")
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_cap_hit_holds_without_calling_llm(fresh_db) -> None:
    """Cap 100 < reserve 8000 ⇒ GIỮ: drafter không đụng tới, finish sạch, counter tăng.

    KHÔNG đếm poison: cap là trạng thái ngân sách, không phải lỗi compose — đếm chung là
    5 lượt chạm trần biến thành give_up vĩnh viễn cho một hội thoại vô tội.
    """
    alert_service._reset_for_test()
    _, session_factory = await fresh_db()
    await _seed_due(session_factory, cap=100)

    drafter = _MustNotBeCalledDrafter()
    await run_debounce_loop(WorkerDeps(session_factory, drafter), run_once=True)

    assert drafter.called == [], "chạm trần mà LLM vẫn bị gọi"
    assert alert_service.cost_cap_hit_count(_SHOP) == 1

    async with session_factory() as s:
        conv = await s.get(Conversation, _CONV)
        drafts = (await s.execute(select(func.count()).select_from(PendingReply))).scalar_one()
    assert drafts == 0
    assert conv is not None and conv.debounce_claimed_at is None, "claim phải được gỡ (finish)"
    assert conv.next_debounce_at is None, "timer phải tắt — không hot-loop trên trần"
    assert conv.compose_failures == 0, "cap-hit không được đi đường poison"
    assert await _open_reservations(session_factory) == 0
    assert await _budget(session_factory) == (0, 0), "trần ⇒ sổ sách không đổi"


@pytest.mark.asyncio
async def test_draft_reconciles_budget_with_real_usage(fresh_db) -> None:
    """Đường xanh: reserve 8000 → draft (usage thật 777) → reconcile: reserved 0, actual 777."""
    _, session_factory = await fresh_db()
    await _seed_due(session_factory, cap=200_000)

    await run_debounce_loop(WorkerDeps(session_factory, _UsageDrafter()), run_once=True)

    async with session_factory() as s:
        drafts = (await s.execute(select(func.count()).select_from(PendingReply))).scalar_one()
    assert drafts == 1
    assert await _open_reservations(session_factory) == 0, "draft xong phải release (§6.5b)"
    assert await _budget(session_factory) == (0, 777), (
        "reconcile phải ghi token THẬT từ usage per-call, không phải ước lượng 8000"
    )


@pytest.mark.asyncio
async def test_llm_failure_releases_reservation_keeps_claim_for_r3(fresh_db) -> None:
    """LLM nổ ⇒ release NGAY (actual=0) — token không rò tới 5' của R4. Claim TREO cho R3
    (retry-có-nhịp của loop debounce) + poison counter +1, KHÔNG finish."""

    class _Boom:
        async def draft(self, *, shop_id, customer_id, message, history) -> _D:
            raise RuntimeError("LLM nổ")

    _, session_factory = await fresh_db()
    await _seed_due(session_factory, cap=200_000)

    await run_debounce_loop(WorkerDeps(session_factory, _Boom()), run_once=True)

    async with session_factory() as s:
        conv = await s.get(Conversation, _CONV)
    assert await _open_reservations(session_factory) == 0, "call hỏng phải release ngay"
    assert await _budget(session_factory) == (0, 0)
    assert conv is not None and conv.debounce_claimed_at is not None, (
        "compose lỗi ⇒ claim treo cho R3 gỡ — finish ở đây là retry nóng mỗi 500ms"
    )
    assert conv.compose_failures == 1, "lỗi LLM phải đếm trần poison"


@pytest.mark.asyncio
async def test_budget_bootstrap_idempotent_and_reads_shop_cap(fresh_db) -> None:
    """`ensure_today` hai lần ⇒ một row; cap trên row = `shops.daily_token_cap` (a10) —
    và lần hai với cap KHÁC không đè (đổi cap giữa ngày là quyết định vận hành)."""
    _, session_factory = await fresh_db()
    await _seed_due(session_factory, cap=12_345)

    async with session_factory() as s:
        shop_cap = (
            await s.execute(select(Shop.daily_token_cap).where(Shop.id == _SHOP))
        ).scalar_one()
        cost = CostRepo(s, shop_scope=_SHOP)
        await cost.ensure_today(cap_tokens=shop_cap)
        await cost.ensure_today(cap_tokens=99)  # lần hai, cap khác: no-op
        rows = (
            await s.execute(
                sa_text("SELECT cap_tokens FROM cost_budget WHERE shop_id=:sid"), {"sid": _SHOP}
            )
        ).all()
    assert len(rows) == 1, "bootstrap phải idempotent — một row một ngày"
    assert int(rows[0][0]) == 12_345, "cap phải lấy từ shops.daily_token_cap, không bị đè"
