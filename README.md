<p align="center">
  <img src="web/public/ohana-mark.svg" alt="Ohana" width="96" height="96" />
</p>

<h1 align="center">Ohana AI</h1>

<p align="center"><strong>Backend</strong> cho super-app 3 tầng của Ohana. Self-host — không SaaS.</p>

---

| Tầng | Vai | Trạng thái |
|---|---|---|
| **1** — Ohana Social | Identity / billing / feed / video | Đợi platform ready; hook `upgrade(user_id)` |
| **2** — Ohana AI Assistant | Chat per-user + memory + freemium gate | **BE code-complete**, FE draft (F1 sidebar) |
| **3** — AI Seller (Zalo) | Webhook → LLM draft → seller duyệt → gửi | **Code-complete GD0.5** (BE + FE 6 màn) |

Golden set PII thật ở lại trong nước ⇒ toàn stack (Postgres 16 · Redis · Langfuse v2) self-host.

---

## 1 · Kiến trúc 30 giây

3 process backend, 4 role Postgres, 1 nginx đầu vào. **Isolation bằng grant**, không bằng discipline.

```
                          Internet (443)
                               │
                               ▼
                        ┌─────────────┐
                        │   nginx     │  TLS terminate + SPA fallback
                        │   (web/     │  (build SPA React 19 stage 1,
                        │  Dockerfile)│   serve + reverse proxy stage 2)
                        └──┬───┬───┬──┘
              /api/chat    │   │   │    /api/inbox
           /api/assistant  │   │   │    /api/admin
                           │   │   │    /webhook
                           ▼   │   ▼
                 ┌──────────┐  │  ┌─────────────┐
                 │ ohana-ai │  │  │ohana-seller │
                 │   :8001  │  │  │    :8002    │
                 │svc_ohana │  │  │ svc_seller  │
                 │   _ai    │  │  │             │
                 └────┬─────┘  │  └──────┬──────┘
                      │        │         │
                      │        │         ▼
                      │        │   ┌─────────────┐
                      │        │   │ohana-worker │
                      │        │   │  (no port)  │
                      │        │   │ svc_seller  │
                      │        │   └──────┬──────┘
                      ▼        │          │
                 ┌─────────────┴──────────┴─────┐
                 │       postgres:16            │
                 │  (pgvector, isolated by      │
                 │   role grant — I2 / I14)     │
                 └──────────────────────────────┘

                 ┌─────────────┐    ┌───────────────┐
                 │  redis:7    │    │  langfuse:2   │
                 │ (counter,   │    │ (self-host,   │
                 │  no persist)│    │  UI :3000     │
                 │             │    │  localhost    │
                 │             │    │  only)        │
                 └─────────────┘    └───────────────┘
```

Route rule: `/webhook/*` + `/api/inbox|admin|mock/*` → **ohana-seller** (luồng B) · `/api/chat|assistant/*` → **ohana-ai** (luồng A) · `/*` → SPA fallback. Chi tiết [deploy/nginx.conf](deploy/nginx.conf).

**16 bất biến** ([design §1](docs/ohana-be-design.md)) — 5 điều quan trọng nhất:

- **I1** Luồng A ⊥ B, không chung process — 3 entrypoint + `importlinter`
- **I2** `svc_ohana_ai` không đọc dữ liệu shop — Postgres grant + `ALTER DEFAULT PRIVILEGES`
- **I5** SDK provider chỉ trong `agent/providers/` — `importlinter forbidden`
- **I13** Mọi claim có timeout + reaper gỡ được — worker reaper loop
- **I16** Langfuse chỉ nhận `Scrubbed` — hook trong `agent/llm_client.py`

**501 test** · gate CI: `ruff · mypy · lint-imports · pytest`. Contract test (I2/I14/I15) khi chạm migration.

---

## 2 · Chọn cách chạy

| Bạn muốn | Đọc | Chạy |
|---|---|---|
| Sửa code local, chạy nhanh | [SETUP.md](SETUP.md) | 3 uvicorn trên host + `docker compose up postgres redis langfuse` |
| Deploy lên server | [DEPLOY.md](DEPLOY.md) + [deploy/README.md](deploy/README.md) | `docker compose -f docker-compose.prod.yml up -d` |
| Chỉ đọc để hiểu | [docs/ohana-be-design.md](docs/ohana-be-design.md) + [docs/adr-tang2-ohana-ai-assistant.md](docs/adr-tang2-ohana-ai-assistant.md) | — |

### Dev — 5 lệnh

Prereq: Python 3.11 · Postgres 16 (pgvector) · Docker · Node 20 (chỉ nếu động `web/`).

```bash
# 1. .env — sinh 8 password HEX (base64 vỡ URL)
cp .env.example .env
for k in POSTGRES_PW MIGRATOR_PW SVC_A_PW SVC_B_PW MCP_RO_PW \
         LANGFUSE_DB_PW LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT; do
  echo "$k=$(openssl rand -hex 24)" >> .env
done

# 2. Infra
docker compose up -d postgres redis langfuse

# 3. Bootstrap 4 role Postgres (1 lần/cluster)
SUPERUSER_DSN="postgresql://ohana:$POSTGRES_PW@localhost:5432/ohana" \
  python scripts/bootstrap_roles.py

# 4. Migrate — PHẢI bằng ohana_migrator (owner ⇒ bảng mới rơi ngoài I14 im lặng)
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5432/ohana" \
  alembic upgrade head
pytest tests/contract/ -q          # 4 xanh ⇒ grant isolation OK

# 5. Chạy 3 process (mỗi cái 1 terminal)
uvicorn app.main_ohana_ai:app --port 8001
uvicorn app.main_seller:app   --port 8002
python -m app.worker_seller
```

### Prod — 1 lệnh (sau lần đầu setup)

```bash
docker compose -f docker-compose.prod.yml up -d
```

Lần đầu cần chạy `--profile bootstrap` (roles) + `--profile migrate` (schema) — xem [deploy/README.md](deploy/README.md).

---

## 3 · Cấu trúc thư mục

```
agent/           logic AI: drafter, PII, policy_gate, tier, memory, cost, tracing_context
├── providers/   ⚠ CHỈ chỗ import SDK LLM/tracing (I5)
api/             FastAPI router — webhook, inbox, chat, assistant_*, admin
app/             3 entrypoint (main_ohana_ai + main_seller + worker_seller) + config
auth/            JWT — 2 identity: Identity (Tầng 3, có shop_id) + UserIdentity (Tầng 2)
bridge/          Tầng 3 outbound tool call
channels/        adapter Zalo/FB/TikTok webhook parse
db/              models + session factory + Alembic revisions
parsing/         ingest wiki → chunk 480tok → embed
retrieval/       BM25 + vector + rerank (Tầng 3)
scripts/         bootstrap_roles.py (1 lần/cluster) + ai_coder tooling
tests/           501 test — unit + contract (I2/I14/I15) + eval (khi chạm prompt)
tools/           MCP `mcp_readonly` role wrappers (I15)
web/             React 19 + Vite + Playwright — 6 màn Tầng 3, Tầng 2 chưa có UI
deploy/          docker-compose prod — nginx.conf, entrypoint, .env.prod.example
```

---

## 4 · Sửa code — quy tắc

1. **`.work-tiers` chốt trước:** `stop` = dừng, mô tả kế hoạch, chờ duyệt. `ask` = hỏi trước khi bắt đầu. `free` = tự do.
2. **Skill `ohana-be-coder`** ([.claude/skills/ohana-be-coder/SKILL.md](.claude/skills/ohana-be-coder/SKILL.md)) là contract Claude Code phải theo — 16 bất biến, 10 câu SQL giữ nguyên văn.
3. **[`docs/ohana-be-design.md`](docs/ohana-be-design.md) là sự thật** — doc ↔ code mâu thuẫn ⇒ sửa code. Doc ↔ `backend-workflow.md` mâu thuẫn ⇒ dừng, hỏi.
4. **Alembic autogenerate KHÔNG thấy** `GRANT` · `CREATE ROLE` · partial index · `CHECK` — viết tay `op.execute()`, review bằng mắt.

**Verify tối thiểu trước commit:**

```bash
ruff check . --no-cache && ruff format --check . --no-cache
mypy app agent retrieval parsing db bridge tools api auth
lint-imports
pytest -q
```

---

## 5 · Bẫy nguy hiểm nhất — đọc thuộc

1. **Chạy Alembic bằng role ≠ `ohana_migrator`** — `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator` chỉ phủ bảng do role đó tạo. Chạy bằng owner `ohana` = mọi bảng sinh sau rơi ngoài I2, **không có gì báo lỗi**.
2. **Nới 10 câu SQL** ở `§6.1` `§6.2` `§6.3` `§6.4` `§6.5` `§6.5b` `§6.6` `§6.7` `§6.9` `§6.10` — tách §6.1 thành 2 INSERT = draft đôi silent. Refactor ⇒ dừng, hỏi.
3. **Import SDK ngoài `agent/providers/`** — vỡ I5, `lint-imports` đỏ.
4. **`gh pr merge` bị chặn** repo này — thay bằng ff-merge local + push main.
5. **`app/main.py` là dev-only combined** — deploy prod dùng `app/main_ohana_ai.py` + `app/main_seller.py` + `app/worker_seller.py` riêng. Trộn = mất I1/I2.

---

## 6 · Docs tham chiếu

| Doc | Vai trò |
|---|---|
| [ohana-be-design.md](docs/ohana-be-design.md) | Hợp đồng kỹ thuật — 16 bất biến, schema, 10 câu SQL. **Authoritative.** |
| [adr-tang2-ohana-ai-assistant.md](docs/adr-tang2-ohana-ai-assistant.md) | Kiến trúc Tầng 2 (D1–D7 ratified) |
| [adopt-plan.md](docs/adopt-plan.md) | Đã có gì · thiếu gì · thứ tự A1–A8 |
| [api-reference.md](docs/api-reference.md) | API dev reference — tất cả endpoint, cookie/CSRF, error code, curl examples |
| [cc-brief-buoc2-*.md](docs/) | Brief chi tiết phase 2.1 → 2.4d |
| [cc-brief-web-*.md](docs/) | Brief FE (F1 sidebar Tầng 2 — draft) |
| [SETUP.md](SETUP.md) | Vì sao từng lệnh dev (432 dòng — deep) |
| [DEPLOY.md](DEPLOY.md) | Hướng dẫn triển khai 13 mục cho single-host |
| [deploy/README.md](deploy/README.md) | docker-compose prod — 8 bước lần đầu + routine deploy |
