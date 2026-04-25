#!/usr/bin/env python3
"""
AlgoSoft Health Watchdog
Polls the /health endpoint every 60 seconds and sends Telegram alerts
to the admin when the bot process goes down or recovers.

Run via PM2 as 'algosoft-watchdog' (see ecosystem.config.js).
Can also be run standalone: python3 scripts/health_watchdog.py
"""
import time
import sys
import os
import logging
import sqlite3
import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Watchdog] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("watchdog")

# --- Configuration --------------------------------------------------------
HEALTH_URL = os.environ.get("HEALTH_URL", "http://localhost:5000/health")
POLL_INTERVAL = int(os.environ.get("WATCHDOG_INTERVAL", "60"))   # seconds
DB_PATH = os.environ.get("DB_PATH", "bot/config/algosoft.db")
TG_API = "https://api.telegram.org/bot{token}/sendMessage"
BOT_NAME = os.environ.get("BOT_NAME", "AlgoSoft Bot")

# Initial startup delay — let the bot fully initialise before first check
STARTUP_WAIT = int(os.environ.get("WATCHDOG_STARTUP_WAIT", "30"))


def _get_db_value(key: str) -> str | None:
    """Read a value from platform_settings in the SQLite DB."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.execute(
            "SELECT value FROM platform_settings WHERE key=?", (key,)
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"DB read failed ({key}): {e}")
        return None


def _get_admin_chat_id() -> str | None:
    """Return the first admin user's Telegram chat ID from the users table."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        cur = conn.execute(
            "SELECT telegram_chat_id FROM users WHERE role='admin' "
            "AND telegram_chat_id IS NOT NULL AND telegram_chat_id != '' LIMIT 1"
        )
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"DB admin chat_id read failed: {e}")
        return None


def _send_telegram(token: str, chat_id: str, message: str) -> bool:
    """Send a Telegram message. Returns True on success."""
    try:
        url = TG_API.format(token=token)
        resp = requests.post(url, json={
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
        }, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")
        return False


def _check_health() -> dict | None:
    """Return parsed /health JSON or None if the endpoint is unreachable."""
    try:
        resp = requests.get(HEALTH_URL, timeout=5)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _alert_down(token: str, chat_id: str):
    msg = (
        f"🚨 <b>{BOT_NAME} is DOWN</b>\n\n"
        f"The health check at <code>{HEALTH_URL}</code> failed.\n"
        f"PM2 is attempting an automatic restart.\n\n"
        f"<i>{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</i>"
    )
    ok = _send_telegram(token, chat_id, msg)
    logger.info(f"DOWN alert sent: {ok}")


def _alert_up(token: str, chat_id: str, health: dict):
    uptime = health.get("uptime", "—")
    sessions = health.get("active_sessions", 0)
    msg = (
        f"✅ <b>{BOT_NAME} is back ONLINE</b>\n\n"
        f"Uptime: <code>{uptime}</code>\n"
        f"Active sessions: <b>{sessions}</b>\n\n"
        f"<i>{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}</i>"
    )
    ok = _send_telegram(token, chat_id, msg)
    logger.info(f"UP alert sent: {ok}")


def run():
    logger.info(f"Health watchdog started — polling {HEALTH_URL} every {POLL_INTERVAL}s")
    logger.info(f"Waiting {STARTUP_WAIT}s for bot to initialise...")
    time.sleep(STARTUP_WAIT)

    was_up = True  # Assume bot is up at start (avoids false alert on watchdog restart)
    consecutive_failures = 0
    FAILURE_THRESHOLD = 2  # must fail N times in a row before alerting

    while True:
        try:
            token = _get_db_value("telegram_bot_token")
            chat_id = _get_admin_chat_id()

            health = _check_health()
            is_up = health is not None and health.get("status") == "ok"

            if is_up:
                consecutive_failures = 0
                if not was_up:
                    logger.info("Bot recovered — sending UP alert")
                    if token and chat_id:
                        _alert_up(token, chat_id, health)
                    was_up = True
                else:
                    uptime = health.get("uptime", "—")
                    sessions = health.get("active_sessions", 0)
                    logger.info(f"Bot OK | uptime={uptime} | active_sessions={sessions}")
            else:
                consecutive_failures += 1
                logger.warning(f"Health check failed (attempt {consecutive_failures}/{FAILURE_THRESHOLD})")
                if consecutive_failures >= FAILURE_THRESHOLD and was_up:
                    logger.warning("Bot appears DOWN — sending DOWN alert")
                    if token and chat_id:
                        _alert_down(token, chat_id)
                    was_up = False

        except Exception as e:
            logger.error(f"Watchdog loop error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        logger.info("Watchdog stopped.")
        sys.exit(0)
