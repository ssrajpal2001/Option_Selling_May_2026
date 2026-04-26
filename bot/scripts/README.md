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
- If the EC2 server was off at 08:00 AM (e.g. overnight maintenance), the bot starts **immediately on next boot** (the `Persistent=true` flag handles this)
- If the bot crashes, systemd **auto-restarts it** within 10 seconds (up to 3 times per 60 s)
- **Clients still control their own trading** from the web dashboard — this only automates the server process layer

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

The script auto-detects your project path and Python interpreter. You can override them:

```bash
# Custom path, user, or port:
sudo INSTALL_ROOT=/opt/algosoft BOT_USER=ubuntu BOT_PORT=5000 \
    bash bot/scripts/setup_systemd.sh
```

After setup, the timer activates. **No further action needed** — the bot will start itself every weekday morning.

---

## Checking Status

```bash
# Is the timer active and when does it next fire?
sudo systemctl list-timers algosoft-bot.timer

# Is the bot service currently running?
sudo systemctl status algosoft-bot@ubuntu

# Follow live logs (Ctrl+C to stop)
sudo journalctl -u algosoft-bot@ubuntu -f

# Last 100 log lines
sudo journalctl -u algosoft-bot@ubuntu -n 100

# Logs from today only
sudo journalctl -u algosoft-bot@ubuntu --since today
```

Replace `ubuntu` with your EC2 username if different.

---

## Deploying a New Version

```bash
# 1. Pull the latest code
cd /opt/algosoft
git pull origin main

# 2. Install any new dependencies
pip install -r bot/requirements.txt

# 3. Stop the bot gracefully, then let systemd restart it
sudo bash bot/scripts/stop_bot.sh

# 4. Start the bot immediately (don't wait until 08:00 AM)
sudo systemctl start algosoft-bot@ubuntu

# 5. Confirm it started cleanly
sudo systemctl status algosoft-bot@ubuntu
```

---

## Manual Start / Stop

```bash
# Start the bot right now (outside of the 08:00 AM schedule)
sudo systemctl start algosoft-bot@ubuntu

# Stop the bot gracefully
sudo bash bot/scripts/stop_bot.sh
# or:
sudo systemctl stop algosoft-bot@ubuntu

# Restart the bot (e.g. after a config change)
sudo systemctl restart algosoft-bot@ubuntu
```

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
sudo journalctl -u algosoft-bot@ubuntu -n 50 # any startup errors?
sudo systemctl list-timers --all             # check next scheduled run
```

### Port 5000 already in use
```bash
sudo lsof -i :5000                           # find the conflicting process
sudo kill -9 <PID>
sudo systemctl start algosoft-bot@ubuntu
```

### Bot keeps crashing (exceeded restart limit)
```bash
sudo journalctl -u algosoft-bot@ubuntu -n 200 --no-pager
# After fixing the issue, reset the restart counter:
sudo systemctl reset-failed algosoft-bot@ubuntu
sudo systemctl start algosoft-bot@ubuntu
```

### Python / module not found errors
```bash
# Verify the correct Python is being used
/opt/algosoft/.pythonlibs/bin/python -c "import uvicorn; print('OK')"

# Re-install dependencies
pip install -r /opt/algosoft/bot/requirements.txt
```

### Check which Python path is in the service file
```bash
sudo systemctl cat algosoft-bot@ubuntu
```
