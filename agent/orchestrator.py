"""F3 receive-and-draft orchestrator (spec 01 §3 Sub-task E → A8).

Glues inbound customer message → draft (from a `Drafter`) → `policy_gate.decide` → a
parked `PendingReply` row scoped to `shop_id`, carrying `escalation_reasons`. PARK là
đường ra DUY NHẤT (A8 · I10): nhánh auto-send đã XOÁ khỏi codebase — phase 1 không có
code path nào đưa draft tới khách mà thiếu seller; mở lại là việc của w§8.1, không phải
của một refactor tiện tay.

Identity contract (spec 06 F1 — was a shim before):
  - `customer_id` and `conversation_id` are OURS, already resolved. This module never sees
    a platform's id format. `channels.identity.resolve_conversation` maps
    `(channel, external_user_id)` → our ids; the caller does that before calling here.
  - `conversation_id` is REQUIRED. It used to default to `customer_id` when the caller had
    no conversation model, which was survivable only while `conversation_id` was a bare
    Text column referencing nothing. Spec 06 F0 gave it a composite foreign key, so that
    fallback would now write a customer id into a conversation column and be rejected by
    Postgres at runtime. Requiring the argument turns a runtime FK violation into a caller
    error at the boundary.

Explicitly deferred to Phase 5+:
  - Real F1/F2 context enrichment — the `Drafter` protocol receives just the raw message
    for GĐ0. Layering wiki + API context happens in the `Drafter` implementation, not here.
  - Auth wire — the caller MUST supply `shop_id` from `auth.identity.Identity.shop_id`.
    This module cannot verify it; if the caller passes an unverified value, that's an S1
    breach caught upstream (webhook layer / API dependency).
"""

from __future__ import annotations

import uuid
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.policy_gate import DraftContext, decide
from db.models import Message
from db.repos import MessageRepo, PendingReplyRepo


class _Draft(Protocol):
    # Property, KHÔNG phải attribute trần: attribute trong Protocol là KHẢ GHI, mà impl thật
    # (`agent.drafter.DraftResult`) là frozen dataclass — mypy từ chối đúng luật (read-only
    # không thay được read-write). Orchestrator chỉ ĐỌC draft, nên read-only là contract đúng.
    @property
    def text(self) -> str: ...

    @property
    def intent(self) -> str: ...

    @property
    def confidence(self) -> float: ...


# Cap KÉP cho history nạp vào draft (spec 10 H2, PRE-1003 — Wyatt ký 2026-07-20).
#
# Vì sao hai cap chứ không một: cap số lượng một mình không chặn được 20 tin mỗi tin 3000
# ký tự — vẫn "đúng 20 tin" trong khi ngân sách token đã vỡ. Cap ký tự một mình thì một
# hội thoại toàn tin ngắn sẽ nạp hàng trăm lượt, tốn round-trip vô ích.
#
# ⚠️ HAI SỐ NÀY CHƯA ĐO — suy từ ước lượng ký tự→token tiếng Việt ≈ 3.3, chưa chạy tokenizer
# Llama-3.3 thật. Cùng họ ISSUE-022 (cap persona 2000). Đặt số để có ràng buộc cứng từ đầu,
# KHÔNG phải vì tin nó đúng. Đo lại khi có hội thoại thật.
HISTORY_MAX_MESSAGES = 20
HISTORY_MAX_CHARS = 4000


def _trim_history(rows: list[Message]) -> list[Message]:
    """Cắt history về trong cap, luôn giữ tin MỚI NHẤT.

    Cắt từ ĐẦU (tin cũ nhất) chứ không từ cuối: tin mới nhất là tin đang cần trả lời, tin
    cũ nhất mới là thứ bỏ được. Cắt nhầm đầu-đuôi không crash và không sai type — vẫn đúng
    số lượng, chỉ là AI đọc phần hội thoại đã cũ và bỏ mất câu đang hỏi.

    Luôn trả về ít nhất 1 row khi `rows` không rỗng, kể cả khi row đó một mình đã vượt
    `HISTORY_MAX_CHARS`: trả rỗng sẽ biến "tin quá dài" thành "không có ngữ cảnh gì", tức
    một tin dài bất thường lại làm AI mất trí nhớ hoàn toàn — im lặng và khó lần ra.
    """
    kept = rows[-HISTORY_MAX_MESSAGES:]
    total = sum(len(r.content) for r in kept)
    while len(kept) > 1 and total > HISTORY_MAX_CHARS:
        total -= len(kept[0].content)
        kept = kept[1:]
    return kept


class Drafter(Protocol):
    async def draft(
        self, *, shop_id: str, customer_id: str, message: str, history: list[Message]
    ) -> _Draft: ...


async def receive_and_draft(
    *,
    shop_id: str,
    customer_id: str,
    conversation_id: str,
    message: str,
    drafter: Drafter,
    session_factory: async_sessionmaker[AsyncSession],
    trace_id: uuid.UUID,
) -> str:
    """Draft → gate → PARK. Một đường ra duy nhất (A8 · I10 — phase 1 không có nhánh gửi;
    tham số `sender` + `shop_auto_enabled_intents` đã XOÁ, không phải tạm ẩn: code path
    gửi không tồn tại thì không có gì để bảo vệ bằng if).

    `shop_id` MUST come from verified auth. Park path writes ONLY to a repo scoped to
    the same `shop_id` — no cross-shop mutation possible even under a buggy caller.

    `trace_id` BẮT BUỘC (A5/G6) — sinh tại webhook, tới đây qua outbox job. Đường gọi
    không-qua-webhook (test, script) tự sinh `uuid.uuid4()` là hợp lệ: trace mới cho một
    lượt mới, miễn là draft nào cũng đối chiếu được §9.
    """
    # History load TRƯỚC khi draft. Repo scope theo `shop_id` ⇒ conversation của shop khác
    # trả rỗng chứ không raise (xem `MessageRepo.last_n`), nên một `conversation_id` sai
    # cho ra "không có ngữ cảnh", không cho ra ngữ cảnh của người khác.
    #
    # ⚠️ History NÀY ĐÃ CHỨA tin nhắn hiện tại. Từ A5/A7 người ghi inbound là outbox loop
    # của `app/worker_seller.py` (`append_inbound`, TRƯỚC khi set debounce — tin bền rồi
    # compose mới chạy ≥5s sau), nên `last_n` thấy luôn nó ở cuối. Hệ quả: `message` và
    # `history[-1].content` trùng nhau trên đường debounce. Giữ cả hai là có chủ ý:
    # `message` là "câu cần trả lời", `history` là "hội thoại tới giờ", và implementation
    # của `Drafter` cần phân biệt được hai vai đó. Đừng "sửa" bằng cách bỏ phần tử cuối —
    # gọi trực tiếp (không qua worker) thì phần tử cuối KHÔNG phải tin hiện tại, và cắt
    # mù sẽ ăn mất một lượt thật.
    async with session_factory() as session:
        history = _trim_history(
            await MessageRepo(session, shop_scope=shop_id).last_n(
                conversation_id, limit=HISTORY_MAX_MESSAGES
            )
        )

    draft = await drafter.draft(
        shop_id=shop_id, customer_id=customer_id, message=message, history=history
    )

    # Gate không còn quyết gửi/không (không có nhánh gửi để quyết) — nó xếp hạng lý do
    # seller cần chú ý. Pipeline hôm nay mới cấp `intent`; các cờ khác B7 wire dần (xem
    # docstring DraftContext).
    gate = decide(DraftContext(intent=draft.intent))

    # Park — đường ra DUY NHẤT. shop_id BAKED from repo scope (not caller args).
    #
    # CỐ Ý KHÔNG ghi `messages` ở đây (PRE-1004, Wyatt ký 2026-07-20). `PendingReply` đã là
    # bản ghi của nhánh này, và chưa có worker nào thực sự gửi (`api/inbox.py` approve chỉ
    # flip status). Ghi lúc park hay lúc approve đều là khai "đã gửi" trong khi không ai gửi.
    # Hệ quả đã chấp nhận: reply seller duyệt không vào history cho tới khi worker gửi land.
    #
    # Trả về CHỈ reply_id: escalation_reasons đã bền trên row — trả thêm bản sao là hai
    # nguồn cho cùng một fact (review A5-A8), ai cần thì đọc PendingReply.
    reply_id = uuid.uuid4().hex
    async with session_factory() as session:
        repo = PendingReplyRepo(session, shop_scope=shop_id)
        await repo.create(
            reply_id=reply_id,
            conversation_id=conversation_id,
            customer_id=customer_id,
            draft_text=draft.text,
            intent=draft.intent,
            confidence=draft.confidence,
            trace_id=trace_id,
            escalation_reasons=gate.escalation_reasons,
        )
    return reply_id
