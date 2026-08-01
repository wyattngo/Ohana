"""Rate-limit per-user cho Tầng 2 (ADR D3+D4, Phase 2.2).

**Algo: fixed window 1-phút.** Đơn giản, thuần primitive INCR + EXPIRE. Đủ cho MVP;
sliding window / token bucket phức tạp hơn không có bằng chứng (abuse đo được) đang
thiếu. Comment tại chỗ marker cho P2.2+ nếu measurement chứng minh cần chặt hơn.

**Key format** `rl:user:{user_id}:qpm:{YYYY-MM-DDTHH:MM}` (UTC, tách bằng phút). Mỗi
phút một bucket mới; bucket cũ tự dọn sau TTL 60s. Không cần `DEL`, không cần cleanup
job.

**D4 fail-open trả True.** Redis chớp ⇒ ALLOW (không chặn user). Tường minh trong test
để refactor sau không lỡ đổi ngữ nghĩa (ví dụ đổi thành fail-closed cho "an toàn hơn"
là ngược spec D4 — cost > UX là quyết định đã ký, không suy đoán lại).

**`limit_qpm` là INPUT** — module không biết tier free/pro. Caller (chat router Phase
2.4) tra `tier` claim → suy limit → gọi `try_acquire`. Isolation "biết tier" khỏi "đếm
request" — cùng bài với `assistant_cost` không cap.

**Race INCR + EXPIRE.** Hai lệnh, không atomic — EXPIRE có thể trượt nếu process chết
giữa hai lệnh (key sống mãi với giá trị 1, allow-until-forever KHÔNG happen — chỉ ảnh
hưởng key **đó** trong **phút đó**; phút sau key mới ra đời với TTL đúng). Trong PROD
dùng Lua atomic; ở MVP chấp nhận drift 60s tối đa cho một bucket lẻ, xem `alert_service`
comment "MVP in-process" cùng dáng.
"""

from __future__ import annotations

from datetime import UTC, datetime

from redis.asyncio import Redis

from agent.redis_client import fail_open

# Window size = 1 phút = 60s. Format key nhét phút xuống dưới giây (nếu về sau đổi window
# thành 10s / 5 phút, đổi hằng số này + format string bên dưới cùng lúc — cùng single
# point như TTL của assistant_cost).
_WINDOW_SECONDS = 60


def _key(user_id: str) -> str:
    """`rl:user:{user_id}:qpm:{YYYY-MM-DDTHH:MM}` (UTC, phút hiện tại)."""
    minute = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    return f"rl:user:{user_id}:qpm:{minute}"


def _search_key(user_id: str) -> str:
    """Key riêng cho search endpoint (R3 · ADR round2). Namespace `rl:search:` để không
    đụng bucket chat (`rl:user:`) — user có thể search full-speed đồng thời chat, không
    ăn quota chéo."""
    minute = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M")
    return f"rl:search:{user_id}:qpm:{minute}"


@fail_open(default=True)
async def try_acquire_search(redis: Redis, user_id: str, limit_qpm: int) -> bool:
    """Search rate limit (R3). Bucket riêng khỏi chat — search cheap ở DB, nhưng dễ spam
    (bot scraping / accidental infinite loop client). Cùng thuật fixed-window 60s, cùng
    fail-open D4. Caller (endpoint) tra `tier` free/pro suy `limit_qpm` (đề xuất: free
    30, pro 120)."""
    key = _search_key(user_id)
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, _WINDOW_SECONDS)
    results = await pipe.execute()
    return int(results[0]) <= limit_qpm


@fail_open(default=True)
async def try_acquire(redis: Redis, user_id: str, limit_qpm: int) -> bool:
    """True nếu user còn quota trong phút hiện tại, False nếu vượt `limit_qpm`.

    Fail-open (D4): Redis chớp ⇒ True (allow). Cost > UX khi Redis down.

    `pipeline` gộp INCR + EXPIRE cùng round-trip (2 RTT → 1). EXPIRE luôn set thay vì
    "chỉ khi mới tạo" (`EXPIRE ... NX`) — refresh TTL mỗi lần INCR làm bucket sống lâu
    hơn 60s, KHÔNG SAO vì key sinh tại minute:M chỉ được INCR trong minute:M (phút sau
    ra key mới); TTL dư chỉ giữ counter đọc-lại cho quan sát, không ảnh hưởng gate.
    """
    key = _key(user_id)
    pipe = redis.pipeline()
    pipe.incr(key)
    pipe.expire(key, _WINDOW_SECONDS)
    results = await pipe.execute()
    # `results[0]` là giá trị mới sau INCR (int, không phải bytes vì INCR trả integer
    # thuần theo Redis protocol). So sánh với limit — vượt = False.
    count = int(results[0])
    return count <= limit_qpm
