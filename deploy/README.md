# deploy/ — docker-compose production

Đóng gói Ohana BE thành 1 stack docker-compose để `docker compose up -d` là chạy.

## Nội dung

| File | Việc |
|---|---|
| `../Dockerfile` | Backend Python 3.11 image (dùng chung cho 3 process) |
| `../web/Dockerfile` | Frontend build (Node 20 + pnpm@10) → nginx 1.27 serve |
| `../docker-compose.prod.yml` | Stack chính — infra + 3 backend + 1 nginx, 2 profile one-shot |
| `../.dockerignore` | Cấm .git/.venv/tests/docs/.env vào image |
| `deploy/nginx.conf` | Reverse proxy 443 → 2 uvicorn upstream + SPA fallback |
| `deploy/entrypoint.sh` | Chờ postgres healthy → exec CMD (KHÔNG auto-migrate) |
| `deploy/.env.prod.example` | Template 32 biến, copy sang `.env.prod` (root repo) |

## Chạy lần đầu (cluster mới)

```bash
# 1. Env — tạo .env.prod, sinh password HEX
cp deploy/.env.prod.example .env.prod
for k in POSTGRES_PW MIGRATOR_PW SVC_A_PW SVC_B_PW MCP_RO_PW \
         LANGFUSE_DB_PW LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT; do
  echo "$k=$(openssl rand -hex 24)" >> .env.prod
done
# Điền tay: ANTHROPIC_API_KEY, TOGETHER_API_KEY, APP_VERSION, ...

# 2. Cert Let's Encrypt (chạy trên HOST, không trong compose)
sudo apt install certbot
sudo certbot certonly --standalone -d api.ohana.example
# Sửa deploy/nginx.conf: đổi `api.ohana.example` sang domain thật

# 3. Build + kéo image infra
docker compose -f docker-compose.prod.yml build
docker compose -f docker-compose.prod.yml pull postgres redis langfuse langfuse-db

# 4. Infra lên trước
docker compose -f docker-compose.prod.yml up -d postgres redis langfuse-db langfuse

# 5. Bootstrap 4 role Postgres — 1 lần, opt-in profile
docker compose -f docker-compose.prod.yml --profile bootstrap up ohana-bootstrap
# Exit 0 = OK. Xem log: "role ok: {ohana_migrator, svc_ohana_ai, svc_seller, mcp_readonly}"

# 6. Migrate — opt-in profile
docker compose -f docker-compose.prod.yml --profile migrate up ohana-migrate
# Exit 0 = OK

# 7. Backend + nginx lên
docker compose -f docker-compose.prod.yml up -d ohana-ai ohana-seller ohana-worker nginx
docker compose -f docker-compose.prod.yml ps      # 8 service, tất cả healthy

# 8. Verify
curl -sf https://api.ohana.example/health          # nginx → SPA
docker compose -f docker-compose.prod.yml exec ohana-ai curl -sf http://127.0.0.1:8001/health
docker compose -f docker-compose.prod.yml exec ohana-seller curl -sf http://127.0.0.1:8002/health
```

## Deploy code mới (routine)

```bash
git pull --ff-only origin main

# Có migration mới?
ls db/migrations/versions/ | tail -3
# Nếu có ⇒ chạy migrate TRƯỚC restart backend
docker compose -f docker-compose.prod.yml build ohana-migrate
docker compose -f docker-compose.prod.yml --profile migrate up ohana-migrate

# Rebuild + rolling restart
docker compose -f docker-compose.prod.yml build ohana-ai ohana-seller ohana-worker nginx
docker compose -f docker-compose.prod.yml up -d ohana-ai ohana-seller ohana-worker nginx

# Verify (checklist §Smoke test trong /engineering:deploy-checklist output)
```

## Rollback

```bash
git reset --hard <prev-sha>
# Nếu migration mới cần downgrade:
docker compose -f docker-compose.prod.yml --profile migrate run --rm ohana-migrate \
  alembic downgrade <prev-revision>
# Rebuild + restart
docker compose -f docker-compose.prod.yml build ohana-ai ohana-seller ohana-worker
docker compose -f docker-compose.prod.yml up -d ohana-ai ohana-seller ohana-worker
```

Downgrade vỡ ⇒ `pg_restore` từ dump (DEPLOY.md §12).

## Backup Postgres (cron trên host)

```bash
# /etc/cron.d/ohana-pg-backup
0 3 * * *  root  docker compose -f /opt/ohana-be/docker-compose.prod.yml exec -T postgres \
  pg_dump -Fc -U ohana ohana > /backups/ohana_$(date +\%F).dump
```

Rotate: `find /backups -name 'ohana_*.dump' -mtime +30 -delete`.

## Renew cert (cron trên host, không trong compose)

```bash
# certbot cài trên host. Renew reload nginx container.
0 4 * * *  root  certbot renew --quiet --deploy-hook \
  "docker compose -f /opt/ohana-be/docker-compose.prod.yml exec nginx nginx -s reload"
```

## Kiến trúc

```
                        Internet (443)
                              │
                              ▼
                       ┌─────────────┐
                       │  nginx      │  TLS terminate + SPA fallback
                       │  (web/      │
                       │   Dockerfile│
                       │   builds    │
                       │   SPA)      │
                       └──┬───┬──┬───┘
              /api/chat   │   │  │   /api/inbox
           /api/assistant │   │  │   /api/admin
                          │   │  │   /webhook
                          ▼   │  ▼
                ┌──────────┐  │ ┌─────────────┐
                │ ohana-ai │  │ │ohana-seller │
                │  :8001   │  │ │  :8002      │
                │ svc_ohana│  │ │ svc_seller  │
                │   _ai    │  │ │             │
                └────┬─────┘  │ └──────┬──────┘
                     │        │        │
                     │        │        ▼
                     │        │  ┌─────────────┐
                     │        │  │ohana-worker │
                     │        │  │ (no port)   │
                     │        │  │ svc_seller  │
                     │        │  └──────┬──────┘
                     ▼        │         │
                ┌─────────────┴─────────┴─────┐
                │      postgres:16            │
                │  (pgvector, isolated by     │
                │   role grant — I2/I14)      │
                └─────────────────────────────┘
                                              
                ┌─────────────┐   ┌───────────────┐
                │  redis:7    │   │  langfuse:2   │
                │ (counter,   │   │ (self-host,   │
                │  no persist)│   │  UI :3000     │
                │             │   │  localhost    │
                │             │   │  only)        │
                └─────────────┘   └───────────────┘
```

## Chưa làm — backlog

- **CI auto-build + push image** — hiện `build:` local. Chuyển sang GHCR/Docker Hub khi có CI budget.
- **Blue-green / rolling multi-replica** — hiện restart = 5s downtime. Muốn zero-downtime cần thêm HAProxy hoặc traefik với health-check-based routing.
- **Prometheus + Grafana + Loki** — hiện log = `docker compose logs`. Alerting = mắt người.
- **Auto-rotate password DB** — hiện thủ công `ALTER ROLE` + edit `.env.prod` + restart.
- **Multi-host** — 1 container = 1 host hiện tại. Chuyển swarm/k8s khi có > 1 node.
- **Certbot vào compose** — hiện chạy trên host. Đóng gói vào chỉ khi mất quyền host.
