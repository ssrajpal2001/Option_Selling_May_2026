# AlgoSoft — EC2 Setup & Operations Guide

## Table of Contents
1. [EC2 Auto-Start (systemd)](#ec2-auto-start)
2. [First-Time EC2 Setup](#first-time-setup)
3. [Checking Bot Status](#checking-status)
4. [Deploying a New Version](#deploying-a-new-version)
5. [Manual Start / Stop](#manual-start--stop)
6. [Uninstalling the Auto-Start](#uninstalling)
7. [Troubleshooting](#troubleshooting)

---

## EC2 Auto-Start

The bot is configured to start **automatically at 08:00 AM IST (02:30 UTC), Monday–Friday** using `systemd`.

| File | Purpose |
|---|---|
| `algosoft-bot.service` | Defines how the bot process runs |
| `algosoft-bot.timer` | Schedules the start time (08:00 IST Mon–Fri) |
| `setup_systemd.sh` | One-time installer — run this once on your EC2 server |
| `stop_bot.sh` | Gracefully stop the bot (for maintenance/deployments) |
| `uninstall_systemd.sh` | Remove the auto-start units completely |

**Key behaviour:**
- The service is **enabled at boot** — systemd starts it automatically on every EC2 restart, whether planned or unplanned, on any day of the week.
- The timer fires every **weekday at 08:00 AM IST** as an additional kick — useful if someone stops the bot manually overnight and forgets to restart it.
- `Persistent=true` on the timer means: if the server was **off at exactly 08:00 AM**, the timer will fire once when it next comes back online. This is a belt-and-suspenders safety net, not the primary boot mechanism.
- If the bot process crashes mid-day, systemd **auto-restarts it** within 10 seconds (up to 3 times per 60 s).
- **Clients still control their own trading** from the web dashboard — this only automates the server process layer.

---

## First-Time Setup

Run this **once** after cloning onto a new EC2 instance:

```bash
# 1. Clone the repository (if not done already)
git clone https://github.com/ssrajpal2001/Option_Selling_May_2026.git /opt/algosoft
cd /opt/algosoft

# 2. Install Python dependencies
pip install -r bot/requirements.txt
# or: uv pip install -r bot/requirements.txt

# 3. Run the systemd setup script
sudo bash bot/scripts/setup_systemd.sh
```

The script auto-detects your project path and Python interpreter, and reads the calling user from `SUDO_USER` automatically.

You can override any value with environment variables:

```bash
sudo INSTALL_ROOT=/opt/algosoft BOT_USER=ubuntu BOT_PORT=5000 \
    bash bot/scripts/setup_systemd.sh
```

After setup, the service is **enabled** (will auto-start on every future reboot) and the timer is **active** (will kick the bot each weekday morning). To start the bot right now without waiting for a reboot or the timer, run:

```bash
sudo systemctl start algosoft-bot
```

---

## Checking Status

```bash
# Is the timer active and when does it next fire?
sudo systemctl list-timers algosoft-bot.timer

# Is the bot service currently running?
sudo systemctl status algosoft-bot

# Follow live logs (Ctrl+C to stop)
sudo journalctl -u algosoft-bot -f

# Last 100 log lines
sudo journalctl -u algosoft-bot -n 100

# Logs from today only
sudo journalctl -u algosoft-bot --since today
```

---

## Deploying a New Version

```bash
# 1. Pull the latest code
cd /opt/algosoft
git pull origin main

# 2. Install any new dependencies
pip install -r bot/requirements.txt

# 3. Stop the bot gracefully
sudo bash bot/scripts/stop_bot.sh

# 4. Start the bot immediately (don't wait until 08:00 AM)
sudo systemctl start algosoft-bot

# 5. Confirm it started cleanly
sudo systemctl status algosoft-bot
```

---

## Manual Start / Stop

```bash
# Start the bot right now (outside of the 08:00 AM schedule)
sudo systemctl start algosoft-bot

# Stop the bot gracefully
sudo bash bot/scripts/stop_bot.sh
# or:
sudo systemctl stop algosoft-bot

# Restart the bot (e.g. after a config change)
sudo systemctl restart algosoft-bot
```

---

## Enabling / Disabling Auto-Start on Reboot

The service is enabled by `setup_systemd.sh` so it starts on every boot.  
You can turn this on or off at any time without removing the unit files:

```bash
# Prevent the bot from starting automatically after the next reboot
sudo systemctl disable algosoft-bot

# Re-enable automatic start on reboot
sudo systemctl enable algosoft-bot

# Check whether auto-start is currently enabled
sudo systemctl is-enabled algosoft-bot
```

> Disabling auto-start does **not** stop a currently running bot — use
> `sudo systemctl stop algosoft-bot` for that.

---

## Uninstalling

To remove the auto-start entirely (project files are NOT touched):

```bash
sudo bash bot/scripts/uninstall_systemd.sh
```

---

## Troubleshooting

### Bot didn't start at 08:00 AM
```bash
sudo systemctl status algosoft-bot.timer     # is the timer enabled?
sudo journalctl -u algosoft-bot -n 50        # any startup errors?
sudo systemctl list-timers --all             # check next scheduled run
```

### Port 5000 already in use
```bash
sudo lsof -i :5000                           # find the conflicting process
sudo kill -9 <PID>
sudo systemctl start algosoft-bot
```

### Bot keeps crashing (exceeded restart limit)
```bash
sudo journalctl -u algosoft-bot -n 200 --no-pager
# After fixing the issue, reset the restart counter:
sudo systemctl reset-failed algosoft-bot
sudo systemctl start algosoft-bot
```

### Python / module not found errors
```bash
# Verify the correct Python is being used
/opt/algosoft/.pythonlibs/bin/python -c "import uvicorn; print('OK')"

# Re-install dependencies
pip install -r /opt/algosoft/bot/requirements.txt
```

### Check which user and paths are in the installed service file
```bash
sudo systemctl cat algosoft-bot
```
