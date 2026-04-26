"""
bot/utils/tg_poller.py — Telegram long-polling bot for AlgoSoft clients.

Clients can type keywords into their personal Telegram chat to query live
trading data without opening the web dashboard.

Supported commands (case-insensitive):
  STATUS  — Show running instances and current session P&L (reads live status file + DB)
  SUMMARY — Show today's closed-trade stats: count, wins/losses, net P&L
  HELP    — List available commands

Lifecycle
---------
start_poller() — Starts the daemon thread; silently no-ops if the Telegram bot
                 token is not configured in platform_settings at the time of the
                 call.
stop_poller()  — Signals the thread to exit cleanly and waits up to 5 s for it
                 to finish.  Called automatically by the FastAPI shutdown hook.
"""
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from utils.logger import logger

_POLL_TIMEOUT = 25          # seconds for Telegram long-poll
_RETRY_BACKOFF = 10         # seconds to wait after a network error

_BASE_DIR = Path(__file__).resolve().parent.parent  # bot/

# ── Module-level state ────────────────────────────────────────────────────────

_stop_event: threading.Event = threading.Event()
_poller_thread: threading.Thread | None = None


# ── Telegram API helpers ──────────────────────────────────────────────────────

def _tg_get(token: str, method: str, params: dict) -> dict | None:
    """Make a GET request to the Telegram Bot API. Returns parsed JSON or None."""
    qs = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    url = f"https://api.telegram.org/bot{token}/{method}?{qs}"
    try:
        with urllib.request.urlopen(url, timeout=_POLL_TIMEOUT + 5) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning(f"[TgPoller] API call {method} failed: {exc}")
        return None


def _tg_post(token: str, method: str, payload: dict) -> dict | None:
    """Make a POST request to the Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as exc:
        logger.warning(f"[TgPoller] POST {method} failed: {exc}")
        return None


def _send(token: str, chat_id: str, text: str) -> None:
    _tg_post(token, "sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


# ── DB helpers ────────────────────────────────────────────────────────────────

def _get_token() -> str | None:
    try:
        from web.db import db_fetchone
        row = db_fetchone("SELECT value FROM platform_settings WHERE key='telegram_bot_token'")
        val = (row["value"] or "").strip() if row else ""
        return val if val else None
    except Exception:
        return None


def _resolve_client(chat_id: str) -> dict | None:
    """Return user row (id, username, full_name) for the given Telegram chat_id, or None."""
    try:
        from web.db import db_fetchone
        return db_fetchone(
            "SELECT id, username, full_name FROM users "
            "WHERE telegram_chat_id=? AND is_active=1 AND role='client'",
            (str(chat_id),),
        )
    except Exception:
        return None


def _get_instances(client_id: int) -> list:
    try:
        from web.db import db_fetchall
        return db_fetchall(
            "SELECT id, broker, instrument, trading_mode, status, daily_pnl, daily_trade_count "
            "FROM client_broker_instances WHERE client_id=?",
            (client_id,),
        )
    except Exception:
        return []


def _get_today_trades(client_id: int) -> list:
    try:
        from web.db import db_fetchall
        return db_fetchall(
            "SELECT direction, pnl_pts, pnl_rs, instrument, broker, exit_reason, trading_mode "
            "FROM trade_history "
            "WHERE client_id=? AND date(closed_at, 'localtime') = date('now', 'localtime') "
            "ORDER BY closed_at ASC",
            (client_id,),
        )
    except Exception:
        return []


# ── Live status file reader ───────────────────────────────────────────────────

def _read_live_status(client_id: int) -> dict | None:
    """
    Read the latest status JSON written by the running bot subprocess.
    The subprocess writes to config/bot_status_client_{client_id}.json every 5 s.
    Returns the parsed dict or None if file is missing / stale (>60 s old).
    """
    status_path = _BASE_DIR / "config" / f"bot_status_client_{client_id}.json"
    if not status_path.exists():
        return None
    try:
        mtime = status_path.stat().st_mtime
        if time.time() - mtime > 60:
            return None  # stale — subprocess not running or has not written recently
        with status_path.open("r") as f:
            return json.load(f)
    except Exception:
        return None


# ── Command handlers ──────────────────────────────────────────────────────────

_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_now_str() -> str:
    return datetime.now(_IST).strftime("%d %b %Y  %H:%M IST")


def handle_status(token: str, chat_id: str, client: dict) -> None:
    client_id = client["id"]
    name = client.get("full_name") or client.get("username") or "Client"
    instances = _get_instances(client_id)

    if not instances:
        _send(token, chat_id,
              f"👤 <b>{name}</b>\n\nNo broker instances configured on your account.\n"
              f"<i>{_ist_now_str()}</i>")
        return

    lines = [f"📊 <b>STATUS — {name}</b>", f"<i>{_ist_now_str()}</i>", ""]

    # Read live status file once per client (one file written by the subprocess,
    # shared across all of the client's broker instances).
    live = _read_live_status(client_id)

    for inst in instances:
        broker    = (inst.get("broker") or "—").upper()
        instr     = (inst.get("instrument") or "—").upper()
        mode      = (inst.get("trading_mode") or "PAPER").upper()
        db_status = (inst.get("status") or "idle").lower()
        daily_pnl = inst.get("daily_pnl") or 0.0
        daily_cnt = inst.get("daily_trade_count") or 0

        mode_badge = "🟡 LIVE" if mode == "LIVE" else "🔵 PAPER"
        run_badge  = "🟢 Running" if db_status == "running" else "⚪ Idle"

        lines.append(f"<b>{broker} / {instr}</b>  {mode_badge}")
        lines.append(f"  Status : {run_badge}")
        lines.append(f"  Trades : {daily_cnt}  |  Daily P&L : ₹{daily_pnl:+,.0f}")
        lines.append("")

    # Append live session data once (account-level, from status file)
    if live:
        session_pnl = live.get("session_pnl") or 0.0
        open_pos    = live.get("open_positions") or []
        lines.append("<b>Live session:</b>")
        if isinstance(open_pos, list) and open_pos:
            lines.append(f"  Open positions : {len(open_pos)}")
            for p in open_pos[:4]:
                side  = p.get("direction") or p.get("side") or "?"
                pnl_p = p.get("pnl_pts") or p.get("pnl") or 0.0
                pnl_r = p.get("pnl_rs") or 0.0
                icon  = "🟢" if float(pnl_r) >= 0 else "🔴"
                lines.append(f"    {icon} {side}: ₹{float(pnl_r):+,.0f}  ({float(pnl_p):+.1f} pts)")
        else:
            lines.append("  Open positions : None (waiting for signal)")
        lines.append(f"  Session P&L    : ₹{session_pnl:+,.0f}")

    _send(token, chat_id, "\n".join(lines).rstrip())


def handle_summary(token: str, chat_id: str, client: dict) -> None:
    client_id = client["id"]
    name = client.get("full_name") or client.get("username") or "Client"
    trades = _get_today_trades(client_id)
    today_str = datetime.now(_IST).strftime("%d %b %Y")

    if not trades:
        _send(token, chat_id,
              f"📅 <b>SUMMARY — {name}</b>\n<i>{today_str}</i>\n\n"
              f"No trades recorded today yet.")
        return

    total_pts = sum(float(t.get("pnl_pts") or 0) for t in trades)
    total_rs  = sum(float(t.get("pnl_rs")  or 0) for t in trades)
    wins      = sum(1 for t in trades if float(t.get("pnl_pts") or 0) >= 0)
    losses    = len(trades) - wins
    win_rate  = f"{round(wins / len(trades) * 100)}%" if trades else "N/A"
    trend     = "🟢 Profitable" if total_pts >= 0 else "🔴 Loss"
    pnl_icon  = "🟢" if total_pts >= 0 else "🔴"

    lines = [
        f"📅 <b>SUMMARY — {name}</b>",
        f"<i>{today_str}</i>",
        "",
        f"<b>Trades    :</b> {len(trades)}  (W:{wins} / L:{losses})",
        f"<b>Win Rate  :</b> {win_rate}",
        f"<b>Net P&L   :</b> {pnl_icon} {total_pts:+.1f} pts  (₹{total_rs:+,.0f})",
        f"<b>Result    :</b> {trend}",
    ]

    # Show last 3 trades as a quick recap
    lines.append("\n<b>Recent trades:</b>")
    for t in trades[-3:]:
        pts    = float(t.get("pnl_pts") or 0)
        rs     = float(t.get("pnl_rs")  or 0)
        dirn   = t.get("direction") or "?"
        icon   = "🟢" if pts >= 0 else "🔴"
        reason = (t.get("exit_reason") or "—")[:18]
        lines.append(f"  {icon} {dirn}  {pts:+.1f} pts (₹{rs:+,.0f})  [{reason}]")

    _send(token, chat_id, "\n".join(lines))


def handle_help(token: str, chat_id: str) -> None:
    msg = (
        "🤖 <b>AlgoSoft Bot — Commands</b>\n\n"
        "<b>STATUS</b>  — Running instances, open positions &amp; live P&amp;L\n"
        "<b>SUMMARY</b> — Today's closed-trade stats &amp; net P&amp;L\n"
        "<b>HELP</b>    — Show this message\n\n"
        "<i>Commands are case-insensitive.  "
        "Data refreshes every few seconds when the bot is active.</i>"
    )
    _send(token, chat_id, msg)


def handle_unknown(token: str, chat_id: str) -> None:
    _send(token, chat_id,
          "❓ Unknown command.\n\nType <b>HELP</b> to see available commands.")


def handle_unregistered(token: str, chat_id: str) -> None:
    """Polite reply when the chat_id is not linked to any active client account."""
    _send(token, chat_id,
          "⚠️ <b>Account not linked</b>\n\n"
          "This Telegram chat is not associated with an active AlgoSoft account.\n"
          "Please contact your administrator to link your Telegram ID.")


# ── Poller loop ───────────────────────────────────────────────────────────────

def _process_update(token: str, update: dict) -> None:
    """Dispatch a single Telegram update to the appropriate command handler."""
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat_id = str(message.get("chat", {}).get("id", ""))
    if not chat_id:
        return

    text = (message.get("text") or "").strip()
    # Strip leading slash so /STATUS and STATUS both work
    if text.startswith("/"):
        text = text[1:]
    cmd = text.upper().split()[0] if text else ""

    # Resolve client
    client = _resolve_client(chat_id)
    if not client:
        logger.info(f"[TgPoller] Message from unregistered chat_id={chat_id} — sending account-not-linked reply.")
        handle_unregistered(token, chat_id)
        return

    logger.info(f"[TgPoller] Command '{cmd}' from client_id={client['id']} (chat={chat_id})")

    if cmd == "STATUS":
        handle_status(token, chat_id, client)
    elif cmd == "SUMMARY":
        handle_summary(token, chat_id, client)
    elif cmd == "HELP":
        handle_help(token, chat_id)
    else:
        handle_unknown(token, chat_id)


def _poll_loop(token: str) -> None:
    """
    Main polling loop.  Runs until _stop_event is set.
    - Uses getUpdates long-polling (timeout=25 s) with offset tracking.
    - On network error → backs off _RETRY_BACKOFF seconds, honouring stop_event.
    - Token is fixed at thread-start time; the thread must be restarted if the
      token changes (server restart required).
    """
    logger.info("[TgPoller] Polling thread started.")
    offset = 0

    while not _stop_event.is_set():
        try:
            # allowed_updates must be a JSON array; encoding as plain string
            # causes Telegram to return 400.  Omitting it is simpler and safe —
            # non-message updates are already ignored by _process_update().
            params: dict = {"timeout": _POLL_TIMEOUT}
            if offset:
                params["offset"] = offset

            result = _tg_get(token, "getUpdates", params)

            if _stop_event.is_set():
                break

            if result is None:
                _stop_event.wait(timeout=_RETRY_BACKOFF)
                continue

            if not result.get("ok"):
                logger.warning(f"[TgPoller] getUpdates returned not-ok: {result}")
                _stop_event.wait(timeout=_RETRY_BACKOFF)
                continue

            updates = result.get("result", [])
            for upd in updates:
                if _stop_event.is_set():
                    break
                uid = upd.get("update_id", 0)
                if uid >= offset:
                    offset = uid + 1
                try:
                    _process_update(token, upd)
                except Exception as exc:
                    logger.error(f"[TgPoller] Error processing update {uid}: {exc}", exc_info=True)

        except Exception as exc:
            logger.error(f"[TgPoller] Unexpected error in poll loop: {exc}", exc_info=True)
            _stop_event.wait(timeout=_RETRY_BACKOFF)

    logger.info("[TgPoller] Polling thread stopped.")


# ── Public start / stop API ───────────────────────────────────────────────────

def start_poller() -> None:
    """
    Start the Telegram polling daemon thread.

    - Silently returns (no-op) if the Telegram bot token is not configured in
      platform_settings at the time of the call.
    - Also a no-op if the thread is already alive (idempotent).
    - Resets the stop event so the thread can run after a previous stop_poller()
      call.
    """
    global _poller_thread

    if _poller_thread is not None and _poller_thread.is_alive():
        logger.debug("[TgPoller] Already running — skipping re-start.")
        return

    token = _get_token()
    if not token:
        logger.info("[TgPoller] Bot token not configured in platform_settings — poller not started.")
        return

    _stop_event.clear()
    _poller_thread = threading.Thread(
        target=_poll_loop,
        args=(token,),
        name="TelegramPoller",
        daemon=True,
    )
    _poller_thread.start()
    logger.info("[TgPoller] Daemon thread launched.")


def stop_poller(timeout: float = 5.0) -> None:
    """
    Signal the polling thread to exit and wait up to `timeout` seconds for it
    to finish.  Safe to call even if the thread was never started.
    """
    global _poller_thread
    _stop_event.set()
    if _poller_thread is not None and _poller_thread.is_alive():
        _poller_thread.join(timeout=timeout)
        if _poller_thread.is_alive():
            logger.warning("[TgPoller] Thread did not stop within timeout — it will exit on next iteration.")
        else:
            logger.info("[TgPoller] Thread stopped cleanly.")
    _poller_thread = None
