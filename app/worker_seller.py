"""Entrypoint worker luồng B (A4) — SẼ chạy 3 loop: outbox dispatch (§6.2),
debounce claim (§6.3), reaper R3/R4 (§6.9 · §6.10).

Ba loop đổ bộ ở A5 (outbox) và A7 (debounce + reaper) — bảng `outbox` chưa tồn
tại nên chưa có gì để chạy. Stub này THOÁT LỖI RÕ RÀNG thay vì giả vờ chạy: một
worker im lặng ngồi không sẽ trông y hệt một worker khỏe trong `ps`, và đó chính
là kiểu hỏng I13 cấm (claim không ai reap, conversation im lặng vĩnh viễn).

Cùng role DB với `main_seller` (`svc_seller`), process riêng:

    DATABASE_URL="postgresql+psycopg://svc_seller:$SVC_B_PW@localhost:5432/ohana" \\
        python -m app.worker_seller
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "worker_seller: chưa có loop nào để chạy — outbox (A5) và debounce/reaper (A7) "
        "chưa đổ bộ. Thoát lỗi thay vì giả vờ chạy.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
