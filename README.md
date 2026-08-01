# Ohana AI

**OHANA AI Super App** — backend cho super-app 3 tầng của Ohana.

- **Tầng 1** — mạng xã hội (identity / billing / feed / video). *Đợi nối, wire qua hook `upgrade(user_id)` khi platform ready.*
- **Tầng 2** — Ohana AI Assistant per-user (chat có memory + freemium gate). *Code-Complete*
- **Tầng 3** — AI Seller copilot cho shop Zalo (webhook → LLM draft → seller duyệt → gửi). *Code-complete.*

Không SaaS. Self-host Postgres 16 + Redis + Langfuse v2 trên máy Wyatt — điều kiện để golden set PII thật ở lại trong nước.

## Kiến trúc 30 giây

3 process, 4 role Postgres, isolation bằng grant chứ không bằng discipline.

```
┌─ main_seller.py      (svc_seller)     Tầng 3 luồng B webhook Zalo → outbox → worker
├─ main_ohana_ai.py    (svc_ohana_ai)   Tầng 3 luồng A chat + Tầng 2 assistant + memory
└─ worker_seller.py    (svc_seller)     background: gửi outbox, reap claim, dead-letter
```

| Bất biến chốt | Cưỡng chế bởi |
|---|---|
| **I1** Luồng A ⊥ luồng B, không chung process | 3 entrypoint riêng + `importlinter` |
| **I2** `svc_ohana_ai` không đọc được dữ liệu shop | Postgres grant + `ALTER DEFAULT PRIVILEGES` |
| **I5** SDK provider chỉ trong `agent/providers/` | `importlinter` forbidden |
| **I13** Mọi claim có timeout + reaper gỡ được | reaper loop trong `worker_seller` |
| **I16** Langfuse chỉ nhận `Scrubbed` | hook trong `agent/llm_client.py` |

Full 16 bất biến + nguồn gốc: [`docs/ohana-be-design.md`](docs/ohana-be-design.md) §1.

## Cấu trúc thư mục

```
agent/               logic AI — drafter, PII, policy gate, tier, memory, cost
├── providers/       ⚠ CHỈ chỗ được import SDK (OpenAI/Anthropic/Together/Langfuse) — I5
└── tracing_context  ContextVar cho Langfuse (user_id/session_id/trace_id)

api/                 FastAPI router
├── webhook.py       Tầng 3 luồng B (§6.1 CTE — MUST giữ nguyên văn)
├── inbox.py         Tầng 3 luồng B seller
├── chat.py          Tầng 3 luồng A seller ↔ AI general
├── assistant_*.py   Tầng 2 user assistant (chat + CRUD conv + memories)
└── admin.py         onboard shop, ingest wiki

app/                 3 entrypoint + config + runtime + worker
auth/                JWT verify — 2 identity: Identity (Tầng 3) + UserIdentity (Tầng 2)
bridge/              (Tầng 3 outbound tool call)
channels/            adapter Zalo/FB/TikTok webhook parse
db/                  models + session factory + Alembic revisions
docs/                design doc + ADR + brief per phase
parsing/             ingest wiki → chunk 480tok → embed
retrieval/           BM25 + vector + rerank cho Tầng 3
scripts/             bootstrap_roles.py (1 lần/cluster)
tests/               472 test — pytest + contract + eval
tools/               MCP `mcp_readonly` role tool wrappers
web/                 React 19 + Vite + Playwright — 6 screens Tầng 3, Tầng 2 chưa có UI
```

**472 tests** (`pytest -q`). CI gate: `ruff check + format`, `mypy`, `lint-imports`, `pytest`, `pytest tests/contract/` (khi chạm migration), `pytest -m eval` (khi chạm prompt/persona).

## Quick start

Prereq: Python 3.11, Postgres 16 (pgvector), Docker, Node 20 (chỉ khi động vào `web/`).

```bash
# 1. Compose (Postgres + Langfuse + Redis)
cp .env.example .env
for k in POSTGRES_PW MIGRATOR_PW SVC_A_PW SVC_B_PW MCP_RO_PW \
         LANGFUSE_DB_PW LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT; do
  echo "$k=$(openssl rand -hex 24)" >> .env  # HEX, KHÔNG base64 (/ + = vỡ URL)
done
docker compose up -d postgres redis langfuse

# 2. Bootstrap 4 role Postgres (1 lần/cluster, KHÔNG phải migration)
SUPERUSER_DSN="postgresql://ohana:$POSTGRES_PW@localhost:5432/ohana" \
  python scripts/bootstrap_roles.py

# 3. Migrate — PHẢI bằng ohana_migrator (chạy bằng owner = bảng mới rơi ngoài I14)
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5432/ohana" \
  alembic upgrade head

# 4. Gate — I14 default privileges đúng chưa?
pytest tests/contract/ -q      # 4 test phải xanh

# 5. Chạy 3 process (dev, mỗi cái 1 terminal)
uvicorn app.main_ohana_ai:app  --port 8001   # Tầng 2 + Tầng 3 luồng A
uvicorn app.main_seller:app    --port 8002   # Tầng 3 luồng B
python -m app.worker_seller                  # background worker
```

Setup chi tiết + xử lý sự cố: [SETUP.md](SETUP.md).

## Development workflow

- **Trước khi sửa file:** đối chiếu `.work-tiers` (stop/ask/free) — file tier `stop` nghĩa là dừng, mô tả kế hoạch, chờ duyệt.
- **Skill `ohana-be-coder`** ([.claude/skills/ohana-be-coder/](.claude/skills/ohana-be-coder/)) là contract cho Claude Code khi code Ohana BE — 16 bất biến, 10 câu SQL giữ nguyên văn, cấm Redis-làm-queue, cấm import SDK ngoài `agent/providers/`.
- **`docs/ohana-be-design.md` là sự thật.** Doc và code mâu thuẫn ⇒ sửa code. Doc và `backend-workflow.md` mâu thuẫn ⇒ dừng, hỏi người.
- **Alembic autogenerate KHÔNG thấy** `GRANT`, `CREATE ROLE`, partial index, `CHECK` — viết tay `op.execute()`, review bằng mắt.

**Verify tối thiểu trước khi commit:**

```bash
ruff check . --no-cache && ruff format --check . --no-cache
mypy app agent retrieval parsing db bridge tools api auth
lint-imports
pytest -q
```

## Contract docs

| Doc | Vai trò |
|---|---|
| [`docs/ohana-be-design.md`](docs/ohana-be-design.md) | Hợp đồng kỹ thuật — 16 bất biến, schema, 10 câu SQL. Authoritative. |
| [`docs/adr-tang2-ohana-ai-assistant.md`](docs/adr-tang2-ohana-ai-assistant.md) | Kiến trúc Tầng 2 (D1–D7 ratified). |
| [`docs/adopt-plan.md`](docs/adopt-plan.md) | Đã có gì · thiếu gì · thứ tự A1–A8. |
| [`docs/cc-brief-buoc2-*.md`](docs/) | Brief chi tiết từng phase 2.1 → 2.4d. |
| [`docs/cc-brief-web-*.md`](docs/) | Brief FE (F1 sidebar Tầng 2, draft). |

## Bẫy nguy hiểm nhất

1. **Chạy Alembic bằng role không phải `ohana_migrator`** — `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator` chỉ phủ bảng do role đó tạo. Chạy bằng owner `ohana` một lần = mọi bảng sinh sau rơi ra ngoài I2, **không có gì báo lỗi**.
2. **Nới 10 câu SQL** ở `§6.1` `§6.2` `§6.3` `§6.4` `§6.5` `§6.5b` `§6.6` `§6.7` `§6.9` `§6.10` — tách §6.1 thành 2 INSERT = draft đôi silent. Refactor mấy câu này ⇒ dừng, hỏi.
3. **Import SDK ngoài `agent/providers/`** — vỡ I5, `lint-imports` đỏ.
4. **`gh pr merge` bị chặn** trên repo này — thay bằng ff-merge local + push main.

## Trạng thái

- Tầng 3 (GD0.5): code-complete, web UI 6 màn, e2e Playwright.
- Tầng 2: BE code-complete tới P2.4d + Langfuse tracing context. FE **chưa có** — brief F1 sidebar draft ở [`docs/cc-brief-web-assistant-sidebar.md`](docs/cc-brief-web-assistant-sidebar.md).
- Tầng 1: Đợi nối app.
