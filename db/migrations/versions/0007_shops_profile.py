"""shops + shop_profile — bảng cha đầu tiên của shop_id

Spec 11 Phase S0. Trước migration này `shop_id` là Text trần ở mọi bảng và không FK về đâu:
một JWT hợp lệ mang `shop_id` là chuỗi BẤT KỲ và mọi tầng dưới đều tin. Composite FK của
spec 06/10 chặn được row shop A trỏ row shop B, nhưng KHÔNG chặn được một shop chưa từng
tồn tại — vì chưa có bảng cha nào để tham chiếu.

**PRE-1104 đã đo (2026-07-20, Postgres thật):** `messages` / `conversations` / `customers` /
`pending_reply` đều 0 row ⇒ không cần backfill, hai bảng tạo rỗng.

⚠️ **KHÔNG được thêm FK `embeddings.shop_id` → `shops.id`.** Nghe rất hợp lý ("mọi shop_id
đều nên trỏ về shops") và sẽ hỏng ngay: `parsing/ingest.py` dùng sentinel `_platform` làm
scope cho corpus dùng chung của Ohana AI, mà `_platform` KHÔNG BAO GIỜ là một row `shops`.
Muốn FK đó thì phải quyết trước: tạo row `shops` giả cho sentinel (bẩn — một "shop" không
phải shop lọt vào mọi câu đếm), hay tách corpus nền tảng sang bảng riêng. Migration này
KHÔNG đụng `embeddings`.

**Reversible thật cả schema lẫn dữ liệu** — hai bảng mới, chưa có row nào, `downgrade` drop
sạch. Khác `0004` (xoá row) và `0006` (drop cột ⇒ mất liên kết). Nhưng khi đã có shop thật
thì `downgrade` sẽ xoá toàn bộ persona + knowledge: reversible về SCHEMA, không về DỮ LIỆU.

`persona_md` mang CHECK constraint ≤ 2000 ký tự (PRE-1101, Wyatt ký). Cap sống ở DB chứ
không chỉ ở Pydantic vì Pydantic bảo vệ đường ứng dụng còn CHECK bảo vệ mọi đường còn lại
(psql tay, script seed, data-fix). Ngân sách token là ràng buộc của hệ thống — nó không nên
phụ thuộc việc người ghi có nhớ dùng repo hay không.

`published_at NULL` = chưa phát hành (PRE-1102). Cố ý KHÔNG có `profile_status`/`approved_by`:
chưa có người duyệt thứ hai nào tồn tại, nên cột tên "approved" sẽ dựng tên cho một quy trình
không có thật.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_PERSONA_MAX_CHARS = 2000


def upgrade() -> None:
    op.execute("""
        CREATE TABLE shops (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'active',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    # `shop_id` vừa PK vừa FK ⇒ đúng MỘT profile mỗi shop. Versioning về sau là bảng KHÁC,
    # không phải nới PK này: hai profile "đang hoạt động" cho một shop nghĩa là không ai
    # biết AI đang nói bằng giọng nào.
    op.execute(f"""
        CREATE TABLE shop_profile (
            shop_id       TEXT PRIMARY KEY
                          REFERENCES shops (id) ON DELETE CASCADE,
            persona_md    TEXT NOT NULL DEFAULT '',
            knowledge     JSONB NOT NULL DEFAULT '{{}}'::jsonb,
            published_at  TIMESTAMPTZ NULL,
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_shop_profile_persona_len
                CHECK (char_length(persona_md) <= {_PERSONA_MAX_CHARS})
        )
    """)


def downgrade() -> None:
    # ⚠️ Mất dữ liệu khi đã có shop thật: toàn bộ persona + knowledge biến mất.
    op.execute("DROP TABLE IF EXISTS shop_profile")
    op.execute("DROP TABLE IF EXISTS shops")
