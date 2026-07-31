# CC Brief — Bước 2 · Phase 2.1 · Schema `assistant` + Isolation Gate

> **Skill:** `ohana-be-coder`.
> **Contract:** `docs/adr-tang2-ohana-ai-assistant.md` (D1–D6 accepted, D7 open) — spec doc chưa có, ADR + brief này là hợp đồng thực thi cho Phase 2.1.
> **Reconcile với:** `docs/ohana-be-design.md` — bất biến I1/I2/I14 giữ nguyên; §4 (schema/role) là mẫu, §5.5 (`seller.conversation/message`) là template shape.
> **Trạng thái:** Linear [OHB-27](https://linear.app/drnick/issue/OHB-27) Todo, parent [OHB-26](https://linear.app/drnick/issue/OHB-26) In Progress, project **Ohana BE — Tầng 2 · AI Assistant**.
> **Mục tiêu:** migration `a12_assistant_schema` LAND + isolation gate xanh, KHÔNG cost/rate-limit/Redis/memory-logic/routers. Dừng ở đó, chờ Phase 2.2 brief.

---

## §0 · Trước khi viết dòng đầu (pre-flight `ohana-be-coder`)

```bash
git log --oneline -3                                    # HEAD = da891c1 (ADR commit)
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5433/ohana" alembic current
                                                        # = a11_pending_sent_claim (head)
DATABASE_URL=…ohana_test… pytest -q                     # 371 passed (baseline Bước 1)
cd web && playwright test                               # 5 passed
```

- [ ] File chạm: `db/migrations/versions/a12_assistant_schema.py` (**NEW**, stop-tier), `db/models.py` (ask), `tests/contract/test_a12_assistant_schema.py` (**NEW**, ask), `tests/contract/conftest.py` (ask — nếu cần thêm wipe cho `assistant.*`).
- [ ] `stop` ⇒ DỪNG mô tả kế hoạch chờ duyệt. Migration = stop, đã brief xong ở đây thay cho pause.
- [ ] Đọc `§ref`: ADR §1 (D2), design §4 (grant pattern), design §5.5 (conversation/message template), `a1_platform_schema_grants.py` + `a2_flow_grants.py` (mẫu op.execute), `tests/contract/test_i14_default_privileges.py` (mẫu contract).
- [ ] Bất biến chạm: **I1, I2, I14** (§3 dưới).

---

## §1 · Quyết định KHOÁ (từ ADR — không mở lại)

- **D1** — extend `main_ohana_ai` (không +process). Phase 2.1 KHÔNG đụng `main_ohana_ai.py` — chỉ migration + gate.
- **D2** — grant WRITE `svc_ohana_ai` **CHỈ** trong `assistant.*`. `public/*` + `platform/*` giữ nguyên I2 cho luồng A. Phase 2.1 = chỗ D2 land thật.
- **D7 (open)** không chặn — schema dùng `user_id text` bất kể ai issue JWT (khớp `auth/identity.py::Identity.user_id: str`).
- **KHÔNG grant `svc_seller`, KHÔNG grant `mcp_readonly` trên `assistant.*`** — user memory là data nhạy cảm; nếu MCP cần đọc thì grant có chủ đích ở phase sau, không mặc định.

---

## §2 · Tasks — MATCH / FITS / OUT

### MATCH (làm)

| # | Việc | File | Tier | Gate |
|---|---|---|---|---|
| 1 | Migration `a12_assistant_schema` | `db/migrations/versions/a12_assistant_schema.py` **(NEW)** | **stop** (chạy `ohana_migrator`) | `alembic upgrade head` xanh · `alembic current` = `a12_assistant_schema` |
| 2 | ORM models cho 3 bảng mới | `db/models.py` (append) | ask | Import sạch, mypy xanh |
| 3 | Contract test isolation (hai chiều) | `tests/contract/test_a12_assistant_schema.py` **(NEW)** | ask | 4 test: A ghi được `assistant.*` · A denied `public/*` · B denied `assistant.*` · MCP denied `assistant.*` |
| 4 | Extend `conftest.py wipe_tenant` cho `assistant.*` (nếu tests dùng) | `tests/contract/conftest.py` | ask | Existing contract tests vẫn xanh |

### Chi tiết Task 1 — migration `a12_assistant_schema.py`

**Revision:** `a12_assistant_schema` · **down_revision:** `a11_pending_sent_claim` · **BẮT BUỘC role `ohana_migrator`** (bẫy Alembic của skill — I14 chỉ phủ bảng do role đó tạo).

**upgrade() thứ tự:**

```sql
-- (1) Schema mới
CREATE SCHEMA IF NOT EXISTS assistant;

-- (2) 3 bảng · timestamptz, GENERATED ALWAYS AS IDENTITY, không prefix cột (schema đã tách)
CREATE TABLE assistant.conversations (
  conversation_id  bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  user_id          text        NOT NULL,          -- JWT sub, khớp auth/identity.py::Identity.user_id
  title            text,                          -- per-user chat like Claude/Grok
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  deleted_at       timestamptz                    -- soft-delete (Phase 2.4 CRUD)
);
CREATE INDEX idx_assistant_conv_user_updated
  ON assistant.conversations (user_id, updated_at DESC)
  WHERE deleted_at IS NULL;                       -- list-recent cho UI, ẩn deleted

CREATE TYPE assistant.msg_role AS ENUM ('user','assistant','system');

CREATE TABLE assistant.messages (
  message_id       bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  conversation_id  bigint NOT NULL REFERENCES assistant.conversations(conversation_id) ON DELETE CASCADE,
  user_id          text   NOT NULL,               -- redundant với conversations.user_id nhưng: (a)
                                                  -- lock scope repo layer đọc last-N; (b) chống query
                                                  -- lỡ mất WHERE user_id (repo pattern Ohana).
  role             assistant.msg_role NOT NULL,
  content          text        NOT NULL,          -- raw, scrub lúc dựng prompt (I3-analog cho luồng A)
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_assistant_msg_conv_created
  ON assistant.messages (conversation_id, created_at);

CREATE TABLE assistant.user_memory (
  memory_id   bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
  user_id     text        NOT NULL,               -- namespace `mem:user:{id}` (ADR §3)
  content     text        NOT NULL,               -- cue: thích/ghét/mục tiêu/dự định
  embedding   vector(1024) NOT NULL,              -- e5 khớp EMBED_DIM=1024, HNSW indexed
  created_at  timestamptz NOT NULL DEFAULT now()
  -- Append-only: không UPDATE, không soft-delete tại Phase 2.1. `forget` (Phase 2.4)
  -- sẽ là DELETE thật hoặc thêm `forgotten_at` — quyết định ở phase đó, không giả sử ở đây.
);
-- HNSW index cho recall per-user (Phase 2.3 gọi). vector_cosine_ops khớp retrieval hiện có
-- (retrieval/pgvector.py dùng cosine_distance). Params default pgvector: m=16, ef_construction=64
-- (Wyatt confirm nếu muốn khác — chưa đo, giữ default là ràng buộc "chưa đo" cùng họ ISSUE-022).
CREATE INDEX idx_assistant_user_memory_hnsw
  ON assistant.user_memory USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_assistant_user_memory_user
  ON assistant.user_memory (user_id, created_at DESC);

-- (3) Grants — D2 · svc_ohana_ai FULL trong assistant, không role khác
GRANT USAGE ON SCHEMA assistant TO svc_ohana_ai;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA assistant TO svc_ohana_ai;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA assistant TO svc_ohana_ai;
GRANT USAGE ON TYPE assistant.msg_role TO svc_ohana_ai;

-- (4) I14 · BẢNG TƯƠNG LAI trong assistant vẫn cấp quyền cho svc_ohana_ai
ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA assistant
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO svc_ohana_ai;
ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA assistant
  GRANT USAGE ON SEQUENCES TO svc_ohana_ai;
```

**downgrade():**
```sql
DROP SCHEMA IF EXISTS assistant CASCADE;
-- Roles/default privileges gắn với ohana_migrator ở scope schema — DROP CASCADE dọn hết.
```

**⚠️ Chú ý:**
- `op.execute()` cho MỌI statement (autogen không thấy: schema, ENUM, HNSW, GRANT, ALTER DEFAULT PRIVILEGES).
- `pgvector` extension đã enabled (`Vector(EMBED_DIM)` trong `db/models.py::Embedding` đang chạy) — KHÔNG cần `CREATE EXTENSION`.
- HNSW yêu cầu pgvector ≥ 0.5.0. Nếu Postgres/pgvector cũ hơn ⇒ index fail. Verify: `SELECT extversion FROM pg_extension WHERE extname='vector'` trước migration (bằng tay, không blocker code).

### Chi tiết Task 2 — ORM models (`db/models.py`)

Append 3 class cuối file (sau `ZaloOAToken` hoặc chỗ có block). Không đụng model cũ.

```python
class AssistantConversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("idx_assistant_conv_user_updated", "user_id", "updated_at",
              postgresql_where=text("deleted_at IS NULL")),
        {"schema": "assistant"},
    )
    conversation_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssistantMessage(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("idx_assistant_msg_conv_created", "conversation_id", "created_at"),
        {"schema": "assistant"},
    )
    message_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("assistant.conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(
        PG_ENUM("user", "assistant", "system", name="msg_role",
                schema="assistant", create_type=False),
        nullable=False,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AssistantUserMemory(Base):
    __tablename__ = "user_memory"
    __table_args__ = (
        Index("idx_assistant_user_memory_user", "user_id", "created_at"),
        # HNSW index viết tay trong migration (autogen không thấy) — không khai ở đây
        # để metadata KHÔNG mâu thuẫn với DB. Precedent: retrieval Embedding cũng không
        # khai vector index ở model.
        {"schema": "assistant"},
    )
    memory_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
```

**⚠️ Tên class prefix `Assistant*`** — tránh clash với `Message`/`Conversation` hiện có (luồng B). `__tablename__` giữ nguyên (`conversations`/`messages`/`user_memory`) vì schema đã tách.

### Chi tiết Task 3 — contract test (mẫu `test_i14_default_privileges.py`)

File **NEW** `tests/contract/test_a12_assistant_schema.py`:

```python
"""D2 · isolation gate hai chiều cho schema `assistant` (Bước 2 Phase 2.1).

Chứng minh D2 (grant WRITE svc_ohana_ai trong assistant) KHÔNG phá I2:
- svc_ohana_ai INSERT/SELECT được assistant.* — D2 mở đường ghi luồng A.
- svc_ohana_ai vẫn DENIED public.* (I2 cho seller data — đã test ở test_i14, ở đây
  test lại cho probe assistant để đảm bảo grant assistant KHÔNG rò sang public).
- svc_seller DENIED assistant.* — luồng B không thấy memory user.
- mcp_readonly DENIED assistant.* — memory user không phải scope MCP.

I14 cho bảng tương lai trong assistant: probe table tạo SAU migration a12 vẫn
được ALTER DEFAULT PRIVILEGES phủ. Đây là song song test_i14_default_privileges
nhưng cho schema mới.
"""

import os
from collections.abc import Iterator

import psycopg
import pytest
from conftest import requires_dsn

pytestmark = requires_dsn

PROBE = "assistant.probe_a12"


@pytest.fixture
def probe_table() -> Iterator[None]:
    """Bảng MỚI trong assistant, tạo bằng ohana_migrator SAU migration a12."""
    with psycopg.connect(os.environ["MIGRATOR_DSN"], autocommit=True) as conn:
        conn.execute(f"CREATE TABLE {PROBE} (id int PRIMARY KEY, note text)")
        try:
            yield
        finally:
            conn.execute(f"DROP TABLE IF EXISTS {PROBE}")


def test_flow_a_can_write_assistant(probe_table: None) -> None:
    """D2 · svc_ohana_ai INSERT/SELECT được assistant.* (bảng mới, thừa kế ALTER
    DEFAULT PRIVILEGES của a12)."""
    with psycopg.connect(os.environ["SVC_A_DSN"], autocommit=True) as conn:
        conn.execute(f"INSERT INTO {PROBE} (id, note) VALUES (1, 'ok')")
        assert conn.execute(f"SELECT id, note FROM {PROBE}").fetchall() == [(1, "ok")]


def test_flow_a_still_denied_public_shop_data() -> None:
    """I2 KHÔNG rò sau D2 · svc_ohana_ai vẫn không đọc được pending_reply (public).
    Nếu test này đỏ ⇒ grant assistant làm hỏng gì đó ở public — phải điều tra ngay."""
    with psycopg.connect(os.environ["SVC_A_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT * FROM pending_reply").fetchall()


def test_flow_b_denied_assistant(probe_table: None) -> None:
    """I2-symmetric · svc_seller không thấy memory user (bảng mới trong assistant)."""
    with psycopg.connect(os.environ["SVC_B_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT * FROM {PROBE}").fetchall()
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"INSERT INTO {PROBE}(id) VALUES (99)")


def test_mcp_denied_assistant(probe_table: None) -> None:
    """MCP scoped cho shop data (public/platform), KHÔNG cho user memory (assistant).
    Nếu design đổi ⇒ grant MCP có chủ đích, KHÔNG mở mặc định."""
    with psycopg.connect(os.environ["MCP_RO_DSN"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(f"SELECT * FROM {PROBE}").fetchall()
```

### Chi tiết Task 4 — extend conftest (chỉ nếu test SAU dùng)

Phase 2.1 test dùng `probe_a12` (tự dọn qua fixture), KHÔNG dùng `wipe_tenant`. **Skip Task 4** nếu không test khác đụng `assistant.*` ở phase này (verify khi chạy suite).

Nếu Phase 2.2+ contract test cần dọn `assistant.conversations/messages/user_memory` → thêm 3 dòng DELETE vào `wipe_tenant` cùng lúc đó.

### FITS (làm nếu trivial)

- Không có.

### OUT (KHÔNG làm — Phase 2.2+)

- Cost/rate-limit/Redis (Phase 2.2).
- Memory service logic (append + recall) (Phase 2.3).
- Chat/conversations/memory routers (Phase 2.4).
- Persona/tools generalize (Phase 2.5).
- Wire `main_ohana_ai.py` — Phase 2.4.
- HNSW params tuning (m/ef_construction) — dùng default pgvector 16/64; đo lại khi có traffic (cùng họ số-chưa-đo ISSUE-022).
- `forgotten_at` cho user_memory — quyết định ở Phase 2.4 (forget endpoint).

---

## §3 · Bất biến chạm (giải thích vì sao vẫn đúng)

| I | Task | Vẫn đúng vì |
|---|---|---|
| **I1** | 1, 2 | Migration + models KHÔNG import luồng B (không đụng `api/webhook`, `api/inbox`, `agent/drafter`, `channels`, `bridge`). `db/models.py` là shared — cả hai luồng đọc, đây là bối cảnh cũ, không đổi hướng phụ thuộc. |
| **I2** | 1 | Grant D2 scoped `assistant.*` — `svc_ohana_ai` vẫn 0 quyền trên `public/*` seller data (`platform/*` vẫn SELECT-only). Test 3 chứng minh (`test_flow_a_still_denied_public_shop_data`). |
| **I14** | 1 | `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA assistant GRANT ... TO svc_ohana_ai` — bảng tương lai trong assistant tự thừa kế. Test 1 chứng minh (`test_flow_a_can_write_assistant` chạy trên probe table tạo SAU migration). |

## §3b · Cấm (nhắc cho batch này)

Chạy Alembic bằng role ≠ `ohana_migrator` · GRANT `mcp_readonly` hoặc `svc_seller` trên `assistant` không có chủ đích · Bỏ `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator` (bẫy I14 sẽ hỏng im lặng) · Autogen migration cho HNSW/GRANT/ENUM · Wire logic Phase 2.2+ (memory/cost/routers) tại đây · Đụng luồng B (bảng public/platform, role svc_seller) · Tạo bảng `assistant.*` ngoài migration a12 (grant sẽ không có).

---

## §4 · Verify / DoD (khuôn báo cáo `ohana-be-coder`)

**Verify tối thiểu:**

```bash
# 1. Migration lên DB dev (bằng ohana_migrator!)
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5433/ohana" alembic upgrade head
# → head = a12_assistant_schema

# 2. Verify schema + grants trên DB dev (python3.11 psycopg)
python3.11 -c "
import psycopg, os
with psycopg.connect(f'postgresql://ohana_migrator:{os.environ[\"MIGRATOR_PW\"]}@localhost:5433/ohana', autocommit=True) as c:
    print('schema:', c.execute(\"SELECT schema_name FROM information_schema.schemata WHERE schema_name='assistant'\").fetchall())
    print('tables:', c.execute(\"SELECT table_name FROM information_schema.tables WHERE table_schema='assistant' ORDER BY table_name\").fetchall())
    print('grants:', c.execute(\"SELECT grantee, privilege_type, table_name FROM information_schema.role_table_grants WHERE table_schema='assistant' ORDER BY grantee, table_name, privilege_type\").fetchall())
    print('hnsw :', c.execute(\"SELECT indexname FROM pg_indexes WHERE tablename='user_memory' AND indexdef LIKE '%%hnsw%%'\").fetchall())
"
# → schema=assistant · 3 tables · grants CHỈ svc_ohana_ai · hnsw idx = idx_assistant_user_memory_hnsw

# 3. Static
ruff check . --no-cache && ruff format --check . --no-cache
mypy app agent retrieval parsing db bridge tools api auth
lint-imports          # Contracts: 4 kept, 0 broken

# 4. Suite Python (5 DSN role như CI)
DATABASE_URL=…ohana_test… pytest -q       # baseline 371 + 4 test a12 = 375 pass

# 5. Contract test riêng (a12)
pytest tests/contract/test_a12_assistant_schema.py -v   # 4/4 pass

# 6. Playwright (không đụng, sanity)
cd web && playwright test                # 5 passed
```

**Gate của bước (bắt buộc xanh):**

- Migration a12 land trên dev + `alembic current` = `a12_assistant_schema (head)`.
- Verify DB trực tiếp: schema `assistant` tồn tại, 3 bảng, grants CHỈ `svc_ohana_ai`, HNSW index tồn tại.
- 4 contract test a12 xanh (A ghi được · A denied public · B denied assistant · MCP denied assistant).
- Suite Python full 375 pass (baseline 371 + 4 mới).
- ruff/mypy/lint-imports xanh, 4 contract imports kept.

**Báo cáo theo khuôn (bắt buộc):**

```
## Đã sửa
<file:line> — <thay đổi gì>

## Bất biến chạm
<I-số> — <vẫn đúng vì...>

## Verify
$ <lệnh đã chạy>
<kết quả THẬT>

## Chưa verify
<những gì chưa chứng minh được>
```

**Checkpoint đạt khi:** OHB-27 → Done · migration a12 head · 4 gate a12 xanh · suite Python + Playwright + ruff/mypy/lint-imports xanh · **KHÔNG** cost/rate-limit/Redis/memory-logic/routers wire tại đây.

→ Đạt = Phase 2.1 done. Chờ Phase 2.2 brief (cost + rate-limit + Redis, D3).

---

## §5 · Chưa quyết (không blocker Phase 2.1, note lại)

- **user_id chiều dài / format** — hiện là `text` không CHECK. Nếu Phase 2.4 (D7) chốt Ohana tự phát ⇒ JWT sub = bigint stringify hay UUID? Không đổi schema nếu vẫn `text`, đo lại nếu cần CHECK regex.
- **HNSW params m/ef_construction** — dùng default 16/64. Đo recall + latency ở Phase 2.3 recall test rồi tune nếu cần.
- **Retention/archive** — assistant.messages append vô hạn per-user. Retention policy defer tới sau khi có traffic đo được (không phải Phase 2.1 concern).
- **PII/scrub** — assistant.messages.content lưu RAW (khớp seller.message.body_raw pattern). Scrub lúc dựng prompt (I3-analog cho luồng A) — logic ở Phase 2.4.
