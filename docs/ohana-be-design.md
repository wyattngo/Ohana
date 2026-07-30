---
doc: ohana-be-design
status: authoritative
db: postgresql-16
extensions: [vector]
llm_generation: anthropic (claude)
llm_embedding: together · intfloat/multilingual-e5-large-instruct · 1024d · max 512 tok
auth: ohana tự phát · JWT 15m + refresh xoay vòng 30d
companion: backend-workflow.md   # quyết CÁI GÌ và VÌ SAO; doc này quyết CHẠY BẰNG GÌ
read_before:
  - any change under agent/, api/, channels/, db/, retrieval/, parsing/
  - any Alembic revision
---

# Ohana BE — Design Standard

## §0 · Cách dùng doc này

Doc này là **hợp đồng**, không phải gợi ý. Quy ước đọc:

| Ký hiệu | Nghĩa |
|---|---|
| **MUST** | Vi phạm = build đỏ hoặc permission denied. Không có ngoại lệ. |
| **MUST NOT** | Như trên, chiều ngược lại. |
| **SHOULD** | Lệch được, nhưng phải ghi lý do vào `WORK.md`. |
| `→ w§x` | Tham chiếu `backend-workflow.md` mục x. |
| `→ §x` | Tham chiếu trong chính doc này. |

**Quy tắc số 1:** nếu doc này và code mâu thuẫn — code sai. Sửa code, không sửa doc.
**Quy tắc số 2:** nếu doc này và `backend-workflow.md` mâu thuẫn — dừng, hỏi người. Không tự chọn.

**Tên bảng ở §5 là trạng thái đích.** Codebase hiện dùng tên phẳng trong schema `public`
(`pending_reply`, `webhook_event_log`, `shops`…). Ánh xạ đích ↔ hiện tại: `adopt-plan.md` §1.
Chỉ `embeddings` → `platform.corpus` được di cư ở A1; phần còn lại đổi tên khi có lý do
độc lập, không đổi chỉ để khớp doc.

---

## §1 · Bất biến

Mười bốn điều dưới đây không thương lượng. Mỗi điều có **cơ chế cưỡng chế** — không dựa vào trí nhớ.

| # | Bất biến | Cưỡng chế bởi | Nguồn |
|---|---|---|---|
| I1 | Luồng A và luồng B **MUST NOT** dùng chung process | 3 process riêng §3 + `importlinter` contract | w§5 |
| I2 | `svc_ohana_ai` **MUST NOT** đọc được dữ liệu shop | Postgres role + `ALTER DEFAULT PRIVILEGES` §4 | w§5 |
| I3 | Mọi payload lên LLM **MUST** qua PII filter | type `Scrubbed`, mypy §8.3 | w§5 |
| I4 | Mọi user content **MUST** được wrap chống injection | type `Wrapped`, mypy §8.3 | w§5 |
| I5 | SDK provider **MUST** chỉ được import trong `agent/providers/` | `importlinter` contract §2 | w§9.2 |
| I6 | Hàm service **MUST NOT** nhận `shop_id: int`; chỉ nhận `ShopContext` | type `ShopContext`, mypy §8.4 | w§5 |
| I7 | `webhook_seen` + `outbox` **MUST** ghi trong **một** câu lệnh | CTE §6.1 | w§2.1 |
| I8 | Cost reserve **MUST** atomic, điều kiện trong `WHERE` | §6.5 | w§5 |
| I9 | Draft **MUST** giữ snapshot tầng 1 + `persona_id` tại T0 | NOT NULL §5.5 | w§2.3 |
| I10 | Phase 1 **MUST NOT** có nhánh tự động gửi | không tồn tại code path | w§2.4 |
| I11 | Embed **MUST** đúng prefix `query:` / `passage:` | router chỉ expose 2 hàm riêng §8.2 | O3 |
| I12 | Window FB/IG **MUST** tính tại chỗ, **MUST NOT** gọi API | §6.8 + §10 | O4 |
| I13 | Mọi **claim MUST có timeout**; reaper phải gỡ được | §3 reaper + §6.9 §6.10 | §6.9 §6.10 |
| I14 | Bảng mới trong schema **MUST** được phủ bởi default privileges | §4 + test §9 | §4 |

---

## §2 · Bố cục repo

```
ohana-ai/
├── api/
│   ├── chat.py              # luồng A — POST /v1/chat
│   ├── webhook.py           # luồng B — POST /v1/webhook/{channel}
│   ├── inbox.py             # luồng B — inbox + draft actions
│   └── admin.py
├── agent/
│   ├── pii.py               # PII redactor → trả Scrubbed
│   ├── types.py             # Scrubbed, Wrapped
│   ├── policy_gate.py       # precedence → escalation_reasons
│   ├── drafter.py
│   ├── orchestrator.py
│   ├── llm_client.py        # nhận Scrubbed
│   └── providers/           # NƠI DUY NHẤT import SDK provider
├── auth/
│   ├── identity.py
│   └── context.py           # ShopContext
├── channels/
│   ├── base.py
│   └── zalo/                # signature, envelope
├── db/
│   ├── models.py
│   ├── repos.py
│   └── migrations/versions/ # Alembic
├── retrieval/               # pgvector — luồng A
├── parsing/                 # chunk, ingest — luồng A
├── tools/                   # shop_kb — tầng 2
├── bridge/                  # zalo_sender, ohana_client
├── app/
│   ├── main_ohana_ai.py     # entrypoint luồng A
│   ├── main_seller.py       # entrypoint luồng B
│   └── worker_seller.py     # 3 loop
├── scripts/bootstrap_roles.py
└── tests/contract/          # PRE-010 C1–C5, I14
```

**Contract bắt buộc** (`pyproject.toml`):

```ini
[[tool.importlinter.contracts]]
name = "I1 · luồng A không thấy luồng B"
type = "forbidden"
source_modules = ["api.chat", "retrieval", "parsing"]
forbidden_modules = ["api.webhook", "api.inbox", "agent.drafter", "channels", "bridge"]

[[tool.importlinter.contracts]]
name = "I5 · SDK provider chỉ trong agent.providers"
type = "forbidden"
source_modules = ["api", "app", "channels", "db", "retrieval", "parsing", "tools",
                  "agent.drafter", "agent.pii", "agent.policy_gate"]
forbidden_modules = ["openai", "anthropic", "together", "langfuse"]
```

I1 được cưỡng chế bằng contract + tách process ở §3, không phải bằng tên thư mục.

## §3 · Topology

| Process | Nội dung | Scale |
|---|---|---|
| `main_ohana_ai` | FastAPI · `/v1/chat` · đồng bộ · SLA ≤3s p95 · DB role `svc_ohana_ai` | N replica |
| `main_seller` | FastAPI · `/v1/webhook/*` + `/v1/shops/*/inbox` + `/v1/drafts/*` · DB role `svc_seller` | N replica |
| `worker_seller` | 3 async loop trong 1 process · DB role `svc_seller` | N replica, claim atomic |

Ba vòng lặp của `worker-seller`:

| Loop | Chu kỳ | Việc |
|---|---|---|
| `outbox` | 200ms | claim `SKIP LOCKED` → ghi message → set debounce |
| `debounce` | 500ms | claim conversation đến hạn → compose draft |
| `reaper` | 10s | 4 việc, xem dưới |

Reaper **MUST** làm đủ bốn (I13 — mọi claim phải gỡ được):

| # | Việc | Câu lệnh |
|---|---|---|
| R1 | draft hết TTL → `expired` | — |
| R2 | outbox kẹt `processing` >5' → `pending` | — |
| R3 | **conversation kẹt `debounce_claimed_at` >5' → NULL** | §6.9 |
| R4 | **reservation chưa release >5' → release** | §6.10 |

R3 và R4 là cùng một họ lỗi: *claim không có timeout*. Thiếu R3 ⇒ conversation im lặng vĩnh viễn — đúng failure mode mà `w§2.2` dựng persistent timer để tránh.

**MUST NOT** gộp hai entrypoint vào một process dù chỉ để tiện dev (I1). Tách process là chỗ I2 thành thật — mỗi process nối bằng `DATABASE_URL` mang role khác nhau.

---

## §4 · Schema & role — cưỡng chế I2

```sql
CREATE SCHEMA core;        -- shop, persona, config, binding, account
CREATE SCHEMA seller;      -- conversation, message, draft, outbox, cost
CREATE SCHEMA platform;    -- corpus luồng A

CREATE ROLE svc_ohana_ai LOGIN;
CREATE ROLE svc_seller   LOGIN;

-- luồng A: CHỈ đọc corpus. Không chạm core, không chạm seller.
GRANT USAGE ON SCHEMA platform TO svc_ohana_ai;
GRANT SELECT ON ALL TABLES IN SCHEMA platform TO svc_ohana_ai;

-- luồng B: full seller, đọc-ghi core, KHÔNG chạm platform.
GRANT USAGE ON SCHEMA core, seller TO svc_seller;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA core, seller TO svc_seller;

-- I14 — BẮT BUỘC. `ALL TABLES` chỉ phủ bảng ĐANG tồn tại; bảng tạo sau
-- sẽ không được cấp quyền và I2 hỏng im lặng ở bảng thứ N+1.
ALTER DEFAULT PRIVILEGES IN SCHEMA platform
  GRANT SELECT ON TABLES TO svc_ohana_ai;
ALTER DEFAULT PRIVILEGES IN SCHEMA core, seller
  GRANT SELECT, INSERT, UPDATE ON TABLES TO svc_seller;
```

`svc_ohana_ai` truy vấn `seller.draft` ⇒ **permission denied** ở tầng DB. I2 không phụ thuộc code review.

**MUST NOT** dùng `GRANT ... ON ALL TABLES` một mình. Không có `ALTER DEFAULT PRIVILEGES` thì mọi migration sau đều mở một lỗ mà không ai thấy.

---

## §5 · Bảng

Quy ước: `timestamptz` toàn bộ · `GENERATED ALWAYS AS IDENTITY` · **không prefix** (schema đã phân tách).

### §5.1 core — tài khoản & shop

```sql
-- Ohana auth riêng, không dùng chung ONFA
CREATE TABLE core.account (
  account_id    bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  email         citext UNIQUE NOT NULL,
  password_hash text   NOT NULL,          -- argon2id
  display_name  text   NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE core.shop (
  shop_id           bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  display_name      text NOT NULL,
  industry          text,
  default_lang      text NOT NULL DEFAULT 'vi',
  active_persona_id bigint,
  created_at        timestamptz NOT NULL DEFAULT now()
);

-- 1 account quản nhiều shop; JWT mang account_id, resolve ra shop_id
CREATE TABLE core.account_shop (
  account_id bigint NOT NULL REFERENCES core.account,
  shop_id    bigint NOT NULL REFERENCES core.shop,
  role       text   NOT NULL DEFAULT 'owner',
  PRIMARY KEY (account_id, shop_id)
);

-- refresh xoay vòng + reuse detection
CREATE TABLE core.refresh_token (
  token_id   bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  account_id bigint NOT NULL REFERENCES core.account,
  token_hash bytea  NOT NULL UNIQUE,        -- sha256; KHÔNG lưu token gốc
  family_id  uuid   NOT NULL,               -- 1 phiên đăng nhập = 1 family
  used_at    timestamptz,                   -- NOT NULL ⇒ đã dùng ⇒ reuse ⇒ huỷ family
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON core.refresh_token (family_id);
CREATE INDEX ON core.refresh_token (expires_at);
```

### §5.2 core — persona (append-only, w§4)

```sql
CREATE TYPE core.persona_status AS ENUM ('draft','approved','retired');

CREATE TABLE core.shop_persona (
  persona_id  bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  shop_id     bigint NOT NULL REFERENCES core.shop,
  version     int    NOT NULL,
  body        text   NOT NULL,
  token_count int    NOT NULL,
  status      core.persona_status NOT NULL DEFAULT 'draft',
  approved_by bigint REFERENCES core.account,
  approved_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (shop_id, version),
  CONSTRAINT persona_token_cap CHECK (token_count <= 2000)   -- w§4 enforce LÚC SAVE
);
ALTER TABLE core.shop ADD CONSTRAINT fk_active_persona
  FOREIGN KEY (active_persona_id) REFERENCES core.shop_persona;
```

**MUST NOT** thay bảng này bằng cột `version int` trên `core.shop` — mất nội dung cũ, không audit được draft cũ sinh từ persona nào.

### §5.3 core — binding & config

```sql
CREATE TABLE core.channel_binding (
  binding_id     bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  shop_id        bigint NOT NULL REFERENCES core.shop,
  channel        text   NOT NULL,          -- 'zalo'|'facebook'|'instagram'
  endpoint       text   NOT NULL,
  page_id        text   NOT NULL,
  secret_env_key text   NOT NULL,          -- TÊN biến env, không phải giá trị
  verified_at    timestamptz,
  UNIQUE (channel, endpoint, page_id)
);
CREATE INDEX ON core.channel_binding (channel, endpoint, page_id)
  WHERE verified_at IS NOT NULL;

CREATE TABLE core.shop_knowledge (               -- tầng 2, w§3
  shop_id    bigint NOT NULL REFERENCES core.shop,
  kind       text   NOT NULL,               -- 'size_table'|'delivery_table'
  payload    jsonb  NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (shop_id, kind)
);

CREATE TABLE core.shop_config (
  shop_id             bigint PRIMARY KEY REFERENCES core.shop,
  cost_cap_tokens_day bigint  NOT NULL DEFAULT 200000,
  draft_ttl_minutes   int     NOT NULL DEFAULT 60,
  tier1_drift_pct     numeric NOT NULL DEFAULT 5.0,
  escalate_channels   text[]  NOT NULL DEFAULT '{push}',
  auto_ack_template   text,
  auto_ack_enabled    boolean NOT NULL DEFAULT false
);
```

**Secret**: `secret_env_key` chứa **tên** biến môi trường (ví dụ `ZALO_SECRET_OA_12345`), giá trị đọc từ `os.environ`. **MUST NOT** lưu giá trị secret trong DB.

### §5.4 seller — ingest

```sql
CREATE TABLE seller.webhook_seen (
  event_id        bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  channel         text   NOT NULL,
  platform_msg_id text   NOT NULL,
  shop_id         bigint NOT NULL REFERENCES core.shop,
  raw_event       jsonb  NOT NULL,
  trace_id        uuid   NOT NULL,        -- sinh tại webhook, xuyên suốt §9
  received_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (channel, platform_msg_id)
);

CREATE TYPE seller.outbox_status AS ENUM ('pending','processing','done','dead');

-- outbox CHÍNH LÀ queue. Không Redis, không RabbitMQ. → §10
CREATE TABLE seller.outbox (
  outbox_id  bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  event_id   bigint NOT NULL REFERENCES seller.webhook_seen,
  shop_id    bigint NOT NULL REFERENCES core.shop,
  payload    jsonb  NOT NULL,
  status     seller.outbox_status NOT NULL DEFAULT 'pending',
  attempts   int    NOT NULL DEFAULT 0,
  next_retry_at timestamptz,   -- backoff 2^attempts giây khi lỗi; NULL = claim được ngay (amend 2026-07-30, review A5-A8)
  claimed_at timestamptz,
  last_error text,
  trace_id   uuid   NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON seller.outbox (created_at) WHERE status = 'pending';
CREATE INDEX ON seller.outbox (claimed_at) WHERE status = 'processing';
```

### §5.5 seller — hội thoại & draft

```sql
CREATE TABLE seller.conversation (
  conversation_id     bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  shop_id             bigint NOT NULL REFERENCES core.shop,
  channel             text   NOT NULL,
  customer_ref        text   NOT NULL,
  next_debounce_at    timestamptz,
  debounce_claimed_at timestamptz,
  window_expires_at   timestamptz,        -- nguồn theo channel, xem §6.8
  created_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (shop_id, channel, customer_ref)
);
CREATE INDEX ON seller.conversation (next_debounce_at)
  WHERE next_debounce_at IS NOT NULL AND debounce_claimed_at IS NULL;

CREATE TYPE seller.msg_sender AS ENUM ('customer','seller','system');

CREATE TABLE seller.message (
  message_id      bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  conversation_id bigint NOT NULL REFERENCES seller.conversation,
  sender          seller.msg_sender NOT NULL,
  body_raw        text,                    -- giữ RAW; scrub lúc dựng prompt (I3)
  has_media       boolean NOT NULL DEFAULT false,
  platform_msg_id text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (conversation_id, platform_msg_id)      -- PRE-010 C1
);

CREATE TYPE seller.draft_status AS ENUM
  ('pending','editing','sending','sent','rejected','expired');
CREATE TYPE seller.draft_label AS ENUM ('approved','edited','rejected');

CREATE TABLE seller.draft (
  draft_id           bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  conversation_id    bigint NOT NULL REFERENCES seller.conversation,
  shop_id            bigint NOT NULL REFERENCES core.shop,
  body               text   NOT NULL,
  intent             text   NOT NULL,
  escalation_reasons text[] NOT NULL DEFAULT '{}',
  tier1_snapshot     jsonb  NOT NULL,               -- I9
  snapshot_at        timestamptz NOT NULL,          -- T0
  persona_id         bigint NOT NULL REFERENCES core.shop_persona,   -- I9
  status             seller.draft_status NOT NULL DEFAULT 'pending',
  ttl_expires_at     timestamptz NOT NULL,
  ttl_extended       boolean NOT NULL DEFAULT false,
  label              seller.draft_label,
  sent_at            timestamptz,
  sent_by            bigint REFERENCES core.account,
  trace_id           uuid   NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  -- chặn typo; label bẩn sẽ đầu độc training set w§8.1
  CONSTRAINT escalation_reasons_known CHECK (
    escalation_reasons <@ ARRAY[
      'sensitive_intent','injection_attempt','data_unavailable',
      'media_content','window_closed','cost_cap','window_unknown'
    ]::text[]
  )
);
CREATE INDEX ON seller.draft (shop_id, status);
CREATE INDEX ON seller.draft (ttl_expires_at) WHERE status IN ('pending','editing');
CREATE INDEX ON seller.draft (conversation_id);
```

### §5.6 seller — cost & observability

```sql
CREATE TABLE seller.cost_budget (
  shop_id         bigint NOT NULL REFERENCES core.shop,
  budget_date     date   NOT NULL,
  cap_tokens      bigint NOT NULL,
  reserved_tokens bigint NOT NULL DEFAULT 0,
  actual_tokens   bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (shop_id, budget_date)
);

-- I13 — reservation phải có DANH TÍNH, không chỉ là một con số tổng.
-- Không có bảng này thì LLM timeout ⇒ reserved_tokens rò rỉ ⇒ shop bị khoá
-- tới nửa đêm, và reaper không có cách nào biết cái nào treo.
CREATE TABLE seller.cost_reservation (
  reservation_id bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  shop_id     bigint NOT NULL REFERENCES core.shop,
  budget_date date   NOT NULL,
  tokens      int    NOT NULL,
  trace_id    uuid   NOT NULL,
  released_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (shop_id, budget_date)
    REFERENCES seller.cost_budget (shop_id, budget_date)
);
CREATE INDEX ON seller.cost_reservation (created_at) WHERE released_at IS NULL;

CREATE TABLE seller.llm_turn (                     -- w§9.3
  turn_id        bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  shop_id        bigint,
  flow           text NOT NULL,                    -- 'ohana_ai'|'ai_seller'
  provider       text NOT NULL,                    -- 'anthropic'|'together'
  model          text NOT NULL,
  prompt_version text NOT NULL,
  latency_ms     int  NOT NULL,
  tokens_in      int  NOT NULL,
  tokens_out     int  NOT NULL,
  pii_hits       int  NOT NULL DEFAULT 0,          -- PRE-010 C4
  injection_flag boolean NOT NULL DEFAULT false,
  trace_id       uuid NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON seller.llm_turn (created_at);
CREATE INDEX ON seller.llm_turn (trace_id);
```

### §5.7 platform — corpus luồng A

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE platform.corpus (
  chunk_id     bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  source_ref   text NOT NULL,              -- sinh từ heading_path
  heading_path text[] NOT NULL,            -- ['Gói cước','Nâng cấp']
  body         text NOT NULL,
  token_count  int  NOT NULL,
  parent_ref   text,                       -- NULL ở phase 1; móc sẵn cho parent-child
  embedding    vector(1024) NOT NULL,      -- multilingual-e5-large-instruct qua Together
  created_at   timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT chunk_fits_model CHECK (token_count <= 480)   -- trần model 512, chừa prefix
);
CREATE INDEX ON platform.corpus USING hnsw (embedding vector_cosine_ops);
```

**MUST NOT** tạo bảng vector nào trong schema `seller` — w§3 cấm RAG cho AI Seller. Ngoại lệ duy nhất đã xếp lịch: w§8.4.

---

## §6 · Năm câu lệnh bắt buộc đúng nguyên văn

Năm câu này **MUST** giữ đúng hình dạng. Viết lại thành nhiều câu = bug im lặng.

### §6.1 Ghi nhận webhook (I7, w§2.1)
```sql
WITH ins AS (
  INSERT INTO seller.webhook_seen (channel, platform_msg_id, shop_id, raw_event, trace_id)
  VALUES ($1, $2, $3, $4, $6)
  ON CONFLICT (channel, platform_msg_id) DO NOTHING
  RETURNING event_id, shop_id, trace_id
)
INSERT INTO seller.outbox (event_id, shop_id, payload, trace_id)
SELECT event_id, shop_id, $5, trace_id FROM ins
RETURNING outbox_id;
```
0 row ⇒ đã nhận trước đó ⇒ trả 200, dừng.
**MUST NOT** tách thành hai `INSERT` — `ON CONFLICT DO NOTHING` không báo cho câu sau biết nó có thật sự insert hay không.

### §6.2 Claim outbox
```sql
UPDATE seller.outbox SET status='processing', claimed_at=now(), attempts=attempts+1
WHERE outbox_id IN (
  SELECT outbox_id FROM seller.outbox
   WHERE status='pending'
     AND (next_retry_at IS NULL OR next_retry_at <= now())
   ORDER BY created_at
   FOR UPDATE SKIP LOCKED LIMIT 20
)
RETURNING *;
```
Commit ngay sau claim. **MUST NOT** giữ transaction mở trong lúc gọi LLM.

Điều kiện `next_retry_at` (amend 2026-07-30, review A5–A8): job lỗi quay về `pending` với
`next_retry_at = now() + 2^attempts giây`. Thiếu điều kiện này, row lỗi (cũ nhất theo
`created_at`) bị claim lại NGAY tick kế tiếp — 5 attempts cháy trong ~1 giây trước khi một
lỗi thoáng qua kịp hết, và tin khách thành `dead` oan.

### §6.3 Claim debounce (PRE-010 C2)
```sql
UPDATE seller.conversation SET debounce_claimed_at = now()
 WHERE conversation_id = $1
   AND next_debounce_at <= now()
   AND debounce_claimed_at IS NULL
RETURNING conversation_id;
```
0 row ⇒ instance khác đã lấy ⇒ bỏ qua. Đúng 1 draft dù N scheduler.

### §6.4 Gia hạn TTL (PRE-010 C3)
```sql
UPDATE seller.draft d SET
  ttl_expires_at = LEAST(d.ttl_expires_at + ($2 || ' minutes')::interval,
                         c.window_expires_at),
  ttl_extended   = true
FROM seller.conversation c
WHERE d.draft_id = $1 AND d.conversation_id = c.conversation_id
  AND d.ttl_extended = false
RETURNING d.ttl_expires_at;
```
`LEAST` là toàn bộ nội dung C3. SQL không cho vượt window.

### §6.5 Reserve cost (I8, I13)
```sql
WITH upd AS (
  UPDATE seller.cost_budget
     SET reserved_tokens = reserved_tokens + $2
   WHERE shop_id = $1 AND budget_date = CURRENT_DATE
     AND reserved_tokens + actual_tokens + $2 <= cap_tokens
  RETURNING shop_id, budget_date
)
INSERT INTO seller.cost_reservation (shop_id, budget_date, tokens, trace_id)
SELECT shop_id, budget_date, $2, $3 FROM upd
RETURNING reservation_id;
```
0 row ⇒ chạm trần ⇒ cổng chính sách chuyển GIỮ, không gọi LLM (w§2.4).
**MUST NOT** check rồi update — đó là race.
**MUST NOT** cộng `reserved_tokens` mà không ghi `cost_reservation` — reservation vô danh thì reaper không gỡ được (R4).

### §6.5b Reconcile sau khi có token thật
```sql
WITH rel AS (
  UPDATE seller.cost_reservation SET released_at = now()
   WHERE reservation_id = $1 AND released_at IS NULL
  RETURNING shop_id, budget_date, tokens
)
UPDATE seller.cost_budget b
   SET reserved_tokens = b.reserved_tokens - rel.tokens,
       actual_tokens   = b.actual_tokens   + $2      -- token thật
  FROM rel
 WHERE b.shop_id = rel.shop_id AND b.budget_date = rel.budget_date;
```

### §6.6 Optimistic lock duyệt (w§2.5)
```sql
UPDATE seller.draft SET status='sending'
 WHERE draft_id = $1 AND status IN ('pending','editing')
RETURNING draft_id;
```

### §6.7 Xoay vòng refresh token + reuse detection
```sql
UPDATE core.refresh_token SET used_at = now()
 WHERE token_hash = $1 AND used_at IS NULL AND expires_at > now()
RETURNING account_id, family_id;
```
0 row ⇒ token sai, hết hạn, **hoặc đã dùng rồi**. Trường hợp đã dùng = dấu hiệu bị đánh cắp ⇒ huỷ cả family:
```sql
UPDATE core.refresh_token SET used_at = now()
 WHERE family_id = $1 AND used_at IS NULL;
```
**MUST NOT** phân biệt ba nguyên nhân trong response — trả `401` giống nhau, tránh lộ thông tin.

### §6.8 Messaging window (I12, O4)
```
facebook | instagram → window = last_customer_msg_at + 24h      TÍNH TẠI CHỖ
zalo                → gọi API Zalo, cache vào conversation.window_expires_at
```
Cập nhật ở loop `outbox` (lúc ghi message), **không** ở loop `debounce` — window reset theo tin khách.

API Zalo fail ⇒ theo đúng quy tắc w§3 tầng 1: **ESCALATE** `reason='window_unknown'`, **MUST NOT** draft dựa trên window cũ.

### §6.9 Reaper R3 — gỡ debounce claim treo (I13)
```sql
UPDATE seller.conversation SET debounce_claimed_at = NULL
 WHERE debounce_claimed_at < now() - interval '5 minutes';
```
Không có câu này, worker chết sau §6.3 ⇒ conversation rơi khỏi index
`WHERE ... debounce_claimed_at IS NULL` ⇒ **im lặng vĩnh viễn**.

### §6.10 Reaper R4 — release reservation treo (I13)
```sql
WITH rel AS (
  UPDATE seller.cost_reservation SET released_at = now()
   WHERE released_at IS NULL AND created_at < now() - interval '5 minutes'
  RETURNING shop_id, budget_date, tokens
), agg AS (
  SELECT shop_id, budget_date, sum(tokens) AS tokens
    FROM rel GROUP BY shop_id, budget_date
)
UPDATE seller.cost_budget b
   SET reserved_tokens = GREATEST(0, b.reserved_tokens - a.tokens)
  FROM agg a
 WHERE b.shop_id = a.shop_id AND b.budget_date = a.budget_date;
```
`GREATEST(0, ...)` phòng double-release; không thay cho việc release đúng.

CTE `agg` (amend 2026-07-30, review A5–A8): `UPDATE … FROM` của Postgres chỉ áp MỘT row
FROM cho mỗi row đích — bản cũ join thẳng `rel` nên hai reservation treo cùng
`(shop_id, budget_date)` chỉ trừ được một, phần còn lại rò trong `reserved_tokens` tới
nửa đêm và không còn reservation chưa-release nào để R4 gỡ nữa. GROUP BY trước rồi mới
UPDATE thì N reservation trừ đúng tổng N.

---

## §7 · API contract

Auth: **Ohana tự phát JWT**. Claim: `{ account_id, exp }`. **Không** mang `shop_id` trong token — resolve qua `core.account_shop` mỗi request (I6).

| | Giá trị | Ghi chú |
|---|---|---|
| access token | **15 phút** | JWT, stateless, không thu hồi được — nên phải ngắn |
| refresh token | **30 ngày** | opaque, hash lưu `core.refresh_token`, xoay vòng mỗi lần dùng |
| rotation | mỗi lần refresh cấp token mới + huỷ token cũ | dùng lại token cũ ⇒ huỷ cả family §6.7 |
| lưu ở client | refresh trong **httpOnly + Secure + SameSite=Lax** cookie | access giữ trong memory, **MUST NOT** localStorage |

```
POST /v1/auth/login       → { access, exp }  + Set-Cookie: refresh
POST /v1/auth/refresh     → { access, exp }  + Set-Cookie: refresh (mới)
POST /v1/auth/logout      → huỷ family hiện tại
```

### Luồng A · `svc-ohana-ai`
```
POST /v1/chat                        Bearer JWT
  → { question: str }
  ← { answer: str, sources: [{source_ref, snippet}], grounded: bool }
```
`grounded=false` ⇒ `answer` là câu "không biết" (w§1). Client **MUST NOT** tự chế câu thay thế.

### Luồng B ingress · `svc-seller`
```
POST /v1/webhook/{channel}           không auth; verify chữ ký trên RAW body
  ← 200 luôn luôn, ≤2s
```
200 kể cả khi không tìm thấy binding — trả lỗi sẽ kích platform retry vô ích.
**MUST** verify chữ ký **trước** khi parse JSON. `page_id` chỉ dùng sau khi chữ ký pass.

### Luồng B seller · `svc-seller`
```
GET  /v1/shops/{shop_id}/inbox?cursor=
  ← [{ draft_id, body, intent, escalation_reasons[], tier1_snapshot,
       ttl_remaining_s, window_remaining_s, persona_stale: bool }]
  sort: ESCALATE > window sắp hết > TTL sắp hết > mới nhất

POST /v1/drafts/{id}/extend    ← { ttl_expires_at }   | 409 already_extended
POST /v1/drafts/{id}/approve   → { body?: str }
  ← 200 { sent_at }
  | 409 { reason: 'taken_by_other' }      ← §6.6 trả 0 row
  | 409 { reason: 'persona_changed' }     ← draft.persona_id ≠ shop.active_persona_id
  | 200 { warn: 'tier1_drift', diff }     ← cần client confirm lại
POST /v1/drafts/{id}/reject
```

`shop_id` là **path param**, không phải query — middleware phải resolve nó thành
`ShopContext` (§8.4) trước khi chạm handler. `/v1/drafts/{id}` không mang `shop_id`:
middleware nạp draft, đọc `draft.shop_id`, kiểm `core.account_shop`, rồi mới dựng context.

`trace_id` của mọi response **MUST** đi ra header `X-Trace-Id` để đối chiếu với §9.

---

## §8 · LLM layer

### §8.1 Phân vai provider

| Việc | Provider | Model | Lý do |
|---|---|---|---|
| Sinh câu trả lời (A & B) | Anthropic | Claude | chất lượng tiếng Việt |
| Embedding corpus + query | **Together** | `intfloat/multilingual-e5-large-instruct` · 1024d · **max 512 tok** | **Anthropic không có embeddings API** |
| PII filter phase 2 (w§8.5) | Together | model nhỏ | rẻ, có thể on-shore |

**Ghi chú bắt buộc:** Claude API **không** có endpoint embeddings. Mọi code gọi `anthropic.embeddings` là sai — không tồn tại.

### §8.2 Router (I5)

`agent/providers/` là **nơi duy nhất** import `anthropic` hoặc `together`.

```python
async def generate(prompt: Scrubbed, *, task: str) -> LLMResult: ...

# I11 — hai hàm RIÊNG, không phải một hàm với tham số prefix.
# Dòng e5 bắt buộc prefix; quên KHÔNG báo lỗi, chỉ giảm chất lượng thầm lặng.
async def embed_passage(texts: list[str]) -> list[list[float]]: ...   # prefix "passage: "
async def embed_query(text: str) -> list[float]: ...                  # prefix "query: "
```
**MUST NOT** expose `embed(texts, prefix=...)` — tham số tuỳ chọn là chỗ để quên.
Đổi provider = sửa `agent/providers/` + config. Không đụng call-site (w§9.2).

### §8.3 Type gate cho I3 & I4

```python
# agent/types.py
class Scrubbed(str):
    """Chỉ scrub() tạo được."""
    __slots__ = ()

class Wrapped(str):
    """Chỉ wrap() tạo được."""
    __slots__ = ()
```
`generate()` nhận `Scrubbed`. Truyền `str` thường ⇒ **mypy fail**. I3/I4 thành lỗi biên dịch, không phải mục review.

Phạm vi scrub (w§2.3): tin khách + 6 lượt lịch sử + **kết quả tool tầng 1** + trường persona. Lọc theo **đích**, không theo nguồn.

### §8.4 Type gate cho I6 — `ShopContext`

`shop_id: int` là kiểu **cấm** trong signature của mọi hàm tầng service/repo.

```python
# auth/context.py
class ShopContext:
    """Chỉ middleware tạo được, SAU khi kiểm core.account_shop."""
    __slots__ = ('shop_id', 'account_id', 'trace_id')
    def __init__(self, *, shop_id: int, account_id: int, trace_id: UUID) -> None: ...

# api/inbox.py
async def list_inbox(ctx: ShopContext, cursor: str | None) -> list[Draft]: ...
```

Truyền `int` vào ⇒ **mypy fail**. I6 thành lỗi biên dịch, không còn phụ thuộc
middleware viết đúng — đúng cùng cơ chế với `Scrubbed`/`Wrapped` §8.3.

**MUST NOT** viết `async def list_inbox(shop_id: int, ...)` dù middleware đã kiểm.
Kiểm đúng một lần ở middleware nhưng signature vẫn nhận `int` thì lần thứ hai ai đó
gọi hàm từ chỗ khác sẽ không có gì chặn.

---

## §9 · PRE-010 → cơ chế → test

| # | Cơ chế | File test |
|---|---|---|
| C1 | `UNIQUE (conversation_id, platform_msg_id)` §5.5 | `tests/contract/test_c1_dedup.py` — deliver 2× ⇒ 1 row |
| C2 | `debounce_claimed_at IS NULL` §6.3 | `tests/contract/test_c2_scheduler.py` — 2 worker ⇒ 1 draft |
| C3 | `LEAST(..., window_expires_at)` §6.4 | `tests/contract/test_c3_ttl.py` — window < N ⇒ = window_end |
| C4 | `llm_turn.pii_hits` + golden set ≥200 tin | `tests/eval/test_c4_pii_fn.py` — ra **con số** |
| C5 | bảng severity rank trong `rules.py` | `tests/contract/test_c5_severity.py` — hoán vị rule ⇒ cùng kết quả |

Bổ sung ngoài PRE-010:

| Bất biến | Test |
|---|---|
| I2 + I14 | `test_i14_default_privileges.py` — tạo bảng mới trong `seller`, `svc_ohana_ai` vẫn **denied** |
| I13 / R3 | `test_r3_debounce_reaper.py` — claim rồi kill worker ⇒ reaper gỡ ⇒ conversation chạy lại |
| I13 / R4 | `test_r4_reservation_leak.py` — reserve rồi timeout ⇒ reaper release ⇒ `reserved_tokens` về đúng |
| I6 | `test_i6_shopcontext.py` — mypy phải fail khi hàm service nhận `shop_id: int` |
| trace | `test_trace_propagation.py` — 1 webhook ⇒ cùng `trace_id` ở outbox, draft, llm_turn |

C1–C3 nằm ở **schema**, không ở application code. Chủ đích: ràng buộc tầng DB thì không lách được bằng cách quên.

---

## §10 · Cấm — phase 1

| Cấm | Vì sao | Mở khi |
|---|---|---|
| Redis / RabbitMQ / SQS | `seller.outbox` đã là queue; broker ngoài = dual-write = PRE-010 C1 | >1000 msg/s → thêm relay, không đổi consumer |
| Vector store cho AI Seller | w§3: RAG không bao giờ nói "không biết" | w§8.4 |
| Classifier ML | chưa đủ label | w§8.1 + noise floor ≥85% |
| Summary hội thoại | phase 1 cứng last-N=6 | w§8.2 |
| VLM parse media | phase 1 auto-ESCALATE | w§8.4 |
| Nhánh tự động gửi | I10 | w§8.1 |
| Row-Level Security | repo layer bắt buộc `shop_id: ShopId` rẻ hơn, audit bằng test AST | nếu có nhiều team ghi DB |
| `anthropic.embeddings` | **không tồn tại** | không bao giờ |
| Gọi API lấy window cho FB/IG | suy ra được từ `last_customer_msg_at + 24h`; gọi API là thêm latency vào đường ACK ≤2s và thêm điểm hỏng (I12) | nếu platform đổi luật window |
| `embed()` có tham số prefix tuỳ chọn | tham số tuỳ chọn = chỗ để quên; sai prefix không báo lỗi (I11) | không bao giờ |
| Lưu access/refresh token trong `localStorage` | XSS đọc được | không bao giờ |
| Chunk corpus > 480 token | trần model 512, chừa chỗ cho prefix; `CHECK` §5.7 chặn | đổi sang model context dài hơn |
| `GRANT ON ALL TABLES` không kèm `ALTER DEFAULT PRIVILEGES` | I2 hỏng im lặng ở bảng thứ N+1 (I14) | không bao giờ |
| Hàm service nhận `shop_id: int` | kiểm ở middleware không ràng buộc được call-site thứ hai (I6) | không bao giờ |
| Claim (`claimed_at`, `debounce_claimed_at`, reservation) không có reaper gỡ | worker chết ⇒ treo vĩnh viễn (I13) | không bao giờ |
| Tách `worker-seller` thành 3 process vì "LLM block event loop" | `await` HTTP không block loop; vấn đề thật là starvation → dùng semaphore + pool riêng | khi đo được starvation thật |

---

## §11 · Thứ tự triển khai

Theo w§7, không đảo được. Tier = mức gác.

| Bước | Nội dung | Tier | Gate |
|---|---|---|---|
| 1 | schema + role §4 + migration | **stop** | permission test: `svc_ohana_ai` đọc `seller.draft` ⇒ denied |
| 2 | webhook + outbox + binding §5.4 §6.1 | **stop** | C1 |
| 3 | PII + injection type gate §8.3 | **stop** | C4 golden set ≥200 |
| 4 | rules + severity §9 | ask | C5 |
| 5 | draft schema §5.5 | **stop** | C3 |
| 6 | cost cap + debounce scheduler §6.3 §6.5 | **stop** | C2 |
| 7 | pipeline compose | ask | eval marker |
| 8 | DPIA cross-border | — | pháp lý |
| 9 | corpus luồng A §5.7 · chunk theo heading ≤480 tok | ask | eval marker + O5 |

Bước 9 độc lập, chạy song song từ đầu được.

---

## §12 · Còn treo

| # | Câu hỏi | Chặn bước |
|---|---|---|
| O5 | Chất lượng tiếng Việt của `multilingual-e5-large-instruct` trên corpus thật — chưa đo. Nếu recall@5 không đạt thì phương án dự phòng là Voyage `voyage-3.5` (cũng 1024d ⇒ schema không đổi, chỉ re-embed). | 9 |
| O6 | Zalo API lấy messaging window — endpoint nào, rate limit bao nhiêu? | 5 |
| O7 | `heading_path` → `source_ref` hiển thị cho user: full path hay 2 cấp cuối? | 9 |
| O8 | **Retention + quyền xoá theo NĐ13.** `webhook_seen.raw_event` giữ PII thô vĩnh viễn, chưa có đường xoá. Cần chính sách trước khi code. | 2 |
| O9 | **Tin mới đến khi draft đang `pending`** — draft cũ có bị vô hiệu không? snapshot T0 còn dùng được không? `w§2.2` chỉ coalesce TRƯỚC compose. | 7 |
| O10 | Deadline/retry/429 cho `router.py` — timeout bao nhiêu? retry mấy lần? (gắn với R4: timeout phải ngắn hơn 5' của reaper) | 7 |
| O11 | Blue/green · capacity model · PITR/RPO/RTO — hoãn tới khi có ngày launch | trước prod |
