"""
bot/utils/emailer.py — SMTP email sender for AlgoSoft platform alerts.
Reads credentials from platform_settings table at call time so admin config
changes take effect immediately without restart.
"""
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from utils.logger import logger


def _get_smtp_config() -> dict | None:
    """Load SMTP settings from platform_settings. Returns None if not configured."""
    try:
        from web.db import db_fetchall
        rows = db_fetchall(
            "SELECT key, value FROM platform_settings WHERE key LIKE 'smtp_%'"
        )
        cfg = {r["key"].replace("smtp_", ""): r["value"] for r in rows}
        if not cfg.get("host") or not cfg.get("username") or not cfg.get("password"):
            return None
        return cfg
    except Exception as e:
        logger.warning(f"[Email] Could not load SMTP config: {e}")
        return None


def send_email(to: str | list[str], subject: str, body_html: str,
               body_text: str = "") -> bool:
    """
    Send an HTML email via SMTP.

    Args:
        to: Recipient email address or list of addresses.
        subject: Email subject line.
        body_html: HTML body content.
        body_text: Plaintext fallback (auto-generated from subject if omitted).

    Returns:
        True on success, False on failure.
    """
    cfg = _get_smtp_config()
    if not cfg:
        logger.warning("[Email] SMTP not configured — skipping email.")
        return False

    host     = cfg.get("host", "smtp.gmail.com")
    port     = int(cfg.get("port", 587))
    username = cfg.get("username", "")
    password = cfg.get("password", "")
    from_name = cfg.get("from_name", "AlgoSoft")
    from_addr = cfg.get("from_addr", username)
    use_tls  = cfg.get("use_tls", "true").lower() != "false"

    recipients = [to] if isinstance(to, str) else to

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"{from_name} <{from_addr}>"
    msg["To"]      = ", ".join(recipients)

    plain = body_text or f"{subject}\n\n{body_html}"
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(body_html, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(host, port, timeout=15) as server:
            if use_tls:
                server.starttls(context=context)
            server.login(username, password)
            server.sendmail(from_addr, recipients, msg.as_string())
        logger.info(f"[Email] Sent '{subject}' → {recipients}")
        return True
    except Exception as e:
        logger.error(f"[Email] Failed to send '{subject}' → {recipients}: {e}")
        return False


def send_subscription_expiry_alert(client: dict, days_remaining: int,
                                   admin_email: str | None = None) -> bool:
    """
    Send subscription expiry warning to client and optionally admin.

    Args:
        client: dict with username, email, subscription_tier, plan_expiry_date
        days_remaining: 7 or 1
        admin_email: admin email address for the CC alert
    """
    name      = client.get("full_name") or client.get("username", "Valued Client")
    plan      = client.get("subscription_tier", "Standard")
    expiry    = client.get("plan_expiry_date", "")[:10]
    urgency   = "today" if days_remaining <= 1 else f"in {days_remaining} days"
    subject   = f"[AlgoSoft] Your {plan} plan expires {urgency}"

    client_html = f"""
    <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;background:#0f172a;
                color:#e2e8f0;padding:32px;border-radius:12px;">
      <h2 style="color:#38bdf8;margin-top:0">⚠️ Subscription Expiry Reminder</h2>
      <p>Hello <strong>{name}</strong>,</p>
      <p>Your <strong>{plan}</strong> plan on AlgoSoft will expire
         <strong style="color:#f97316">{urgency}</strong>
         (on {expiry}).</p>
      <p>After expiry, your bot will continue with limited access (1 broker only).</p>
      <p>Please contact your administrator to renew your subscription before the deadline.</p>
      <hr style="border-color:#334155;margin:24px 0">
      <p style="font-size:12px;color:#64748b">AlgoSoft Automated Trading Platform</p>
    </div>
    """
    ok = send_email(client["email"], subject, client_html)

    if admin_email:
        admin_html = f"""
        <div style="font-family:Arial,sans-serif;max-width:600px;margin:auto;background:#0f172a;
                    color:#e2e8f0;padding:32px;border-radius:12px;">
          <h2 style="color:#f97316;margin-top:0">[Action Needed] Client Subscription Expiring</h2>
          <table style="width:100%;border-collapse:collapse">
            <tr><td style="padding:6px;color:#94a3b8">Client</td>
                <td style="padding:6px"><strong>{name}</strong> ({client['username']})</td></tr>
            <tr><td style="padding:6px;color:#94a3b8">Email</td>
                <td style="padding:6px">{client['email']}</td></tr>
            <tr><td style="padding:6px;color:#94a3b8">Plan</td>
                <td style="padding:6px">{plan}</td></tr>
            <tr><td style="padding:6px;color:#94a3b8">Expires</td>
                <td style="padding:6px;color:#f97316"><strong>{expiry} ({urgency})</strong></td></tr>
          </table>
          <p style="margin-top:20px">Log in to the admin panel to renew this client's subscription.</p>
        </div>
        """
        send_email(admin_email, f"[AlgoSoft Admin] {name}'s plan expires {urgency}", admin_html)

    return ok
