# Deployment Scripts Reference

Quick reference for all deployment automation scripts.

## Development Setup

### `./scripts/preflight.sh`
Pre-deployment validation checklist. Verifies Python, Docker, git, configuration files.

```bash
./scripts/preflight.sh
# Output: list of all checks, passes only if everything ready
```

### `./scripts/validate-env.sh [--prod]`
Validates environment file completeness and format.

```bash
./scripts/validate-env.sh           # Validate .env (dev)
./scripts/validate-env.sh --prod    # Validate .env.prod
```

### `./scripts/generate-env-prod.sh [file]`
Generates `.env.prod` with secure random passwords.

```bash
./scripts/generate-env-prod.sh .env.prod
# Generates 5 database passwords (stored in password manager)
```

## Deployment

### `./scripts/deploy.sh <environment> <tag>`
Wrapper for safe deployments. Runs local checks, then triggers GitHub Actions.

```bash
./scripts/deploy.sh production latest       # Deploy production
./scripts/deploy.sh staging my-feature      # Deploy staging
```

Options:
- Environment: `staging` or `production`
- Tag: Docker image tag (SHA or `latest`)

## Operations

### `./scripts/status.sh`
Real-time dashboard of production services, health checks, resource usage.

```bash
./scripts/status.sh
# Shows: docker ps, health checks, logs, cpu/memory, disk usage, backups
```

### `./scripts/rescue.sh <service>`
Automated recovery for stuck services. Checks logs, restarts, verifies health.

```bash
./scripts/rescue.sh ohana-ai        # Restart chat API
./scripts/rescue.sh ohana-worker    # Restart background worker
./scripts/rescue.sh all             # Restart all services (careful!)
```

Services:
- `ohana-ai` — Tầng 2 chat + Tầng 3 luồng A
- `ohana-seller` — Tầng 3 luồng B
- `ohana-worker` — Background worker
- `redis` — Redis cache
- `postgres` — PostgreSQL (⚠ risky)
- `all` — Everything

### `./scripts/backup.sh`
Manual database backup. Also runs on cron (see `crontab.example`).

```bash
./scripts/backup.sh
# Creates: .backups/ohana_YYYY-MM-DD.dump (daily), langfuse dump (weekly)
# Rotates: backups older than 30 days deleted
```

### `./scripts/health-check.sh`
5-minute health check for cron. Alerts and auto-restarts on failure.

```bash
./scripts/health-check.sh
# Checks: http://127.0.0.1:8001/health, :8002/health
# On failure: email alert (if ALERT_EMAIL set) + auto restart
```

Add to crontab:
```bash
*/5 * * * * cd /opt/ohana-be && ./scripts/health-check.sh
```

### `./scripts/setup-systemd.sh` (requires sudo)
Creates systemd service units for production (alternative to docker-compose).

```bash
sudo ./scripts/setup-systemd.sh
# Creates: /etc/systemd/system/ohana-{ai,seller,worker}.service
```

Then:
```bash
sudo systemctl enable --now ohana-*
sudo systemctl status ohana-*
sudo journalctl -u ohana-ai -f
```

## Cron Setup

Add to `crontab -e`:

```bash
# Daily backup at 2 AM
0 2 * * * cd /opt/ohana-be && ./scripts/backup.sh >> /var/log/ohana-backup.log 2>&1

# Health check every 5 minutes
*/5 * * * * cd /opt/ohana-be && ./scripts/health-check.sh >> /var/log/ohana-health.log 2>&1

# Weekly password rotation reminder (optional)
0 3 * * 1 echo "Check if password rotation needed" | mail -s "Ohana reminder" ops@example.com
```

Or use provided template:
```bash
crontab scripts/crontab.example
```

## GitHub Actions

All scripts are integrated with CI/CD:

### `.github/workflows/ci.yml` (on every push)
- Lint (ruff), type check (mypy), import validation
- Run test suite + contract tests
- Build Docker images

### `.github/workflows/deploy.yml` (manual trigger)
- SSH into target server
- Pull images, run migrations + bootstrap
- Verify isolation contracts
- Restart services with healthchecks

Trigger from GitHub UI:
1. **Actions → Deploy to Production**
2. **Run workflow**
3. Select environment + image tag

## Troubleshooting

| Problem | Check | Fix |
|---------|-------|-----|
| `./scripts/deploy.sh` fails | DNS/SSH? | `ssh -i ~/.ssh/deploy_key deploy@prod.example.com` |
| Preflight fails | Missing python/docker | Install via package manager |
| Env validation fails | Missing `.env.prod` | `./scripts/generate-env-prod.sh` |
| Services won't start | Check logs | `./scripts/status.sh`, then `docker compose logs <svc>` |
| OOM / high CPU | Resource check | `./scripts/status.sh`, increase limits or scale up |
| Database won't recover | Data corruption? | Restore from backup: `./scripts/backup.sh` to find latest dump |

## Full deployment workflow

1. **Local** (10 min)
   ```bash
   ./scripts/preflight.sh          # All checks pass?
   git push origin main            # CI triggered
   ```

2. **Wait** (5 min)
   - GitHub Actions runs tests + builds images
   - Check: github.com/user/ohana-be/actions

3. **Deploy** (10 min)
   ```bash
   ./scripts/deploy.sh production latest
   # Or manually via GitHub UI
   ```

4. **Verify** (5 min)
   ```bash
   ./scripts/status.sh
   curl -sf https://api.example.com/health
   ```

5. **Done!**
   - Backups running on cron
   - Health monitoring active
   - Ready to serve traffic

See also: [DEPLOY.md](../DEPLOY.md), [deploy/CHECKLIST.md](CHECKLIST.md), [deploy/README.md](README.md)
