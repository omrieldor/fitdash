#!/bin/bash
# ============================================================
# SpendTrack — One-command install
#
# Point your domain at this server first (e.g. via DuckDNS), then:
#   bash install.sh yoursubdomain.duckdns.org
#
# Without a domain it still installs, but the monthly push reminder
# won't work — iOS only delivers push to a Home Screen PWA over HTTPS.
# ============================================================

set -e

DOMAIN="$1"
REPO_DIR="/home/ubuntu/fitdash"
APP_DIR="$REPO_DIR/spending-tracker"

echo ""
echo "=================================================="
echo "  SpendTrack — install"
echo "=================================================="

echo ""
echo "[1/3] Getting the latest code..."
cd "$REPO_DIR"
git pull

echo ""
echo "[2/3] Installing SpendTrack..."
bash "$APP_DIR/deploy/setup-server.sh"

if [ -n "$DOMAIN" ]; then
    echo ""
    echo "[3/3] Setting up HTTPS for ${DOMAIN}..."
    bash "$APP_DIR/deploy/add-ssl.sh" "$DOMAIN"
    URL="https://${DOMAIN}"
else
    echo ""
    echo "[3/3] No domain given — skipping HTTPS."
    echo "      Monthly reminders need HTTPS. When you have a domain, re-run:"
    echo "        bash $0 yoursubdomain.duckdns.org"
    URL="http://$(curl -s ifconfig.me):8081"
fi

echo ""
echo "=================================================="
echo "  Checking both apps"
echo "=================================================="
if systemctl is-active --quiet eldorado; then
    echo "  fitness app (eldorado): running"
else
    echo "  fitness app (eldorado): NOT RUNNING  <-- check this"
fi
if systemctl is-active --quiet spendtrack; then
    echo "  SpendTrack:             running"
else
    echo "  SpendTrack:             NOT RUNNING  <-- check this"
fi

echo ""
echo "=================================================="
echo "  DONE — open this on your iPhone (in Safari):"
echo ""
echo "    ${URL}"
echo ""
echo "  1. Create your account NOW — signup locks after"
echo "     the first account is made"
echo "  2. Share button -> Add to Home Screen -> Add"
echo "  3. Close Safari, open it from the Home Screen icon"
echo "  4. Tap the bell to switch on monthly reminders"
echo ""
echo "  Your data lives at:"
echo "    ${APP_DIR}/instance/spending.db"
echo "=================================================="
echo ""
