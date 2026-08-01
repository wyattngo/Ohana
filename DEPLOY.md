# DEPLOY — Ohana BE

Hướng dẫn triển khai Ohana BE lên máy mới, từ zero → 3 process chạy + monitoring xanh. Cả **local dev** và **single-host self-host** (production hiện tại của Wyatt) dùng chung guide này — khác nhau ở step §7 (systemd) và §8 (reverse proxy) chỉ.

Đọc trước: [README.md](README.md) (bức tranh tổng), [SETUP.md](SETUP.md) (giải thích **vì sao** từng lệnh).

---

## §0 · Prereq — kiểm trước khi bắt đầu

| Yêu cầu | Verify | Nếu thiếu |
|---|---|---|
| Python **3.11**+ | `python3 --version` | `pyenv install 3.11` |
| Postgres **16** (có `pgvector`) | dùng `docker-compose.yml` hoặc `psql -c "SELECT extname FROM pg_extension"` | image `pgvector/pgvector:pg16` |
| Docker + docker compose v2 | `docker compose version` | Docker Desktop / OCI |
| **Node 20** (nếu build `web/`) | `node --version` | ⚠ Node 22 vỡ pnpm 11 corepack — pin 20 |
| `openssl` | `openssl version` | package manager |
| Anthropic API key | `.env` `ANTHROPIC_API_KEY=` | console.anthropic.com |
| Together API key (embedding) | `.env` `TOGETHER_API_KEY=` | api.together.xyz |

**Chặn cứng:** không có Anthropic key ⇒ Tầng 2 chat + Tầng 3 drafter chết. Không có Together key ⇒ ingest wiki chết.

---

## §1 · Kéo mã + Python env

```bash
git clone <ohana-be-repo>.git ohana-be
cd ohana-be
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"          # 472 test cần [dev]; production tối thiểu: pip install -e .
```

**Verify:** `python -c "import fastapi, sqlalchemy, alembic, anthropic, together, langfuse; print('ok')"`.

---

## §2 · Env vars — 32 biến, chia 5 nhóm

```bash
cp .env.example .env
```

Sinh mật khẩu **hex** (KHÔNG base64 — `/` `+` `=` vỡ URL):

```bash
for k in POSTGRES_PW MIGRATOR_PW SVC_A_PW SVC_B_PW MCP_RO_PW \
         LANGFUSE_DB_PW LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT; do
  echo "$k=$(openssl rand -hex 24)" >> .env
done
```

Nhóm còn lại điền tay:

| Nhóm | Biến | Nguồn / mẫu |
|---|---|---|
| **App** | `ENV`, `APP_VERSION`, `DEV_AUTH_ENABLED` | `ENV=prod`, `APP_VERSION=$(git rev-parse --short HEAD)`, `DEV_AUTH_ENABLED=false` (prod) |
| **LLM chat** | `ANTHROPIC_API_KEY`, `CLAUDE_MODEL_CHAT`, `CLAUDE_MODEL_SMART`, `CLAUDE_CACHE_ENABLED`, `REASONING_MODE` | Anthropic console; models = `claude-sonnet-4-5` / `claude-opus-4-5` |
| **LLM embed** | `TOGETHER_API_KEY` | together.ai; model pin trong code (`intfloat/multilingual-e5-large-instruct`) |
| **LLM gateway (optional)** | `LLM_GATEWAY_*` (5 biến) | Bỏ nếu không dùng proxy |
| **Langfuse** | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST` | Sau §5, mint qua UI localhost:3000 |
| **TTS (optional)** | `ELEVENLABS_API_KEY`, `TTS_MODEL`, `TTS_VOICE_ID`, `TTS_MAX_CHARS_PER_USER_PER_DAY` | Bỏ nếu chưa ship voice |
| **Ollama (optional)** | `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | Local LLM fallback, bỏ nếu không có |
| **Ports** | `OHANA_PG_PORT` (5432 mặc định) | Đổi khi conflict với Postgres khác trên host |

Thứ tự bắt buộc `.env` phải có TRƯỚC các bước sau — sinh bằng script trên cho 5 nhóm password, còn lại điền tay.

---

## §3 · Compose — Postgres + Redis + Langfuse

```bash
docker compose up -d postgres redis langfuse-db langfuse
docker compose ps         # 4 service phải `healthy`
```

`postgres` = data. `redis` = cost counter + rate-limit (D3 ADR §1, không persistent — `--save ""`, `allkeys-lru`). `langfuse` = self-host v2 (KHÔNG v3, v3 cần ClickHouse+Redis+MinIO thêm).

**Verify:**

```bash
docker compose logs postgres --tail=5 | grep "ready"
curl -sf http://localhost:3000 > /dev/null && echo "langfuse ok"
docker compose exec redis redis-cli ping   # PONG
```

---

## §4 · Bootstrap 4 role Postgres — 1 LẦN/cluster

Role là cấp **cluster**, không phải database ⇒ KHÔNG nhét vào Alembic (restore dump sang cluster mới sẽ mất role trong khi `schema_migration` báo đã chạy). Idempotent, chạy lại không sao.

```bash
export $(grep -v '^#' .env | xargs)   # load env vào shell
SUPERUSER_DSN="postgresql://ohana:$POSTGRES_PW@localhost:${OHANA_PG_PORT:-5432}/ohana" \
  python scripts/bootstrap_roles.py
```

Output kỳ vọng: 4 dòng `role ok: {ohana_migrator, svc_ohana_ai, svc_seller, mcp_readonly}`.

**Verify:**

```bash
docker compose exec postgres psql -U ohana -d ohana -c "\du"
# Phải thấy 5 role: ohana + 4 role trên
```

---

## §5 · Migrate — PHẢI bằng `ohana_migrator`

⚠ **Bẫy nguy hiểm nhất repo này:** chạy Alembic bằng role khác `ohana_migrator` = mọi bảng sinh sau rơi ngoài I14 (default privileges), I2 hỏng im lặng ở bảng thứ N+1, **không có gì báo lỗi**.

```bash
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:${OHANA_PG_PORT:-5432}/ohana" \
  alembic upgrade head
```

**Verify — gate I14:**

```bash
pytest tests/contract/ -q      # 4 test PHẢI xanh
```

4 assertion chứng minh:
- `svc_ohana_ai` đọc bảng public mới ⇒ **denied** (I2)
- `svc_seller` đọc bảng public mới ⇒ **ok** (I14 — không cần GRANT tay)
- `mcp_readonly` đọc ok, ghi ⇒ **denied** (I15)
- `svc_ohana_ai` đọc `pending_reply` ⇒ **denied** (I2 bảng đang có)

4 xanh ⇒ isolation grant sẵn sàng.

---

## §6 · Langfuse — mint keys

1. Mở `http://localhost:3000` → Sign up (email/password bất kỳ, self-host).
2. Create Project "Ohana BE".
3. **Settings → API keys → Create new key.**
4. Copy vào `.env`:
   ```
   LANGFUSE_PUBLIC_KEY=pk-lf-...
   LANGFUSE_SECRET_KEY=sk-lf-...
   LANGFUSE_HOST=http://localhost:3000
   ```
5. **Settings → Models** — add entry `meta-llama/Llama-3.3-70B-Instruct-Turbo` với Together pricing (nếu dùng Together LLM chat), else Anthropic sonnet pricing. Không thêm ⇒ tab **Total Cost** Langfuse trống.

Thiếu 2 key trên: tracing tự tắt (fail-safe), không sập app — log 1 dòng `langfuse: thiếu LANGFUSE_PUBLIC_KEY/SECRET_KEY — tracing tắt` lúc wire.

---

## §7 · 3 process — wire chạy nền

### 7a. Local dev — 3 terminal

```bash
# Terminal 1 — Tầng 2 + Tầng 3 luồng A (svc_ohana_ai)
DATABASE_URL="postgresql+psycopg://svc_ohana_ai:$SVC_A_PW@localhost:${OHANA_PG_PORT:-5432}/ohana" \
  uvicorn app.main_ohana_ai:app --host 127.0.0.1 --port 8001

# Terminal 2 — Tầng 3 luồng B (svc_seller)
DATABASE_URL="postgresql+psycopg://svc_seller:$SVC_B_PW@localhost:${OHANA_PG_PORT:-5432}/ohana" \
  uvicorn app.main_seller:app --host 127.0.0.1 --port 8002

# Terminal 3 — worker background (svc_seller)
DATABASE_URL="postgresql+psycopg://svc_seller:$SVC_B_PW@localhost:${OHANA_PG_PORT:-5432}/ohana" \
  python -m app.worker_seller
```

**KHÔNG dùng `app.main:app` cho production** — file `main.py` gộp cả hai luồng vào một `DATABASE_URL`, KHÔNG có ranh giới I1/I2 nào; chỉ giữ cho local one-process debug.

### 7b. Self-host — systemd unit

`/etc/systemd/system/ohana-ai.service` (Tầng 2 + Tầng 3 luồng A):

```ini
[Unit]
Description=Ohana BE — main_ohana_ai (svc_ohana_ai)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=ohana
WorkingDirectory=/opt/ohana-be
EnvironmentFile=/opt/ohana-be/.env
Environment="DATABASE_URL=postgresql+psycopg://svc_ohana_ai:${SVC_A_PW}@localhost:5432/ohana"
ExecStart=/opt/ohana-be/.venv/bin/uvicorn app.main_ohana_ai:app --host 127.0.0.1 --port 8001 --workers 2
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Copy tạo 2 file tương tự:
- `ohana-seller.service` → `main_seller:app --port 8002`, `DATABASE_URL` role `svc_seller`.
- `ohana-worker.service` → `python -m app.worker_seller`, `DATABASE_URL` role `svc_seller`, KHÔNG có port.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now ohana-ai ohana-seller ohana-worker
sudo systemctl status ohana-*                 # 3 xanh
```

**Verify healthcheck:**

```bash
curl -sf http://127.0.0.1:8001/health && echo ok    # Tầng 2 + A
curl -sf http://127.0.0.1:8002/health && echo ok    # luồng B
journalctl -u ohana-worker -n 20 --no-pager         # thấy reaper tick
```

---

## §8 · Reverse proxy — nginx

Zalo webhook chỉ chấp nhận HTTPS. Nginx trước 2 uvicorn:

```nginx
server {
    listen 443 ssl http2;
    server_name api.ohana.example;
    ssl_certificate     /etc/letsencrypt/live/api.ohana.example/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/api.ohana.example/privkey.pem;

    # Webhook Zalo — bắt buộc HTTPS, timeout dài để CTE §6.1 xong trong 1 request
    location /webhook/ {
        proxy_pass http://127.0.0.1:8002;
        proxy_read_timeout 30s;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Seller UI + inbox
    location ~ ^/(api/inbox|api/admin|api/mock)/ {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host $host;
    }

    # Tầng 2 assistant + Tầng 3 chat luồng A
    location ~ ^/api/(assistant|chat)/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_read_timeout 60s;   # LLM cold start ~25s (đo được, spec 07 §14)
        proxy_set_header Host $host;
    }

    # Static SPA (build `web/dist/` copy sang /var/www/ohana)
    location / {
        root /var/www/ohana;
        try_files $uri /index.html;
    }
}
```

**Verify:** `curl -sf https://api.ohana.example/health` từ máy khác trả 200.

---

## §9 · Web frontend — build + deploy static

```bash
cd web
corepack use pnpm@10                    # ⚠ pnpm 11 vỡ với Node 20
pnpm install --frozen-lockfile
pnpm build                              # tsc -b && vite build → dist/
sudo cp -r dist/* /var/www/ohana/
sudo nginx -s reload
```

`web/dist/` là artifact — không commit vào git.

---

## §10 · Verify end-to-end

```bash
# 1. 3 process xanh
sudo systemctl status ohana-ai ohana-seller ohana-worker | grep Active

# 2. Health tất cả endpoint
curl -sf http://127.0.0.1:8001/health
curl -sf http://127.0.0.1:8002/health

# 3. Grant isolation vẫn đúng (không regressed sau restart)
pytest tests/contract/ -q

# 4. Mock chat Tầng 3 (need DEV_AUTH_ENABLED=true)
curl -sX POST http://127.0.0.1:8001/api/mock/authorize?role=seller \
  -c /tmp/cookies.txt
curl -sX POST http://127.0.0.1:8001/api/chat \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $(grep ohana_csrf /tmp/cookies.txt | awk '{print $7}')" \
  -b /tmp/cookies.txt \
  -d '{"message":"ping"}' | jq

# 5. Langfuse thấy trace vừa gọi
open http://localhost:3000    # Traces → phải thấy generation gần nhất
```

---

## §11 · Backup + rotate

| Đối tượng | Backup | Tần suất |
|---|---|---|
| Postgres `ohana` | `pg_dump -Fc -U ohana ohana > ohana_$(date +%F).dump` | Daily, giữ 30 ngày |
| Langfuse `langfuse` DB | `pg_dump -Fc -U postgres langfuse > langfuse_$(date +%F).dump` | Weekly |
| `.env` | copy offline (LastPass/1Password) | Ngay khi đổi |
| Redis | KHÔNG backup (D4 fail-open — mất counter = accept) | — |
| `web/dist/` | Không backup (rebuild từ git) | — |

**Rotate:** `MIGRATOR_PW`, `SVC_A_PW`, `SVC_B_PW` — đổi bằng `ALTER ROLE ... WITH PASSWORD` rồi update `.env` + `systemctl restart ohana-*`.

---

## §12 · Rollback — khi migrate hỏng

```bash
# 1. Lấy revision trước
alembic history | head -5

# 2. Downgrade (chỉ khi revision có downgrade viết tay — mặc định A1+ ĐỀU có)
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5432/ohana" \
  alembic downgrade <prev-revision>

# 3. Nếu downgrade cũng vỡ: restore từ dump
sudo systemctl stop ohana-*
docker compose exec -T postgres pg_restore -U ohana -d ohana --clean < ohana_YYYY-MM-DD.dump
sudo systemctl start ohana-*
pytest tests/contract/ -q       # 4 xanh
```

⚠ Restore dump sang **cluster khác** ⇒ role không có trong dump — chạy lại **§4** trước khi restore, else `alembic upgrade` sẽ vỡ ở lần deploy kế tiếp.

---

## §13 · Escalation

| Triệu chứng | Kiểm | Fix |
|---|---|---|
| `pytest tests/contract/` đỏ | `docker compose exec postgres psql -U ohana -c "\du"` | Chạy lại §4 (idempotent), sau đó verify grant bằng `\dp platform.corpus` |
| Langfuse "No data" | `.env` `LANGFUSE_PUBLIC_KEY` set chưa? `curl localhost:3000/api/public/health` | Mint lại key ở UI §6 |
| Webhook Zalo 502 | `journalctl -u ohana-seller -n 50` | Check nginx `proxy_read_timeout`, check `worker_seller` outbox drain |
| Redis chớp → 500 | `docker compose logs redis` | D4 fail-open đã đảm bảo không sập; nếu vẫn 500 = bug, không phải config |
| SDK import ngoài `agent/providers/` | `lint-imports` đỏ | Move import về `agent/providers/`, KHÔNG whitelist |

---

## Checklist deploy production (in ra)

```
[ ] §0 · Prereq đầy đủ
[ ] §1 · git clone + .venv + pip install -e ".[dev]"
[ ] §2 · .env có đủ 32 biến (password HEX)
[ ] §3 · docker compose ps — 4 service healthy
[ ] §4 · bootstrap_roles.py OK, \du thấy 5 role
[ ] §5 · alembic upgrade head OK
[ ] §5 · pytest tests/contract/ — 4 xanh
[ ] §6 · Langfuse key mint + .env update
[ ] §6 · Langfuse Models UI có pricing entry
[ ] §7 · 3 systemd unit enabled + active
[ ] §8 · nginx reload, curl /health 200
[ ] §9 · web/dist copy sang /var/www/ohana
[ ] §10 · end-to-end mock chat trả 200 + Langfuse thấy trace
[ ] §11 · cron pg_dump chạy được
```

Ký tên ai deploy: __________ · Ngày: __________ · APP_VERSION: __________
