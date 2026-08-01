"""Tier gate cho Tầng 2 — free/pro static limit + consume rate + cost primitives.

Consume `agent/assistant_rate_limit.py::try_acquire` và `agent/assistant_cost.py::
get_daily_tokens` (P2.2 primitives). Gate function `check_and_reserve` gọi TRƯỚC LLM
call ở router (P2.4b consume; primitive-only tại P2.4a).

**Thứ tự cố ý** trong `check_and_reserve`:
1. Rate-limit FIRST (INCR — side-effect: chiếm 1 slot phút hiện tại).
2. Cost cap SECOND (GET — read-only).

Ngược lại (cost trước): nếu rate deny, ta vẫn tốn 1 GET Redis. Với thứ tự này, rate
deny short-circuit ngay, không tốn GET; và cost deny chỉ xảy ra khi rate còn quota.

**Fail-open D4 propagate tự nhiên** — cả `try_acquire` và `get_daily_tokens` đã
fail-open (True/0). Redis chớp ⇒ `check_and_reserve` trả `allowed=True, daily_tokens_
used=0`. KHÔNG override fail-open ở gate — cost > UX khi Redis lỗi là quyết định D4.

**KHÔNG record token ở đây** — record là việc SAU LLM call (biết `usage.total_tokens`
thật). Đây chỉ check + reserve rate slot.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

from agent.assistant_cost import get_daily_tokens
from agent.assistant_rate_limit import try_acquire
from auth.user_identity import UserIdentity


@dataclass(frozen=True)
class TierLimit:
    """Hạn mức của một tier. `qpm` = requests per minute (rate); `daily_tokens` = token
    cap trong 1 ngày (cost)."""

    qpm: int
    daily_tokens: int


# Whitelist ĐÓNG — value phải khớp `auth/user_identity.py::_ALLOWED_TIERS`. Thêm tier
# mới: sửa cả hai chỗ CÙNG LÚC (verify pass + gate map). Một chỗ đổi tạo silent hole
# (verify pass tier "team" nhưng gate không có key ⇒ `unknown_tier` deny).
TIER_LIMITS: dict[str, TierLimit] = {
    "free": TierLimit(qpm=10, daily_tokens=50_000),
    "pro": TierLimit(qpm=60, daily_tokens=500_000),
}


@dataclass(frozen=True)
class TierVerdict:
    """Kết quả gate. Router (P2.4b) dùng `reason` để trả 429 có structure cho UI:
    - `""` khi `allowed=True`.
    - `"rate_limit_exceeded"` khi vượt QPM.
    - `"daily_cost_cap_exceeded"` khi vượt token cap.
    - `"unknown_tier"` khi tier không có trong `TIER_LIMITS` (belt-and-braces với verify).

    `daily_tokens_used` echo cho response body — UI hiển thị quota progress. Là int, 0
    nếu chưa GET được (fail-open) hoặc chưa tiêu.
    """

    allowed: bool
    reason: str
    daily_tokens_used: int


async def check_and_reserve(redis: Redis, user_identity: UserIdentity) -> TierVerdict:
    """Gate trước LLM call: chiếm 1 rate slot + kiểm cost cap.

    Belt-and-braces `TIER_LIMITS.get(tier)` cho case verify + gate lệch (refactor sau
    lỡ mở tier mới ở verify nhưng quên gate). Bình thường không xảy ra vì verify đã
    whitelist enum.
    """
    limit = TIER_LIMITS.get(user_identity.tier)
    if limit is None:
        return TierVerdict(allowed=False, reason="unknown_tier", daily_tokens_used=0)

    rate_ok = await try_acquire(redis, user_identity.user_id, limit_qpm=limit.qpm)
    if not rate_ok:
        return TierVerdict(allowed=False, reason="rate_limit_exceeded", daily_tokens_used=0)

    used = await get_daily_tokens(redis, user_identity.user_id)
    if used >= limit.daily_tokens:
        return TierVerdict(allowed=False, reason="daily_cost_cap_exceeded", daily_tokens_used=used)

    return TierVerdict(allowed=True, reason="", daily_tokens_used=used)
