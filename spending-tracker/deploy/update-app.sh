#!/bin/bash
# ============================================================
# SpendTrack — Update Script
# Run this anytime you push changes to GitHub.
# Usage: bash update-app.sh
# ============================================================

set -e

REPO_DIR="/home/ubuntu/fitdash"
APP_DIR="$REPO_DIR/spending-tracker"

echo "Pulling latest code..."
cd "$REPO_DIR"
git pull

echo "Updating dependencies..."
source "$APP_DIR/venv/bin/activate"
pip install -r "$APP_DIR/requirements.txt" --quiet

echo "Restarting app..."
sudo systemctl restart spendtrack

echo "Done! App updated and running."
echo "Check status: sudo systemctl status spendtrack"
