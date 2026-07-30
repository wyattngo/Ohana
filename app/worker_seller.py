"""Worker luồng B (A5) — 2 loop: outbox dispatch (§6.2) + reaper outbox (I13).

Dispatch: claim ≤20 row `FOR UPDATE SKIP LOCKED` → COMMIT NGAY (không giữ transaction
qua lời gọi LLM — nguyên văn §6.2) → mỗi row gọi `receive_and_draft` (draft → gate →
park/send) → `done` (payload NULL, O8 trim) hoặc `settle_failure` (attempts <3 về
`pending`, hết lượt thì `failed` + last_error).

Reaper: `processing` kẹt >5' (worker chết sau claim) → về `pending`. I13: claim nào
cũng phải có reaper gỡ — thêm loại claim mới vào worker này thì nối nó vào reaper
TRƯỚC khi ship.

Debounce claim (§6.3) và reaper R3/R4 đổ bộ ở B6 — KHÔNG thuộc file này hôm nay.

Cùng role DB với `main_seller` (`svc_seller`), process riêng (I1):

    DATABASE_URL="postgresql+psycopg://svc_seller:$SVC_B_PW@localhost:5432/ohana" \\
        python -m app.worker_seller

`main()` cần drafter + sender thật (TOGETHER_API_KEY, Zalo creds — PRE-004/GD0-ZALO).
Thiếu thì THOÁT LỖI RÕ RÀNG như stub cũ: một worker im lặng ngồi không trông y hệt
worker khỏe trong `ps`, đúng kiểu hỏng I13 cấm. Logic loop test được không cần creds
(inject fake — tests/test_outbox_a5.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from dataclasses import dataclass
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.orchestrator import Drafter, receive_and_draft
from bridge.zalo_sender import ZaloSender
from db.repos import OutboxRepo

logger = logging.getLogger(__name__)

CLAIM_BATCH = 20
MAX_ATTEMPTS = 3
DISPATCH_IDLE_SECONDS = 2.0
REAPER_INTERVAL_SECONDS = 60.0
REAP_AFTER_MINUTES = 5


@dataclass
class OutboxWorker:
    """Hai loop của A5, dependencies inject được (test không cần creds thật).

    `senders`: channel name → sender — cùng shape với `channels` của webhook router;
    row có channel không đăng ký ⇒ settle_failure (không crash cả batch vì một row độc).
    """

    drafter: Drafter
    senders: dict[str, ZaloSender]
    session_factory: async_sessionmaker[AsyncSession]
    shop_auto_enabled: dict[str, frozenset[str]]

    async def run_dispatch_once(self) -> int:
        """Một vòng dispatch: claim batch → commit ngay → xử lý từng row. Trả số row claim."""
        async with self.session_factory() as session:
            rows = await OutboxRepo(session).claim_batch(limit=CLAIM_BATCH)
            await session.commit()  # §6.2: commit NGAY sau claim, trước mọi việc chậm

        for row in rows:
            await self._process(dict(row))
        return len(rows)

    async def _process(self, row: dict[str, object]) -> None:
        outbox_id = int(cast("int", row["outbox_id"]))
        try:
            payload = row["payload"]
            if not isinstance(payload, dict):
                raise ValueError(f"payload không phải object JSON: {type(payload).__name__}")
            sender = self.senders.get(str(row["channel"]))
            if sender is None:
                raise ValueError(f"channel không đăng ký sender: {row['channel']!r}")
            shop_id = str(row["shop_id"])
            await receive_and_draft(
                shop_id=shop_id,
                customer_id=str(payload["customer_id"]),
                conversation_id=str(payload["conversation_id"]),
                message=str(payload["text"]),
                drafter=self.drafter,
                sender=sender,
                session_factory=self.session_factory,
                shop_auto_enabled_intents=self.shop_auto_enabled.get(shop_id, frozenset()),
            )
        except Exception as exc:
            # Một row độc không được giết batch/worker: settle rồi đi tiếp. attempts đã
            # tăng lúc claim nên vòng đời hữu hạn (≤MAX_ATTEMPTS lần thử rồi `failed`).
            logger.warning("outbox %s lỗi: %r", outbox_id, exc)
            async with self.session_factory() as session:
                status = await OutboxRepo(session).settle_failure(
                    outbox_id, error=repr(exc), max_attempts=MAX_ATTEMPTS
                )
            logger.info("outbox %s → %s", outbox_id, status)
            return
        async with self.session_factory() as session:
            await OutboxRepo(session).mark_done(outbox_id)

    async def run_reaper_once(self) -> int:
        """Một vòng reaper (I13): gỡ claim kẹt quá `REAP_AFTER_MINUTES`. Trả số row gỡ."""
        async with self.session_factory() as session:
            reaped = await OutboxRepo(session).reap_stuck(older_than_minutes=REAP_AFTER_MINUTES)
        if reaped:
            logger.warning("reaper: gỡ %d claim outbox kẹt — có worker chết sau claim?", reaped)
        return reaped

    async def run_forever(self) -> None:
        """Hai loop song song. Dispatch ngủ ngắn khi queue rỗng; reaper chạy thưa."""

        async def dispatch_loop() -> None:
            while True:
                claimed = await self.run_dispatch_once()
                if claimed == 0:
                    await asyncio.sleep(DISPATCH_IDLE_SECONDS)

        async def reaper_loop() -> None:
            while True:
                await self.run_reaper_once()
                await asyncio.sleep(REAPER_INTERVAL_SECONDS)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(dispatch_loop())
            tg.create_task(reaper_loop())


def main() -> int:
    """Wire deps thật rồi chạy. Thiếu creds ⇒ thoát lỗi RÕ (đọc docstring module)."""
    from app.runtime import setup_logging

    setup_logging()

    if not os.environ.get("TOGETHER_API_KEY"):
        print(
            "worker_seller: thiếu TOGETHER_API_KEY — drafter không dựng được. "
            "Thoát lỗi thay vì giả vờ chạy (I13).",
            file=sys.stderr,
        )
        return 1

    # Sender Zalo thật cần OA creds (PRE-004/GD0-ZALO). Chưa clear ⇒ khai báo thẳng:
    # webhook cũng chưa mount nên queue chỉ có thể rỗng — worker chạy thật chỉ có nghĩa
    # khi GD0-ZALO xong. Tới lúc đó: dựng senders từ registry chung với main_seller.
    print(
        "worker_seller: chưa có sender registry (GD0-ZALO/PRE-004 chưa clear) — "
        "chưa có gì hợp lệ để drain. Thoát lỗi thay vì giả vờ chạy (I13).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
