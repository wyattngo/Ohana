"""Entrypoint worker luồng B (A4 → A5 → A7 → OHB-23) — đủ 4 loop của design §3:

| Loop       | Chu kỳ | Việc                                                        |
|------------|--------|-------------------------------------------------------------|
| `outbox`   | 200ms  | claim §6.2 → ghi tin khách (C1) → set debounce (coalesce)   |
| `debounce` | 500ms  | claim §6.3 conversation đến hạn → compose draft (guard O9)  |
| `send`     | 500ms  | claim draft `approved` → OutboundChannel.send → mark 'sent' |
| `reaper`   | 10s    | R1 draft hết TTL · R2 outbox kẹt · R3 §6.9 · R4 §6.10 · R5  |

Coalesce (w§2.2): tin mới ĐẨY LÙI `next_debounce_at`, nên cụm tin gõ liên tiếp thành MỘT
draft. Lỗi compose ⇒ để claim treo cho R3 gỡ sau 5' — retry có nhịp, không retry nóng.
Lỗ "row kẹt processing khi worker chết" khai ở A5 đã ĐÓNG bằng R2 (I13 tròn: mọi claim —
outbox, debounce, reservation, gửi — đều có reaper gỡ).

Send-worker (OHB-23) là loop SAU seller approve — NOT auto-send. `receive_and_draft` chỉ
park (I10 giữ nguyên); `api/inbox.py::approve` chỉ flip status → 'approved'; loop này
drain 'approved' rồi gọi sender. Không có path nào đi TỪ compose THẲNG tới sender —
seller vẫn là chốt duy nhất giữa draft và khách. GĐ0 sender là `MockZaloSender` (bridge)
— HTTP thật (`HttpZaloSender`) chờ PRE-004 (Zalo creds); wire là đổi MỘT dòng ở main().

Cùng role DB với `main_seller` (`svc_seller`), process riêng:

    DATABASE_URL="postgresql+psycopg://svc_seller:$SVC_B_PW@localhost:5432/ohana" \\
        python -m app.worker_seller
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.llm_client import default_llm_client
from agent.orchestrator import Drafter, receive_and_draft
from app import alert_service
from app.runtime import setup_logging
from bridge.zalo_sender import MockZaloSender, ZaloSender
from db.models import Shop
from db.repos import (
    CostRepo,
    DebounceDue,
    MessageRepo,
    OutboxJob,
    OutboxPayload,
    OutboxRepo,
    PendingReplyRepo,
    SchedulerRepo,
    SendJob,
    SendQueueRepo,
)
from db.session import make_session_factory

logger = logging.getLogger(__name__)

OUTBOX_TICK_SECONDS = 0.2  # design §3 — chu kỳ loop outbox khi queue rỗng
DEBOUNCE_TICK_SECONDS = 0.5  # design §3 — chu kỳ quét conversation đến hạn
SEND_TICK_SECONDS = 0.5  # OHB-23 — chu kỳ quét approved (cùng nhịp debounce; approved
# tới từ seller-approve, không có hot-path SLA như outbox 200ms)
REAPER_TICK_SECONDS = 10.0  # design §3 — chu kỳ reaper

# Khoảng chờ coalesce: từ TIN CUỐI tới lúc compose (khác chu kỳ quét 500ms ở trên — quét
# là nhịp nhìn đồng hồ, đây là đồng hồ). Design không chốt số (Wyatt ký 5s, 2026-07-30,
# lượt duyệt A7). ⚠️ CHƯA ĐO trên hội thoại thật — cùng họ HISTORY caps ở orchestrator:
# đặt số để có ràng buộc cứng từ đầu, KHÔNG phải vì tin nó đúng. Đo lại khi có traffic.
DEBOUNCE_DELAY_SECONDS = 5.0

# Trần thời gian một lượt compose (bọc cả LLM) — PHẢI < 5' của reaper: compose sống lâu
# hơn mốc R3 là claim bị gỡ giữa chừng ⇒ worker khác compose song song ⇒ draft đôi (vỡ
# C2). 240s chừa 20% lề. Đây là enforcement bằng code của điều kiện O10 vốn chỉ nằm trong
# docstring (review A5-A8 #5). TimeoutError đi chung đường compose-lỗi (đếm trần poison).
COMPOSE_TIMEOUT_SECONDS = 240.0

# Trần lỗi compose LIÊN TIẾP trước khi bỏ cuộc một conversation (review A5-A8 #6): thiếu
# nó, drafter nổ tất định ⇒ R3 gỡ claim mỗi 5' ⇒ gọi LLM lại vĩnh viễn (~288 lần/ngày).
# Đạt trần ⇒ NULL timer + GIỮ conversations.compose_failures cho vận hành query (quyết
# 2026-07-30: dừng + counter, không park draft placeholder). Thành công ⇒ reset 0.
MAX_COMPOSE_FAILURES = 5

# Reserve ước lượng mỗi lượt draft (Wyatt duyệt spec B6): history cap 4k chars (~1.2k tok)
# + persona + tool rounds — 8k là trần thô có lề, cùng họ số-chưa-đo ISSUE-022. Reconcile
# §6.5b sửa sổ sách về số thật ngay sau mỗi call nên sai số chỉ sống vài giây; provider
# không báo usage ⇒ ghi luôn ước lượng làm actual (cao hơn thật — an toàn theo chiều
# không vượt cap).
RESERVE_TOKENS_PER_DRAFT = 8_000


@dataclass(frozen=True)
class WorkerDeps:
    """Wiring của worker — DI như `build_router` để test thay được từng mảnh.

    `sender` (OHB-23): dùng CHO loop 3 (send-worker) — gửi CHỈ SAU khi seller approve
    (I10 giữ nguyên: không path nào gửi khi draft chưa approve; `receive_and_draft` vẫn
    park-only). GĐ0 nhận `MockZaloSender` (bridge, zero I/O); PRE-004 clear thì đổi
    sang `HttpZaloSender` — cùng `bridge.zalo_sender.ZaloSender` Protocol, không đổi
    wiring loop. `channels.base.OutboundChannel` là seam INBOUND-parse + outbound cấp
    channel (mang `name`) cho webhook mount — send-worker không cần `name` (routing
    đọc từ row conversation), nên ràng buộc bằng `ZaloSender` sát hơn.
    """

    session_factory: async_sessionmaker[AsyncSession]
    drafter: Drafter
    sender: ZaloSender


# ── Loop 1 · outbox (§6.2) ───────────────────────────────────────────────────────────────


async def process_job(job: OutboxJob, deps: WorkerDeps) -> None:
    """Một job = một tin khách: ghi `messages` rồi đặt/đẩy lùi timer debounce.

    KHÔNG compose ở đây (khác A5): compose thuộc loop debounce — tách ra để N tin liên
    tiếp của một khách thành MỘT draft thay vì N draft (w§2.2). Payload parse qua
    `OutboxPayload.from_payload` — hợp đồng MỘT chỗ với webhook (ISSUE-024); row hỏng/
    thiếu key vẫn KeyError bay lên cho vòng lỗi xử lý (pending/dead), không vá tại chỗ.

    Ghi message TRƯỚC set debounce: timer nổ sớm nhất cũng 5s sau, tin đã bền trong
    `messages` cho compose đọc. `append_inbound` trả False (job requeue — tin đã ghi lần
    trước) vẫn set debounce tiếp: draft có thể chưa kịp compose trước khi worker cũ chết.
    """
    payload = OutboxPayload.from_payload(job.payload)
    # MỘT session cho cả job — hai repo call vẫn commit tuần tự riêng (luật "không
    # transaction xuyên bước" nói về transaction, không cấm dùng chung connection); tách
    # session là nhân đôi pool checkout trên hot path 200ms không được gì (review A5-A8).
    async with deps.session_factory() as session:
        await MessageRepo(session, shop_scope=job.shop_id).append_inbound(
            conversation_id=payload.conversation_id,
            customer_id=payload.customer_id,
            content=payload.text,
            platform_msg_id=payload.platform_msg_id,
        )
        await SchedulerRepo(session).set_debounce(
            conversation_id=payload.conversation_id,
            delay_seconds=DEBOUNCE_DELAY_SECONDS,
            trace_id=job.trace_id,
        )


async def run_outbox_loop(deps: WorkerDeps, *, run_once: bool = False) -> None:
    """Loop §6.2: claim → xử lý từng job → done/failed. `run_once=True` cho test.

    Claim mở session riêng và commit ngay bên trong `claim_batch`; mỗi lần đổi trạng thái
    sau đó cũng session riêng — KHÔNG có transaction nào sống qua bước xử lý (yêu cầu
    tường minh của §6.2). Lỗi một job không giết loop: job đó về pending/dead (worker
    chết giữa chừng thì R2 trả về pending sau 5'), job sau chạy tiếp.
    """
    while True:
        async with deps.session_factory() as session:
            jobs = await OutboxRepo(session).claim_batch()

        for job in jobs:
            try:
                await process_job(job, deps)
            except asyncio.CancelledError:
                # OHB-22 · SIGTERM giữa job: mark_failed → về `pending` với backoff 2s
                # (attempts+1). Nhanh hơn R2 5' và cùng shape với đường lỗi thường
                # (attempts đếm đều). Không đặc biệt hóa vì SIGTERM không phải trạng
                # thái row — vận hành đọc last_error để phân biệt.
                async with deps.session_factory() as session:
                    await OutboxRepo(session).mark_failed(
                        job.outbox_id, "graceful shutdown (SIGTERM)"
                    )
                raise
            except Exception as exc:
                logger.exception("outbox job %s lỗi (attempts=%s)", job.outbox_id, job.attempts)
                async with deps.session_factory() as session:
                    await OutboxRepo(session).mark_failed(
                        job.outbox_id, f"{type(exc).__name__}: {exc}"
                    )
            else:
                async with deps.session_factory() as session:
                    await OutboxRepo(session).mark_done(job.outbox_id)

        if run_once:
            return
        if not jobs:
            await asyncio.sleep(OUTBOX_TICK_SECONDS)


# ── Loop 2 · debounce (§6.3) ─────────────────────────────────────────────────────────────


async def compose_due(item: DebounceDue, deps: WorkerDeps) -> bool:
    """Compose draft cho một conversation ĐÃ claim. Trả `True` = đã compose (caller finish),
    `False` = không có tin khách nào để trả lời — caller KHÔNG finish (xem loop bên dưới).

    `message` = tin KHÁCH mới nhất, query đúng 1 row `role='user'` ở SQL — KHÔNG lọc trong
    cửa sổ last-N (review A5-A8 #8: N tin phía shop liên tiếp làm pre-scan mù tin khách,
    draft bị nuốt êm).

    `trace_id` từ `debounce_trace_id` (G6 — trace của tin cuối batch); row từ trước A7
    chưa có ⇒ sinh mới, hợp lệ theo contract của `receive_and_draft`.

    Pre-charge (B6 · I8): reserve TRƯỚC lời gọi LLM — chạm trần ⇒ KHÔNG gọi LLM, đếm
    alert, trả `True` để caller finish (KHÔNG đi đường poison: cap là trạng thái ngân
    sách, không phải lỗi compose; retry mỗi 5' tới nửa đêm chỉ spam counter). LLM nổ ⇒
    release ngay (`reconcile(actual=0)`) rồi re-raise cho đường đếm trần; release chính
    nó nổ thì R4 (§6.10) gỡ sau 5' — reservation không bao giờ mồ côi quá một chu kỳ reaper.
    """
    async with deps.session_factory() as session:
        latest_customer = await MessageRepo(
            session, shop_scope=item.shop_id
        ).latest_customer_message(item.conversation_id)
    if latest_customer is None:
        return False

    if item.trace_id is None:
        # Trace bịa là ĐỨT CHUỖI G6 — hợp lệ chỉ cho row từ trước A7/ghi tay, nhưng phải
        # THẤY được: một regression làm set_debounce rơi trace sẽ hiện ở đây thành warning
        # đều đặn thay vì âm thầm sinh draft không đối chiếu được §9 (review A5-A8).
        logger.warning(
            "conversation %s không có debounce_trace_id — sinh trace mới, đứt chuỗi G6",
            item.conversation_id,
        )
    trace_id = item.trace_id if item.trace_id is not None else uuid.uuid4()

    # Pre-charge §6.5 — cap đọc từ `shops.daily_token_cap` (a10, nguồn cap DUY NHẤT).
    # `ensure_today` idempotent, KHÔNG đè cap của row ngân sách đang đếm giữa ngày.
    async with deps.session_factory() as session:
        cap_tokens = (
            await session.execute(select(Shop.daily_token_cap).where(Shop.id == item.shop_id))
        ).scalar_one()
        cost = CostRepo(session, shop_scope=item.shop_id)
        await cost.ensure_today(cap_tokens=cap_tokens)
        reservation_id = await cost.reserve(tokens=RESERVE_TOKENS_PER_DRAFT, trace_id=trace_id)

    if reservation_id is None:
        # Chạm trần (hoặc fail-closed thiếu ngân sách): GIỮ im — không gọi LLM (w§2.4).
        # Counter là tín hiệu vận hành duy nhất của nhánh này, đừng để nó âm thầm.
        alert_service.record_cost_cap_hit(item.shop_id)
        logger.warning(
            "cost cap: shop %s chạm trần %s tokens/ngày — bỏ lượt compose %s (trace %s)",
            item.shop_id,
            cap_tokens,
            item.conversation_id,
            trace_id,
        )
        return True

    # Timeout BỌC cả lượt compose (LLM bên trong) — cưỡng chế O10 < 5' của R3/R4 bằng
    # code: compose sống lâu hơn mốc reaper là draft đôi (C2) + reservation bị R4 gỡ oan.
    try:
        async with asyncio.timeout(COMPOSE_TIMEOUT_SECONDS):
            outcome = await receive_and_draft(
                shop_id=item.shop_id,
                customer_id=item.customer_id,
                conversation_id=item.conversation_id,
                message=latest_customer.content,
                drafter=deps.drafter,
                session_factory=deps.session_factory,
                trace_id=trace_id,
            )
    except BaseException:
        # BaseException (KHÔNG Exception): OHB-22 · asyncio.CancelledError không phải
        # Exception trong Python 3.8+; SIGTERM giữa LLM call phải release reservation
        # ngay như đường lỗi thường — nếu không, cancel-path leak reserved_tokens tới 5'
        # (R4 gỡ, nhưng brief đòi "nhường ngay"). Release nổ thì nuốt-và-log (R4 là
        # lưới), re-raise lỗi GỐC (cả CancelledError lẫn Exception) cho caller quyết.
        try:
            async with deps.session_factory() as session:
                await CostRepo(session, shop_scope=item.shop_id).reconcile(
                    reservation_id=reservation_id, actual_tokens=0
                )
        except Exception:
            logger.exception("release reservation %s lỗi — R4 gỡ sau 5' (§6.10)", reservation_id)
        raise

    # Reconcile §6.5b về token THẬT. Provider không báo ⇒ giữ ước lượng làm actual (cao
    # hơn thật — an toàn theo chiều không vượt cap). Trả False = R4 đã release trước
    # (compose sát mốc 5') — token lượt này không vào sổ, chỉ log, không raise.
    actual = (outcome.usage or {}).get("total_tokens", RESERVE_TOKENS_PER_DRAFT)
    async with deps.session_factory() as session:
        reconciled = await CostRepo(session, shop_scope=item.shop_id).reconcile(
            reservation_id=reservation_id, actual_tokens=actual
        )
    if not reconciled:
        logger.warning(
            "reconcile no-op: reservation %s đã released (R4 gỡ trước?) — %s tokens không vào sổ",
            reservation_id,
            actual,
        )
    return True


async def run_debounce_loop(deps: WorkerDeps, *, run_once: bool = False) -> None:
    """Loop §6.3: đọc ứng viên đến hạn → claim từng cái → compose → thả claim.

    Claim 0 row ⇒ instance khác đã lấy ⇒ bỏ qua (C2: đúng 1 draft dù N scheduler).
    Compose LỖI ⇒ KHÔNG finish — claim treo có chủ đích, R3 gỡ sau 5' rồi lượt sau thử
    lại; finish trong nhánh lỗi là biến retry-có-nhịp thành retry nóng mỗi 500ms.
    """
    while True:
        async with deps.session_factory() as session:
            due = await SchedulerRepo(session).due_conversations()

        for item in due:
            # O9=B guard (OHB-7) — TRƯỚC claim: conversation đã có draft `pending` ⇒ BỎ lượt
            # compose này. Không claim, không reserve, không finish: tin mới ở lại `messages`,
            # timer giữ nguyên `due` để lượt sau (sau khi seller quyết draft hiện tại) cuốn cả
            # cụm tích luỹ. 🚫 KHÔNG finish/clear timer trong nhánh skip — clear = drop tin
            # arming (approve/reject KHÔNG re-arm debounce). Không claim nên không có gì để
            # drop; đánh đổi: timer treo due ⇒ re-check mỗi DEBOUNCE_TICK tới khi pending được
            # quyết (rẻ — một SELECT, và chỉ khi khách nhắn tiếp lúc đang review).
            async with deps.session_factory() as session:
                if await PendingReplyRepo(session, shop_scope=item.shop_id).has_pending(
                    item.conversation_id
                ):
                    continue
            async with deps.session_factory() as session:
                claimed_at = await SchedulerRepo(session).claim_debounce(item.conversation_id)
            if claimed_at is None:
                continue
            try:
                composed = await compose_due(item, deps)
            except asyncio.CancelledError:
                # OHB-22 · SIGTERM giữa compose: release claim NGAY (không NULL timer,
                # không đếm poison — cancel ≠ lỗi drafter). Worker khác/lượt kế claim
                # lại tick sau, không chờ R3 5'. Reservation đã release trong compose_due
                # (BaseException handler). Re-raise CancelledError cho run_worker cleanup.
                async with deps.session_factory() as session:
                    await SchedulerRepo(session).release_debounce_claim(
                        item.conversation_id, claimed_at=claimed_at
                    )
                raise
            except Exception:
                # Lỗi (kể cả TimeoutError của COMPOSE_TIMEOUT_SECONDS): đếm trần poison.
                # Chưa đạt trần ⇒ để claim treo cho R3 gỡ sau 5' — retry có nhịp. Đạt trần
                # ⇒ bỏ cuộc: thôi retry, giữ counter cho vận hành (không đốt LLM vĩnh viễn).
                logger.exception("compose lỗi: %s", item.conversation_id)
                async with deps.session_factory() as session:
                    failures = await SchedulerRepo(session).record_compose_failure(
                        item.conversation_id
                    )
                if failures >= MAX_COMPOSE_FAILURES:
                    async with deps.session_factory() as session:
                        await SchedulerRepo(session).give_up_debounce(
                            item.conversation_id, claimed_at=claimed_at
                        )
                    logger.error(
                        "compose bỏ cuộc sau %s lỗi liên tiếp: %s "
                        "(conversations.compose_failures giữ nguyên cho vận hành)",
                        failures,
                        item.conversation_id,
                    )
                continue
            if not composed:
                # Không có tin khách để trả lời — BẤT THƯỜNG (timer chỉ được arm sau khi
                # append_inbound). KHÔNG finish: finish sẽ nuốt timer và nếu tin khách vào
                # muộn (race) thì không ai draft. Đi cùng đường đếm trần như compose lỗi —
                # claim treo cho R3, lặp đủ trần thì bỏ cuộc thay vì loop 5' vĩnh viễn.
                logger.warning(
                    "debounce đến hạn nhưng không có tin khách: %s", item.conversation_id
                )
                async with deps.session_factory() as session:
                    failures = await SchedulerRepo(session).record_compose_failure(
                        item.conversation_id
                    )
                if failures >= MAX_COMPOSE_FAILURES:
                    async with deps.session_factory() as session:
                        await SchedulerRepo(session).give_up_debounce(
                            item.conversation_id, claimed_at=claimed_at
                        )
                continue
            async with deps.session_factory() as session:
                await SchedulerRepo(session).finish_debounce(
                    item.conversation_id, due_at=item.due_at, claimed_at=claimed_at
                )

        if run_once:
            return
        if not due:
            await asyncio.sleep(DEBOUNCE_TICK_SECONDS)


# ── Loop 3 · send (OHB-23) ───────────────────────────────────────────────────────────────


async def send_one(job: SendJob, deps: WorkerDeps) -> None:
    """Một send-job: gọi `OutboundChannel.send` (network) → mark_sent (echo :claimed_at).
    Sender raise ⇒ release_send_claim ngay (draft về 'approved' cho lượt kế) rồi re-raise
    cho caller log — KHÔNG mark 'sent' khi sender chưa xác nhận.

    Mark_sent rowcount=0 nghĩa là R5 đã gỡ claim (worker này treo >5') hoặc worker khác
    đã claim lại — không log 'sent' oan, cảnh báo cho vận hành thấy claim-timeout đang
    xảy ra. rowcount=1 là đường thành công bình thường."""
    await deps.sender.send(shop_id=job.shop_id, customer_id=job.customer_id, text=job.draft_text)
    async with deps.session_factory() as session:
        n = await PendingReplyRepo(session, shop_scope=job.shop_id).mark_sent(
            job.reply_id, claimed_at=job.claimed_at
        )
    if n == 0:
        # R5 gỡ claim trước khi mark_sent land — sender ĐÃ gửi (có thể double-send nếu
        # worker sau claim lại và gửi lại). Đây là đánh đổi đã ký: I13 (không im lặng
        # vĩnh viễn) ưu tiên hơn "gửi đúng 1 lần" tuyệt đối cho gạch dưới GĐ0 — Zalo
        # HTTP thật lấy idempotency key qua bên platform (PRE-004), chỗ đó khoá cứng.
        logger.warning(
            "mark_sent no-op: reply %s bị R5/worker khác soán quyền — có thể double-send "
            "khi lượt sau claim lại (I13 ưu tiên hơn strict-once ở GĐ0, xem OHB-23 note)",
            job.reply_id,
        )


async def run_send_loop(deps: WorkerDeps, *, run_once: bool = False) -> None:
    """Loop OHB-23: claim → gửi → mark. Lỗi một job không giết loop (cùng bài outbox loop):
    release claim, log exception, job kế chạy tiếp. `run_once=True` cho test."""
    while True:
        async with deps.session_factory() as session:
            jobs = await SendQueueRepo(session).claim_batch()

        for job in jobs:
            try:
                await send_one(job, deps)
            except asyncio.CancelledError:
                # OHB-22 · SIGTERM giữa send (giữa network call, giữa mark_sent): release
                # claim ngay để lượt kế/instance restart claim lại, không chờ R5 5'.
                # KHÔNG log exception ở đây (SIGTERM là bình thường — noise ở shutdown).
                try:
                    async with deps.session_factory() as session:
                        await PendingReplyRepo(session, shop_scope=job.shop_id).release_send_claim(
                            job.reply_id, claimed_at=job.claimed_at
                        )
                except Exception:
                    logger.exception(
                        "release_send_claim (SIGTERM) lỗi reply=%s — R5 gỡ sau 5'", job.reply_id
                    )
                raise
            except Exception:
                logger.exception(
                    "send lỗi reply=%s shop=%s trace=%s", job.reply_id, job.shop_id, job.trace_id
                )
                try:
                    async with deps.session_factory() as session:
                        await PendingReplyRepo(session, shop_scope=job.shop_id).release_send_claim(
                            job.reply_id, claimed_at=job.claimed_at
                        )
                except Exception:
                    logger.exception("release_send_claim lỗi reply=%s — R5 gỡ sau 5'", job.reply_id)

        if run_once:
            return
        if not jobs:
            await asyncio.sleep(SEND_TICK_SECONDS)


# ── Loop 4 · reaper (R1–R5) ──────────────────────────────────────────────────────────────


async def run_reaper_loop(deps: WorkerDeps, *, run_once: bool = False) -> None:
    """Năm việc mỗi 10s. Log CHỈ khi có gì để gỡ — reaper im lặng là reaper khỏe; một dòng
    log mỗi 10s là noise che mất chính tín hiệu nó phải phát."""
    while True:
        async with deps.session_factory() as session:
            counts = await SchedulerRepo(session).reap()
        if any(counts.values()):
            logger.warning("reaper gỡ: %s", {k: v for k, v in counts.items() if v})
        if run_once:
            return
        await asyncio.sleep(REAPER_TICK_SECONDS)


async def run_worker(deps: WorkerDeps) -> None:
    """Bốn loop trong MỘT process (design §3 — MUST NOT tách N process vì 'LLM/HTTP block
    event loop'; `await` không block loop). Một loop chết = cả worker chết + exit ≠ 0:
    worker khập khiễng (còn outbox, mất send) trông y hệt worker khỏe trong `ps` — đúng
    kiểu hỏng I13 cấm. Chết to để orchestration (systemd/k8s) restart cả cụm.

    OHB-22 · SIGTERM graceful: nhận SIGTERM/SIGINT → set stop_event → cancel 4 loop task
    → mỗi loop bắt CancelledError → release claim đang cầm (debounce/send/outbox) → exit 0.
    Signal handler chỉ SET event (không cancel task trong callback — asyncio yêu cầu
    handler chạy ngắn và không được cancel task từ signal context an toàn); loop chính
    quan sát event rồi cancel + await gather. Kill -9 vẫn bypass — R2/R3/R5 giữ mạng lưới
    5' cho ca đó (I13 tròn ở cả hai đường).
    """
    stop_event = asyncio.Event()
    running_loop = asyncio.get_running_loop()

    def _request_stop(signame: str) -> None:
        # Chỉ set event, KHÔNG cancel tasks ở đây — signal handler chạy trong context
        # đặc biệt, cancel trực tiếp dễ tạo race. Loop chính đọc event, cancel gọn.
        logger.info("nhận %s — bắt đầu graceful shutdown", signame)
        stop_event.set()

    # add_signal_handler CHỈ hoạt động trên Unix event loop; test có thể chạy trên loop
    # khác (Windows CI, ProactorEventLoop) — bọc để không giết run_worker khi platform
    # không hỗ trợ. Bỏ qua signal handler cũng không mất tính đúng: kill -9 hoặc CTRL-C
    # trên platform lạ vẫn kick-in qua KeyboardInterrupt path (asyncio.run bắt).
    for signame in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            running_loop.add_signal_handler(sig, _request_stop, signame)
        except (NotImplementedError, RuntimeError):
            # Windows / SelectorEventLoop trong test không cài được — dựa vào
            # asyncio.run bắt KeyboardInterrupt (SIGINT) làm fallback SIGINT-only.
            logger.debug("add_signal_handler(%s) không hỗ trợ trên platform này", signame)

    loop_tasks = [
        asyncio.create_task(run_outbox_loop(deps), name="outbox"),
        asyncio.create_task(run_debounce_loop(deps), name="debounce"),
        asyncio.create_task(run_send_loop(deps), name="send"),
        asyncio.create_task(run_reaper_loop(deps), name="reaper"),
    ]
    stop_task = asyncio.create_task(stop_event.wait(), name="stop_watch")

    done, pending = await asyncio.wait(
        [*loop_tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
    )

    # Hai đường tới đây:
    # (a) stop_event set (SIGTERM) — cancel 4 loop, gather kết quả, exit 0.
    # (b) một loop chết — cancel 3 loop còn lại + stop_task, re-raise lỗi loop chết.
    if stop_task in done:
        for t in loop_tasks:
            t.cancel()
        results = await asyncio.gather(*loop_tasks, return_exceptions=True)
        for name, res in zip(("outbox", "debounce", "send", "reaper"), results, strict=True):
            if isinstance(res, BaseException) and not isinstance(res, asyncio.CancelledError):
                logger.exception("loop %s lỗi lúc shutdown: %r", name, res)
        return

    # Đường (b): một loop chết. Cancel stop_task + các loop còn lại rồi re-raise lỗi gốc.
    stop_task.cancel()
    dead = next(t for t in done if t in loop_tasks)
    for t in loop_tasks:
        if t is not dead and not t.done():
            t.cancel()
    await asyncio.gather(*(t for t in loop_tasks if t is not dead), return_exceptions=True)
    # dead.result() re-raise exception gốc của loop chết (nếu có) — worker chết to.
    dead.result()


def main() -> int:
    setup_logging()
    # Dựng LLM client TRƯỚC khi vào loop — thiếu env provider thì thoát lỗi rõ ràng ngay
    # lúc start (một worker im lặng ngồi không trông y hệt worker khỏe trong `ps` — đúng
    # kiểu hỏng I13 cấm), không phải nổ ở job đầu tiên rồi đếm attempts oan.
    try:
        llm = default_llm_client()
    except Exception as exc:
        print(f"worker_seller: không dựng được LLM client — {exc}", file=sys.stderr)
        return 1

    # Import trong hàm, cùng lý do `default_llm_client` import lười provider: LLMDrafter
    # kéo persona/tools — để module này import được trong test mà không cần cả cây đó.
    from agent.drafter import LLMDrafter

    session_factory = make_session_factory()
    deps = WorkerDeps(
        session_factory=session_factory,
        drafter=LLMDrafter(llm, session_factory),
        # GĐ0: MockZaloSender (zero I/O, ghi vào .sends). PRE-004 clear thì đổi sang
        # HttpZaloSender(client=None, session_factory=session_factory, app_id=..., app_secret=...)
        # — cùng ZaloSender Protocol, không đổi thân loop. Wire creds là việc của GD0-ZALO,
        # không phải OHB-23.
        sender=MockZaloSender(),
    )
    asyncio.run(run_worker(deps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
