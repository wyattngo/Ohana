# Production Deployment Checklist

Print this out and check items off during deployment.

## Pre-deployment (Local, ~15 min)

- [ ] Environment setup (§0 DEPLOY.md)
  - [ ] Python 3.11+
  - [ ] Docker + docker compose v2
  - [ ] PostgreSQL client tools
  - [ ] All API keys obtained

- [ ] Code validation (§1)
  - [ ] Run `./scripts/preflight.sh` — all checks pass
  - [ ] Run `pytest tests/contract/ -q` — 4 tests pass
  - [ ] Git: on `main` branch, no uncommitted changes

- [ ] Configuration (§2)
  - [ ] `.env` complete (dev setup)
  - [ ] `.env.prod` generated with `./scripts/generate-env-prod.sh`
  - [ ] Run `./scripts/validate-env.sh --prod` — validates

- [ ] Docker images (§3)
  - [ ] Backend image builds: `docker build .`
  - [ ] Frontend image builds: `docker build web/`
  - [ ] Images pushed to registry (via CI on main)

## Infrastructure setup (Target server, ~1 hour)

- [ ] Server access
  - [ ] Can SSH without password: `ssh -i deploy_key deploy@prod.example.com`
  - [ ] `sudo` access or `/opt/ohana-be` is writable by `deploy`

- [ ] System packages (run once)
  ```bash
  ssh deploy@prod.example.com << 'EOF'
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose git openssl postgresql-client
  sudo usermod -aG docker deploy
  EOF
  ```

- [ ] Directories and permissions
  - [ ] `/opt/ohana-be` directory created
  - [ ] `.backups` directory created (for pg_dump)
  - [ ] `/etc/letsencrypt` mounted or certificate path set
  - [ ] `/var/www/ohana` ready for static files

- [ ] Cron jobs
  - [ ] Backup job added: `0 2 * * * cd /opt/ohana-be && ./scripts/backup.sh`
  - [ ] Health check job added: `*/5 * * * * cd /opt/ohana-be && ./scripts/health-check.sh`

## Initial deployment (Target server, ~30 min)

- [ ] Repository clone
  ```bash
  ssh deploy@prod.example.com
  git clone <repo-url> /opt/ohana-be
  cd /opt/ohana-be
  ```

- [ ] Environment files
  - [ ] `.env.prod` uploaded securely
  - [ ] File permissions: `chmod 600 .env.prod`
  - [ ] Validate: `./scripts/validate-env.sh --prod`

- [ ] Infrastructure services (§3 DEPLOY.md)
  ```bash
  docker compose up -d postgres redis langfuse-db langfuse
  docker compose ps  # all 4 healthy
  ```

- [ ] Database bootstrap (§4)
  ```bash
  export $(grep -v '^#' .env.prod | xargs)
  SUPERUSER_DSN="postgresql://ohana:$POSTGRES_PW@localhost:5432/ohana" \
    python scripts/bootstrap_roles.py
  # Output: 4 lines "role ok: ..."
  ```

- [ ] Verify roles
  ```bash
  docker compose exec postgres psql -U ohana -c "\du"
  # Should see: ohana, ohana_migrator, svc_ohana_ai, svc_seller, mcp_readonly
  ```

- [ ] Database migration (§5)
  ```bash
  DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5432/ohana" \
    alembic upgrade head
  ```

- [ ] Verify isolation contracts (§5)
  ```bash
  pytest tests/contract/ -q
  # Should see: 4 passed
  ```

- [ ] Langfuse setup (§6)
  - [ ] Open Langfuse UI: `http://localhost:3000`
  - [ ] Create account and project "Ohana BE"
  - [ ] Generate API keys
  - [ ] Add to `.env.prod`: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`
  - [ ] Set Models pricing in Langfuse UI

- [ ] Start backend services (§7b)
  ```bash
  docker compose -f docker-compose.prod.yml up -d ohana-ai ohana-seller ohana-worker
  docker compose -f docker-compose.prod.yml ps
  # Wait for all to be healthy (20-30s)
  ```

- [ ] Verify healthchecks
  ```bash
  curl -sf http://127.0.0.1:8001/health && echo "✓ Tầng 2 (AI)"
  curl -sf http://127.0.0.1:8002/health && echo "✓ Luồng B (seller)"
  ```

- [ ] Reverse proxy (§8)
  - [ ] Nginx config created and tested
  - [ ] SSL cert installed (Let's Encrypt)
  - [ ] `nginx -t` passes
  - [ ] `systemctl restart nginx`
  - [ ] HTTPS endpoint responds: `curl -sf https://api.example.com/health`

- [ ] Frontend deployment (§9)
  ```bash
  cd web
  corepack use pnpm@10
  pnpm install --frozen-lockfile
  pnpm build
  sudo cp -r dist/* /var/www/ohana/
  ```

## Post-deployment verification (§10 DEPLOY.md)

- [ ] Services running
  ```bash
  docker compose -f docker-compose.prod.yml ps
  # All services should show running/healthy
  ```

- [ ] Health endpoints all 200
  ```bash
  curl -sf http://127.0.0.1:8001/health
  curl -sf http://127.0.0.1:8002/health
  curl -sf https://api.example.com/health
  ```

- [ ] Contract tests pass
  ```bash
  pytest tests/contract/ -q
  ```

- [ ] Mock test
  ```bash
  curl -sX POST http://127.0.0.1:8001/api/mock/authorize?role=seller -c /tmp/cookies.txt
  curl -sX POST http://127.0.0.1:8001/api/chat \
    -H "Content-Type: application/json" \
    -H "X-CSRF-Token: $(grep ohana_csrf /tmp/cookies.txt | awk '{print $7}')" \
    -b /tmp/cookies.txt \
    -d '{"message":"ping"}' | jq
  ```

- [ ] Langfuse trace visible
  - [ ] Open `http://localhost:3000/traces`
  - [ ] Should see generation from last mock test

- [ ] Logs clean
  ```bash
  docker compose -f docker-compose.prod.yml logs --tail=20 | grep -i error
  # Should see no critical errors
  ```

## Backup & rotation (§11 DEPLOY.md)

- [ ] First backup runs
  ```bash
  ./scripts/backup.sh
  ls -lh .backups/
  # Should see ohana_YYYY-MM-DD.dump
  ```

- [ ] Restore procedure tested (optional but recommended)
  ```bash
  pg_restore -U ohana -d ohana --clean < .backups/ohana_YYYY-MM-DD.dump
  ```

## Handoff

- [ ] Deployment owner: _______________
- [ ] Deployment date: _______________
- [ ] APP_VERSION deployed: _______________
- [ ] Production URL: _______________
- [ ] Monitoring dashboard link: _______________
- [ ] Escalation contact (on-call): _______________

## Issues encountered & resolution

```
(Document any issues and how they were fixed)



```

## Notes for next deployment

```
(Record lessons learned, improvements needed, etc.)



```
