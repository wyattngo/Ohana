#!/bin/bash
# Pre-deployment checklist
# Verifies all infrastructure is ready before deploying

set -euo pipefail

CHECKS_PASSED=0
CHECKS_FAILED=0

check() {
    local name=$1
    local cmd=$2
    
    echo -n "▶ $name ... "
    if eval "$cmd" &> /dev/null; then
        echo "✓"
        ((CHECKS_PASSED++))
    else
        echo "✗"
        ((CHECKS_FAILED++))
    fi
}

echo "Deployment Pre-flight Checklist"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

echo "Local environment:"
check "Python 3.11+" "python3 --version | grep -q '3.1[1-9]'"
check "Docker installed" "docker --version"
check "Docker compose v2" "docker compose version"
check "Git installed" "git --version"
check "OpenSSL installed" "openssl version"

echo ""
echo "Repository state:"
check "On main branch" "git rev-parse --abbrev-ref HEAD | grep -q '^main$'"
check "No uncommitted changes" "[[ -z \$(git status --porcelain) ]]"
check "No untracked files (optional)" "[[ \$(git ls-files --others --exclude-standard | wc -l) -eq 0 ]]" || true

echo ""
echo "Configuration files:"
check ".env exists" "[[ -f .env ]]"
check ".env.prod exists" "[[ -f .env.prod ]]"
check "Dockerfile exists" "[[ -f Dockerfile ]]"
check "docker-compose.yml exists" "[[ -f docker-compose.yml ]]"
check "docker-compose.prod.yml exists" "[[ -f docker-compose.prod.yml ]]"

echo ""
echo "Dependencies:"
check "pyproject.toml valid" "python -m tomllib < pyproject.toml"
check "All Python deps available" "python -c 'import fastapi, sqlalchemy, alembic, pydantic' 2>/dev/null"

echo ""
echo "GitHub setup:"
check "GitHub CLI installed (optional)" "gh --version" || true
check ".github/workflows/ exists" "[[ -d .github/workflows ]]"
check "CI workflow exists" "[[ -f .github/workflows/ci.yml ]]"
check "Deploy workflow exists" "[[ -f .github/workflows/deploy.yml ]]"

echo ""
echo "Local database (dev):"
check "Docker postgres reachable" "docker compose ps postgres 2>/dev/null | grep -q running" || true

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Passed: $CHECKS_PASSED"
echo "Failed: $CHECKS_FAILED"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [[ $CHECKS_FAILED -gt 0 ]]; then
    echo "⚠ Fix issues above before deploying"
    exit 1
else
    echo "✓ All checks passed!"
    echo ""
    echo "Next steps:"
    echo "  1. Push to main: git push origin main"
    echo "  2. Wait for CI: github.com/user/ohana-be/actions"
    echo "  3. Deploy: ./scripts/deploy.sh production latest"
fi
