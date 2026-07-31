"""Shared test fixtures (spec 06 Phase F2 — closes ISSUE-014).

Before this file, every DB test hand-rolled the same six lines: build an async engine, drop
every table, recreate them, wrap a sessionmaker, and remember to dispose at the end. That
duplication is what ISSUE-014 tracked — not an aesthetic complaint: each copy was a place
where someone could forget the dispose (leaked connections) or the drop (a test seeing the
previous test's rows and passing for the wrong reason).

Deliberately NOT done here: mass-refactoring the pre-existing tests onto these fixtures.
`test_inbox_ui_e2e.py` in particular manages its own engine because it also owns a targeted
row-cleanup fixture tied to a SHARED tenant id; rewriting green tests purely for tidiness
risks changing what they assert, and spec 06 §12 forbids that trade. New tests use `fresh_db`;
old tests get migrated only when they're being touched for another reason.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana_test"
)

# Guard chống mất dữ liệu (thêm 2026-07-30, sau sự cố thật): `fresh_db` DROP mọi bảng
# trong `Base.metadata` trên DATABASE_URL. Ở repo cũ biến đó luôn trỏ DB vứt đi; ở đây
# nó từng bị trỏ vào DB dev do alembic + bootstrap_roles quản — một lần `pytest` xoá
# sạch schema `platform` lẫn GRANT của a1/a2. Suite phá hoại theo thiết kế thì DB đích
# phải KHAI là phá được: tên DB kết thúc `_test`, hoặc set OHANA_TESTS_DESTRUCTIVE_OK=1
# (đường thoát cho CI container dùng một lần).
_DB_NAME = DATABASE_URL.rsplit("/", 1)[-1].split("?", 1)[0]
if not _DB_NAME.endswith("_test") and os.environ.get("OHANA_TESTS_DESTRUCTIVE_OK") != "1":
    raise RuntimeError(
        f"DATABASE_URL trỏ vào DB {_DB_NAME!r} — fresh_db sẽ DROP mọi bảng ở đó. "
        "Trỏ vào DB tên *_test, hoặc set OHANA_TESTS_DESTRUCTIVE_OK=1 nếu DB là đồ vứt đi."
    )

_FreshDb = Callable[[], Awaitable[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]]

# ENUM khai `create_type=False` trong models (migration a5 sở hữu type trên DB thật) ⇒
# `create_all` KHÔNG tạo nó — DB test trắng tinh (CI tạo mới mỗi run) nổ UndefinedObject
# ở MỌI fixture setup (đo trên CI 2026-07-30; local lọt vì ohana_test còn type từ các lần
# alembic cũ). Postgres không có CREATE TYPE IF NOT EXISTS cho enum → DO-block nuốt
# duplicate_object. Giá trị chép NGUYÊN VĂN từ migration a5_outbox_trace.
_ENSURE_ENUMS = """
DO $$ BEGIN
    CREATE TYPE outbox_status AS ENUM ('pending','processing','done','dead');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
DO $$ BEGIN
    CREATE TYPE assistant.msg_role AS ENUM ('user','assistant','system');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
"""


@pytest.fixture(scope="session", autouse=True)
def _tables_exist_up_front() -> None:
    """Dựng bảng MỘT lần đầu session — DB test khởi đầu trống (CI tạo mới mỗi run).

    Nhiều test đời đầu tự dựng engine và kỳ vọng bảng ĐÃ CÓ SẴN (ở repo cũ,
    DATABASE_URL trỏ vào DB dev đã migrate nên kỳ vọng đó luôn đúng). Không có fixture
    này, kết quả suite phụ thuộc THỨ TỰ: test dùng `fresh_db` chạy trước thì để lại
    bảng cho test sau — xanh; chạy sau thì 41 test đỏ vì UndefinedTable (đo 2026-07-30).

    Sync engine cho một việc thuần DDL — không kéo event-loop session-scope vào đây.
    """
    from sqlalchemy import create_engine

    from db.models import Base

    engine = create_engine(DATABASE_URL)
    with engine.begin() as conn:
        # DB test có thể mới tạo TRẮNG (CI): extension + schema là việc của migration
        # trên DB thật, ở đây phải tự lo. Cả hai đều idempotent.
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
        conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS platform")
        # Tầng 2 · a12 · schema mới cho Ohana AI Assistant; ENUM phải tạo TRƯỚC
        # `_ENSURE_ENUMS` chạy (nó qualify `assistant.msg_role`).
        conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS assistant")
        conn.exec_driver_sql(_ENSURE_ENUMS)
        Base.metadata.create_all(conn)
    engine.dispose()


@pytest.fixture
async def fresh_db() -> AsyncIterator[_FreshDb]:
    """Yield a factory that hands back `(engine, session_factory)` over a CLEAN schema.

    Clean means every table in `Base.metadata` is dropped and recreated, so a test can never
    pass because of a row another test left behind. Engines are disposed on teardown even if
    the test raises — the old hand-rolled `await engine.dispose()` at the end of each test
    was skipped entirely whenever an assertion failed mid-test, leaking a pool per failure.

    Schema comes from `Base.metadata.create_all`, NOT from Alembic. That is intentional for
    speed, and it means these fixtures do NOT prove the migrations are correct — migration
    correctness has its own gate (`alembic upgrade head && downgrade -1 && upgrade head` in
    spec 06 F0's GATE_FULL). Do not conflate the two.
    """
    from db.models import Base

    created: list[AsyncEngine] = []

    async def _make() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
        engine = create_async_engine(DATABASE_URL, echo=False)
        async with engine.begin() as conn:
            # a1 chuyển `embeddings` sang schema `platform`; `create_all` KHÔNG tự tạo
            # schema (đó là việc của migration a1 trên DB thật — DB test không chạy alembic).
            await conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS platform")
            # Tầng 2 · a12 · schema cho Ohana AI Assistant; ENUM `assistant.msg_role`
            # qualify schema này, phải tạo trước `_ENSURE_ENUMS`.
            await conn.exec_driver_sql("CREATE SCHEMA IF NOT EXISTS assistant")
            # Type có thể vừa bị test_embedding_dim downgrade-from-zero xoá — đảm bảo lại
            # mỗi lượt dựng (idempotent, xem _ENSURE_ENUMS).
            await conn.exec_driver_sql(_ENSURE_ENUMS)
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        created.append(engine)
        return engine, async_sessionmaker(engine, expire_on_commit=False)

    yield _make

    for engine in created:
        await engine.dispose()
