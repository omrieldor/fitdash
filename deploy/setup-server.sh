#!/bin/bash
# ============================================================
# Path to Eldorado — Oracle Cloud Setup Script
# Run this ONCE after SSH-ing into your new Oracle VM.
# Usage: bash setup-server.sh
# ============================================================

set -e

APP_DIR="/home/ubuntu/fitdash"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="eldorado"

echo ""
echo "=== Path to Eldorado — Server Setup ==="
echo ""

# --- 1. System packages ---
echo "[1/6] Installing system packages..."
sudo apt update -y
sudo apt install -y python3-pip python3-venv nginx certbot python3-certbot-nginx git ufw

# --- 2. Firewall (Oracle VMs need iptables rules too) ---
echo "[2/6] Configuring firewall..."
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save

# --- 3. Clone repo ---
echo "[3/6] Cloning repo..."
if [ -d "$APP_DIR" ]; then
    echo "  Directory exists, pulling latest..."
    cd "$APP_DIR" && git pull
else
    git clone https://github.com/omrieldor/fitdash.git "$APP_DIR"
fi

# --- 4. Python venv + deps ---
echo "[4/6] Setting up Python environment..."
cd "$APP_DIR"
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

# --- 5. Generate secret key & create systemd service ---
echo "[5/6] Creating systemd service..."
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

sudo tee /etc/systemd/system/${SERVICE_NAME}.service > /dev/null <<EOF
[Unit]
Description=Path to Eldorado Fitness App
After=network.target

[Service]
User=ubuntu
Group=ubuntu
WorkingDirectory=${APP_DIR}
ExecStart=${VENV_DIR}/bin/gunicorn -w 2 -b 127.0.0.1:5000 app:app
Environment=SECRET_KEY=${SECRET}
Environment=COMMANDER_PASSWORD=3110
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable ${SERVICE_NAME}
sudo systemctl start ${SERVICE_NAME}

# --- 6. Nginx reverse proxy ---
echo "[6/6] Configuring Nginx..."
SERVER_IP=$(curl -s ifconfig.me)

sudo tee /etc/nginx/sites-available/${SERVICE_NAME} > /dev/null <<EOF
server {
    listen 80;
    server_name ${SERVER_IP};

    client_max_body_size 5M;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

sudo ln -sf /etc/nginx/sites-available/${SERVICE_NAME} /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl restart nginx

echo ""
echo "=========================================="
echo "  DONE! Your app is live at:"
echo "  http://${SERVER_IP}"
echo ""
echo "  Secret key saved in systemd service."
echo "  SQLite DB will persist at:"
echo "  ${APP_DIR}/instance/dashboard.db"
echo "=========================================="
echo ""
