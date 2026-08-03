#!/bin/bash
# Automated recovery procedures for common production issues

set -euo pipefail

rescue_service() {
    local service=$1
    local description=$2
    
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "Rescuing: $description"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    
    # 1. Check current status
    echo "Current status:"
    docker compose -f docker-compose.prod.yml ps "$service" || true
    
    # 2. Check logs for errors
    echo ""
    echo "Recent logs:"
    docker compose -f docker-compose.prod.yml logs --tail=20 "$service" 2>&1 | head -20 || true
    
    # 3. Attempt restart
    echo ""
    echo "Restarting $service..."
    docker compose -f docker-compose.prod.yml restart "$service"
    
    # 4. Wait for health
    echo "Waiting for service to become healthy..."
    sleep 10
    
    # 5. Verify health
    echo ""
    echo "Health check:"
    docker compose -f docker-compose.prod.yml ps "$service"
}

case "${1:-help}" in
    ohana-ai)
        rescue_service "ohana-ai" "Tầng 2 chat + Tầng 3 luồng A"
        ;;
    ohana-seller)
        rescue_service "ohana-seller" "Tầng 3 luồng B (seller API)"
        ;;
    ohana-worker)
        rescue_service "ohana-worker" "Background worker"
        ;;
    redis)
        rescue_service "redis" "Redis cache"
        ;;
    postgres)
        echo "⚠ Database restart may cause data loss!"
        read -p "Continue? (y/n) " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rescue_service "postgres" "PostgreSQL database"
        fi
        ;;
    all)
        for svc in postgres redis ohana-ai ohana-seller ohana-worker; do
            rescue_service "$svc" "$svc"
            echo ""
        done
        ;;
    *)
        echo "Usage: $0 <service> | all"
        echo ""
        echo "Services:"
        echo "  ohana-ai      Restart Tầng 2 chat API"
        echo "  ohana-seller  Restart Tầng 3 seller API"
        echo "  ohana-worker  Restart background worker"
        echo "  redis         Restart Redis cache"
        echo "  postgres      Restart database (⚠ dangerous)"
        echo "  all           Restart all services in order"
        exit 1
        ;;
esac

echo ""
echo "✓ Rescue complete"
