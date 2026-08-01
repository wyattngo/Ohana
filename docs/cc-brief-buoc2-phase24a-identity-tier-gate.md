# CC Brief · Bước 2 · Phase 2.4a — User identity + tier gate primitive

**Frozen** · form `ohana-be-coder` · PR đầu trong 3 (Wyatt 2026-08-01 pick split).

## Bối cảnh & phạm vi

D7 ratified (PR #11, `b5daca5`): Ohana tự phát JWT cho user Tầng 2. P2.4a ship
**identity + tier gate primitive** — chưa consume ở router (P2.4b).

**Trong scope P2.4a:**
- `auth/user_identity.py` — `UserIdentity(user_id, tier)` dc + `verify_user_token` +
  `user_identity_from_cookie` dep. Tách khỏi `Identity` (Tầng 3).
- `agent/assistant_tier.py` — bảng static `TIER_LIMITS` + `check_and_reserve(redis,
  user_identity) -> TierVerdict` gate function. Consume `assistant_rate_limit.
  try_acquire` + `assistant_cost.get_daily_tokens` (P2.2 primitives).
- `api/mock_auth.py` — thêm route `POST /mock/authorize_user` (dev-only) mint user
  token. Tách khỏi `/mock/authorize` (seller). Same `_is_dev_env` gate.
- Tests: verify_user_token happy/reject, dep 401, tier gate allow/deny cả rate và cost.

**KHÔNG scope (P2.4b/c):**
- Chat router consume (`/api/assistant/chat` — P2.4b).
- Conversations CRUD, memory endpoints (P2.4c).
- Wire tier gate vào router (P2.4b).
- Refresh token rotation (bổ sung khi cần; MVP giữ 24h max_age như seller).
- Real login flow (P4+; MVP dùng mock endpoint dev-only).
- Billing `upgrade(user_id)` hook (D6 delegated).

## Hồ sơ ADR (đọc trước khi code)

`docs/adr-tang2-ohana-ai-assistant.md` §2 D7 (ratified 2026-08-01):
> Token shape mới cho user Tầng 2: `{sub: user_id, role: "user", tier: "free"|"pro"}` —
> KHÔNG có `shop_id`. `UserIdentity(user_id, tier)` dc tách khỏi `Identity`.
> `verify_user_token` riêng, KHÔNG chia SQL WHERE với `verify_token`. Same secret +
> allowed algo, khác payload validator.

`auth/identity.py` (Tầng 3 pattern để mimic):
- `verify_token(token, secret) -> Identity` — HS256, verify + project payload.
- `identity_from_cookie(request) -> Identity` — cookie flow.
- `build_active_shop_dep(session_factory)` — bonus tra shop_id vs bảng `shops`.
- `_ALLOWED_ALGOS = ["HS256"]` — pinned literal, KHÔNG đọc từ config (alg confusion bypass).
- Cookie `SESSION_COOKIE_NAME = "ohana_session"`.
- `get_jwt_secret()` — fresh `Settings()` per call (không cache lru).

## Bất biến chạm

- **U-scope invariant** (analog Tầng 3 shop_id scope) — `UserIdentity.user_id` LÀ nguồn
  scope duy nhất. Không bao giờ đọc `user_id` từ body/header/query — luôn từ verified
  JWT claim. Cùng bài `Identity.shop_id`.
- **Algorithm pinning** — `_ALLOWED_ALGOS = ["HS256"]` pinned literal (KHÔNG đọc env).
  Cùng lý do `auth/identity.py`: đọc từ config = classic alg-confusion bypass (attacker
  gửi `alg: none` hoặc `alg: RS256` với secret HS256).
- **No default fall-through** — token thiếu `sub`/`role`/`tier` ⇒ raise ValueError. Không
  im lặng dùng default cho identity fields (attacker forge = accept as valid).
- **Tier enum whitelist** — `tier ∈ {"free", "pro"}` chỉ. Value khác (typo, injected)
  ⇒ raise. Không có "default tier" (đó là leak: forget claim = pro miễn phí).
- **I1c** (P2.2) đã cover `agent.assistant_*` — module mới `agent.assistant_tier` tự
  động thuộc luồng A. `auth/user_identity.py` KHÔNG cần contract mới: `auth/` đã ở
  source_modules của I1/I1c gián tiếp qua `api/chat` chain.
- **Fail-open D4** — tier gate consume `assistant_rate_limit.try_acquire` (fail-open
  True) và `assistant_cost.get_daily_tokens` (fail-open 0). Redis chớp ⇒ allow. Cùng
  spec D4, không override.

## Việc — thứ tự thi công

### 1. `auth/user_identity.py`

```python
@dataclass(frozen=True)
class UserIdentity:
    """Verified user identity cho Tầng 2 (D7 · Ohana tự phát JWT).

    TÁCH khỏi `Identity` (Tầng 3, có `shop_id`) — Tầng 2 per-user, không có shop
    concept. Trộn hai type = drift; wire nhầm route Tầng 3 với `UserIdentity` ⇒
    mypy đỏ."""

    user_id: str
    tier: str  # "free" | "pro" — verify enforce enum


_ALLOWED_TIERS = frozenset({"free", "pro"})
_USER_ROLE = "user"


def verify_user_token(token: str, *, secret: str) -> UserIdentity:
    """Same HS256 secret + allowed algo với `verify_token`; khác payload validator:
    - `role` PHẢI == "user" (KHÔNG seller/admin nào lọt vào route Tầng 2).
    - `sub` (user_id) required.
    - `tier` required, PHẢI ∈ {"free", "pro"}.
    - KHÔNG kiểm `shop_id` (Tầng 2 không cần).
    """
    claims = jwt.decode(token, secret, algorithms=_ALLOWED_ALGOS)
    sub = claims.get("sub")
    if not isinstance(sub, str) or not sub:
        raise ValueError("invalid_sub — token missing 'sub' claim")
    role = claims.get("role")
    if role != _USER_ROLE:
        raise ValueError(f"invalid_role — expected 'user', got {role!r}")
    tier = claims.get("tier")
    if not isinstance(tier, str) or tier not in _ALLOWED_TIERS:
        raise ValueError(f"invalid_tier — must be 'free' or 'pro', got {tier!r}")
    return UserIdentity(user_id=sub, tier=tier)


def user_identity_from_cookie(request: Request) -> UserIdentity:
    """FastAPI dep — cùng cookie `ohana_session` với seller flow (chia sẻ transport).
    Route Tầng 2 chỉ chấp nhận token role `user`; seller/admin token → 401 cùng
    shape với missing/invalid (no leak về ai đang login)."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="missing_session_cookie")
    try:
        return verify_user_token(token, secret=get_jwt_secret())
    except (jwt.InvalidTokenError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="invalid_session_cookie") from exc
```

### 2. `agent/assistant_tier.py`

```python
@dataclass(frozen=True)
class TierLimit:
    qpm: int          # requests per minute
    daily_tokens: int  # total token cap per day


TIER_LIMITS: dict[str, TierLimit] = {
    "free": TierLimit(qpm=10,  daily_tokens=50_000),
    "pro":  TierLimit(qpm=60,  daily_tokens=500_000),
}


@dataclass(frozen=True)
class TierVerdict:
    """Kết quả gate. `allowed=False` KÈM lý do để router trả 429 có structure."""
    allowed: bool
    reason: str  # "" | "rate_limit_exceeded" | "daily_cost_cap_exceeded" | "unknown_tier"
    daily_tokens_used: int


async def check_and_reserve(
    redis: Redis, user_identity: UserIdentity
) -> TierVerdict:
    """Gate trước LLM call: consume rate-limit (INCR) + kiểm cost cap (GET).

    Thứ tự CỐ Ý:
    1. Rate-limit check FIRST (INCR). Nếu deny, không tốn 1 GET cost.
    2. Cost cap check SECOND (GET). Read-only, không side-effect.

    Fail-open D4 tự nhiên propagate: `try_acquire` fail-open True → tính là "allow rate";
    `get_daily_tokens` fail-open 0 → tính là "chưa tiêu" → allow cost. Redis down ⇒
    verdict `allowed=True, daily_tokens_used=0`. Không có branch fail-closed.

    KHÔNG record token ở đây — record là việc SAU LLM call (biết real usage). Đây chỉ
    check + reserve (rate slot).
    """
    limit = TIER_LIMITS.get(user_identity.tier)
    if limit is None:
        # Tier không trong whitelist ở verify — không thể xảy ra bình thường; guard belt-
        # and-braces cho case verify + gate lệch (refactor sau lỡ mở tier mới).
        return TierVerdict(allowed=False, reason="unknown_tier", daily_tokens_used=0)

    rate_ok = await try_acquire(redis, user_identity.user_id, limit_qpm=limit.qpm)
    if not rate_ok:
        return TierVerdict(
            allowed=False, reason="rate_limit_exceeded", daily_tokens_used=0
        )

    used = await get_daily_tokens(redis, user_identity.user_id)
    if used >= limit.daily_tokens:
        return TierVerdict(
            allowed=False, reason="daily_cost_cap_exceeded", daily_tokens_used=used
        )

    return TierVerdict(allowed=True, reason="", daily_tokens_used=used)
```

### 3. `api/mock_auth.py` — thêm route `/mock/authorize_user`

Nối tiếp `_ensure_fixture_shop`/`mock_authorize` pattern: same `_is_dev_env` gate,
same cookie, KHÔNG seed shop (user Tầng 2 không có shop). Sub-fixture `dev-user-002`
(khác `dev-user-001` để tránh clash nếu cùng session).

```python
_FIXTURE_TIER_2_USER_ID = "dev-user-t2-001"
_ALLOWED_TIERS_MOCK = ("free", "pro")

@router.post("/authorize_user")
async def mock_authorize_user(response: Response, tier: str = "free") -> dict[str, str]:
    if not _is_dev_env():
        raise HTTPException(status_code=404)
    if tier not in _ALLOWED_TIERS_MOCK:
        raise HTTPException(status_code=422, detail="invalid_tier")
    token = jwt.encode(
        {"sub": _FIXTURE_TIER_2_USER_ID, "role": "user", "tier": tier},
        get_jwt_secret(),
        algorithm="HS256",
    )
    response.set_cookie(
        key=SESSION_COOKIE_NAME, value=token,
        httponly=True, secure=False, samesite="lax",
        max_age=_SESSION_MAX_AGE_SECONDS,
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME, value=secrets.token_urlsafe(32),
        httponly=False, secure=False, samesite="lax",
        max_age=_SESSION_MAX_AGE_SECONDS,
    )
    return {"user_id": _FIXTURE_TIER_2_USER_ID, "tier": tier, "role": "user"}
```

### 4. `tests/test_user_identity.py`

- `test_verify_user_token_happy` — HS256 sign + verify → UserIdentity match.
- `test_verify_rejects_wrong_role` — role="seller" ⇒ ValueError.
- `test_verify_rejects_missing_sub` — no sub ⇒ ValueError.
- `test_verify_rejects_missing_tier` — no tier ⇒ ValueError.
- `test_verify_rejects_invalid_tier` — tier="premium" (không whitelist) ⇒ ValueError.
- `test_verify_rejects_wrong_algorithm` — token ký RS256 với secret khác ⇒
  InvalidTokenError (pin HS256 giữ).
- `test_verify_rejects_tampered_signature` — sửa payload sau khi ký ⇒ InvalidSignatureError.
- `test_user_identity_from_cookie_401_when_missing` — no cookie ⇒ 401.
- `test_user_identity_from_cookie_401_when_seller_token` — seller token trong cookie
  ⇒ 401 (không nhận nhầm identity).
- `test_user_identity_from_cookie_happy` — valid user token trong cookie ⇒ UserIdentity.

### 5. `tests/test_assistant_tier.py`

Dùng `fakeredis.aioredis.FakeRedis` như P2.2.

- `test_check_and_reserve_allows_when_under_limits` — free tier, count 0, get 0 ⇒ allow.
- `test_check_and_reserve_denies_rate_limit_first` — call gate 11 lần free (qpm=10) →
  lần 11 deny reason="rate_limit_exceeded".
- `test_check_and_reserve_denies_daily_cost_cap` — free (cap 50k), pre-set counter =
  50k → deny reason="daily_cost_cap_exceeded".
- `test_check_and_reserve_rate_checked_before_cost` — verify order: khi cả hai vượt,
  reason phải là "rate_limit_exceeded" (INCR trước GET, hiển thị lý do đầu tiên).
- `test_pro_tier_has_higher_limits` — pro qpm=60, free qpm=10; call 20 lần pro → allow;
  cùng 20 lần free → deny từ lần 11.
- `test_unknown_tier_returns_denied` — tier "unknown" (không qua verify — direct call)
  → deny reason="unknown_tier".
- `test_fail_open_allows_when_redis_down` — mock redis raise → gate trả allow (D4
  propagate qua try_acquire+get_daily_tokens fail-open).
- `test_daily_tokens_used_echoed_in_verdict` — trước gate: set counter=1000 → verdict
  `daily_tokens_used=1000` cho router log/response.

### 6. `tests/test_mock_auth.py` — extend cho `/mock/authorize_user`

- `test_authorize_user_404_outside_dev` — OHANA_ENV unset ⇒ 404 (fail-closed).
- `test_authorize_user_422_invalid_tier` — tier="premium" ⇒ 422.
- `test_authorize_user_mints_valid_token` — dev + tier=free ⇒ 200, cookie set, token
  verify được với `verify_user_token`.

## Chống drift

- **KHÔNG** đọc `user_id` từ body/header/query — luôn từ verified JWT claim ở
  `UserIdentity`. Cùng bài `Identity.shop_id`.
- **KHÔNG** default tier — thiếu claim ⇒ raise, không âm thầm assign "free".
- **KHÔNG** cho tier ngoài whitelist — thêm tier mới phải sửa `_ALLOWED_TIERS` +
  `TIER_LIMITS` cùng lúc (2 chỗ, chống drift 1 chỗ).
- **KHÔNG** dùng chung `Identity` cho Tầng 2 — hai type khác nhau, mypy giữ tường lửa.
- **KHÔNG** ghi tier vào DB (đó là claim, không phải state). Nâng gói ⇒ mint token mới.
- **KHÔNG** đọc alg từ config — pin literal HS256.
- **KHÔNG** wire gate vào router (đó là P2.4b). P2.4a chỉ ship primitive.
- **KHÔNG** đo cost trước khi consume rate (nếu cost fail thì rate slot đã INCR — sai
  thứ tự user experience khi cost hard cap gần biên). Thứ tự: rate FIRST, cost SECOND.

## Verify

```bash
set -a && source .env && set +a
export DATABASE_URL="postgresql+psycopg://ohana:${POSTGRES_PW}@localhost:5433/ohana_test"

pytest tests/test_user_identity.py tests/test_assistant_tier.py -v
pytest --ignore=web -q  # kỳ vọng 412 + new = ~430 passed

ruff check . && ruff format --check .
mypy app agent retrieval parsing db bridge tools api auth
lint-imports  # 5 kept, 0 broken (I1c cover assistant_tier tự động)
```

**Kỳ vọng:**
- ≥18 test mới GREEN (identity 10 + tier gate 8 + mock_auth 3).
- Full suite không regress.
- lint-imports 5 kept (không cần contract mới).

## Rollback

Test + module mới ⇒ `git revert`. Không migration, không grant, không lifespan wire.
`api/mock_auth.py` extension idempotent — không phá route `/mock/authorize` cũ.

## Ghi chú P2.4b (không phải việc)

- Chat router `/api/assistant/chat` inject `user_identity_from_cookie` + `check_and_
  reserve(redis, user_identity)` trước LLM call.
- Sau LLM call: `record_tokens(redis, user_id, response_tokens)` (không blocking; fail-
  open no-op).
- Wire memory `AssistantMemory(user_scope=user_id).recall_text(query, k=5)` cho context
  augmentation.
- Response body kèm `daily_tokens_used` (từ TierVerdict) cho UI hiển thị quota.
