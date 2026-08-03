# Deployment Pipeline — Complete Reference

Your Ohana BE app now has a production-grade deployment pipeline. This document summarizes what's been created and how to use it.

## 📦 What's Included

### GitHub Actions Workflows
- **`.github/workflows/ci.yml`** — Automated testing on every push
  - Linting (ruff), type checking (mypy), import validation
  - Full test suite including contract/isolation tests
  - Docker image build & push on `main` branch

- **`.github/workflows/deploy.yml`** — Manual production deployment
  - SSH into target server, pull images
  - Run migrations + bootstrap
  - Verify isolation contracts post-deploy
  - Service healthchecks

### Deployment Scripts (in `scripts/`)

| Script | Purpose |
|--------|---------|
| `quickstart.sh` | 🚀 **Start here** — interactive setup guide |
| `preflight.sh` | Pre-deployment validation checklist |
| `generate-env-prod.sh` | Create `.env.prod` with secure passwords |
| `validate-env.sh` | Check environment file completeness |
| `deploy.sh` | Trigger deployment (local or GitHub Actions) |
| `status.sh` | Real-time dashboard (services, health, resources) |
| `rescue.sh` | Emergency service recovery |
| `backup.sh` | Database backups + rotation |
| `health-check.sh` | 5-minute monitoring (for cron) |
| `setup-systemd.sh` | Create systemd service units |

### Configuration & Documentation

- **`.env.example`** — Template with all 32 environment variables documented
- **`.github/SECRETS.md`** — Step-by-step GitHub Secrets setup
- **`deploy/README.md`** — Pipeline architecture & troubleshooting
- **`deploy/CHECKLIST.md`** — Production deployment checklist (print & check off)

## 🚀 Getting Started (15 minutes)

### 1. Local Setup

```bash
# Run interactive quickstart
./scripts/quickstart.sh

# Or manually:
./scripts/preflight.sh              # Verify prerequisites
./scripts/generate-env-prod.sh      # Create .env.prod
./scripts/validate-env.sh --prod    # Validate config
```

### 2. Configure GitHub

```bash
# Add secrets to GitHub repo settings (see .github/SECRETS.md):
# - DOCKERHUB_USERNAME & DOCKERHUB_TOKEN
# - DEPLOY_HOST, DEPLOY_USER, DEPLOY_PORT
# - DEPLOY_KEY (SSH private key, base64-encoded)
```

### 3. Push to Main

```bash
git add .github/ deploy/ scripts/ .env.example
git commit -m "Add production deployment pipeline"
git push origin main

# Wait for CI to pass (GitHub Actions → check your repo)
```

### 4. Deploy

```bash
# Option A: GitHub UI
# Actions → Deploy to Production → Run workflow

# Option B: CLI
./scripts/deploy.sh production latest

# Option C: Manual SSH
ssh deploy@prod.example.com
cd /opt/ohana-be && docker compose -f docker-compose.prod.yml up -d
```

## 📊 Key Features

### Automated Testing
- Contract tests verify isolation gates (I1, I2, I14, I15)
- Tests run on every push + before deployment
- Prevents breaking changes from reaching production

### Safe Deployments
- Pre-flight checks verify all prerequisites
- Blue-green deploy possible (control image tag)
- Healthchecks verify services after restart
- Rollback by reverting image tag

### Operations
- Real-time status dashboard (`./scripts/status.sh`)
- Automated daily backups with 30-day retention
- 5-minute health monitoring with auto-restart + alerts
- Emergency rescue procedure for stuck services

### Secrets Management
- All passwords generated securely (48-char hex)
- Never committed to git (in `.gitignore`)
- GitHub Secrets for CI/CD
- `.env.prod` stays on server only

## 📋 Daily Operations

### Check Status
```bash
./scripts/status.sh
```
Shows: services, health, CPU/memory, disk space, backup status.

### Monitor Logs
```bash
docker compose -f docker-compose.prod.yml logs -f ohana-ai
```

### If Service Goes Down
```bash
./scripts/rescue.sh ohana-ai      # Auto-restart with diagnostics
```

### Manual Database Operations
```bash
# Backup
./scripts/backup.sh

# Restore
pg_restore -U ohana -d ohana .backups/ohana_YYYY-MM-DD.dump

# Query
docker compose -f docker-compose.prod.yml exec postgres \
  psql -U ohana -d ohana -c "SELECT COUNT(*) FROM schema_migration"
```

## 📚 Documentation

| Document | Purpose |
|----------|---------|
| `scripts/README.md` | Script reference & troubleshooting |
| `deploy/README.md` | Pipeline architecture & setup |
| `deploy/CHECKLIST.md` | Print & check off during deployment |
| `.github/SECRETS.md` | GitHub Secrets configuration |
| `DEPLOY.md` (existing) | Full deployment guide (§0-§13) |

## 🔧 Customization

### Change Docker Registry
Edit `.github/workflows/ci.yml`:
```yaml
tags: |
  my-registry.com/ohana-be:${{ steps.meta.outputs.image-short }}
```

### Change Backup Retention
Edit `scripts/backup.sh`:
```bash
RETENTION_DAYS=60  # Keep 60 days instead of 30
```

### Change Health Check Frequency
Edit `scripts/crontab.example`:
```bash
*/2 * * * * ...  # Every 2 minutes instead of 5
```

### Scale Services
Edit `docker-compose.prod.yml` or systemd units to add replicas:
```bash
sudo docker service scale ohana-ai=3
```

## 🚨 Emergency Procedures

### Service Crash Loop
```bash
./scripts/rescue.sh <service>   # Diagnostic restart
docker compose -f docker-compose.prod.yml logs <service>
```

### Database Corruption
```bash
./scripts/backup.sh                    # See available backups
docker compose -f docker-compose.prod.yml stop
pg_restore -U ohana -d ohana .backups/ohana_YYYY-MM-DD.dump
docker compose -f docker-compose.prod.yml start
```

### Out of Disk Space
```bash
./scripts/status.sh                    # Check disk usage
docker system prune                    # Clean up old images
df -h                                  # Verify freed space
```

### Certificate Expired
```bash
# If using Let's Encrypt (certbot on host):
sudo certbot renew
sudo systemctl reload nginx
```

## ✅ Pre-Production Checklist

Before going live:

- [ ] Run `./scripts/preflight.sh` — all checks pass
- [ ] Run `pytest tests/contract/ -q` — 4 tests pass
- [ ] Generate `.env.prod` with real API keys
- [ ] Configure GitHub Secrets (see `.github/SECRETS.md`)
- [ ] Test deployment to staging first
- [ ] Print `deploy/CHECKLIST.md` and check off as you go
- [ ] Set up cron jobs for backup + health checks
- [ ] Test backup restore procedure
- [ ] Verify DNS points to new server
- [ ] Load test with expected traffic profile

## 📖 Detailed Guides

For step-by-step instructions:
1. **First-time deployment?** → See `deploy/CHECKLIST.md` (print it)
2. **Troubleshooting?** → See `scripts/README.md`
3. **Architecture questions?** → See `deploy/README.md`
4. **GitHub setup?** → See `.github/SECRETS.md`
5. **Full reference?** → See `DEPLOY.md` (existing guide)

## 🤝 Support

### Common Issues

| Issue | Solution |
|-------|----------|
| Workflow won't run | Check `.github/workflows/` is committed, branch is `main` |
| SSH fails | Verify `DEPLOY_KEY` is PEM format, host is reachable |
| Services won't start | Check `.env.prod` has all 32 vars, run `./scripts/validate-env.sh --prod` |
| Healthcheck fails | Run `./scripts/status.sh` to see current state |
| Tests fail locally | Run `pytest -v` to see specific failures |

### Getting Help

1. Check logs: `docker compose -f docker-compose.prod.yml logs <service>`
2. Check status: `./scripts/status.sh`
3. Run diagnostics: `./scripts/rescue.sh <service>`
4. Review docs: `deploy/README.md` or `scripts/README.md`

---

**You're ready to deploy!** Start with:
```bash
./scripts/quickstart.sh
```
