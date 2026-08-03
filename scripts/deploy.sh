#!/bin/bash
# ohana-deploy — wrapper for safe production deployments
# Usage: ./scripts/deploy.sh <environment> <image-tag>

set -euo pipefail

ENVIRONMENT="${1:-production}"
IMAGE_TAG="${2:-latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

if [[ ! "$ENVIRONMENT" =~ ^(staging|production)$ ]]; then
    echo "Error: environment must be 'staging' or 'production'"
    exit 1
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Deploying to: $ENVIRONMENT"
echo "Image tag: $IMAGE_TAG"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Verify prerequisites
echo "▶ Checking prerequisites..."
if ! command -v docker &> /dev/null; then
    echo "✗ Docker not found"
    exit 1
fi

if [[ ! -f "$REPO_ROOT/.env.prod" ]]; then
    echo "✗ .env.prod not found"
    exit 1
fi

# 2. Load environment
echo "▶ Loading environment..."
set -a
source "$REPO_ROOT/.env.prod"
set +a

# 3. Verify database connectivity
echo "▶ Verifying database connectivity..."
if ! pg_isready -h localhost -U ohana &> /dev/null; then
    echo "⚠ Database not accessible locally"
    echo "   (This is expected if deploying via CI — database is on target host)"
fi

# 4. Run local tests (if development)
if [[ "$ENVIRONMENT" == "staging" ]] && python -c "import pytest" 2>/dev/null; then
    echo "▶ Running contract tests..."
    python -m pytest tests/contract/ -q
    echo "✓ Contract tests passed"
fi

# 5. Build images locally (optional)
read -p "Build Docker images locally? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "▶ Building Docker images..."
    docker build -t ohana-be:$IMAGE_TAG .
    docker build -t ohana-web:$IMAGE_TAG ./web
    echo "✓ Images built"
fi

# 6. Trigger GitHub Actions deployment
echo "▶ Triggering GitHub Actions workflow..."
if command -v gh &> /dev/null; then
    gh workflow run deploy.yml \
        -f environment=$ENVIRONMENT \
        -f image_tag=$IMAGE_TAG \
        --ref $(git rev-parse --abbrev-ref HEAD)
    echo "✓ Workflow triggered"
    echo ""
    echo "Monitor progress at:"
    echo "  https://github.com/$(gh repo view --json nameWithOwner -q)/actions"
else
    echo "⚠ GitHub CLI not found — manually trigger deploy workflow"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Deployment initiated!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
