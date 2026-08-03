#!/bin/bash
# Quick start guide for new deployments
# Usage: ./scripts/quickstart.sh

set -euo pipefail

echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║  Ohana BE — Deployment Quick Start                                ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo ""

STEP=1

step() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "STEP $STEP: $1"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    ((STEP++))
}

step "Prerequisites"
cat << 'EOF'
Verify you have:
  ✓ Python 3.11+
  ✓ Docker + docker-compose
  ✓ Git
  ✓ API keys: Anthropic + Together
  ✓ SSH access to production server (optional)

Run: ./scripts/preflight.sh
EOF

read -p "Continue? (y/n) " -n 1 -r
echo
[[ $REPLY =~ ^[Yy]$ ]] || exit 0

step "Generate Production Environment"
cat << 'EOF'
This generates a .env.prod file with secure passwords.
You'll fill in API keys manually.
EOF

read -p "Generate .env.prod? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [[ -f .env.prod ]]; then
        echo "⚠ .env.prod exists. Skipping generation."
    else
        ./scripts/generate-env-prod.sh
        echo "✓ Edit .env.prod to add API keys"
    fi
else
    echo "Skipped. Make sure .env.prod exists before deploying."
fi

step "Validate Configuration"
./scripts/validate-env.sh --prod || {
    echo "✗ Fix errors in .env.prod"
    exit 1
}
echo "✓ Configuration valid"

step "Local Testing"
echo "Running local tests..."
python -m pytest tests/contract/ -q || {
    echo "✗ Tests failed"
    exit 1
}
echo "✓ All tests passed"

step "Push to Repository"
cat << 'EOF'
Commit your changes and push to main:
  git add .github/ deploy/ scripts/ .env.example
  git commit -m "Add deployment pipeline"
  git push origin main

Wait for GitHub Actions to pass (5-10 minutes):
  github.com/your-org/ohana-be/actions
EOF

read -p "Continue after GitHub Actions passes? (y/n) " -n 1 -r
echo
[[ $REPLY =~ ^[Yy]$ ]] || exit 0

step "Deploy to Production"
cat << 'EOF'
Choose deployment method:

A) Via GitHub UI (easiest):
   1. Go to: Actions → Deploy to Production
   2. Click: Run workflow
   3. Select: environment = production, image_tag = latest

B) Via CLI:
   ./scripts/deploy.sh production latest
EOF

read -p "Deploy now? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    ./scripts/deploy.sh production latest
else
    echo "Manual deployment: ./scripts/deploy.sh production latest"
fi

step "Verify Deployment"
cat << 'EOF'
After deployment:
  1. Check status: ./scripts/status.sh
  2. Test endpoints: curl -sf http://127.0.0.1:8001/health
  3. Review logs: docker compose -f docker-compose.prod.yml logs --tail=20
EOF

step "Setup Monitoring & Backups"
cat << 'EOF'
Add to production server crontab:

  0 2 * * * cd /opt/ohana-be && ./scripts/backup.sh
  */5 * * * * cd /opt/ohana-be && ./scripts/health-check.sh

Or: crontab scripts/crontab.example
EOF

echo ""
echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║  ✓ Deployment complete!                                           ║"
echo "║                                                                    ║"
echo "║  Next steps:                                                       ║"
echo "║  • Monitor: ./scripts/status.sh (run periodically)                 ║"
echo "║  • On issues: ./scripts/rescue.sh <service>                       ║"
echo "║  • Docs: cat deploy/README.md                                     ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
