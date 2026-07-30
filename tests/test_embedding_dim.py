"""E1 gate (spec `08-Task-OhanaAISeller-EmbedderSwap-E5.md` §7 Phase E1).

Viết TRƯỚC khi đổi `_EMBED_DIM` và trước khi có `0004_embedding_dim_1024.py` — expected RED.

Khoá 4 assertion (a)–(d) của spec §7 bước 6. Vì sao từng cái tồn tại:

(a) **Cột phải là `Vector(1024)` SAU KHI MIGRATE, đọc từ Postgres.** Không đọc từ
    `db/models.py` — đọc model rồi assert model là tautology. Nguồn sự thật ở đây là
    `pg_attribute.atttypmod` của database thật.

(b) **Chèn vector 1536 vào cột 1024 phải RAISE.** Đây là assertion đắt nhất và là toàn bộ
    lý do dim mismatch KHÔNG thuộc lớp "hỏng âm thầm": Postgres từ chối ồn ào. Nếu ngày nào
    đó cột thành `Vector` không ràng buộc chiều, test này đỏ — đúng lúc cần đỏ, vì khi đó
    corpus lẫn lộn hai không gian vector mà không ai thấy.

(c) **up → down → up sạch.** Migration destructive vẫn phải reversible VỀ SCHEMA. Ghi rõ:
    down KHÔNG khôi phục dữ liệu (PRE-E04, Wyatt ký XOÁ) — test này chỉ chứng minh schema
    quay về được, không chứng minh dữ liệu quay về được. Đừng đọc nó rộng hơn thế.

(d) **`db/models._EMBED_DIM` khớp `app.config.EMBED_DIM`.** Hai nguồn sự thật về số chiều là
    cách chắc chắn nhất để một ngày nào đó chúng lệch nhau. Test rẻ, chặn được lớp lỗi đắt.

Cần Postgres thật (`DATABASE_URL`) — giống `test_wiki_rag.py` / `test_tenant_isolation.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine

_REPO_ROOT = Path(__file__).resolve().parents[1]

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana"
)

EXPECTED_DIM = 1024


# `downgrade -1` KHÔNG dùng được ở đây. Nó nghĩa là "lùi MỘT bước từ head", nên nó chỉ trỏ
# đúng vào `0004` chừng nào `0004` CÒN là head — và điều đó hết đúng ngay khi spec 09 thêm
# `0005`. Test này nói về migration `0004`, nên nó phải GỌI TÊN `0004`, không phải mô tả vị
# trí tương đối của nó. (Lỗi thật: thêm `0005` làm 2 test ở file này đỏ mà không dòng nào của
# spec 08 sai.)
_DOWN_TARGET = "0003"  # ngay TRƯỚC 0004 — cố định, không trôi theo head


@pytest.fixture(scope="module", autouse=True)
def _alembic_owns_this_db() -> None:
    """Gate migration cần DB mà ALEMBIC là nguồn sự thật DUY NHẤT.

    Từ khi suite trỏ vào `ohana_test` (DB không chạy alembic), các file khác dựng bảng
    bằng `create_all` — leftovers đó làm `upgrade head` từ zero nổ DuplicateTable ngay
    migration đầu. Reset về trống trước module này; sau module DB ở trạng thái migrated
    head, các test dùng `fresh_db` tự drop/create nên không bị ảnh hưởng.

    Sync + psycopg trần (không async fixture): tránh phụ thuộc scope/event-loop của
    pytest-asyncio cho một việc thuần DDL.
    """
    import psycopg

    from db.models import Base

    dsn = DATABASE_URL.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS platform CASCADE")
        # Đảo thứ tự sorted_tables để con (FK) rơi trước cha; CASCADE đỡ phần còn lại.
        # Drop theo TÊN TRẦN cho mọi bảng, kể cả bảng metadata khai ở schema khác:
        # sau `downgrade 0003`, `embeddings` sống ở PUBLIC (a1 chưa move) — leftover đó
        # không nằm trong metadata dưới tên public nên vòng lặp chỉ-schema-None sẽ sót,
        # và alembic-from-zero nổ DuplicateTable ở 0003 (đo 2026-07-30).
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(f'DROP TABLE IF EXISTS "{table.name}" CASCADE')
        conn.execute("DROP TABLE IF EXISTS alembic_version")


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Chạy alembic trong tiến trình con.

    Tiến trình con chứ không phải `alembic.command` in-process: alembic nạp `env.py` và cấu
    hình logging ở cấp module, chạy nhiều lần trong một tiến trình pytest sẽ dính state thừa
    từ lần trước — nghĩa là up→down→up có thể xanh vì lý do sai.
    """
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


async def _column_dim(url: str) -> int | None:
    """Số chiều THẬT của `embeddings.embedding`, hỏi thẳng Postgres.

    pgvector lưu chiều trong `atttypmod` (không trừ 4 như varchar — pgvector dùng thẳng).
    Trả None nếu cột/bảng chưa tồn tại.
    """
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return (
                await conn.execute(
                    sa.text(
                        "select a.atttypmod from pg_attribute a "
                        "join pg_class t on a.attrelid = t.oid "
                        "where t.relname = 'embeddings' and a.attname = 'embedding'"
                    )
                )
            ).scalar()
    finally:
        await engine.dispose()


# --- (d) một nguồn sự thật — không cần DB, chạy nhanh, đỏ trước tiên ----------------


def test_models_dim_matches_config_dim() -> None:
    """(d) `db/models._EMBED_DIM` và `app.config.EMBED_DIM` phải là CÙNG một số.

    Hai hằng số độc lập cùng mô tả một thực tế vật lý (số cột trong Postgres) là cách chắc
    chắn nhất để một ngày chúng lệch nhau — và khi lệch, triệu chứng là insert bị từ chối ở
    một đường code, còn đường khác vẫn chạy.
    """
    from app.config import EMBED_DIM
    from db.models import _EMBED_DIM

    assert _EMBED_DIM == EMBED_DIM, (
        f"db/models._EMBED_DIM={_EMBED_DIM} nhưng app.config.EMBED_DIM={EMBED_DIM} — "
        "hai nguồn sự thật về số chiều"
    )


def test_embed_dim_is_1024() -> None:
    """(d) Số đó là 1024 — đo được ở E0 bằng gọi e5 THẬT (`docs/smokes/08-E0.md`), không đọc doc."""
    from app.config import EMBED_DIM

    assert EMBED_DIM == EXPECTED_DIM


# --- (a) cột thật trong Postgres ---------------------------------------------------


@pytest.mark.asyncio
async def test_column_is_vector_1024_after_migrate() -> None:
    """(a) Sau `alembic upgrade head`, cột `embeddings.embedding` là `Vector(1024)`.

    Hỏi Postgres, KHÔNG hỏi `db/models.py` — đọc model rồi assert model không chứng minh gì
    về database đang chạy.
    """
    r = _alembic("upgrade", "head")
    assert r.returncode == 0, f"alembic upgrade head lỗi:\n{r.stderr}"

    assert await _column_dim(DATABASE_URL) == EXPECTED_DIM


# --- (b) dim mismatch phải NỔ, không được lọt âm thầm -------------------------------


@pytest.mark.asyncio
async def test_inserting_1536_vector_is_rejected_by_postgres() -> None:
    """(b) Chèn vector 1536 vào cột 1024 ⇒ Postgres RAISE.

    Đây là thứ khiến dim mismatch KHÔNG thuộc lớp "hỏng âm thầm" — khác hẳn prefix bất đối
    xứng ở E0, vốn không có lỗi nào cả. Nếu ngày nào cột mất ràng buộc chiều, test này đỏ:
    lúc đó corpus sẽ lẫn hai không gian vector mà không ai thấy triệu chứng.
    """
    r = _alembic("upgrade", "head")
    assert r.returncode == 0, f"alembic upgrade head lỗi:\n{r.stderr}"

    engine = create_async_engine(DATABASE_URL)
    try:
        with pytest.raises(Exception) as ei:  # noqa: B017 — driver có thể gói nhiều lớp
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "insert into platform.embeddings (shop_id, namespace, chunk, embedding)"
                        " values ('_platform', 'dimtest', 'c', :v)"
                    ),
                    {"v": "[" + ",".join(["0.1"] * 1536) + "]"},
                )
        msg = str(ei.value).lower()
        assert "dimension" in msg or "expected" in msg, (
            f"Postgres có raise nhưng không phải vì sai chiều: {ei.value!r}"
        )
    finally:
        await engine.dispose()


# --- (c) reversible VỀ SCHEMA (không phải về dữ liệu) -------------------------------


@pytest.mark.asyncio
async def test_migration_up_down_up_is_clean() -> None:
    """(c) `upgrade head` → `downgrade -1` → `upgrade head` chạy sạch, cột về đúng 1024.

    **Phạm vi hẹp, đọc đúng:** test này chứng minh SCHEMA quay về được. Nó KHÔNG chứng minh
    dữ liệu quay về được — down-migration cũng `DELETE FROM embeddings` (PRE-E04, Wyatt ký
    XOÁ 2026-07-19). Reversible về schema, KHÔNG reversible về dữ liệu; ghi thẳng ra đây để
    người sau không đọc màu xanh của test này thành "migration an toàn, cứ rollback thoải mái".
    """
    r = _alembic("upgrade", "head")
    assert r.returncode == 0, f"upgrade head lỗi:\n{r.stderr}"
    assert await _column_dim(DATABASE_URL) == EXPECTED_DIM

    r = _alembic("downgrade", _DOWN_TARGET)
    assert r.returncode == 0, f"downgrade -1 lỗi:\n{r.stderr}"
    assert await _column_dim(DATABASE_URL) == 1536, "down phải trả cột về 1536"

    r = _alembic("upgrade", "head")
    assert r.returncode == 0, f"upgrade head (lần 2) lỗi:\n{r.stderr}"
    assert await _column_dim(DATABASE_URL) == EXPECTED_DIM


# --- guard cơ học: migration TỪ CHỐI xoá khi dữ liệu vượt ngưỡng --------------------


@pytest.mark.asyncio
async def test_migration_refuses_when_rows_exceed_safe_threshold() -> None:
    """Migration RAISE thay vì xoá khi bảng có nhiều hơn `_SAFE_ROW_THRESHOLD` row.

    Vì sao test này tồn tại: bản đầu của `0004` chỉ có docstring cảnh báo "khi corpus thật land,
    chạy lại cái này là mất corpus" — và không dòng code nào chặn. Reviewer bắt được. Đó đúng là
    hình dạng đã hỏng ở chỗ khác trong cùng spec: `_DEV_EMBED_DIM = 1536  # must match db.models`
    lệch ngay khi cột đổi, vì comment không phải cơ chế.

    Ca mô phỏng: corpus thật đã land (ở đây giả lập bằng > ngưỡng row), ai đó chạy `downgrade`.
    Kỳ vọng: alembic THẤT BẠI, dữ liệu CÒN NGUYÊN.
    """

    r = _alembic("upgrade", "head")
    assert r.returncode == 0, f"upgrade head lỗi:\n{r.stderr}"

    engine = create_async_engine(DATABASE_URL)
    try:
        async with engine.begin() as conn:
            for i in range(12):  # > _SAFE_ROW_THRESHOLD (10)
                await conn.execute(
                    sa.text(
                        "insert into platform.embeddings (shop_id, namespace, chunk, embedding)"
                        " values ('_platform', 'guardtest', :c, :v)"
                    ),
                    {"c": f"chunk {i}", "v": "[" + ",".join(["0.1"] * EXPECTED_DIM) + "]"},
                )

        r = _alembic("downgrade", _DOWN_TARGET)
        assert r.returncode != 0, (
            "migration PHẢI từ chối khi vượt ngưỡng — nó đã chạy và xoá dữ liệu:\n" + r.stdout
        )
        assert "TỪ CHỐI CHẠY" in (r.stderr + r.stdout), (
            f"thất bại nhưng KHÔNG phải vì guard — lý do khác:\n{r.stderr[-600:]}"
        )

        async with engine.connect() as conn:
            still = (
                await conn.execute(sa.text("select count(*) from platform.embeddings"))
            ).scalar()
        assert still == 12, f"guard raise nhưng dữ liệu vẫn mất: còn {still}/12 row"

        # Guard là PHANH, không phải TƯỜNG: có override tường minh thì phải chạy được. Không
        # kiểm vế này thì guard có thể là một bức tường chặn cả use-case hợp lệ, và người ta sẽ
        # gỡ nó ra thay vì dùng đúng — kết cục tệ hơn không có guard.
        env = {**os.environ, "OHANA_MIGRATION_ALLOW_EMBEDDING_DATA_LOSS": "1"}
        r = subprocess.run(  # noqa: S603
            [sys.executable, "-m", "alembic", "downgrade", _DOWN_TARGET],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            env=env,
        )
        assert r.returncode == 0, f"có override mà vẫn chặn — guard thành tường:\n{r.stderr[-600:]}"
        assert await _column_dim(DATABASE_URL) == 1536
    finally:
        # Restore head TRƯỚC khi dọn row: nhánh override kết thúc ở 0003, nơi schema
        # `platform` KHÔNG tồn tại — delete platform.embeddings lúc đó tự nổ và nuốt
        # luôn bước upgrade phục hồi (chuỗi lỗi thật 2026-07-30, để lại DB kẹt ở 0003
        # + bảng public.embeddings mồ côi cho test kế tiếp giẫm phải).
        _alembic("upgrade", "head")
        async with engine.begin() as conn:
            await conn.execute(
                sa.text("delete from platform.embeddings where namespace='guardtest'")
            )
        await engine.dispose()
