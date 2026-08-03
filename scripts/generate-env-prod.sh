#!/bin/bash
# Generate production environment file with secure random passwords
# Usage: ./scripts/generate-env-prod.sh [output-file]

set -euo pipefail

OUTPUT_FILE="${1:-.env.prod}"

if [[ -f "$OUTPUT_FILE" ]]; then
    read -p "$OUTPUT_FILE already exists. Overwrite? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

echo "Generating production environment file: $OUTPUT_FILE"
echo ""

# Generate hex passwords (48 chars = openssl rand -hex 24)
gen_pw() {
    openssl rand -hex 24
}

cat > "$OUTPUT_FILE" << EOF
# Ohana BE — Production Environment
# Generated: $(date -u +'%Y-%m-%dT%H:%M:%SZ')
# DO NOT COMMIT THIS FILE

# ── App ──────────────────────────────────────────────────────────────────
ENV=prod
APP_VERSION=\$(git rev-parse --short HEAD)
DEV_AUTH_ENABLED=false

# ── Database ─────────────────────────────────────────────────────────────
POSTGRES_PW=$(gen_pw)
POSTGRES_DB=ohana
POSTGRES_USER=ohana
MIGRATOR_PW=$(gen_pw)
SVC_A_PW=$(gen_pw)
SVC_B_PW=$(gen_pw)
MCP_RO_PW=$(gen_pw)

# ── LLM Chat (Tầng 2) ───────────────────────────────────────────────────
# Get from Anthropic console (console.anthropic.com)
ANTHROPIC_API_KEY=sk-ant-
CLAUDE_MODEL_CHAT=claude-3-5-sonnet-20241022
CLAUDE_MODEL_SMART=claude-3-opus-20250219
CLAUDE_CACHE_ENABLED=true
REASONING_MODE=disabled

# ── LLM Embedding (Together) ────────────────────────────────────────────
# Get from together.ai
TOGETHER_API_KEY=

# ── Langfuse (self-host tracing) ────────────────────────────────────────
LANGFUSE_PUBLIC_KEY=pk-lf-
LANGFUSE_SECRET_KEY=sk-lf-
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_DB_PW=$(gen_pw)
LANGFUSE_NEXTAUTH_SECRET=$(gen_pw)
LANGFUSE_SALT=$(gen_pw)
LANGFUSE_PUBLIC_URL=http://localhost:3000

# ── TTS (optional) ──────────────────────────────────────────────────────
ELEVENLABS_API_KEY=
TTS_MODEL=eleven_monolingual_v1
TTS_VOICE_ID=21m00Tcm4TlvDq8ikWAM
TTS_MAX_CHARS_PER_USER_PER_DAY=10000

# ── Redis ────────────────────────────────────────────────────────────────
REDIS_URL=redis://redis:6379/0

# ── Ports ────────────────────────────────────────────────────────────────
OHANA_PG_PORT=5432
OHANA_REDIS_PORT=6379
EOF

chmod 600 "$OUTPUT_FILE"

echo "✓ Generated $OUTPUT_FILE"
echo ""
echo "Next steps:"
echo "  1. Edit $OUTPUT_FILE to fill in API keys:"
echo "     - ANTHROPIC_API_KEY (Anthropic console)"
echo "     - TOGETHER_API_KEY (together.ai)"
echo "     - LANGFUSE_PUBLIC_KEY (mint from Langfuse UI)"
echo "     - LANGFUSE_SECRET_KEY (mint from Langfuse UI)"
echo "     - Optional: ELEVENLABS_API_KEY"
echo ""
echo "  2. Validate with: ./scripts/validate-env.sh --prod"
echo ""
echo "  3. Upload to production server (never commit):"
echo "     scp -P 22 $OUTPUT_FILE deploy@prod.example.com:/opt/ohana-be/"
echo ""
echo "Passwords generated (store in password manager):"
grep "_PW=" "$OUTPUT_FILE" | sed 's/^/  /'
