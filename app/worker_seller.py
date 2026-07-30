"""Entrypoint worker luồng B (A4 → A5) — loop `outbox` chạy thật; debounce §6.3 và
reaper R3/R4 đổ bộ ở A7 (bảng/cột của chúng chưa tồn tại).

Nhịp một job (design §3, loop `outbox` 200ms):

    claim §6.2 (commit NGAY — không giữ transaction qua lời gọi LLM)
      → ghi tin khách vào `messages` (idempotent qua khoá C1 — requeue không nhân đôi tin)
      → compose draft qua `receive_and_draft` (A7 sẽ thay bước này bằng "set debounce";
        hôm nay compose trực tiếp = giữ hành vi draft-per-message có từ trước A5)
      → `done` · lỗi ⇒ `pending` thử lại, chạm trần attempts ⇒ `dead` + `last_error`

⚠️ Lỗ hổng ĐÃ BIẾT tới khi A7 land reaper R2: worker chết GIỮA claim và done thì row kẹt
`processing` vĩnh viễn (I13 đòi reaper gỡ — R2 là mảnh của A7). Chấp nhận được ở A5 vì
webhook chưa mount (PRE-004) nên chưa có traffic thật; KHÔNG mở webhook trước khi có R2.

Auto-send KHÔNG wire ở đây: `shop_auto_enabled` rỗng ⇒ policy_gate luôn park, và sender
là chốt-nổ (`RefuseSender`) để một nhánh auto-send ngoài dự kiến chết to thay vì gửi im
lặng ra ngoài (I10 — phase 1 không có đường tự gửi).

Cùng role DB với `main_seller` (`svc_seller`), process riêng:

    DATABASE_URL="postgresql+psycopg://svc_seller:$SVC_B_PW@localhost:5432/ohana" \\
        python -m app.worker_seller
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.llm_client import default_llm_client
from agent.orchestrator import Drafter, receive_and_draft
from app.runtime import setup_logging
from bridge.zalo_sender import ZaloSender
from db.repos import MessageRepo, OutboxJob, OutboxRepo
from db.session import make_session_factory

logger = logging.getLogger(__name__)

OUTBOX_TICK_SECONDS = 0.2  # design §3 — chu kỳ loop outbox khi queue rỗng


class RefuseSender:
    """Sender duy nhất của worker hôm nay — NỔ khi bị gọi.

    `shop_auto_enabled` rỗng nghĩa là `policy_gate` luôn park, nên `send()` không có đường
    nào tới được. Nếu nó vẫn được gọi thì một nhánh auto-send ngoài dự kiến đã mở — đó là
    vi phạm I10, phải chết to tại chỗ chứ không gửi im lặng ra khách. Outbound thật (Zalo
    HttpZaloSender + token) wire khi GD0-ZALO mở, qua `WorkerDeps.senders`.
    """

    name = "refuse"

    async def send(self, *, shop_id: str, customer_id: str, text: str) -> None:
        raise RuntimeError(
            "worker_seller: send() bị gọi trong khi auto-send chưa wire — nhánh auto-send "
            f"ngoài dự kiến (I10). shop_id={shop_id!r}"
        )


_REFUSE_SENDER = RefuseSender()


@dataclass(frozen=True)
class WorkerDeps:
    """Wiring của worker — DI như `build_router` để test thay được từng mảnh."""

    session_factory: async_sessionmaker[AsyncSession]
    drafter: Drafter
    senders: Mapping[str, ZaloSender] = field(default_factory=dict)
    shop_auto_enabled: Mapping[str, frozenset[str]] = field(default_factory=dict)


async def process_job(job: OutboxJob, deps: WorkerDeps) -> None:
    """Một job = một tin khách: ghi `messages` rồi compose draft.

    Payload đã được webhook chuẩn hoá + resolve identity (§6.1) — thiếu key là payload hỏng
    từ nguồn, KeyError bay lên cho vòng lỗi xử lý (pending/dead), không vá tại chỗ.

    Ghi message TRƯỚC compose, cùng lý do H1 cũ: drafter nổ thì tin khách vẫn đã bền; và
    `last_n` trong `receive_and_draft` nhờ vậy thấy tin hiện tại ở cuối history (contract
    đã ghi ở orchestrator). `append_inbound` trả False (job requeue — tin đã ghi lần trước)
    vẫn đi tiếp: draft có thể chưa kịp tạo trước khi worker cũ chết.
    """
    payload = job.payload
    async with deps.session_factory() as session:
        await MessageRepo(session, shop_scope=job.shop_id).append_inbound(
            conversation_id=payload["conversation_id"],
            customer_id=payload["customer_id"],
            content=payload["text"],
            platform_msg_id=payload["platform_msg_id"],
        )

    await receive_and_draft(
        shop_id=job.shop_id,
        customer_id=payload["customer_id"],
        conversation_id=payload["conversation_id"],
        message=payload["text"],
        drafter=deps.drafter,
        sender=deps.senders.get(payload["channel"], _REFUSE_SENDER),
        session_factory=deps.session_factory,
        shop_auto_enabled_intents=deps.shop_auto_enabled.get(job.shop_id, frozenset()),
        trace_id=job.trace_id,
    )


async def run_outbox_loop(deps: WorkerDeps, *, run_once: bool = False) -> None:
    """Loop §6.2: claim → xử lý từng job → done/failed. `run_once=True` cho test.

    Claim mở session riêng và commit ngay bên trong `claim_batch`; mỗi lần đổi trạng thái
    sau đó cũng session riêng — KHÔNG có transaction nào sống qua lời gọi LLM (yêu cầu
    tường minh của §6.2). Lỗi một job không giết loop: job đó về pending/dead, job sau chạy
    tiếp — một payload độc không được phép chặn cả queue.
    """
    while True:
        async with deps.session_factory() as session:
            jobs = await OutboxRepo(session).claim_batch()

        for job in jobs:
            try:
                await process_job(job, deps)
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
    )
    asyncio.run(run_outbox_loop(deps))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
