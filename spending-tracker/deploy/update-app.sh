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
# Say so loudly when the pull cannot advance. A tracked file modified directly on
# the server makes git refuse to overwrite it and abort -- while /deploy still
# returns 200, so the deploy looks successful and the app quietly keeps serving
# the old code. Naming the offending files turns a silent stall into an obvious
# error.
if ! git pull --ff-only; then
    echo ""
    echo "ERROR: pull failed — the app is still on $(git rev-parse --short HEAD)."
    echo "Locally modified files block a pull; these differ from the commit:"
    git status --short
    echo ""
    echo "If a local copy is already identical to what was committed, discard it:"
    echo "  git checkout -- <file>"
    exit 1
fi

echo "Updating dependencies..."
source "$APP_DIR/venv/bin/activate"
pip install -r "$APP_DIR/requirements.txt" --quiet

echo "Restarting app..."
sudo systemctl restart spendtrack

echo "Done! App updated and running."
echo "Check status:   sudo systemctl status spendtrack"
echo "Check reminder: systemctl list-timers spendtrack-reminder.timer"
