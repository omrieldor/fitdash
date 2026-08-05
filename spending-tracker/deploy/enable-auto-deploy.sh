#!/bin/bash
# ============================================================
# SpendTrack — Update now, and switch on auto-deploy
#
# Run this once. It brings the server up to date, then sets up a
# webhook secret so every future push deploys itself and you never
# need to SSH in again.
#
# Usage: bash enable-auto-deploy.sh
# ============================================================

set -e

REPO_DIR="/home/ubuntu/fitdash"
APP_DIR="$REPO_DIR/spending-tracker"
ENV_FILE="$APP_DIR/.env"

echo ""
echo "=================================================="
echo "  SpendTrack — update + enable auto-deploy"
echo "=================================================="

# --- 1. Update to the latest code ---
echo ""
echo "[1/4] Pulling latest code..."
cd "$REPO_DIR"
git pull

echo ""
echo "[2/4] Installing dependencies (Excel support, etc.)..."
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

# --- 3. Deploy secret ---
echo ""
echo "[3/4] Setting up the webhook secret..."
if grep -q '^DEPLOY_SECRET=.\+' "$ENV_FILE" 2>/dev/null; then
    echo "  Already configured — reusing the existing secret."
    DEPLOY_SECRET=$(grep '^DEPLOY_SECRET=' "$ENV_FILE" | cut -d= -f2-)
else
    DEPLOY_SECRET=$("$APP_DIR/venv/bin/python" -c "import secrets; print(secrets.token_hex(32))")
    # Drop any empty placeholder line first, then append the real one
    sed -i '/^DEPLOY_SECRET=$/d' "$ENV_FILE" 2>/dev/null || true
    echo "DEPLOY_SECRET=${DEPLOY_SECRET}" >> "$ENV_FILE"
    echo "  Generated and saved."
fi

# --- 4. Restart so the new code and secret take effect ---
echo ""
echo "[4/4] Restarting SpendTrack..."
sudo systemctl restart spendtrack
sleep 2

if systemctl is-active --quiet spendtrack; then
    echo "  SpendTrack:             running"
else
    echo "  SpendTrack:             NOT RUNNING  <-- check: sudo journalctl -u spendtrack -n 30"
fi
if systemctl is-active --quiet eldorado; then
    echo "  fitness app (eldorado): running (untouched)"
else
    echo "  fitness app (eldorado): NOT RUNNING  <-- check this"
fi

DOMAIN=$(grep -oP 'server_name \K[^;]+' /etc/nginx/sites-available/spendtrack 2>/dev/null | head -1 | tr -d ' ')
[ -z "$DOMAIN" ] && DOMAIN="YOUR-DOMAIN"

echo ""
echo "=================================================="
echo "  UPDATE DONE. Excel uploads and notifications"
echo "  are now live."
echo ""
echo "  LAST STEP — do this once in your browser so you"
echo "  never have to SSH in again:"
echo ""
echo "  1. Open:"
echo "     https://github.com/omrieldor/fitdash/settings/hooks/new"
echo ""
echo "  2. Payload URL:"
echo "     https://${DOMAIN}/deploy"
echo ""
echo "  3. Content type:  application/json"
echo ""
echo "  4. Secret (copy this exactly):"
echo "     ${DEPLOY_SECRET}"
echo ""
echo "  5. Leave 'Just the push event' selected, tick"
echo "     Active, then click 'Add webhook'."
echo ""
echo "  From then on, any update deploys itself."
echo "=================================================="
echo ""
