#!/bin/bash
# ============================================================
# SpendTrack — Add Free SSL (Let's Encrypt)
# HTTPS is REQUIRED for the monthly push reminder to work:
# iOS only delivers web push to a Home Screen PWA served over TLS.
#
# Run AFTER pointing a domain at this server's IP.
# Usage: bash add-ssl.sh spend.yourdomain.com
# ============================================================

set -e

if [ -z "$1" ]; then
    echo "Usage: bash add-ssl.sh spend.yourdomain.com"
    echo ""
    echo "Don't own a domain? Grab a free subdomain at https://www.duckdns.org"
    echo "and point it at this server's public IP first."
    exit 1
fi

DOMAIN="$1"
SERVICE_NAME="spendtrack"
PORT=5001

# Serve this domain on 80/443 (certbot needs :80 for the HTTP-01 challenge).
sudo tee /etc/nginx/sites-available/${SERVICE_NAME} > /dev/null <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

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
sudo systemctl reload nginx

# certbot rewrites the block above to add the 443 listener + redirect.
sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m omrieldor@gmail.com --redirect

echo ""
echo "=========================================="
echo "  SSL enabled — SpendTrack is at:"
echo "  https://${DOMAIN}"
echo ""
echo "  Now enable the monthly reminder on your iPhone:"
echo "    1. Open https://${DOMAIN} in Safari"
echo "    2. Share -> Add to Home Screen"
echo "    3. Launch it from the Home Screen icon (not Safari)"
echo "    4. Tap 🔔 in the header and allow notifications"
echo ""
echo "  You'll get a reminder on the 8th of each month at 20:00 Israel time."
echo "  Test it any time with:"
echo "    cd /home/ubuntu/fitdash/spending-tracker"
echo "    venv/bin/python send_reminders.py --force"
echo ""
echo "  Cert auto-renews via the certbot timer."
echo "=========================================="
