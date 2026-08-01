# Contributing — Ohana BE

Quy tắc sửa code + verify + bẫy. Kiến trúc ở [architecture.md](architecture.md); hợp đồng ở [ohana-be-design.md](ohana-be-design.md).

## 4 quy tắc

1. **`.work-tiers` chốt trước:** `stop` = dừng, mô tả kế hoạch, chờ duyệt. `ask` = hỏi trước khi bắt đầu. `free` = tự do.
2. **Skill [`ohana-be-coder`](../.claude/skills/ohana-be-coder/SKILL.md) là contract Claude Code** phải theo — 16 bất biến, 10 câu SQL giữ nguyên văn.
3. **[`ohana-be-design.md`](ohana-be-design.md) là sự thật** — doc ↔ code mâu thuẫn ⇒ sửa code. Doc ↔ `backend-workflow.md` mâu thuẫn ⇒ dừng, hỏi.
4. **Alembic autogenerate KHÔNG thấy** `GRANT` · `CREATE ROLE` · partial index · `CHECK` — viết tay `op.execute()`, review bằng mắt.

## Verify trước commit

```bash
ruff check . --no-cache && ruff format --check . --no-cache
mypy app agent retrieval parsing db bridge tools api auth
lint-imports
pytest -q
```

Chạm `db/migrations/` ⇒ thêm `pytest tests/contract/ -q`.
Chạm `agent/policy_gate.py` · `agent/persona.py` · `parsing/` · `retrieval/` ⇒ thêm `pytest -m eval -q`.

## 5 bẫy nguy hiểm nhất

1. **Chạy Alembic bằng role ≠ `ohana_migrator`** — `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator` chỉ phủ bảng do role đó tạo. Chạy bằng owner `ohana` = mọi bảng sinh sau rơi ngoài I2, **không có gì báo lỗi**.
2. **Nới 10 câu SQL** ở `§6.1` `§6.2` `§6.3` `§6.4` `§6.5` `§6.5b` `§6.6` `§6.7` `§6.9` `§6.10` — tách §6.1 thành 2 INSERT = draft đôi silent. Refactor ⇒ dừng, hỏi.
3. **Import SDK ngoài `agent/providers/`** — vỡ I5, `lint-imports` đỏ.
4. **`gh pr merge` bị chặn** — thay bằng ff-merge local + push main.
5. **`app/main.py` là dev-only combined** — deploy prod dùng `app/main_ohana_ai.py` + `app/main_seller.py` + `app/worker_seller.py` riêng. Trộn = mất I1/I2.

## Test DB fixture

Test integration cần Postgres port 5433, DB `ohana_test`:

```bash
source .env
export DATABASE_URL="postgresql+psycopg://ohana:$POSTGRES_PW@localhost:5433/ohana_test"
pytest -q
```

`fresh_db` fixture destructive theo thiết kế; `conftest` có name-guard `_test` để không vô tình wipe DB dev.

## Deploy · Debug

- Deploy prod: [DEPLOY.md](../DEPLOY.md) + [deploy/README.md](../deploy/README.md)
- Setup dev deep-dive: [SETUP.md](../SETUP.md)
- Brief per phase: [docs/cc-brief-*.md](.) — mỗi phase 2.1 → F1 có 1 brief
