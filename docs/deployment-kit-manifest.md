# Ohana BE — gói triển khai

Đích đến: repo `ohana-ai`. Mọi file dưới đây copy vào đúng path tương ứng.

## Nội dung

| File | Đặt ở | Việc |
|---|---|---|
| `docs/ohana-be-design.md` | `docs/` | Hợp đồng kỹ thuật — 16 bất biến, schema đích, 10 câu SQL |
| `docs/adopt-plan.md` | `docs/` | Đã có gì · thiếu gì · thứ tự A1–A8 |
| `SETUP.md` | gốc repo | Quy trình §0–§13 |
| `.claude/skills/ohana-be-coder/SKILL.md` | `.claude/skills/…` | Skill cho Claude Code |
| `scripts/bootstrap_roles.py` | `scripts/` | Tạo 4 role cấp cluster |
| `db/migrations/versions/a1_platform_schema_grants.py` | `db/migrations/versions/` | Revision A1 |
| `agent/types.py` | `agent/` | `Scrubbed`, `Wrapped` |
| `auth/context.py` | `auth/` | `ShopContext` |
| `tests/contract/test_i14_default_privileges.py` | `tests/contract/` | Gate 4 test |
| `docker-compose.yml` | gốc repo | Postgres + Langfuse cho local dev |
| `env.additions` | nối vào `.env.example` | 11 biến mới |
| `pyproject.additions.toml` | nối vào `pyproject.toml` | 2 contract import-linter |
| `.work-tiers` | gốc repo | Phân tier stop/ask/free |

## Ba việc phải làm tay

**1. `down_revision` trong revision A1** đang để `None`. Đổi thành revision cuối hiện tại:

```bash
alembic heads
```

**2. Model `Embedding`** thêm `__table_args__ = {"schema": "platform"}` sau khi A1 chạy.
Alembic đổi DB nhưng SQLAlchemy vẫn trỏ `public` — lỗi chỉ hiện ở runtime luồng A.

**3. `.github/workflows/ci.yml`** thêm hai step: `bootstrap_roles.py` trước
`alembic upgrade head`, và `lint-imports` sau `mypy`.

## Thứ tự chạy

```bash
docker compose up -d postgres
SUPERUSER_DSN=postgresql://ohana:$POSTGRES_PW@localhost:5432/ohana \
  python scripts/bootstrap_roles.py
DATABASE_URL=postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5432/ohana \
  alembic upgrade head
pytest tests/contract/ -q          # 4 passed ⇒ đóng OHB-1
```

Chi tiết + xử lý sự cố: `SETUP.md`.

## Chặn trước khi bắt đầu

`SETUP.md` §0 — đối chiếu HEAD. Snapshot dùng để lập kế hoạch có thể cũ hơn nhánh hiện tại;
`docs/adopt-plan.md` §1 liệt kê 6 lỗ hổng, cái nào đã có rồi thì gạch đi.
