"""embeddings.embedding: Vector(1536) -> Vector(1024) for Together e5

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-19

Spec 08 Phase E1. ADR PRE-007 chuyển embedding provider từ OpenAI `text-embedding-3-small`
(1536) sang Together `intfloat/multilingual-e5-large-instruct` (1024).

⚠️ MIGRATION NÀY XOÁ DỮ LIỆU — CÓ CHỦ Ý, KHÔNG PHẢI SƠ SUẤT.

Vì sao không convert mà phải xoá: 1536 → 1024 KHÔNG phải phép chiếu. Hai model sinh vector
trong hai không gian khác nhau; không có phép biến đổi nào mang nghĩa của vector cũ sang
không gian mới. Cắt bớt 512 chiều, pad số 0, hay chiếu tuyến tính đều cho ra vector *hợp lệ
về kiểu* nhưng *vô nghĩa về ngữ nghĩa* — và đó là kiểu hỏng tệ nhất ở tầng retrieval: không
lỗi, không stack trace, chỉ là chunk trả về sai và AI trả lời khách bằng căn cứ sai. Xoá rồi
re-embed là con đường DUY NHẤT đúng. Xoá ồn ào tốt hơn giữ lại một cách vô nghĩa.

PRE-E04 (Wyatt ký 2026-07-19, spec 08 §7): XOÁ. Verify sống ngay trước khi ký — bảng có đúng
2 row (`_platform` 1, `shop_a` 1), cả hai là test fixture. Corpus thật chưa land (PRE-003).
Làm swap ở thời điểm này rẻ nhất trong toàn bộ vòng đời dự án.

⚠️ CHỮ KÝ ĐÓ GẮN VỚI THỜI ĐIỂM, KHÔNG VĨNH VIỄN. Khi corpus thật đã land, chạy lại migration
này (hoặc `downgrade`) = MẤT CORPUS. Lúc đó phải có bước re-embed TRƯỚC.

Và vì cảnh báo bằng chữ KHÔNG chặn được ai (cả spec 08 lẫn session viết nó đã trả giá đúng
bài học này nhiều lần), migration TỰ CHẶN bằng code: quá `_SAFE_ROW_THRESHOLD` row thì nó
raise và từ chối chạy, trừ khi có env override tường minh. Xem `_wipe_and_alter()`.

`downgrade()` CŨNG xoá: reversible về SCHEMA, KHÔNG reversible về DỮ LIỆU. Nói thẳng thay vì
giả vờ đối xứng — một `downgrade` trông có vẻ khôi phục được là thứ khiến người ta yên tâm
rollback rồi mới phát hiện dữ liệu đã đi.

Không dùng `USING` cast trong `ALTER TYPE`: Postgres sẽ vui vẻ nhận `embedding::vector(1024)`
trên cột rỗng, nhưng nếu cột KHÔNG rỗng thì nó fail giữa chừng với thông báo khó đọc. Xoá
tường minh trước, rồi ALTER trên bảng rỗng — một bước một việc, lỗi nào cũng đọc được.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_DIM = 1536
_NEW_DIM = 1024

# Ngưỡng an toàn: trên số này, migration TỪ CHỐI CHẠY thay vì xoá.
#
# Vì sao có, dù docstring ở trên đã cảnh báo dài: docstring là lời nhắc cho người, và cả
# session này đã chứng minh lời nhắc cho người không giữ được lời hứa — `_DEV_EMBED_DIM = 1536`
# kèm comment "must match db.models" vẫn lệch ngay khi cột đổi. PRE-E02 ("nếu > 100 row ⇒ STOP")
# cũng chỉ là bước pre-flight đọc bằng mắt, không phải code.
#
# Ca thật cần chặn: PRE-003 land corpus thật, rồi ai đó chạy `alembic downgrade -1` để rollback
# một thứ khác. Không có guard thì corpus đi, và họ chỉ biết sau khi đọc log.
#
# 10 chứ không phải 100: test fixture thực tế là 2 row. Ngưỡng nên sát mức "chỉ có dữ liệu
# test" — mọi thứ trên đó đáng để một con người nhìn lại.
_SAFE_ROW_THRESHOLD = 10

# Env override — cố ý DÀI và khó gõ nhầm. Đây là công tắc "tôi biết mình đang xoá gì".
_OVERRIDE_ENV = "OHANA_MIGRATION_ALLOW_EMBEDDING_DATA_LOSS"


def _wipe_and_alter(target_dim: int) -> None:
    """Xoá sạch `embeddings` rồi đổi chiều cột. Dùng chung cho up và down — cả hai đều xoá.

    Log số row bị xoá vào output alembic: người chạy migration phải THẤY con số đó, không
    phải suy ra. Nếu một ngày ai chạy nhầm lúc corpus thật đã land, dòng log này là thứ duy
    nhất phân biệt "xoá 2 row test" với "vừa xoá cả corpus".
    """
    conn = op.get_bind()
    n = conn.execute(sa.text("select count(*) from embeddings")).scalar() or 0

    if n > _SAFE_ROW_THRESHOLD and not os.environ.get(_OVERRIDE_ENV):
        raise RuntimeError(
            f"[0004] TỪ CHỐI CHẠY: bảng embeddings có {n} row (ngưỡng an toàn "
            f"{_SAFE_ROW_THRESHOLD}). Migration này XOÁ TOÀN BỘ vector và KHÔNG khôi phục "
            f"được — 1536 và 1024 là hai không gian khác nhau, không có phép chiếu.\n"
            f"\n"
            f"Nếu đây là corpus thật: DỪNG. Cần re-embed, không phải xoá. Xem spec 08 §8.\n"
            f"Nếu {n} row này thật sự bỏ được, chạy lại với:\n"
            f"    {_OVERRIDE_ENV}=1 alembic <lệnh>\n"
        )
    # Câu log cố ý KHÔNG chứa chữ "DELETE FROM": heuristic S608 của ruff bắt theo từ khoá SQL
    # trong f-string và sẽ báo nhầm câu print này là SQL injection. Đổi chữ rẻ hơn thêm một
    # dòng chặn lint — dòng chặn sẽ dạy người đọc sau rằng S608 ở file migration là nhiễu.
    print(f"[0004] xoá {n} row khỏi bảng embeddings — KHÔNG khôi phục được")

    conn.execute(sa.text("delete from embeddings"))
    # f-string dựng SQL, nhưng `target_dim` là hằng số module (`_NEW_DIM`/`_OLD_DIM`), không
    # phải input ngoài — không có đường nào cho dữ liệu người dùng chạm vào đây. Ghi ra vì
    # ruff KHÔNG bắt dòng này (nó bắt câu print ở trên); đừng đọc sự im lặng của linter thành
    # lời bảo đảm.
    op.execute(f"alter table embeddings alter column embedding type vector({target_dim})")


def upgrade() -> None:
    """1536 → 1024. Xoá toàn bộ vector cũ (xem docstring module)."""
    _wipe_and_alter(_NEW_DIM)


def downgrade() -> None:
    """1024 → 1536. **CŨNG XOÁ** — schema quay về được, dữ liệu thì không."""
    _wipe_and_alter(_OLD_DIM)
