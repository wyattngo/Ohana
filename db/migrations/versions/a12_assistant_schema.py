"""A12 — schema `assistant` + isolation gate cho Tầng 2 (OHB-27 · Phase 2.1).

Chunk code đầu Tầng 2. Reconcile ADR D2 vào bất biến I1/I2/I14 của repo:

- **D2**: mở WRITE `svc_ohana_ai` CHỈ trong schema `assistant` (public/platform giữ
  nguyên grants — I2 cho seller data không đổi).
- **I14 áp cho schema mới**: `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN
  SCHEMA assistant GRANT ... TO svc_ohana_ai` — bảng tương lai trong `assistant`
  (memory extension, cost_budget per-user tương lai...) tự thừa kế grants, không
  cần migration phụ, không hỏng im lặng ở bảng thứ N+1.
- **KHÔNG grant svc_seller/mcp_readonly**: user memory là data nhạy cảm; nếu cần
  cross-cutting (MCP debug, seller-view memory) thì grant có chủ đích ở migration
  riêng, KHÔNG mặc định.

**BẮT BUỘC chạy role `ohana_migrator`** (bẫy Alembic của skill ohana-be-coder —
ALTER DEFAULT PRIVILEGES FOR ROLE X chỉ phủ bảng do role X tạo, chạy bằng owner
là mọi bảng sau đó rơi ra ngoài I14 âm thầm):

    DATABASE_URL="postgresql+psycopg://ohana_migrator:PW@localhost:5433/ohana" \\
        alembic upgrade head

Autogen KHÔNG thấy: schema, ENUM, HNSW, GRANT, ALTER DEFAULT PRIVILEGES — mọi
statement dưới đây viết tay bằng `op.execute()`, review bằng mắt.

pgvector extension đã enabled (đường Embedding luồng B đang chạy) — KHÔNG cần
`CREATE EXTENSION vector`. HNSW yêu cầu pgvector ≥ 0.5.0.

Gate: tests/contract/test_a12_assistant_schema.py (4 test isolation hai chiều).

Revision ID: a12_assistant_schema
Revises: a11_pending_sent_claim
"""

from __future__ import annotations

from alembic import op

revision = "a12_assistant_schema"
down_revision = "a11_pending_sent_claim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── (1) Schema mới ──────────────────────────────────────────────────────────
    # `IF NOT EXISTS` để re-run downgrade→upgrade trong test không nổ ở CI (schema
    # có thể tồn tại từ lần chạy trước nếu downgrade sót).
    op.execute("CREATE SCHEMA IF NOT EXISTS assistant")

    # ── (2) ENUM msg_role ───────────────────────────────────────────────────────
    # Trong schema `assistant` (không phải `public`) để tránh clash với ENUM cùng
    # tên khả dĩ ở luồng B tương lai. IF NOT EXISTS cho cùng lý do (1).
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE assistant.msg_role AS ENUM ('user','assistant','system'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
    )

    # ── (3) Bảng conversations ──────────────────────────────────────────────────
    # `user_id text`: khớp `auth/identity.py::Identity.user_id: str` (JWT sub).
    # KHÔNG bigint vì D7 còn open — ai issue JWT (Ohana tự phát vs ONFA) cho ra
    # format khác nhau. `text` là trung dung.
    # Soft-delete (Phase 2.4 CRUD): `deleted_at` NULL = row sống. Partial index
    # bên dưới scope theo predicate đó, list-recent không quét row đã xoá.
    op.execute(
        "CREATE TABLE assistant.conversations ("
        "  conversation_id  bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,"
        "  user_id          text        NOT NULL,"
        "  title            text,"
        "  created_at       timestamptz NOT NULL DEFAULT now(),"
        "  updated_at       timestamptz NOT NULL DEFAULT now(),"
        "  deleted_at       timestamptz"
        ")"
    )
    op.execute(
        "CREATE INDEX idx_assistant_conv_user_updated "
        "ON assistant.conversations (user_id, updated_at DESC) "
        "WHERE deleted_at IS NULL"
    )

    # ── (4) Bảng messages ───────────────────────────────────────────────────────
    # `user_id text` redundant với `conversations.user_id`: (a) lock scope repo
    # layer đọc last-N (khớp pattern Ohana `MessageRepo(shop_scope=...)`); (b)
    # chống query lỡ mất WHERE user_id ⇒ leak cross-user memory.
    # `content text` RAW — khớp seller.message.body_raw pattern §5.5. Scrub lúc
    # dựng prompt ở Phase 2.4 (I3-analog cho luồng A).
    # ON DELETE CASCADE: xoá conversation ⇒ xoá messages (soft-delete Phase 2.4
    # sẽ NULL deleted_at trên conversations, còn hard-delete từ Phase 2.4 forget-
    # endpoint sẽ dọn messages theo — CASCADE đảm bảo không có orphan).
    op.execute(
        "CREATE TABLE assistant.messages ("
        "  message_id       bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,"
        "  conversation_id  bigint NOT NULL REFERENCES assistant.conversations(conversation_id) "
        "                   ON DELETE CASCADE,"
        "  user_id          text        NOT NULL,"
        "  role             assistant.msg_role NOT NULL,"
        "  content          text        NOT NULL,"
        "  created_at       timestamptz NOT NULL DEFAULT now()"
        ")"
    )
    op.execute(
        "CREATE INDEX idx_assistant_msg_conv_created "
        "ON assistant.messages (conversation_id, created_at)"
    )

    # ── (5) Bảng user_memory ────────────────────────────────────────────────────
    # Append-only tại Phase 2.1. `forget` endpoint (Phase 2.4) quyết DELETE thật
    # hay thêm `forgotten_at` — không giả sử ở đây.
    # `embedding vector(1024)` khớp EMBED_DIM=1024 (e5 embedder hiện có). HNSW
    # cho recall per-user cao volume (Phase 2.3 gọi mỗi câu chat). Default params
    # pgvector: m=16, ef_construction=64 (WITH clause bỏ ⇒ dùng default). Đo lại
    # ở Phase 2.3 recall test rồi tune nếu cần — cùng họ số-chưa-đo ISSUE-022.
    op.execute(
        "CREATE TABLE assistant.user_memory ("
        "  memory_id   bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,"
        "  user_id     text         NOT NULL,"
        "  content     text         NOT NULL,"
        "  embedding   vector(1024) NOT NULL,"
        "  created_at  timestamptz  NOT NULL DEFAULT now()"
        ")"
    )
    # HNSW index. `vector_cosine_ops` khớp retrieval/pgvector.py (dùng
    # `cosine_distance` cho Embedding). Không đi cùng btree(user_id) trong HNSW —
    # filter bởi WHERE user_id = ? qua `idx_assistant_user_memory_user` dưới.
    op.execute(
        "CREATE INDEX idx_assistant_user_memory_hnsw "
        "ON assistant.user_memory USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX idx_assistant_user_memory_user "
        "ON assistant.user_memory (user_id, created_at DESC)"
    )

    # ── (6) Grants — D2 · svc_ohana_ai FULL trong assistant ─────────────────────
    # KHÔNG grant svc_seller, KHÔNG grant mcp_readonly. Test contract a12 chứng
    # minh isolation hai chiều.
    op.execute("GRANT USAGE ON SCHEMA assistant TO svc_ohana_ai")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA assistant TO svc_ohana_ai"
    )
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA assistant TO svc_ohana_ai")
    op.execute("GRANT USAGE ON TYPE assistant.msg_role TO svc_ohana_ai")

    # ── (7) I14 · BẢNG TƯƠNG LAI trong assistant ────────────────────────────────
    # `ALL TABLES/SEQUENCES` ở (6) chỉ phủ 3 bảng đang tồn tại. Bảng thứ N+1
    # (memory extension, cost per-user, telemetry per-user...) tạo bằng
    # ohana_migrator trong assistant sẽ tự thừa kế nhờ khối này. Không có nó,
    # I14 hỏng im lặng cho schema mới — cùng bài a1 khai.
    for statement in (
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA assistant "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO svc_ohana_ai",
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA assistant "
        "GRANT USAGE ON SEQUENCES TO svc_ohana_ai",
    ):
        op.execute(statement)


def downgrade() -> None:
    # DROP SCHEMA CASCADE dọn cả 3 bảng, 2 index HNSW/btree, ENUM msg_role, và
    # mọi ALTER DEFAULT PRIVILEGES scoped tới schema này.
    # Role sống ở cấp cluster — gỡ bằng bootstrap_roles, không phải ở đây.
    op.execute("DROP SCHEMA IF EXISTS assistant CASCADE")
