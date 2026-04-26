"""
bot/utils/notifier.py — Telegram + Email notification dispatcher for AlgoSoft.
Reads config from platform_settings at call-time so changes take effect immediately.
"""
import json
import urllib.request
import urllib.parse
import urllib.error
from utils.logger import logger


# ── Telegram ──────────────────────────────────────────────────────────────────

def _get_tg_token() -> str | None:
    try:
        from web.db import db_fetchone
        row = db_fetchone("SELECT value FROM platform_settings WHERE key='telegram_bot_token'")
        return (row["value"] or "").strip() if row else None
    except Exception:
        return None


def _is_telegram_enabled() -> bool:
    """Check if Telegram alerts are globally enabled (default: enabled)."""
    try:
        from web.db import db_fetchone
        row = db_fetchone("SELECT value FROM platform_settings WHERE key='telegram_alerts_enabled'")
        if row and (row["value"] or "").strip().lower() == "false":
            return False
    except Exception:
        pass
    return True


def send_telegram(chat_id: str, message: str, parse_mode: str = "HTML",
                  force: bool = False) -> bool:
    """
    Send a Telegram message to a specific chat_id.

    Args:
        chat_id: Telegram chat ID (user or group).
        message: HTML-formatted message text.
        parse_mode: "HTML" or "Markdown".
        force: If True, bypass the global telegram_alerts_enabled toggle.
               Use only for admin/client test-message calls.

    Returns:
        True on success, False on failure.
    """
    if not force and not _is_telegram_enabled():
        logger.debug("[Telegram] Alerts globally disabled — skipping.")
        return False
    token = _get_tg_token()
    if not token:
        logger.warning("[Telegram] Bot token not configured — skipping.")
        return False
    if not chat_id:
        logger.warning("[Telegram] No chat_id provided — skipping.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode()

    try:
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read())
            if not body.get("ok"):
                logger.warning(f"[Telegram] API error: {body}")
                return False
        logger.debug(f"[Telegram] Message sent to {chat_id}")
        return True
    except Exception as e:
        logger.error(f"[Telegram] Failed to send message to {chat_id}: {e}")
        return False


def notify_trade_entry(chat_id: str, trade: dict) -> bool:
    """Send a trade entry notification (strangle opened) to a client's Telegram chat."""
    instrument = trade.get("instrument", "NIFTY")
    ce_strike  = trade.get("ce_strike", "—")
    pe_strike  = trade.get("pe_strike", "—")
    ce_price   = trade.get("ce_price", 0)
    pe_price   = trade.get("pe_price", 0)
    broker     = trade.get("broker", "")
    reason     = trade.get("reason", "Signal")

    msg = (
        f"<b>📈 Trade Entered — AlgoSoft</b>\n"
        f"<b>Instrument:</b> {instrument}\n"
        f"<b>CE Strike:</b> {ce_strike}  @ ₹{ce_price:.2f}\n"
        f"<b>PE Strike:</b> {pe_strike}  @ ₹{pe_price:.2f}\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Broker:</b> {broker}   🟡 LIVE"
    )
    return send_telegram(chat_id, msg)


def notify_trade(chat_id: str, trade: dict) -> bool:
    """Send a trade entry/exit notification to a client's Telegram chat."""
    direction = trade.get("direction", "UNKNOWN")
    pnl_pts   = trade.get("pnl_pts")
    pnl_rs    = trade.get("pnl_rs")
    reason    = trade.get("exit_reason", "")
    broker    = trade.get("broker", "")
    mode      = trade.get("trading_mode", "paper").upper()
    instrument= trade.get("instrument", "NIFTY")

    strike     = trade.get("strike")
    exit_price = trade.get("exit_price")
    lots       = trade.get("lots")

    pnl_icon = "🟢" if (pnl_pts or 0) >= 0 else "🔴"
    pnl_text = f"{pnl_pts:+.1f} pts (₹{pnl_rs:+,.0f})" if pnl_pts is not None else "—"
    mode_badge = "🔵 PAPER" if mode == "PAPER" else "🟡 LIVE"

    strike_line = f"<b>Strike:</b> {strike:.0f}  Exit @ ₹{exit_price:.2f}\n" if strike and exit_price else ""
    lots_line   = f"<b>Lots:</b> {lots}\n" if lots else ""

    msg = (
        f"<b>📊 Trade Closed — AlgoSoft</b>\n"
        f"<b>Instrument:</b> {instrument}  ({direction})\n"
        f"{strike_line}"
        f"{lots_line}"
        f"<b>Exit Reason:</b> {reason}\n"
        f"<b>PnL:</b> {pnl_icon} {pnl_text}\n"
        f"<b>Broker:</b> {broker}   {mode_badge}"
    )
    return send_telegram(chat_id, msg)


def notify_squareoff(chat_id: str, data: dict) -> bool:
    """Send a SQUARED OFF alert when all positions are closed (EOD or kill-switch)."""
    instrument   = data.get("instrument", "NIFTY")
    broker       = data.get("broker", "")
    reason       = data.get("reason", "EOD Square-off")
    total_pnl_rs = data.get("total_pnl_rs", 0.0)
    total_pnl_pts= data.get("total_pnl_pts", 0.0)

    ce_strike    = data.get("ce_strike", "—")
    ce_exit      = data.get("ce_exit_price")
    pe_strike    = data.get("pe_strike", "—")
    pe_exit      = data.get("pe_exit_price")
    lots         = data.get("lots")

    trend = "🟢" if total_pnl_pts >= 0 else "🔴"
    ce_line = f"<b>CE:</b> {ce_strike:.0f}  Exit @ ₹{ce_exit:.2f}\n" if ce_strike != "—" and ce_exit else ""
    pe_line = f"<b>PE:</b> {pe_strike:.0f}  Exit @ ₹{pe_exit:.2f}\n" if pe_strike != "—" and pe_exit else ""
    lots_line = f"<b>Lots:</b> {lots}\n" if lots else ""

    msg = (
        f"🔒 <b>SQUARED OFF — AlgoSoft</b>\n"
        f"<b>Instrument:</b> {instrument}\n"
        f"{ce_line}"
        f"{pe_line}"
        f"{lots_line}"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Net PnL:</b> {trend} {total_pnl_pts:+.1f} pts (₹{total_pnl_rs:+,.0f})\n"
        f"<b>Broker:</b> {broker}   🟡 LIVE\n"
        f"<i>All positions closed.</i>"
    )
    return send_telegram(chat_id, msg)


def notify_day_end_summary(chat_id: str, summary: dict) -> bool:
    """Send daily PnL summary to client's Telegram chat."""
    date       = summary.get("date", "Today")
    trades     = summary.get("total_trades", 0)
    wins       = summary.get("wins", 0)
    losses     = summary.get("losses", 0)
    total_pts  = summary.get("total_pnl_pts", 0.0)
    total_rs   = summary.get("total_pnl_rs", 0.0)
    broker     = summary.get("broker", "")

    trend = "🟢 Profitable" if total_pts >= 0 else "🔴 Loss"
    pnl_str = f"{total_pts:+.1f} pts (₹{total_rs:+,.0f})"
    win_rate = f"{round(wins / trades * 100)}%" if trades else "N/A"

    msg = (
        f"<b>📅 Day-End Summary — {date}</b>\n"
        f"<b>Broker:</b> {broker}\n"
        f"<b>Total Trades:</b> {trades}  (W:{wins} / L:{losses})\n"
        f"<b>Win Rate:</b> {win_rate}\n"
        f"<b>Net PnL:</b> {pnl_str}\n"
        f"<b>Result:</b> {trend}\n"
        f"Bot: Connected ✅"
    )
    return send_telegram(chat_id, msg)


def notify_subscription_expiry(chat_id: str, username: str, plan: str,
                                days_remaining: int) -> bool:
    """Alert client on Telegram that their plan is about to expire."""
    urgency = "today" if days_remaining <= 1 else f"in {days_remaining} days"
    msg = (
        f"⚠️ <b>Subscription Expiry — AlgoSoft</b>\n"
        f"Hello <b>{username}</b>, your <b>{plan}</b> plan expires <b>{urgency}</b>.\n"
        f"Please contact your administrator to renew."
    )
    return send_telegram(chat_id, msg)


def notify_kill_switch(chat_id: str, username: str, reason: str, pnl_rs: float) -> bool:
    """Alert client on Telegram that the daily loss kill-switch fired."""
    msg = (
        f"🚨 <b>Kill-Switch Triggered — AlgoSoft</b>\n"
        f"<b>Client:</b> {username}\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Daily Loss:</b> ₹{abs(pnl_rs):,.0f}\n"
        f"<i>Bot has been halted for today. Contact admin if needed.</i>"
    )
    return send_telegram(chat_id, msg)


def _get_admin_chat_id() -> str | None:
    """Return the first admin user's Telegram chat ID from the database."""
    try:
        from web.db import db_fetchone
        row = db_fetchone(
            "SELECT telegram_chat_id FROM users "
            "WHERE role='admin' AND telegram_chat_id IS NOT NULL AND telegram_chat_id != '' "
            "LIMIT 1"
        )
        return row["telegram_chat_id"] if row else None
    except Exception:
        return None


def get_admin_chat_id() -> str | None:
    """Public accessor: return the first admin's Telegram chat ID from the database."""
    return _get_admin_chat_id()


def notify_admin_startup(client_count: int, active_count: int) -> bool:
    """Alert admin on Telegram that the AlgoSoft server has started."""
    chat_id = _get_admin_chat_id()
    if not chat_id:
        return False
    msg = (
        f"🟢 <b>AlgoSoft Started</b>\n"
        f"<b>Total clients configured:</b> {client_count}\n"
        f"<b>Active subscriptions:</b> {active_count}\n"
        f"<i>Server is up and ready.</i>"
    )
    return send_telegram(chat_id, msg, force=True)


def notify_admin_instance_event(username: str, event: str, mode: str,
                                 instrument: str = "", reason: str = "") -> bool:
    """
    Alert admin when a client instance starts or stops.

    Args:
        username: Client's username.
        event: "started" or "stopped".
        mode: "LIVE" or "PAPER".
        instrument: Trading instrument (e.g. "NIFTY").
        reason: Stop reason or error message (optional).
    """
    chat_id = _get_admin_chat_id()
    if not chat_id:
        return False

    if event == "started":
        mode_badge = "🟡 LIVE" if mode.upper() == "LIVE" else "🔵 PAPER"
        instr_line = f"\n<b>Instrument:</b> {instrument}" if instrument else ""
        msg = (
            f"▶ <b>{username}</b> started trading\n"
            f"<b>Mode:</b> {mode_badge}{instr_line}"
        )
    else:
        reason_line = f"\n<b>Reason:</b> {reason}" if reason else ""
        msg = (
            f"⚠️ <b>{username}</b> instance stopped{reason_line}"
        )
    return send_telegram(chat_id, msg, force=True)


def notify_admin_kill_switch(username: str, pnl_rs: float, reason: str) -> bool:
    """Alert admin on Telegram when a client's daily-loss kill-switch fires."""
    chat_id = _get_admin_chat_id()
    if not chat_id:
        return False
    msg = (
        f"🚨 <b>Kill-Switch Triggered — Admin Alert</b>\n"
        f"<b>Client:</b> {username}\n"
        f"<b>Reason:</b> {reason}\n"
        f"<b>Daily Loss:</b> ₹{abs(pnl_rs):,.0f}\n"
        f"<i>Bot halted for today.</i>"
    )
    return send_telegram(chat_id, msg, force=True)


def notify_admin_day_end(summary_rows: list) -> bool:
    """
    Send a consolidated day-end digest to the admin.

    Args:
        summary_rows: list of dicts with keys:
            username, trades, pnl_rs, mode (LIVE/PAPER/MIXED)
    """
    chat_id = _get_admin_chat_id()
    if not chat_id:
        return False

    if not summary_rows:
        msg = "📅 <b>Day-End Digest</b>\nNo trades recorded today."
        return send_telegram(chat_id, msg, force=True)

    lines = ["📅 <b>Day-End Digest — AlgoSoft</b>\n"]
    total_clients = len(summary_rows)
    total_pnl = sum(r.get("pnl_rs", 0) for r in summary_rows)

    for r in summary_rows:
        uname  = r.get("username", "?")
        trades = r.get("trades", 0)
        pnl    = r.get("pnl_rs", 0.0)
        mode   = r.get("mode", "")
        icon   = "🟢" if pnl >= 0 else "🔴"
        mode_badge = f" [{mode}]" if mode else ""
        lines.append(f"{icon} <b>{uname}</b>{mode_badge} — {trades} trades  ₹{pnl:+,.0f}")

    trend = "🟢" if total_pnl >= 0 else "🔴"
    lines.append(f"\n<b>Total:</b> {total_clients} clients  {trend} ₹{total_pnl:+,.0f}")
    return send_telegram(chat_id, "\n".join(lines), force=True)


def notify_rust_fallback() -> bool:
    """
    Send a one-time Telegram alert to the admin when the Rust engine is
    unavailable and the bot has fallen back to Python.

    Called once at module import time from rust_bridge.py — Python's module
    cache guarantees the alert is sent at most once per process start.
    """
    chat_id = _get_admin_chat_id()
    if not chat_id:
        logger.warning("[Telegram] Rust fallback alert: no admin chat_id found — skipping.")
        return False

    msg = (
        "⚠️ <b>Rust Engine Unavailable — AlgoSoft</b>\n\n"
        "The Rust acceleration module (<code>rust_core</code>) failed to load "
        "on this startup. The bot is running on <b>Python fallback mode</b>, "
        "which is slower but fully functional.\n\n"
        "<b>Action required:</b> Rebuild the Rust module on the server:\n"
        "<code>bash bot/scripts/install_rust_core.sh</code>\n\n"
        "<i>Then restart the bot to activate hardware acceleration.</i>"
    )
    return send_telegram(chat_id, msg, force=True)
