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
ENV_FILE="$APP_DIR/.env"
SERVICE_NAME="spendtrack"
PORT=5001
HTTP_PORT=8081

echo ""
echo "=== SpendTrack — Server Setup ==="
echo ""

# --- 1. Repo (reuses the same fitdash clone) ---
echo "[1/6] Ensuring repo is present..."
if [ -d "$REPO_DIR" ]; then
    cd "$REPO_DIR" && git pull
else
    git clone https://github.com/omrieldor/fitdash.git "$REPO_DIR"
fi

# --- 2. Python venv + deps (own venv, own requirements.txt) ---
echo "[2/6] Setting up Python environment..."
cd "$APP_DIR"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

# --- 3. Secrets: app key + VAPID keypair for push ---
echo "[3/6] Generating secrets..."
if [ -f "$ENV_FILE" ]; then
    echo "  $ENV_FILE already exists — keeping existing keys."
    echo "  (Regenerating VAPID keys would invalidate your phone's subscription.)"
else
    SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    {
        echo "SECRET_KEY=${SECRET}"
        python3 generate_vapid_keys.py
        echo "VAPID_SUBJECT=mailto:omrieldor@gmail.com"
        echo "# Get a free key at https://twelvedata.com and paste it here for live prices:"
        echo "MARKET_DATA_API_KEY="
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo "  Wrote $ENV_FILE"
fi

# --- 4. systemd service for the web app ---
echo "[4/6] Creating systemd service..."
sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=SpendTrack Spending Tracker
After=network.target

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/gunicorn -w 2 -b 127.0.0.1:${PORT} app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl restart ${SERVICE_NAME}

# --- 5. Monthly reminder timer (hourly tick; the script decides when to fire) ---
echo "[5/6] Installing monthly reminder timer..."
sudo tee /etc/systemd/system/${SERVICE_NAME}-reminder.service > /dev/null <<EOF
[Unit]
Description=SpendTrack monthly logging reminder
After=network.target

[Service]
Type=oneshot
User=ubuntu
Group=ubuntu
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python send_reminders.py
EOF

sudo tee /etc/systemd/system/${SERVICE_NAME}-reminder.timer > /dev/null <<EOF
[Unit]
Description=Tick hourly so SpendTrack can fire its 8th-of-month reminder

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now ${SERVICE_NAME}-reminder.timer

# --- 6. Nginx on its own port ---
# Templates use root-relative paths (/static/..., fetch('/api/...')), so a
# path-prefix mount would need rewrite-aware middleware. Its own port keeps
# things simple and needs no DNS record to get started.
echo "[6/6] Configuring Nginx..."
SERVER_IP=$(curl -s ifconfig.me)

sudo tee /etc/nginx/sites-available/${SERVICE_NAME} > /dev/null <<EOF
server {
    listen ${HTTP_PORT};
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
# reload, not restart — picks up the new site without dropping connections to the
# fitness app already being served by this nginx.
sudo systemctl reload nginx

echo ""
echo "=========================================="
echo "  DONE! SpendTrack is live at:"
echo "  http://${SERVER_IP}:${HTTP_PORT}"
echo ""
echo "  Open Oracle's security list and add an ingress rule for TCP ${HTTP_PORT}"
echo "  if you can't reach it."
echo ""
echo "  !! PUSH NOTIFICATIONS NEED HTTPS !!"
echo "  The 8th-of-month reminder will NOT work over plain HTTP — iOS only"
echo "  allows web push for a Home Screen PWA served over TLS. To enable it:"
echo "    1. Point a domain (or a free DuckDNS subdomain) at ${SERVER_IP}"
echo "    2. bash ${APP_DIR}/deploy/add-ssl.sh yourdomain.com"
echo "    3. Open the HTTPS URL on your iPhone, Share -> Add to Home Screen"
echo "    4. Launch it from the Home Screen icon and tap the 🔔 button"
echo ""
echo "  For live portfolio prices, put your Twelve Data key in:"
echo "  ${ENV_FILE}   then: sudo systemctl restart ${SERVICE_NAME}"
echo ""
echo "  SQLite DB persists at: ${APP_DIR}/instance/spending.db"
echo "=========================================="
echo ""
