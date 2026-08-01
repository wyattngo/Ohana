"""A15 — FTS index cho R3 search endpoint (ADR round2).

**GIN index trên `to_tsvector('simple', content)` cho `assistant.messages`.** `simple` config
(không stem tiếng Việt) đủ MVP — hầu hết search là substring exact hoặc từ khoá riêng lẻ.
Nâng cấp analyzer Vietnamese (pg_search / pgroonga / custom dictionary) là v2, revisit khi
có measurement chứng minh cần recall cao hơn.

**KHÔNG index title** — title cap 200 char, thường ít conversation per user (< 100), ILIKE
đủ nhanh không cần GIN. Ít index = ít write overhead trên hot path append_pair.

**IMMUTABLE cast cần thiết** — `to_tsvector('simple', content)` là IMMUTABLE khi config đưa
làm literal ('simple'), không cast dynamic. Kiểu này Postgres index expression accept được.

**BẮT BUỘC chạy role `ohana_migrator`** (bẫy Alembic — I14):

    DATABASE_URL="postgresql+psycopg://ohana_migrator:PW@localhost:5433/ohana" \\
        alembic upgrade head

**Downgrade DROP INDEX** — an toàn tuyệt đối (không data loss, chỉ mất search perf).

Revision ID: a15_assistant_search_index
Revises: a14_assistant_feedback
"""

from __future__ import annotations

from alembic import op

revision = "a15_assistant_search_index"
down_revision = "a14_assistant_feedback"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # GIN cho FTS query `to_tsvector('simple', content) @@ plainto_tsquery('simple', :q)`.
    # `simple` immutable ⇒ index expression OK. Không cần STORED column vì bảng nhỏ
    # (< 1M messages seed hiện tại; nếu > 10M cân nhắc materialized tsvector column).
    op.execute(
        "CREATE INDEX idx_assistant_messages_body_fts "
        "ON assistant.messages USING GIN (to_tsvector('simple', content))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS assistant.idx_assistant_messages_body_fts")
