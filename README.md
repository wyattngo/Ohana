<p align="center">
  <img src="web/public/ohana-mark.svg" alt="Ohana" width="96" height="96" />
</p>

<h1 align="center">Ohana AI</h1>

<p align="center"><strong>Backend</strong> cho super-app 3 tầng của Ohana. Self-host — không SaaS.</p>

---

| Tầng | Vai | Trạng thái |
|---|---|---|
| **1** — Ohana Social | Identity / billing / feed / video | Đợi platform ready |
| **2** — Ohana AI Assistant | Chat per-user + memory + freemium gate | **BE code-complete**, FE F1 sidebar ship |
| **3** — AI Seller (Zalo) | Webhook → LLM draft → seller duyệt → gửi | **Code-complete GD0.5** (BE + FE 6 màn) |

## Chọn cách chạy

| Bạn muốn | Đọc |
|---|---|
| Chạy dev nhanh | Quick start dưới đây |
| Deploy lên server | [DEPLOY.md](DEPLOY.md) hoặc [deploy/README.md](deploy/README.md) (docker compose) |
| Hiểu kiến trúc | [docs/architecture.md](docs/architecture.md) |
| Sửa code — quy tắc, verify, bẫy | [docs/contributing.md](docs/contributing.md) |
| Endpoint reference | [docs/api-reference.md](docs/api-reference.md) |
| Hợp đồng authoritative | [docs/ohana-be-design.md](docs/ohana-be-design.md) |

## Quick start (dev)

Prereq: Python 3.11 · Postgres 16 (pgvector) · Docker · Node 20 (chỉ nếu động `web/`).

```bash
# 1. Env
cp .env.example .env
for k in POSTGRES_PW MIGRATOR_PW SVC_A_PW SVC_B_PW MCP_RO_PW \
         LANGFUSE_DB_PW LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT; do
  echo "$k=$(openssl rand -hex 24)" >> .env
done

# 2. Infra + roles + migrate
docker compose up -d postgres redis langfuse
SUPERUSER_DSN="postgresql://ohana:$POSTGRES_PW@localhost:5432/ohana" \
  python scripts/bootstrap_roles.py
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5432/ohana" \
  alembic upgrade head
pytest tests/contract/ -q     # 4 xanh ⇒ grant isolation OK

# 3. 3 process (mỗi cái 1 terminal)
uvicorn app.main_ohana_ai:app --port 8001
uvicorn app.main_seller:app   --port 8002
python -m app.worker_seller
```

Setup deep-dive (vì sao từng lệnh): [SETUP.md](SETUP.md).

## Trạng thái

501 test · main sạch · dev + prod đã docker-hoá. Backlog: Tầng 2 FE F2 (edit title, memory UI, search, streaming).
