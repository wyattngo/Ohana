#!/bin/bash
# Quick status dashboard for production deployment
# Usage: ./scripts/status.sh

set -euo pipefail

show_status() {
    local label=$1
    local cmd=$2
    
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "$label"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    eval "$cmd"
    echo ""
}

show_status "Docker Services" "docker compose -f docker-compose.prod.yml ps"

show_status "Health Checks" "
for port in 8001 8002; do
    http_code=\$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:\$port/health 2>/dev/null || echo '000')
    if [[ \$http_code == '200' ]]; then
        echo \"✓ Port \$port: healthy\"
    else
        echo \"✗ Port \$port: HTTP \$http_code\"
    fi
done
"

show_status "Recent Logs (last 10 lines)" "
docker compose -f docker-compose.prod.yml logs --tail=10 | sed 's/^/  /'
"

show_status "Resource Usage" "
docker stats --no-stream --format 'table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}'
"

show_status "Database Status" "
docker compose -f docker-compose.prod.yml exec -T postgres pg_isready -U ohana -d ohana && echo '✓ Database healthy' || echo '✗ Database down'
"

show_status "Redis Status" "
docker compose -f docker-compose.prod.yml exec -T redis redis-cli ping && echo '✓ Redis healthy' || echo '✗ Redis down'
"

show_status "Disk Space" "
df -h / | tail -1 | awk '{print \"  Root: \" \$5 \" used (\"\$4\" free)\"}'
docker system df | grep -E 'Type|Images|Containers|Local Volumes'
"

show_status "Backup Status" "
if [[ -d .backups ]]; then
    echo \"Backups: \$(find .backups -name '*.dump' | wc -l) files\"
    ls -lhS .backups/*.dump 2>/dev/null | tail -3 | awk '{print \"  \"\$9 \" (\" \$5 \")\"}' || echo \"  No backups yet\"
else
    echo \"  .backups directory not found\"
fi
"
