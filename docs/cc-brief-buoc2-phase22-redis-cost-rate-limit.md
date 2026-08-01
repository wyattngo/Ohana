# CC Brief · Bước 2 · Phase 2.2 — Redis + cost counter + rate-limit per-user (Tầng 2)

**Frozen** · form `ohana-be-coder` · scope decision: **1 PR** (Wyatt 2026-08-01).

## Bối cảnh & phạm vi

Phase 2.1 (PR #6, `8aba78b`) đã ship: schema `assistant` + grant `svc_ohana_ai` WRITE
trong `assistant` (D2), isolation gate hai chiều xanh. Phase 2.2 mở luồng A chạy được:
đo cost per-user + rate-limit per-user + fail-open (D3/D4).

**KHÔNG scope** (Phase 2.4+):
- Chat router mới sử dụng cost/rate-limit (P2.4).
- Tier gate free/pro dùng `tier` claim (P2.4, cần D7).
- Memory port (P2.3).
- Billing hook `upgrade(user_id)` (P2.4+).
- Rewrite `app/alert_service.py` MVP in-process → Redis (out of scope — riêng track).

**Chỉ ship** hạ tầng + primitives để P2.4 wire vào chat router.

## Hồ sơ ADR (đọc trước khi code)

`docs/adr-tang2-ohana-ai-assistant.md`:
- **D3** — Redis vào compose. Cost counter + rate-limit per-user cần Redis KV. §10 cấm
  Redis-làm-queue, KHÔNG cấm Redis-làm-counter. +1 infra dep là chấp nhận vì counter
  trên Postgres thua ở hot-path contention.
- **D4** — Redis down ⇒ rate-limit **fail-open** (v1). Redis chớp ⇒ không chặn user,
  mất gate tạm. An toàn trải nghiệm > an toàn cost khi Redis lỗi. Revisit khi có abuse đo được.

## Bất biến chạm

- **I1** — luồng A/B tách. Modules mới sống trong `agent/` (luồng A). Thêm contract
  `I1c · agent.assistant_* chỉ luồng A` vào `pyproject.toml [tool.importlinter]` — chặn
  `bridge/channels/api.webhook/api.inbox/app.main_seller/agent.drafter/orchestrator/policy_gate`
  import `agent.assistant_cost`/`agent.assistant_rate_limit`/`agent.redis_client`.
- **I2** — dữ liệu shop vẫn DENIED cho `svc_ohana_ai`. Phase 2.2 KHÔNG chạm DB (Redis-only
  cho cost/rate), KHÔNG migration, KHÔNG grant Postgres mới. I2 giữ tự động.
- **I14** — không có bảng mới ⇒ không áp dụng.
- **D4 (fail-open)** — cưỡng chế bằng decorator `@fail_open`, test tường minh (chèn Redis
  raise) — không phải "trust me it fails open", có test bắt regress.

## Việc — thứ tự thi công

### 1. Compose + config

**`docker-compose.yml`** — thêm service `redis` (image `redis:7-alpine`, port
`${OHANA_REDIS_PORT:-6379}:6379`, healthcheck `redis-cli ping`, KHÔNG volume — counter
per-day có thể mất, kỷ luật MVP).

**`app/config.py`** — thêm field `redis_url: str = "redis://localhost:6379/0"` trong
`Settings`. Field VÔ HƯỚNG ⇒ `_blank_env_means_unset` tự phủ (env `REDIS_URL=` rỗng ⇒
default). Comment tại chỗ: "0" là DB index Redis; tách namespace bằng key prefix, không
tách DB (Redis DB 0..15 là legacy anti-pattern).

### 2. Redis client + fail-open helper (`agent/redis_client.py`)

**Chức năng:**
- `make_redis_pool() -> ConnectionPool` — factory từ `Settings.redis_url`, dùng
  `redis.asyncio.ConnectionPool.from_url(...)`. Singleton qua lifespan (giống
  `make_session_factory` cho DB).
- `get_redis(pool) -> Redis` — trả `redis.asyncio.Redis(connection_pool=pool)`.
- `fail_open(default: T)` — async decorator. Bắt `redis.RedisError` +
  `ConnectionError` + `asyncio.TimeoutError`, log WARNING structured
  (`event=redis_fail_open, op=<fn>, err=...`), return `default`. Không catch `Exception`
  chung — bắt hết = che bug code mình.

**Nguyên tắc D4:** `fail_open` cho READ (get_daily_tokens → 0) và cho GATE
(try_acquire → True). Cho WRITE (record_tokens) cũng fail-open (mất 1 record token)
vì Redis lỗi không nên hiện lỗi cho user trong lúc gọi LLM.

### 3. Cost counter (`agent/assistant_cost.py`)

**Semantic:** đếm token per-user per-day. **KHÁC Tầng 3 B6** (per-shop, atomic reserve
trên Postgres). Tầng 2 per-user, best-effort trên Redis (counter thuần, không reserve).

**API:**
```python
async def record_tokens(redis, user_id: str, tokens: int) -> None:
    """INCRBY key = cost:user:{user_id}:{YYYY-MM-DD}, EXPIRE 25h."""

async def get_daily_tokens(redis, user_id: str) -> int:
    """GET key, trả 0 nếu chưa có (Redis nil → 0)."""
```

**Key format:** `cost:user:{user_id}:{YYYY-MM-DD}` (UTC).
`YYYY-MM-DD` = "cost bucket" theo ngày UTC. Không dùng timezone user vì (a) user_id
chưa có tz claim ở P2.2, (b) bucket UTC ổn cho MVP.

**Idempotency:** không cần. `INCRBY` là accumulate — hai lần gọi cộng đôi là ĐÚNG (khác
với `mark_sent` idempotent). Caller `chat_router` gọi 1 lần sau khi LLM trả token thật.

**Bounds:** không cap trong module — cost gate (so sánh với tier limit) là việc của caller
(P2.4). Module này chỉ cung cấp COUNT.

### 4. Rate-limit (`agent/assistant_rate_limit.py`)

**Algo:** fixed window 1-phút. Đơn giản, thuần primitive (INCR + EXPIRE), không cần Lua
script. Đủ cho MVP; sliding window / token bucket phức tạp hơn không có evidence bio đang
thiếu.

**API:**
```python
async def try_acquire(redis, user_id: str, limit_qpm: int) -> bool:
    """True nếu allow, False nếu vượt limit trong phút hiện tại.

    Key = rl:user:{user_id}:qpm:{YYYY-MM-DDTHH:MM} (UTC, phút).
    Op: INCR key → nếu == 1: EXPIRE 60 (window tự dọn). Nếu > limit_qpm: False.
    """
```

**Ghi chú race:** `INCR` + `EXPIRE` tách hai lệnh: EXPIRE có thể trượt nếu process chết
giữa hai lệnh. Trong PROD dùng pipeline `MULTI/EXEC` hoặc Lua atomic. Ở MVP fixed-window,
key có TTL vô định thoáng qua (60s max drift) không đủ nặng để cần Lua ngay — comment tại
chỗ marker "P2.2+" nếu measurement chứng minh cần chặt hơn.

**limit_qpm là INPUT** — module không biết tier free vs pro. Caller (chat router P2.4)
tra `tier` claim → suy ra `limit_qpm` → gọi `try_acquire`. Isolation giữa "biết tier" và
"đếm request".

### 5. Wire lifespan trong `app/main_ohana_ai.py`

- App startup: `redis_pool = await make_redis_pool()`.
- App shutdown: `await redis_pool.aclose()`.
- Expose `redis_pool` qua `app.state.redis_pool` để router lấy được (D1 · router chat
  chưa dùng ở P2.2, nhưng infra sẵn sàng cho P2.4).
- Dùng `fastapi.lifespan` context manager (không phải `on_startup`/`on_shutdown` cũ —
  FastAPI đã deprecate).

### 6. `pyproject.toml` — dev deps + importlinter

- Thêm `fakeredis==2.29.0` (hoặc phiên bản đã verify) vào `dev = [...]`. Pin cứng theo
  kỷ luật comment ở đầu block (KHÔNG `>=`).
- Thêm contract `I1c · agent.assistant_* chỉ luồng A` — như liệt kê ở §Bất biến chạm.

### 7. Tests

Tất cả trong `tests/` (không phải `tests/contract/`) vì primitive Redis không phải DB
gate. Contract tests riêng cho DB.

**`tests/test_redis_client.py`:**
- `test_make_redis_pool_from_url` — fake settings, verify pool builds.
- `test_fail_open_returns_default_on_redis_error` — hàm bọc raise `RedisError` ⇒ decorator
  trả default, log emit WARNING với key `event=redis_fail_open`.
- `test_fail_open_returns_value_when_op_ok` — happy path, decorator không đụng.
- `test_fail_open_does_not_swallow_non_redis_errors` — hàm raise `ValueError` ⇒ decorator
  RE-RAISE (không phải Exception chung).

**`tests/test_assistant_cost.py`** (dùng `fakeredis.aioredis.FakeRedis`):
- `test_record_and_get_daily_tokens` — record 100, record 250 ⇒ get = 350.
- `test_get_daily_tokens_returns_zero_when_key_absent` — chưa có key ⇒ 0.
- `test_key_expires_after_25h` — verify TTL đã set (fakeredis hỗ trợ TTL introspection).
- `test_different_users_isolated` — user A record ≠ user B count.
- `test_fail_open_get_returns_zero_when_redis_down` — mock RedisError ⇒ get trả 0.

**`tests/test_assistant_rate_limit.py`:**
- `test_try_acquire_allows_under_limit` — limit=5, gọi 5 lần ⇒ tất cả True.
- `test_try_acquire_blocks_at_limit` — lần thứ 6 ⇒ False.
- `test_different_users_have_separate_limits` — A và B mỗi người có bucket riêng.
- `test_window_expires_after_60s` — dùng `fakeredis` với time-travel helper.
- `test_fail_open_returns_true_on_redis_down` — D4 tường minh: Redis chớp ⇒ allow.

**`tests/test_main_ohana_ai_lifespan.py`:**
- `test_redis_pool_created_on_startup` — dùng `TestClient` với `app.state.redis_pool`
  assert exists.
- `test_redis_pool_closed_on_shutdown` — track `aclose` gọi (mock pool).

### 8. Docs

**`app/alert_service.py`** — KHÔNG sửa. Comment "when Redis wired, replace" đã đúng
định hướng nhưng scope P2.2 là primitive luồng A, KHÔNG rewire alert_service (đó là scope
alert-service upgrade riêng).

## Chống drift

- **KHÔNG** dùng Redis làm queue (§10 cấm). Chỉ counter + rate-limit key-value.
- **KHÔNG** thêm Redis vào bridge/channels/luồng B — importlinter cưỡng chế.
- **KHÔNG** hardcode `limit_qpm` trong module rate-limit — input từ caller. Không biết tier.
- **KHÔNG** rewrite `alert_service.py` (out of scope).
- **KHÔNG** dùng `on_startup`/`on_shutdown` cũ — lifespan context manager.
- **KHÔNG** tạo persistent volume cho Redis — MVP không cần, mất counter là chấp nhận
  (fail-open cùng bài).
- **KHÔNG** try/except `Exception` — chỉ catch `redis.RedisError`, `ConnectionError`,
  `asyncio.TimeoutError`. Bắt hết = che bug code mình.
- **KHÔNG** dùng nhiều Redis DB index (0..15) tách namespace — anti-pattern. Namespace
  bằng key prefix (`cost:`, `rl:`).

## Verify

```bash
# Bootstrap: Redis lên
docker-compose up -d redis
docker-compose ps redis  # kỳ vọng healthy

# Test cost + rate-limit (fakeredis, không cần Redis thật)
pytest tests/test_assistant_cost.py tests/test_assistant_rate_limit.py \
       tests/test_redis_client.py tests/test_main_ohana_ai_lifespan.py -v

# Lifespan integration (cần Redis thật)
REDIS_URL=redis://localhost:6379/0 \
  pytest tests/test_main_ohana_ai_lifespan.py -v

# Toàn suite (regression)
pytest --ignore=web -q

# Lint + type + import
ruff check . && ruff format --check .
mypy app agent retrieval parsing db bridge tools api auth
lint-imports  # kỳ vọng "5 kept, 0 broken" (4 cũ + I1c mới)
```

**Kỳ vọng:**
- 4 file test mới GREEN
- Full suite 379+ passed không regress (baseline 379 sau OHB-25)
- lint-imports 5 contracts kept (thêm I1c)
- Compose: `docker-compose config` xanh, `docker-compose up -d redis` → healthy

## Rollback

- Compose rollback: `git revert` — Redis service dừng bằng `docker-compose stop redis`.
- Config rollback: `REDIS_URL` không dùng ⇒ ai đọc `Settings.redis_url` mà không lifespan
  ⇒ pool None ⇒ fail-open trả default. Không crash user.
- Không migration ⇒ không rollback DB.

## Ghi chú kiến trúc (không phải việc)

- **P2.4 sẽ sử dụng:** `chat_router` inject `redis: Redis = Depends(get_redis_from_state)`,
  gọi `try_acquire(redis, user_id, limit_qpm=free_tier_qpm)` trước LLM call, gọi
  `record_tokens(redis, user_id, response_tokens)` sau LLM call.
- **D7 sẽ đóng:** `user_id` từ JWT claim; nếu (a) Ohana tự phát ⇒ đã có ở `Identity`; nếu
  (b) ONFA delegated ⇒ port JWT verifier. Phase 2.2 không quan tâm — `user_id` là string
  input.
- **Alert service upgrade** (nếu ưu tiên): re-wire `_provider_429_total` sang Redis (dùng
  `agent/redis_client.py`), 1 PR riêng, giữ chữ ký `record_provider_429`/`provider_429_count`.
