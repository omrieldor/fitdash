#!/bin/bash
# ============================================================
# Path to Eldorado — Add Free SSL (Let's Encrypt)
# Run AFTER you point a domain to your server IP.
# Usage: bash add-ssl.sh yourdomain.com
# ============================================================

set -e

if [ -z "$1" ]; then
    echo "Usage: bash add-ssl.sh yourdomain.com"
    exit 1
fi

DOMAIN="$1"

# Update nginx config with domain
sudo sed -i "s/server_name .*/server_name ${DOMAIN};/" /etc/nginx/sites-available/eldorado
sudo nginx -t
sudo systemctl reload nginx

# Get SSL cert
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m omrieldor@gmail.com

echo ""
echo "SSL enabled! App is live at: https://${DOMAIN}"
echo "Cert auto-renews via certbot timer."
