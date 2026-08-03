#!/bin/bash
# Health check and alert for Ohana services
# Run via cron every 5 minutes to detect issues early

set -euo pipefail

ALERT_EMAIL="${ALERT_EMAIL:-ops@example.com}"
ENDPOINTS=(
    "http://127.0.0.1:8001/health"
    "http://127.0.0.1:8002/health"
)

check_endpoint() {
    local url=$1
    local timeout=5
    local http_code=$(curl -s -o /dev/null -w "%{http_code}" --max-time $timeout "$url" 2>/dev/null || echo "000")
    
    if [[ "$http_code" == "200" ]]; then
        return 0
    else
        return 1
    fi
}

failed_services=()

for endpoint in "${ENDPOINTS[@]}"; do
    if ! check_endpoint "$endpoint"; then
        failed_services+=("$endpoint")
    fi
done

if [[ ${#failed_services[@]} -gt 0 ]]; then
    # Check if this is a transient failure (retry once)
    sleep 5
    failed_retry=()
    for endpoint in "${failed_services[@]}"; do
        if ! check_endpoint "$endpoint"; then
            failed_retry+=("$endpoint")
        fi
    done
    
    if [[ ${#failed_retry[@]} -gt 0 ]]; then
        # Alert
        {
            echo "ALERT: Ohana services down"
            echo "Failed endpoints: ${failed_retry[*]}"
            echo ""
            echo "Docker status:"
            docker compose -f docker-compose.prod.yml ps
            echo ""
            echo "Recent logs:"
            docker compose -f docker-compose.prod.yml logs --tail=20 ohana-ai
        } | mail -s "🚨 Ohana Health Alert" "$ALERT_EMAIL"
        
        # Attempt auto-restart
        docker compose -f docker-compose.prod.yml restart ${failed_retry[*]//http:\/\/127.0.0.1:/} || true
        exit 1
    fi
fi

exit 0
