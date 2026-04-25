#!/usr/bin/env bash
# =============================================================================
# AlgoSoft Bot — Production Startup Script
# Run this ONCE on the EC2 instance to set up PM2 and enable auto-start on reboot.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "==> AlgoSoft production setup starting from: $REPO_DIR"
cd "$REPO_DIR"

# --- 1. Install PM2 globally if not already present --------------------------
if ! command -v pm2 &>/dev/null; then
  echo "==> Installing PM2 globally..."
  npm install -g pm2
else
  echo "==> PM2 already installed: $(pm2 --version)"
fi

# --- 2. Install PM2 log-rotate module ----------------------------------------
# Rotates logs daily, keeps 30 days, max 50 MB per file
if ! pm2 list 2>/dev/null | grep -q pm2-logrotate; then
  echo "==> Installing pm2-logrotate..."
  pm2 install pm2-logrotate
  pm2 set pm2-logrotate:max_size 50M
  pm2 set pm2-logrotate:retain 30
  pm2 set pm2-logrotate:compress true
  pm2 set pm2-logrotate:dateFormat YYYY-MM-DD
fi

# --- 3. Ensure logs directory exists -----------------------------------------
mkdir -p "$REPO_DIR/bot/logs"
mkdir -p "$REPO_DIR/logs"

# --- 4. Install Python dependencies (if requirements.txt exists) --------------
if [ -f "$REPO_DIR/bot/requirements.txt" ]; then
  echo "==> Installing Python dependencies..."
  pip3 install -r "$REPO_DIR/bot/requirements.txt" --quiet
fi

# --- 5. Start / reload bot with PM2 ------------------------------------------
echo "==> Starting AlgoSoft Bot with PM2..."
pm2 start "$REPO_DIR/ecosystem.config.js" --env production

# --- 6. Register PM2 with the OS init system ---------------------------------
# This ensures PM2 (and all managed processes) restart automatically on reboot.
echo "==> Registering PM2 with system startup..."
pm2 startup       # Prints a command — run it if prompted (requires sudo)
pm2 save          # Saves current process list so it survives reboots

# --- 7. Display status -------------------------------------------------------
echo ""
echo "==> Setup complete! Current PM2 status:"
pm2 status

echo ""
echo "==================================================================="
echo "  AlgoSoft Bot is now managed by PM2."
echo ""
echo "  Useful commands:"
echo "    pm2 status              — show all process status"
echo "    pm2 logs algosoft-bot   — stream bot logs"
echo "    pm2 logs algosoft-watchdog — stream watchdog logs"
echo "    pm2 restart algosoft-bot — restart the bot"
echo "    pm2 stop algosoft-bot   — stop the bot"
echo "    pm2 monit               — live CPU/memory monitor"
echo ""
echo "  Health check: curl http://localhost:5000/health"
echo "==================================================================="
