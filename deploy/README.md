# Deployment Pipeline for Ohana BE

This directory contains deployment automation and documentation for Ohana BE across environments.

## Files

- **ci.yml** — GitHub Actions workflow for testing, linting, type checking, and building Docker images
- **deploy.yml** — GitHub Actions workflow for deploying to staging/production with safety checks
- **deploy.sh** — Local deployment helper script

## Quick Start

### 1. Set up GitHub secrets (one-time)

In your GitHub repository settings, add:

| Secret | Purpose |
|--------|---------|
| `DOCKERHUB_USERNAME` | Docker Hub account for image registry |
| `DOCKERHUB_TOKEN` | Docker Hub token (personal access token) |
| `DEPLOY_HOST` | Production/staging server hostname or IP |
| `DEPLOY_USER` | SSH user (e.g., `deploy`) |
| `DEPLOY_PORT` | SSH port (default `22`) |
| `DEPLOY_KEY` | SSH private key (base64 or PEM) |

**Generate deploy key:**
```bash
ssh-keygen -t ed25519 -f /tmp/deploy_key -N ""
cat /tmp/deploy_key | base64 -w0  # Copy into DEPLOY_KEY secret
```

Then add public key to target server's `~/.ssh/authorized_keys`.

### 2. Merge to main branch

- Push to `develop` for testing
- Open PR and ensure CI passes
- Merge to `main` → images auto-build and push to Docker Hub

### 3. Deploy

**Option A: Via GitHub UI**
- Go to **Actions → Deploy to Production**
- Click **Run workflow**
- Select environment (staging/production) and image tag

**Option B: Via CLI**
```bash
./scripts/deploy.sh production latest
```

**Option C: Manual SSH**
```bash
ssh deploy@prod.example.com
cd /opt/ohana-be
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

## Pipeline Stages

### 1. CI (on every push)
- ✓ Lint with `ruff`
- ✓ Type check with `mypy`
- ✓ Import structure validation with `lint-imports`
- ✓ Run pytest suite
- ✓ Verify isolation contracts

### 2. Build (on main branch only)
- ✓ Build backend image (Python + FastAPI)
- ✓ Build frontend image (Node + Vite)
- ✓ Push to Docker Hub with SHA tag and `latest`

### 3. Deploy (manual trigger)
- ✓ SSH into target server
- ✓ Pull latest images
- ✓ Run Alembic migrations
- ✓ Run bootstrap (idempotent)
- ✓ Verify isolation contracts on target
- ✓ Update docker-compose.prod.yml
- ✓ Restart services with healthchecks
- ✓ Verify endpoints respond

## Monitoring

### During deployment
```bash
# Watch logs
ssh deploy@prod.example.com
docker compose -f docker-compose.prod.yml logs -f

# Check service health
docker compose -f docker-compose.prod.yml ps
```

### Post-deployment
```bash
# Verify healthchecks
curl -sf https://api.example.com/health

# Check database
docker compose -f docker-compose.prod.yml exec postgres psql -U ohana -c "SELECT VERSION();"

# Verify isolation
pytest tests/contract/ -q
```

## Rollback

If deployment fails:

```bash
ssh deploy@prod.example.com
cd /opt/ohana-be

# Restore previous image tag
sed -i 's|:latest|:PREVIOUS_SHA|g' docker-compose.prod.yml

# Restart
docker compose -f docker-compose.prod.yml up -d

# Verify
docker compose -f docker-compose.prod.yml ps
curl -sf http://127.0.0.1:8001/health
```

## Secrets management

All sensitive values are stored in:
- **GitHub Secrets** — for CI/CD workflows
- **.env.prod** — on target server (never committed)
- **SSH private key** — on your machine, in `~/.ssh/deploy_key`

Never commit `.env.prod` or private keys to git.

## Troubleshooting

| Issue | Solution |
|-------|----------|
| CI fails on lint | Run `ruff check --fix .` locally and commit |
| CI fails on type check | Run `mypy api agent app ...` to see errors |
| Deploy fails on SSH | Verify `DEPLOY_KEY` is PEM format, host is reachable |
| Deploy fails on migration | Check logs: `docker compose logs ohana-migrate` |
| Services won't start | Verify `.env.prod` has all 32 required vars |
| Healthchecks failing | Check logs: `docker compose logs ohana-ai`, `curl -v http://127.0.0.1:8001/health` |

See [DEPLOY.md](../DEPLOY.md) for full deployment guide.
