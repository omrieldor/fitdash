#!/bin/bash
# ============================================================
# Path to Eldorado — Update Script
# Run this anytime you push changes to GitHub.
# Usage: bash update-app.sh
# ============================================================

set -e

APP_DIR="/home/ubuntu/fitdash"

echo "Pulling latest code..."
cd "$APP_DIR"
git pull

echo "Updating dependencies..."
source "$APP_DIR/venv/bin/activate"
pip install -r requirements.txt --quiet

echo "Restarting app..."
sudo systemctl restart eldorado

echo "Done! App updated and running."
echo "Check status: sudo systemctl status eldorado"
