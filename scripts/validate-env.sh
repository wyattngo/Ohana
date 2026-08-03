#!/bin/bash
# Validate environment configuration before deployment
# Usage: ./scripts/validate-env.sh [--prod]

set -euo pipefail

PROD_MODE=false
if [[ "${1:-}" == "--prod" ]]; then
    PROD_MODE=true
fi

ENV_FILE="${ENV_FILE:-.env}"
if [[ "$PROD_MODE" == "true" ]]; then
    ENV_FILE=".env.prod"
fi

if [[ ! -f "$ENV_FILE" ]]; then
    echo "✗ $ENV_FILE not found"
    exit 1
fi

echo "Validating $ENV_FILE..."
echo ""

# Track errors
ERRORS=0

# Helper function
check_var() {
    local var=$1
    local description=$2
    local optional=${3:-false}
    
    local value=$(grep "^$var=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || echo "")
    
    if [[ -z "$value" ]] || [[ "$value" == "change-me"* ]]; then
        if [[ "$optional" == "true" ]]; then
            echo "⊘ $var (optional, skipped)"
        else
            echo "✗ $var — missing or not set"
            ((ERRORS++))
        fi
    else
        # Validate format if applicable
        case "$var" in
            *_PW)
                if [[ ! "$value" =~ ^[a-f0-9]{48}$ ]]; then
                    echo "⚠ $var — not 48-char hex (generated via openssl rand -hex 24)"
                fi
                ;;
            ANTHROPIC_API_KEY)
                if [[ ! "$value" =~ ^sk-ant- ]]; then
                    echo "✗ $var — invalid format (should start with 'sk-ant-')"
                    ((ERRORS++))
                fi
                ;;
            TOGETHER_API_KEY)
                if [[ -z "$value" ]]; then
                    echo "✗ $var — required for embedding"
                    ((ERRORS++))
                fi
                ;;
        esac
        echo "✓ $var"
    fi
}

echo "Required (database & auth):"
check_var "POSTGRES_PW"
check_var "MIGRATOR_PW"
check_var "SVC_A_PW"
check_var "SVC_B_PW"
check_var "MCP_RO_PW"

echo ""
echo "Required (LLM chat):"
check_var "ANTHROPIC_API_KEY"
check_var "CLAUDE_MODEL_CHAT"
check_var "CLAUDE_MODEL_SMART"

echo ""
echo "Required (LLM embedding):"
check_var "TOGETHER_API_KEY"

echo ""
echo "Required (Langfuse tracing):"
check_var "LANGFUSE_PUBLIC_KEY"
check_var "LANGFUSE_SECRET_KEY"
check_var "LANGFUSE_HOST"
check_var "LANGFUSE_DB_PW"
check_var "LANGFUSE_NEXTAUTH_SECRET"
check_var "LANGFUSE_SALT"

if [[ "$PROD_MODE" == "false" ]]; then
    echo ""
    echo "Optional (dev):"
    check_var "DEV_AUTH_ENABLED" "enable mock auth" true
    check_var "ELEVENLABS_API_KEY" "TTS support" true
    check_var "OLLAMA_BASE_URL" "local LLM fallback" true
fi

echo ""
if [[ $ERRORS -gt 0 ]]; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "✗ $ERRORS error(s) found"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    exit 1
else
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "✓ Environment is valid"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
fi
