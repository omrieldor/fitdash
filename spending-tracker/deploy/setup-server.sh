#!/bin/bash
# ============================================================
# SpendTrack — Oracle Cloud Setup Script
# Runs SpendTrack as a second, independent service alongside
# the existing "eldorado" (fitdash) app on the same VM.
# Usage: bash setup-server.sh
# ============================================================

set -e

REPO_DIR="/home/ubuntu/fitdash"
APP_DIR="$REPO_DIR/spending-tracker"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="spendtrack"
PORT=5001

echo ""
echo "=== SpendTrack — Server Setup ==="
echo ""

# --- 1. Repo (reuses the same fitdash clone; pulls latest if it exists) ---
echo "[1/5] Ensuring repo is present..."
if [ -d "$REPO_DIR" ]; then
    cd "$REPO_DIR" && git pull
else
    git clone https://github.com/omrieldor/fitdash.git "$REPO_DIR"
fi

# --- 2. Python venv + deps (own venv, own requirements.txt) ---
echo "[2/5] Setting up Python environment..."
cd "$APP_DIR"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

# --- 3. Generate secret key & create systemd service ---
echo "[3/5] Creating systemd service..."
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=SpendTrack Spending Tracker
After=network.target

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=${APP_DIR}
ExecStart=${VENV_DIR}/bin/gunicorn -w 2 -b 127.0.0.1:${PORT} app:app
Environment=SECRET_KEY=${SECRET}
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl start ${SERVICE_NAME}

# --- 4. Nginx: dedicated server block on its own port ---
# Flask templates here use root-relative paths (/static/..., fetch('/api/...'), etc.),
# so a path-prefix like /spending/ would need extra rewrite-aware middleware to work
# correctly. The simplest correct setup is its own server block on a distinct port —
# no path-rewriting, no new DNS record needed, app just answers at its own port.
echo "[4/5] Configuring Nginx on its own port (8081) ..."
SERVER_IP=$(curl -s ifconfig.me)

sudo tee /etc/nginx/sites-available/${SERVICE_NAME} > /dev/null <<EOF
server {
    listen 8081;
    server_name ${SERVER_IP};

    client_max_body_size 3M;

    location / {
        proxy_pass http://127.0.0.1:${PORT};
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/${SERVICE_NAME} /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx

echo "  App will be reachable at: http://${SERVER_IP}:8081"
echo "  If you already run add-ssl.sh's cert for a domain, you can instead point a"
echo "  DNS A record (e.g. spend.<yourdomain>) at this server and add SSL for it the"
echo "  same way fitdash's deploy/add-ssl.sh did, then proxy that server_name to"
echo "  127.0.0.1:${PORT} at port 443 instead of exposing raw port 8081."

# --- 5. Done ---
echo "[5/5] Done!"
echo ""
echo "=========================================="
echo "  DONE! Your app is live at:"
echo "  http://${SERVER_IP}:8081"
echo ""
echo "  Secret key saved in systemd service."
echo "  SQLite DB will persist at:"
echo "  ${APP_DIR}/instance/spending.db"
echo "=========================================="
echo ""
