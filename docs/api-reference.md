# API Reference — Ohana BE (Dev)

Tất cả endpoint HTTP hiện có. Base path `/api` (đã prefix bởi `app.include_router`).

**Contract:** `docs/ohana-be-design.md` §6 SQL + I2/I14 grant, `docs/adr-tang2-ohana-ai-assistant.md` D1–D7.
**Live verify (dev):** `POST /api/mock/authorize*` mint cookie → tất cả endpoint dùng cookie đó.

---

## 1 · Auth model — 2 identity, 1 cookie

Cookie **`ohana_session`** (httpOnly, samesite=lax) chứa JWT HS256. Payload xác định route được gọi:

| Route target | Verify bởi | Payload claims required |
|---|---|---|
| Tầng 3 seller (`/inbox`, `/chat`, `/admin`) | `auth.identity.identity_from_cookie` | `{sub, shop_id, role: "seller"\|"admin"}` |
| Tầng 2 assistant (`/assistant/*`) | `auth.user_identity.user_identity_from_cookie` | `{sub, role: "user", tier: "free"\|"pro"}` |

**Sai role ⇒ 401** cùng shape (`invalid_session_cookie`) — không leak "endpoint tồn tại nhưng bạn sai role".

**CSRF double-submit:** POST/PATCH/DELETE cần header `X-CSRF-Token` = cookie `ohana_csrf` (non-httpOnly, JS đọc được).

---

## 2 · Dev auth (mock)

Gated bằng `OHANA_ENV=dev`. Ngoài dev ⇒ **404** (fail-close, không 403 để không leak route tồn tại).

### `POST /api/mock/authorize?role=seller|admin`

Mint seller/admin token + set 2 cookie. Seed fixture shop (best-effort dưới role hiện tại).

```bash
curl -X POST 'http://localhost:8001/api/mock/authorize?role=seller' -c cookies.txt
```

```json
{"oa_id": "fixture-oa-001", "shop_id": "fixture-shop-001", "role": "seller"}
```

- `role` in `{seller, admin}` — mặc định `seller`. Khác ⇒ 422 `invalid_role`.
- Fixture user_id server-side = `dev-user-001`.

### `POST /api/mock/authorize_user?tier=free|pro`

**P2.4a** — mint user Tầng 2 token (`role="user"` + `tier` claim).

```bash
curl -X POST 'http://localhost:8001/api/mock/authorize_user?tier=free' -c cookies.txt
```

```json
{"user_id": "dev-user-t2-001", "tier": "free", "role": "user"}
```

- `tier` in `{free, pro}` — mặc định `free`. Khác ⇒ 422 `invalid_tier`.
- KHÔNG seed shop (Tầng 2 per-user, không có shop concept).

---

## 3 · Tầng 3 · Seller (luồng B webhook + inbox)

### `GET /api/inbox`

List draft đang chờ seller duyệt. Scoped by `shop_id` trong JWT.

```bash
curl -b cookies.txt http://localhost:8001/api/inbox
```

```json
[
  {
    "reply_id": "pr_abc123",
    "conversation_id": "conv_1",
    "customer_id": "Khách An",
    "draft_text": "Dạ shop cảm ơn anh...",
    "intent": "order_question",
    "confidence": 0.87,
    "status": "pending",
    "escalation_reasons": []
  }
]
```

- **401** — cookie thiếu/hỏng/sai role.
- Order: oldest first (server-side, không tự sort ở client).
- `escalation_reasons` server đã sort theo `SEVERITY_RANK` (agent/policy_gate.py).

### `POST /api/inbox/{reply_id}/approve` · `POST /api/inbox/{reply_id}/reject`

Flip status draft. **KHÔNG gửi tin cho khách** (PRE-004 — send-on-approve worker chưa ship).

```bash
curl -X POST -b cookies.txt \
  -H "X-CSRF-Token: $(grep ohana_csrf cookies.txt | awk '{print $7}')" \
  http://localhost:8001/api/inbox/pr_abc123/approve
```

```json
{"status": "approved"}
```

- **404** `reply_not_found_or_already_decided` — cross-shop / đã decided / không tồn tại (cùng shape, không leak).

### `POST /api/chat`

Seller ↔ AI general (Tầng 3 luồng A). Không grounding, không RAG.

```bash
curl -X POST -b cookies.txt \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"message": "Cách trả lời khách hỏi ship?"}' \
  http://localhost:8001/api/chat
```

```json
{
  "reply": "Bạn có thể trả lời theo mẫu...",
  "model": "claude-sonnet-4-5",
  "grounded": false,
  "usage": {"prompt_tokens": 320, "completion_tokens": 145, "total_tokens": 465}
}
```

- `message`: 1–4000 ký tự.
- `extra="ignore"`: gửi `shop_id` trong body sẽ bị **bỏ qua** (scope từ JWT only).
- **Cold start ~25s** ở request đầu (spec 07 §14).
- **502** khi model fail (`empty_reply` / provider error).

### `POST /webhook/{channel}/{external_id}`

**KHÔNG mount ở `main.py` dev combined** — mở đường customer-inbound cần Zalo signature verify + creds (PRE-004 blocked). Chỉ mount khi ship real webhook.

---

## 4 · Tầng 3 · Admin

Gated `require_admin` (JWT `role=admin`). Non-admin ⇒ **403**.

### `POST /api/admin/wiki/ingest`

Nạp text vào wiki dùng chung. Body backend hardcode `shop_id=PLATFORM_SHOP_ID` (namespace shared).

```bash
curl -X POST -b cookies.txt \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"text": "Chính sách...", "source_ref": "policy-v1"}' \
  http://localhost:8001/api/admin/wiki/ingest
```

```json
{"success": true, "chunks": 4}
```

- `text`: min 1 char (client SPA min 100).
- `source_ref`: required.
- `shop_id`: optional, default `PLATFORM_SHOP_ID` — client SPA KHÔNG gửi.

### `POST /api/admin/shops` → 201

Tạo tenant thật. **`shop_id` server mint** (uuid4) — client KHÔNG được đề xuất.

```bash
curl -X POST -b cookies.txt -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"name": "Shop Áo Thun Sài Gòn"}' \
  http://localhost:8001/api/admin/shops
```

```json
{"shop_id": "8f2a1b7c-...", "name": "Shop Áo Thun Sài Gòn"}
```

- `name`: 1–200 ký tự.
- `extra="ignore"`: gửi `id`/`shop_id`/`status` bị bỏ qua (chống chọn danh tính tenant).

---

## 5 · Tầng 2 · Ohana AI Assistant (per-user)

Cookie phải là `role="user"` (mint qua `POST /api/mock/authorize_user`). Seller/admin cookie ⇒ **401**.

### `POST /api/assistant/chat`

Chat 1 lượt, tự persist vào `assistant.messages`. Response luôn có `conversation_id` (server tạo mới nếu null).

```bash
curl -X POST -b cookies.txt \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"message": "Chào bạn", "conversation_id": null}' \
  http://localhost:8001/api/assistant/chat
```

```json
{
  "reply": "Chào bạn! Tôi sẵn sàng giúp đỡ...",
  "model": "claude-sonnet-4-5",
  "grounded": false,
  "usage": {"prompt_tokens": 245, "completion_tokens": 82, "total_tokens": 327},
  "tier": "free",
  "daily_tokens_used": 327,
  "conversation_id": 42
}
```

- `message`: 1–4000 ký tự.
- `conversation_id`: `null` ⇒ auto-create (title = `message[:40].strip()` hoặc null nếu empty sau trim). Non-null ⇒ ownership check → **404** nếu cross-user hoặc không tồn tại.
- **401** cookie sai. **429** đạt daily token limit (tier gate). **502** LLM fail — KHÔNG persist message (transaction chưa run).
- Memory recall (P2.3) chạy trước LLM step; auto-save memory sau reply.
- Tracing: `user_id` + `session_id = conversation_id` push vào Langfuse trace.

### `GET /api/assistant/conversations?limit=20&before=<iso>`

List conversations user, DESC by `updated_at`. Cursor pagination.

```bash
curl -b cookies.txt 'http://localhost:8001/api/assistant/conversations?limit=20'
```

```json
{
  "items": [
    {
      "conversation_id": 42,
      "title": "Chào bạn",
      "created_at": "2026-08-01T14:30:00Z",
      "updated_at": "2026-08-01T14:32:15Z"
    }
  ],
  "next_cursor": null
}
```

- `limit`: 1–100, mặc định 20.
- `before`: cursor ISO datetime (= `updated_at` item cuối page trước). Omit ⇒ page đầu.
- `next_cursor: null` ⇒ hết trang.

### `POST /api/assistant/conversations` → 201

Tạo conversation trống (không auto-title). Chủ yếu cho "New chat" flow — thường dùng `POST /assistant/chat` với `conversation_id=null` (auto-create + auto-title trong 1 request).

```bash
curl -X POST -b cookies.txt \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"title": null}' \
  http://localhost:8001/api/assistant/conversations
```

```json
{"conversation_id": 43, "title": null, "created_at": "...", "updated_at": "..."}
```

- `title`: optional, ≤ 200 ký tự.

### `GET /api/assistant/conversations/{id}`

Fetch 1 conversation. Cross-user / soft-deleted ⇒ **404** `conversation_not_found`.

### `PATCH /api/assistant/conversations/{id}`

Sửa title (endpoint duy nhất cho phép sửa ở P2.4c).

```bash
curl -X PATCH -b cookies.txt -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"title": "Nghiên cứu ship"}' \
  http://localhost:8001/api/assistant/conversations/42
```

```json
{"conversation_id": 42, "title": "Nghiên cứu ship", "created_at": "...", "updated_at": "..."}
```

- `title`: 1–200 ký tự (required, khác POST optional).
- **404** cross-user / không tồn tại.

### `DELETE /api/assistant/conversations/{id}` → 204

Soft-delete. Idempotent trong scope user (cùng user gọi lại → 404 vì đã invisible).

```bash
curl -X DELETE -b cookies.txt \
  -H "X-CSRF-Token: $CSRF" \
  http://localhost:8001/api/assistant/conversations/42
```

### `GET /api/assistant/conversations/{id}/messages?limit=50&before=<iso>`

**P2.4d.** List messages 1 conversation, ASC by `(created_at, message_id)` — tie-breaker `message_id` là chủ đích (user+assistant trong 1 transaction có cùng `created_at`, sort chỉ theo created_at ⇒ thứ tự đảo ngẫu nhiên).

```bash
curl -b cookies.txt 'http://localhost:8001/api/assistant/conversations/42/messages?limit=50'
```

```json
{
  "items": [
    {"message_id": 101, "role": "user", "content": "Chào bạn", "created_at": "..."},
    {"message_id": 102, "role": "assistant", "content": "Chào bạn! Tôi...", "created_at": "..."}
  ],
  "next_cursor": null
}
```

- `role` in `{user, assistant, system}` — string (không Literal) để ENUM `msg_role` mở rộng không break Pydantic.
- **404** cross-user / conversation không tồn tại (ownership check trước list).

### `GET /api/assistant/memories?limit=20&before=<iso>`

List memory items của user (auto-extracted từ chat). DESC by `created_at`.

```bash
curl -b cookies.txt 'http://localhost:8001/api/assistant/memories?limit=20'
```

```json
{
  "items": [
    {"memory_id": 55, "content": "User quan tâm shipping thời gian 2-3 ngày", "created_at": "..."}
  ],
  "next_cursor": null
}
```

### `DELETE /api/assistant/memories/{id}` → 204

Hard delete (khác conversations soft-delete). Cross-user ⇒ **404**.

---

## 6 · Health

### `GET /health`

Không auth. Trả `{"status": "ok"}` khi process alive. Dùng cho container healthcheck + external monitor.

---

## 7 · Error shape chung

| Status | Ý nghĩa | Body |
|---|---|---|
| **200** | OK | Response schema |
| **201** | Created | Response schema |
| **204** | No content | (empty) |
| **401** | Cookie thiếu/invalid/sai role | `{"detail": "invalid_session_cookie"}` |
| **403** | Sai permission (admin-only endpoint gọi bằng seller) | `{"detail": "..."}` |
| **404** | Resource không tồn tại HOẶC cross-tenant (không leak) | `{"detail": "conversation_not_found"}` |
| **422** | Body/query invalid (Pydantic validation) | `{"detail": [{"loc": [...], "msg": "..."}]}` |
| **429** | Rate limit / cost cap tier gate | `{"detail": "daily_token_limit_reached"}` |
| **502** | LLM/upstream provider fail | `{"detail": "empty_reply"}` |

**Cross-tenant policy:** endpoint đọc/sửa resource của user khác ⇒ **404 luôn** (không 403). Lý do: 403 leak "resource tồn tại nhưng bạn không có quyền" — attacker enumerate được.

---

## 8 · Rate limit / cost

Tầng 2 có tier gate (P2.2 · P2.3):

| Tier | Daily token limit | Rate limit |
|---|---|---|
| `free` | Cấu hình `TIER_LIMITS['free']` (agent/assistant_tier.py) | Redis token bucket per-user |
| `pro` | `TIER_LIMITS['pro']` | Cao hơn free |

- Đạt limit ⇒ **429** `daily_token_limit_reached`. Client hiển thị "Đã đạt giới hạn hôm nay. Nâng cấp gói."
- Redis chớp ⇒ **D4 fail-open**: allow request (mất gate tạm, chấp nhận).

Tầng 3 chưa có rate limit riêng — chỉ Anthropic upstream giới hạn.

---

## 9 · Base URL

| Env | URL | Backend process |
|---|---|---|
| Dev combined | `http://localhost:8001` | `app.main:app` (mount cả A + B, KHÔNG có I1/I2) |
| Dev split A | `http://localhost:8001` | `app.main_ohana_ai:app` (chỉ Tầng 2 + Tầng 3 chat) |
| Dev split B | `http://localhost:8002` | `app.main_seller:app` (chỉ inbox + admin + webhook) |
| Prod | `https://api.ohana.example` | nginx → 8001/8002 theo route rule (deploy/nginx.conf) |

Vite dev proxy `/api → 127.0.0.1:8001` (web/vite.config.ts) — FE luôn gọi relative `/api/*`.

---

## 10 · Chưa có (backlog)

- **Streaming SSE** — `/assistant/chat` hiện non-stream. Nếu ship, persist ở `StreamDone` (P2.4d note).
- **Bulk delete messages** — chưa endpoint. Xoá conversation ⇒ soft-delete parent (messages vẫn nằm, ẩn qua ownership).
- **Search conversations** — chưa endpoint (F2+).
- **Feedback / rating** — `POST /assistant/feedback` cho Langfuse Scores tab (backlog).
- **Send-on-approve worker** — `POST /inbox/{id}/approve` KHÔNG gửi tin cho khách (PRE-004).
- **Real Zalo OAuth** — `mock_authorize*` là dev only, spec 05+ ship real login flow.
