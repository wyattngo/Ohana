"""Cost counter per-user cho Tầng 2 (ADR D3, Phase 2.2).

KHÁC Tầng 3 B6 (`db/repos.py::CostRepo` — atomic reserve trên Postgres per-shop). Tầng 2
per-USER, best-effort trên Redis (counter thuần INCRBY, không reserve). Trade-off có
chủ đích:
- Tầng 3 B6 · reserve → LLM call → reconcile: PHẢI atomic (double-charge = mất tiền).
- Tầng 2 P2.2 · record sau LLM: best-effort là ĐỦ (over-count 1 request khi Redis chớp
  còn hơn crash user; D4).

**Không cap trong module.** Cost gate (so sánh với tier limit free/pro) là việc của
caller (chat router Phase 2.4). Module này chỉ cung cấp COUNT — separation of concerns:
"biết tier" tách khỏi "đếm token".

**UTC bucket.** Key format `cost:user:{user_id}:{YYYY-MM-DD}` theo UTC. Không dùng
timezone user vì (a) user_id chưa có tz claim ở P2.2, (b) bucket UTC ổn cho MVP; nếu về
sau cần "reset lúc nửa đêm giờ VN", đổi format ở đây (single point) + backfill nếu cần.

**TTL 25h** (đủ overlap 1h qua ngày mới): key ngày X tự dọn sau ngày X+1 dù không ai gọi
GET. Không dùng `EXPIRE` thuần vì `INCRBY` không set TTL nếu key đã tồn tại — dùng
pipeline `INCRBY + EXPIRE` (idempotent: `EXPIRE` gọi lại chỉ reset TTL, không ảnh hưởng
counter).
"""

from __future__ import annotations

from datetime import UTC, datetime

from redis.asyncio import Redis

from agent.redis_client import fail_open

# TTL 25h = 90000s. Overlap 1h qua nửa đêm UTC để race read-after-midnight không mất key
# vừa bị đọc cho báo cáo. Đủ ngắn để Redis memory không phình vô hạn.
_KEY_TTL_SECONDS = 25 * 60 * 60


def _key(user_id: str) -> str:
    """`cost:user:{user_id}:{YYYY-MM-DD}` (UTC)."""
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"cost:user:{user_id}:{day}"


@fail_open(default=None)
async def record_tokens(redis: Redis, user_id: str, tokens: int) -> None:
    """INCRBY `tokens` vào bucket ngày hôm nay + EXPIRE 25h.

    Fail-open (D4): Redis chớp ⇒ mất 1 record, KHÔNG raise vì đây gọi ngay sau LLM
    response — exception ở tầng cost không được nuốt exception LLM/render.

    Pipeline `INCRBY + EXPIRE`: INCRBY không set TTL khi key đã tồn tại; EXPIRE đảm
    bảo TTL luôn được refresh khi có ghi. Nếu chỉ có INCRBY, key tạo trước Phase 2.2
    ship (không TTL) sẽ NGỒI VĨNH VIỄN — đây là bẫy Redis kinh điển.
    """
    key = _key(user_id)
    pipe = redis.pipeline()
    pipe.incrby(key, tokens)
    pipe.expire(key, _KEY_TTL_SECONDS)
    await pipe.execute()


@fail_open(default=0)
async def get_daily_tokens(redis: Redis, user_id: str) -> int:
    """GET bucket ngày hôm nay. Trả 0 nếu key chưa có (Redis nil → 0).

    Fail-open trả 0 = "không biết đã tiêu bao nhiêu ⇒ coi như chưa tiêu". Tier gate ở
    caller sẽ ALLOW (dưới limit) — cùng bài D4 rate-limit fail-open True.
    """
    value = await redis.get(_key(user_id))
    if value is None:
        return 0
    return int(value)
