#!/bin/bash
# Setup systemd services for production deployment
# Usage: sudo ./scripts/setup-systemd.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root (sudo)"
    exit 1
fi

REPO_DIR="${REPO_DIR:-.}"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"
VENV_UVICORN="$REPO_DIR/.venv/bin/uvicorn"

if [[ ! -f "$VENV_PYTHON" ]]; then
    echo "✗ Virtual environment not found at $REPO_DIR/.venv"
    exit 1
fi

echo "Setting up systemd services..."
echo ""

# Create ohana-ai.service (Tầng 2 + Tầng 3 luồng A)
cat > /etc/systemd/system/ohana-ai.service << 'SYSTEMD_EOF'
[Unit]
Description=Ohana BE — main_ohana_ai (svc_ohana_ai)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=deploy
WorkingDirectory=/opt/ohana-be
EnvironmentFile=/opt/ohana-be/.env.prod
# Override DATABASE_URL for this service's role
Environment="DATABASE_URL=postgresql+psycopg://svc_ohana_ai:${SVC_A_PW}@localhost:5432/ohana"
ExecStart=/opt/ohana-be/.venv/bin/uvicorn app.main_ohana_ai:app --host 127.0.0.1 --port 8001 --workers 2
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD_EOF

# Create ohana-seller.service (Tầng 3 luồng B)
cat > /etc/systemd/system/ohana-seller.service << 'SYSTEMD_EOF'
[Unit]
Description=Ohana BE — main_seller (svc_seller)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=deploy
WorkingDirectory=/opt/ohana-be
EnvironmentFile=/opt/ohana-be/.env.prod
Environment="DATABASE_URL=postgresql+psycopg://svc_seller:${SVC_B_PW}@localhost:5432/ohana"
ExecStart=/opt/ohana-be/.venv/bin/uvicorn app.main_seller:app --host 127.0.0.1 --port 8002 --workers 2
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD_EOF

# Create ohana-worker.service (background worker)
cat > /etc/systemd/system/ohana-worker.service << 'SYSTEMD_EOF'
[Unit]
Description=Ohana BE — worker_seller (background)
After=network.target docker.service
Requires=docker.service

[Service]
Type=simple
User=deploy
WorkingDirectory=/opt/ohana-be
EnvironmentFile=/opt/ohana-be/.env.prod
Environment="DATABASE_URL=postgresql+psycopg://svc_seller:${SVC_B_PW}@localhost:5432/ohana"
ExecStart=/opt/ohana-be/.venv/bin/python -m app.worker_seller
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
SYSTEMD_EOF

# Reload and enable services
systemctl daemon-reload
systemctl enable ohana-ai ohana-seller ohana-worker

echo "✓ Systemd services created"
echo ""
echo "Start services:"
echo "  sudo systemctl start ohana-ai ohana-seller ohana-worker"
echo ""
echo "Check status:"
echo "  sudo systemctl status ohana-*"
echo ""
echo "View logs:"
echo "  journalctl -u ohana-ai -f"
echo "  journalctl -u ohana-seller -f"
echo "  journalctl -u ohana-worker -f"
