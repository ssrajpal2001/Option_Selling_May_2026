import time
import hashlib
import urllib.parse
from datetime import datetime, timezone, timedelta

# Track server start time for /health and uptime reporting
APP_START_TIME: float = time.time()

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pathlib import Path

from web.config_api import router as config_router
from web.broker_api import router as broker_router
from web.status_api import router as status_router
from web.bot_control import router as bot_router
from web.auth_api import router as auth_router
from web.admin_api import router as admin_router
from web.client_api import router as client_router
from web.auth import decode_token, encrypt_secret, decrypt_secret, _fernet
from web.db import get_db, db_fetchone, db_execute, db_fetchall
from hub.event_bus import event_bus
import asyncio
import logging

BASE_DIR = Path(__file__).parent

app = FastAPI(title="AlgoSoft", version="2.0.0")
logger = logging.getLogger(__name__)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

def _get_default_theme() -> str:
    try:
        row = db_fetchone("SELECT value FROM platform_settings WHERE key='default_theme'")
        return row["value"] if row and row["value"] in ("dark", "light", "midnight", "saffron") else "dark"
    except Exception:
        return "dark"

templates.env.globals["default_theme"] = _get_default_theme

# Existing bot APIs
app.include_router(config_router, prefix="/api")
app.include_router(broker_router, prefix="/api")
app.include_router(status_router, prefix="/api")
app.include_router(bot_router, prefix="/api")

# Multi-tenant APIs
app.include_router(auth_router, prefix="/api")
app.include_router(admin_router, prefix="/api")
app.include_router(client_router, prefix="/api")

def _get_user_from_request(request: Request):
    token = request.cookies.get("access_token")
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    try:
        conn = get_db()
        row = conn.execute("SELECT id, role, is_active, username FROM users WHERE id=?", (int(payload["sub"]),)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None


# ─── Auth Pages ───────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    user = _get_user_from_request(request)
    if user and user["is_active"]:
        return RedirectResponse("/admin" if user["role"] == "admin" else "/dashboard")
    return templates.TemplateResponse(request, "login.html")


@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse(request, "register.html")


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login")
    response.delete_cookie("access_token")
    return response


# ─── Admin Pages ──────────────────────────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_overview(request: Request):
    user = _get_user_from_request(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "admin_overview.html")


@app.get("/admin/clients", response_class=HTMLResponse)
async def admin_clients(request: Request):
    user = _get_user_from_request(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "admin_clients.html")


@app.get("/admin/clients/{client_id}", response_class=HTMLResponse)
async def admin_client_detail(request: Request, client_id: int):
    user = _get_user_from_request(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "admin_client_detail.html", {"client_id": client_id})


@app.get("/admin/data-providers-page", response_class=HTMLResponse)
async def admin_data_providers_page(request: Request):
    user = _get_user_from_request(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "admin_data_providers.html")


@app.get("/admin/subscription-plans", response_class=HTMLResponse)
async def admin_subscription_plans_page(request: Request):
    user = _get_user_from_request(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "admin_subscription_plans.html")


@app.get("/admin/strategy", response_class=HTMLResponse)
async def admin_strategy(request: Request):
    user = _get_user_from_request(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "strategy.html")


@app.get("/admin/audit-log", response_class=HTMLResponse)
async def admin_audit_log(request: Request):
    user = _get_user_from_request(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "admin_audit_log.html")


@app.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit_redirect(request: Request):
    return RedirectResponse("/admin/audit-log", status_code=301)


@app.get("/admin/platform-settings", response_class=HTMLResponse)
async def admin_platform_settings_page(request: Request):
    user = _get_user_from_request(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login")
    return templates.TemplateResponse(request, "admin_platform_settings.html")




# ─── Client Pages ─────────────────────────────────────────────────────────────

@app.get("/dashboard", response_class=HTMLResponse)
async def client_dashboard(request: Request):
    user = _get_user_from_request(request)
    if not user:
        return RedirectResponse("/login")
    if user["role"] == "admin":
        return RedirectResponse("/admin")
    return templates.TemplateResponse(request, "client_dashboard.html")


# ─── Shared OAuth Helpers ──────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))

def _get_success_html(broker_name: str, message: str = "Your access token has been captured."):
    return HTMLResponse(f"""
        <html>
            <head><title>Authentication Successful</title></head>
            <body style="background:#0a0f1e; color:#e2e8f0; font-family:sans-serif; display:flex; flex-direction:column; align-items:center; justify-content:center; height:100vh; margin:0;">
                <div style="background:#1e293b; padding:2.5rem; border-radius:16px; border:1px solid #334155; text-align:center; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5); max-width:400px;">
                    <div style="background:#064e3b; color:#34d399; width:64px; height:64px; border-radius:50%; display:flex; items-center; justify-content:center; font-size:32px; margin:0 auto 1.5rem auto; line-height:64px;">✓</div>
                    <h1 style="margin:0 0 0.5rem 0; font-size:1.5rem;">{broker_name} Connected</h1>
                    <p style="color:#94a3b8; line-height:1.5; margin-bottom:2rem;">{message}<br>The bot is now initializing your data feed.</p>
                    <div style="height:4px; background:#0f172a; border-radius:2px; overflow:hidden; margin-bottom:1rem;">
                        <div id="progress" style="height:100%; background:#00d4aa; width:100%; transition: width 3s linear;"></div>
                    </div>
                    <p style="font-size:0.75rem; color:#64748b; text-transform:uppercase; letter-spacing:0.05em;">Auto-closing window...</p>
                </div>
                <script>
                    document.getElementById('progress').style.width = '0%';
                    setTimeout(() => {{ window.close(); }}, 3000);
                </script>
            </body>
        </html>
    """)

def _get_error_html(broker_name: str, error: str):
    return HTMLResponse(f"""
        <html>
            <body style="background:#0a0f1e; color:#f87171; font-family:sans-serif; display:flex; justify-content:center; align-items:center; height:100vh; margin:0;">
                <div style="background:#1e293b; padding:2rem; border-radius:12px; border:1px solid #dc2626; text-align:center; max-width:400px;">
                    <h2 style="margin-top:0;">{broker_name} Error</h2>
                    <p style="color:#94a3b8; font-size:0.9rem;">{error}</p>
                    <button onclick="window.close()" style="background:#334155; color:white; border:none; padding:10px 20px; border-radius:8px; cursor:pointer; font-weight:bold; margin-top:1rem;">Close Window</button>
                </div>
            </body>
        </html>
    """)


# ─── Zerodha OAuth Callback ──────────────────────────────────────────────

@app.get("/auth/zerodha/callback")
async def zerodha_oauth_callback(
    request: Request,
    request_token: str = Query(default=None),
    action: str = Query(default=None),
    status: str = Query(default=None),
    state: str = Query(default=None),
):
    if action == "login" and status != "success":
        return RedirectResponse("/dashboard?zerodha=denied")

    if not request_token or not state:
        return _get_error_html("Zerodha", "Missing request parameters from broker.")

    try:
        state_payload = _fernet.decrypt(state.encode()).decode()
        client_id_str, ts_str = state_payload.split(":")
        client_id = int(client_id_str)
    except Exception:
        return _get_error_html("Zerodha", "Invalid or expired security state.")

    instance = db_fetchone(
        "SELECT * FROM client_broker_instances WHERE client_id=? AND broker='zerodha'",
        (client_id,)
    )
    if not instance:
        return _get_error_html("Zerodha", "Broker instance not found for this user.")

    api_key = decrypt_secret(instance["api_key_encrypted"])
    api_secret = decrypt_secret(instance.get("api_secret_encrypted", ""))

    try:
        checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
        import requests as http_requests
        resp = http_requests.post(
            "https://api.kite.trade/session/token",
            data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
        )
        resp_data = resp.json()

        if resp.status_code != 200 or resp_data.get("status") == "error":
            error_msg = resp_data.get("message", "Token exchange failed.")
            print(f"[Zerodha OAuth] Exchange failed: {resp.status_code} - {resp_data}")
            return _get_error_html("Zerodha", error_msg)

        access_token = resp_data.get("data", {}).get("access_token", "")
        enc_token = encrypt_secret(access_token)
        now_ist = datetime.now(IST).isoformat()
        db_execute(
            "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='zerodha'",
            (enc_token, now_ist, client_id)
        )

        await event_bus.publish('BROKER_TOKEN_UPDATED', {'user_id': client_id, 'broker': 'zerodha', 'access_token': access_token})
        return _get_success_html("Zerodha")
    except Exception as e:
        return _get_error_html("Zerodha", str(e))


# ─── Dhan OAuth Callback ──────────────────────────────────────────────────

@app.get("/auth/dhan/callback")
async def dhan_oauth_callback(
    request: Request,
    access_token: str = Query(default=None),
    state: str = Query(default=None),
):
    if not access_token or not state:
        return _get_error_html("Dhan", "Missing request parameters from broker.")

    try:
        state_payload = _fernet.decrypt(state.encode()).decode()
        if state_payload.startswith("admin:"):
            client_id = "admin"
        else:
            client_id_str, ts_str = state_payload.split(":")
            client_id = int(client_id_str)
    except Exception:
        return _get_error_html("Dhan", "Invalid or expired security state.")

    try:
        enc_token = encrypt_secret(access_token)
        if client_id == "admin":
            now = datetime.now(timezone.utc).isoformat()
            db_execute(
                "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=? WHERE provider='dhan'",
                (enc_token, now)
            )
            return _get_success_html("Dhan", "Global data provider token updated.")
        else:
            now_ist = datetime.now(IST).isoformat()
            db_execute(
                "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='dhan'",
                (enc_token, now_ist, client_id)
            )
            await event_bus.publish('BROKER_TOKEN_UPDATED', {'user_id': client_id, 'broker': 'dhan', 'access_token': access_token})
            return _get_success_html("Dhan")
    except Exception as e:
        return _get_error_html("Dhan", str(e))


# ─── Upstox OAuth Callback (Handles both Global and Client Mode) ──────────

@app.get("/auth/upstox/callback")
async def upstox_oauth_callback(
    request: Request,
    code: str = Query(default=None),
    state: str = Query(default=None),
):
    if not code:
        return _get_error_html("Upstox", "No authorization code returned.")

    client_id = None
    if state:
        try:
            state_payload = _fernet.decrypt(state.encode()).decode()
            if state_payload.startswith("admin:"):
                client_id = "admin"
            else:
                client_id_str, ts_str = state_payload.split(":")
                client_id = int(client_id_str)
        except Exception:
            return _get_error_html("Upstox", "Invalid or expired security state.")

    # 1. CLIENT MODE PATH (Individual user account)
    if client_id and client_id != "admin":
        instance = db_fetchone(
            "SELECT * FROM client_broker_instances WHERE client_id=? AND broker='upstox'",
            (client_id,)
        )
        if not instance:
            return _get_error_html("Upstox", "Broker instance not found for this user.")

        api_key = decrypt_secret(instance["api_key_encrypted"])
        api_secret = decrypt_secret(instance.get("api_secret_encrypted", ""))

        try:
            import requests as http_requests
            redirect_uri = str(request.url).split('?')[0]
            resp = http_requests.post(
                "https://api.upstox.com/v2/login/authorization/token",
                headers={'accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'},
                data={
                    "code": code,
                    "client_id": api_key,
                    "client_secret": api_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            resp_data = resp.json()

            if resp.status_code != 200:
                error_msg = resp_data.get("errors", [{}])[0].get("message", "Token exchange failed.")
                return _get_error_html("Upstox", error_msg)

            access_token = resp_data.get("access_token", "")
            enc_token = encrypt_secret(access_token)
            now_ist = datetime.now(IST).isoformat()
            db_execute(
                "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='upstox'",
                (enc_token, now_ist, client_id)
            )

            await event_bus.publish('BROKER_TOKEN_UPDATED', {'user_id': client_id, 'broker': 'upstox', 'access_token': access_token})
            return _get_success_html("Upstox")
        except Exception as e:
            return _get_error_html("Upstox", str(e))

    # 2. GLOBAL PATH (Admin setup)
    else:
        dp = db_fetchone("SELECT * FROM data_providers WHERE provider='upstox'")
        if not dp:
            return _get_error_html("Upstox", "Upstox provider not configured in system.")

        api_key = decrypt_secret(dp["api_key_encrypted"])
        api_secret = decrypt_secret(dp.get("api_secret_encrypted", ""))

        try:
            import requests as http_requests
            redirect_uri = str(request.url).split('?')[0]
            resp = http_requests.post(
                "https://api.upstox.com/v2/login/authorization/token",
                headers={'accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'},
                data={
                    "code": code,
                    "client_id": api_key,
                    "client_secret": api_secret,
                    "redirect_uri": redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
            resp_data = resp.json()

            if resp.status_code != 200:
                error_msg = resp_data.get("errors", [{}])[0].get("message", "Token exchange failed.")
                return _get_error_html("Upstox", error_msg)

            access_token = resp_data.get("access_token", "")
            enc_token = encrypt_secret(access_token)
            now = datetime.now(timezone.utc).isoformat()
            db_execute(
                "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=? WHERE provider='upstox'",
                (enc_token, now)
            )

            from web.admin_api import _sync_upstox_to_credentials
            _sync_upstox_to_credentials(api_key, access_token, api_secret)

            return _get_success_html("Upstox", "Global data provider token updated.")
        except Exception as e:
            return _get_error_html("Upstox", str(e))


# ─── Root redirect ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = _get_user_from_request(request)
    if not user:
        return RedirectResponse("/login")
    if user["role"] == "admin":
        return RedirectResponse("/admin")
    return RedirectResponse("/dashboard")


# ─── Legacy pages (keep for backward compat) ─────────────────────────────────

@app.get("/strategy", response_class=HTMLResponse)
async def strategy_page(request: Request):
    return templates.TemplateResponse(request, "strategy.html")


@app.get("/brokers", response_class=HTMLResponse)
async def brokers_page(request: Request):
    return templates.TemplateResponse(request, "brokers.html")


def _seed_global_providers_from_ini():
    """
    On startup, auto-seed the data_providers DB table from credentials.ini
    if the providers are still in 'not_configured' state.
    This allows the system to bootstrap without manual admin input.
    """
    try:
        import configparser as cp_lib
        creds_path = BASE_DIR.parent / 'config' / 'credentials.ini'
        if not creds_path.exists():
            return

        config = cp_lib.ConfigParser()
        config.read(str(creds_path))
        now = datetime.now(IST).astimezone(timezone.utc).isoformat()

        # ── Seed Upstox ──────────────────────────────────────────────────────
        dp_upstox = db_fetchone("SELECT * FROM data_providers WHERE provider='upstox'")
        if dp_upstox and dp_upstox.get('status') == 'not_configured' and config.has_section('upstox_global'):
            s = config['upstox_global']
            api_key    = s.get('api_key', '').strip()
            api_secret = s.get('api_secret', '').strip()
            user_id    = s.get('user_id', '').strip()
            password   = s.get('password', '').strip()
            # Support both 'totp_secret' and 'totp' as key names
            totp_val   = (s.get('totp_secret', '') or s.get('totp', '')).strip()
            access_token = s.get('access_token', '').strip()

            if api_key:
                enc_key    = encrypt_secret(api_key)
                enc_secret = encrypt_secret(api_secret) if api_secret else None
                enc_user   = encrypt_secret(user_id)   if user_id   else None
                enc_pass   = encrypt_secret(password)  if password  else None
                enc_totp   = encrypt_secret(totp_val)  if totp_val  else None
                enc_token  = encrypt_secret(access_token) if access_token else None
                status = 'configured' if access_token else 'not_configured'

                db_execute(
                    """UPDATE data_providers
                       SET api_key_encrypted=?, api_secret_encrypted=?,
                           user_id_encrypted=?, password_encrypted=?, totp_encrypted=?,
                           access_token_encrypted=COALESCE(?, access_token_encrypted),
                           status=?, updated_at=?
                       WHERE provider='upstox'""",
                    (enc_key, enc_secret, enc_user, enc_pass, enc_totp, enc_token, status, now)
                )
                logger.info(f"[Startup] Upstox global provider seeded from credentials.ini (status={status})")

        # ── Seed Dhan ────────────────────────────────────────────────────────
        dp_dhan = db_fetchone("SELECT * FROM data_providers WHERE provider='dhan'")
        if dp_dhan and dp_dhan.get('status') == 'not_configured' and config.has_section('dhan_global'):
            s = config['dhan_global']
            client_id    = s.get('client_id', '').strip()
            access_token = s.get('access_token', '').strip()
            totp_val     = (s.get('totp_secret', '') or s.get('totp', '')).strip()

            if client_id and access_token:
                enc_key   = encrypt_secret(client_id)
                enc_token = encrypt_secret(access_token)
                enc_totp  = encrypt_secret(totp_val) if totp_val else None

                db_execute(
                    """UPDATE data_providers
                       SET api_key_encrypted=?, api_secret_encrypted=?,
                           access_token_encrypted=?, totp_encrypted=?,
                           status='configured', updated_at=?,
                           token_issued_at=COALESCE(token_issued_at, ?)
                       WHERE provider='dhan'""",
                    (enc_key, enc_token, enc_token, enc_totp, now, now)
                )
                logger.info("[Startup] Dhan global provider seeded from credentials.ini (status=configured)")

    except Exception as e:
        logger.error(f"[Startup] Failed to seed global providers from credentials.ini: {e}")


async def _global_provider_scheduler():
    """Background task to auto-connect global providers every morning."""
    while True:
        try:
            now = datetime.now(IST)
            # Target: 08:30 AM IST — before market open
            target = now.replace(hour=8, minute=30, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)

            wait_seconds = (target - now).total_seconds()
            logger.info(f"[Scheduler] Next global provider sync at {target.isoformat()} (waiting {wait_seconds:.0f}s)")
            await asyncio.sleep(wait_seconds)

            logger.info("[Scheduler] Starting morning global provider sync...")
            from web.admin_api import global_provider_connect_background

            providers = ['upstox', 'dhan']
            for p in providers:
                try:
                    # Mocking admin user for internal call
                    await global_provider_connect_background(p, admin={"id": 0})
                except Exception as e:
                    logger.error(f"[Scheduler] Failed to auto-connect {p}: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Scheduler] Global provider scheduler error: {e}")
            await asyncio.sleep(60)

async def _startup_auto_connect():
    """
    On bot startup, wait a few seconds for the server to fully initialise, then
    attempt to connect both global data feeders automatically.  This means the
    admin never needs to click 'Connect Now' after a restart.
    """
    await asyncio.sleep(5)           # let Uvicorn finish startup
    logger.info("[Startup] Auto-connecting global data providers...")
    try:
        from web.admin_api import global_provider_connect_background
        for p in ('upstox', 'dhan'):
            try:
                result = await global_provider_connect_background(p, admin={"id": 0})
                status = "OK" if result.get("success") else f"FAILED: {result.get('message','')}"
                logger.info(f"[Startup] Auto-connect {p}: {status}")
            except Exception as _e:
                logger.error(f"[Startup] Auto-connect {p} error: {_e}")
    except Exception as e:
        logger.error(f"[Startup] Auto-connect task error: {e}")


async def _subscription_expiry_scheduler():
    """Run daily at 09:15 AM IST — send expiry alerts for plans expiring in 7 or 1 days."""
    while True:
        try:
            now = datetime.now(IST)
            target = now.replace(hour=9, minute=15, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            logger.info("[Scheduler] Running subscription expiry check...")
            try:
                clients = db_fetchall(
                    "SELECT id, username, email, full_name, subscription_tier, "
                    "plan_expiry_date, telegram_chat_id "
                    "FROM users WHERE role='client' AND is_active=1 "
                    "AND plan_expiry_date IS NOT NULL AND plan_expiry_date != ''"
                )
                from utils.emailer import send_subscription_expiry_alert
                from utils.notifier import notify_subscription_expiry
                admin_row = db_fetchone("SELECT email FROM users WHERE role='admin' LIMIT 1")
                admin_email = admin_row["email"] if admin_row else None

                for c in clients:
                    try:
                        exp = datetime.fromisoformat(c["plan_expiry_date"]).date()
                        days_left = (exp - datetime.now(IST).date()).days
                        if days_left in (7, 1):
                            send_subscription_expiry_alert(c, days_left, admin_email)
                            if c.get("telegram_chat_id"):
                                notify_subscription_expiry(
                                    c["telegram_chat_id"], c["username"],
                                    c["subscription_tier"], days_left
                                )
                            logger.info(f"[Scheduler] Expiry alert sent for {c['username']} (expires in {days_left}d)")
                    except Exception as _ce:
                        logger.error(f"[Scheduler] Expiry check error for {c['username']}: {_ce}")
            except Exception as e:
                logger.error(f"[Scheduler] Expiry scheduler inner error: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Scheduler] Expiry scheduler error: {e}")
            await asyncio.sleep(60)


async def _kill_switch_enforcer():
    """
    Runs every 5 minutes during market hours (9:15–15:30 IST).
    Checks each running client instance's daily live PnL against their daily_loss_limit.
    If the loss limit is exceeded, stops the bot and sets trading_locked_until.
    """
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes
            now = datetime.now(IST)
            weekday = now.weekday()
            if weekday >= 5:
                continue
            market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
            market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
            if not (market_open <= now <= market_close):
                continue

            today = now.strftime("%Y-%m-%d")
            try:
                running_instances = db_fetchall(
                    """SELECT id, client_id, daily_loss_limit, trading_locked_until,
                              capital_allocated, max_drawdown_pct, pnl_reset_date,
                              max_daily_trades, daily_trade_count
                       FROM client_broker_instances
                       WHERE status='running'
                         AND (daily_loss_limit > 0 OR max_drawdown_pct > 0)"""
                )
                for inst in running_instances:
                    try:
                        # Already locked — skip
                        if inst.get("trading_locked_until"):
                            continue
                        # Sum today's live P&L from trade history
                        row = db_fetchone(
                            "SELECT COALESCE(SUM(pnl_rs), 0) as total_rs, COUNT(*) as cnt "
                            "FROM trade_history WHERE instance_id=? AND UPPER(trading_mode)='LIVE' "
                            "AND date(closed_at)=?",
                            (inst["id"], today)
                        )
                        daily_pnl_rs = row["total_rs"] if row else 0
                        trade_count  = row["cnt"] if row else 0

                        # Update live P&L and trade count in instance row
                        db_execute(
                            "UPDATE client_broker_instances SET daily_pnl=?, daily_trade_count=?, pnl_reset_date=? WHERE id=?",
                            (daily_pnl_rs, trade_count, today, inst["id"])
                        )

                        if daily_pnl_rs >= 0:
                            continue  # profitable, nothing to enforce

                        trigger_reason = None
                        # Check daily loss limit (₹)
                        loss_limit = inst.get("daily_loss_limit") or 0
                        if loss_limit > 0 and abs(daily_pnl_rs) >= loss_limit:
                            trigger_reason = f"Daily loss limit ₹{loss_limit:.0f} exceeded (loss: ₹{abs(daily_pnl_rs):.0f})"
                        # Check max drawdown % of capital
                        drawdown_pct = inst.get("max_drawdown_pct") or 0
                        capital = inst.get("capital_allocated") or 0
                        if not trigger_reason and drawdown_pct > 0 and capital > 0:
                            actual_drawdown_pct = abs(daily_pnl_rs) / capital * 100
                            if actual_drawdown_pct >= drawdown_pct:
                                trigger_reason = f"Max drawdown {drawdown_pct:.1f}% exceeded ({actual_drawdown_pct:.1f}% of ₹{capital:.0f})"

                        if not trigger_reason:
                            continue  # within all limits

                        # KILL-SWITCH TRIGGERED
                        logger.warning(
                            f"[KillSwitch] Instance {inst['id']} (client {inst['client_id']}): {trigger_reason}"
                        )
                        # Find next trading day 9:15 AM for unlock time
                        unlock = now + timedelta(days=1)
                        for _ in range(7):
                            if unlock.weekday() < 5:
                                break
                            unlock += timedelta(days=1)
                        unlock = unlock.replace(hour=9, minute=15, second=0, microsecond=0)

                        # Stop the instance and lock
                        from hub.instance_manager import instance_manager as _im
                        _im.stop_instance(inst["id"])
                        db_execute(
                            "UPDATE client_broker_instances SET status='idle', bot_pid=NULL, "
                            "trading_locked_until=? WHERE id=?",
                            (unlock.isoformat(), inst["id"])
                        )

                        # Notify client via Telegram
                        client_row = db_fetchone(
                            "SELECT username, telegram_chat_id FROM users WHERE id=?",
                            (inst["client_id"],)
                        )
                        if client_row and client_row.get("telegram_chat_id"):
                            _tg_id = client_row["telegram_chat_id"]
                            from utils.notifier import notify_kill_switch, notify_squareoff
                            notify_kill_switch(
                                _tg_id,
                                client_row["username"],
                                trigger_reason,
                                daily_pnl_rs,
                            )
                            # Explicit squareoff notification so client sees all
                            # positions are closed regardless of graceful/forced shutdown
                            notify_squareoff(_tg_id, {
                                "instrument": inst.get("instrument", ""),
                                "broker": inst.get("broker", ""),
                                "reason": f"Kill-switch: {trigger_reason}",
                                "total_pnl_rs": daily_pnl_rs,
                                "total_pnl_pts": 0.0,
                            })
                        logger.info(f"[KillSwitch] Instance {inst['id']} stopped. Locked until {unlock.isoformat()}")
                    except Exception as _ie:
                        logger.error(f"[KillSwitch] Instance {inst.get('id')} error: {_ie}")
            except Exception as e:
                logger.error(f"[KillSwitch] Enforcer inner error: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[KillSwitch] Enforcer error: {e}")
            await asyncio.sleep(60)


async def _dhan_auto_renewal_scheduler():
    """
    Run every hour — check client Dhan instances in API Key mode.
    If their token age > 22 hours (expires at 24h), auto-renew it.
    """
    while True:
        try:
            await asyncio.sleep(3600)  # check every hour
            logger.info("[Scheduler] Running Dhan token auto-renewal check...")
            try:
                from web.auth import encrypt_secret, decrypt_secret
                from utils.auth_manager_dhan import is_dhan_api_key_mode, generate_dhan_token

                instances = db_fetchall("""
                    SELECT cbi.id, cbi.client_id,
                           cbi.api_key_encrypted, cbi.api_secret_encrypted,
                           cbi.broker_user_id_encrypted, cbi.password_encrypted,
                           cbi.totp_encrypted, cbi.token_updated_at
                    FROM client_broker_instances cbi
                    WHERE cbi.broker='dhan' AND cbi.status != 'removed'
                      AND cbi.api_key_encrypted IS NOT NULL
                      AND cbi.password_encrypted IS NOT NULL
                """)

                now = datetime.now(IST)
                for inst in instances:
                    try:
                        api_secret = decrypt_secret(inst["api_secret_encrypted"]) if inst.get("api_secret_encrypted") else ""
                        if not is_dhan_api_key_mode({"api_secret": api_secret}):
                            continue  # skip non-API-Key-mode instances

                        # Check token age
                        ts_str = inst.get("token_updated_at") or ""
                        if ts_str:
                            ts = datetime.fromisoformat(ts_str).replace(tzinfo=IST)
                            age_hours = (now - ts).total_seconds() / 3600
                            if age_hours < 22:
                                continue  # still fresh

                        api_key    = decrypt_secret(inst["api_key_encrypted"])
                        client_id  = decrypt_secret(inst["broker_user_id_encrypted"]) if inst.get("broker_user_id_encrypted") else ""
                        password   = decrypt_secret(inst["password_encrypted"])
                        totp_sec   = decrypt_secret(inst["totp_encrypted"]) if inst.get("totp_encrypted") else ""

                        _dhan_result = generate_dhan_token(api_key, client_id, password, totp_sec)
                        token = _dhan_result['token']
                        if token:
                            enc_token = encrypt_secret(token)
                            now_ist   = now.isoformat()
                            db_execute(
                                "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE id=?",
                                (enc_token, now_ist, inst["id"])
                            )
                            logger.info(f"[Dhan Renewal] Auto-renewed token for instance {inst['id']} (client {inst['client_id']})")
                        else:
                            logger.warning(f"[Dhan Renewal] Token renewal FAILED for instance {inst['id']}: {_dhan_result['error']}")
                    except Exception as _ie:
                        logger.error(f"[Dhan Renewal] Instance {inst.get('id')} error: {_ie}")
            except Exception as e:
                logger.error(f"[Dhan Renewal] Scheduler inner error: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Dhan Renewal] Scheduler error: {e}")
            await asyncio.sleep(60)


_NSE_HOLIDAYS_DEFAULT = frozenset({
    # 2025
    "2025-01-26", "2025-02-26", "2025-03-14", "2025-03-31",
    "2025-04-10", "2025-04-14", "2025-04-18", "2025-05-01",
    "2025-08-15", "2025-10-02", "2025-10-20", "2025-10-21",
    "2025-11-05", "2025-12-25",
    # 2026
    "2026-01-26", "2026-03-03", "2026-04-03", "2026-04-14",
    "2026-04-17", "2026-05-01", "2026-08-15", "2026-10-02",
    "2026-10-09", "2026-11-24", "2026-12-25",
})


def _get_nse_holidays() -> frozenset:
    """Load NSE holiday list from platform_settings (key: nse_holidays_json).
    Falls back to the hardcoded default list if not configured in DB."""
    try:
        import json as _json
        row = db_fetchone("SELECT value FROM platform_settings WHERE key='nse_holidays_json'")
        if row and row.get("value"):
            return frozenset(_json.loads(row["value"]))
    except Exception:
        pass
    return _NSE_HOLIDAYS_DEFAULT


async def _day_end_summary_scheduler():
    """Run daily at 15:30 IST on market days (weekdays, excluding NSE holidays)."""
    while True:
        try:
            now = datetime.now(IST)
            target = now.replace(hour=15, minute=30, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            # Advance to next market day (weekday + not NSE holiday)
            _holidays = _get_nse_holidays()
            while target.weekday() >= 5 or target.strftime("%Y-%m-%d") in _holidays:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())

            # Re-check after sleep — skip if weekend or NSE holiday
            _now_ist = datetime.now(IST)
            _today_str = _now_ist.strftime("%Y-%m-%d")
            _holidays = _get_nse_holidays()
            if _now_ist.weekday() >= 5 or _today_str in _holidays:
                logger.info(f"[Scheduler] Day-end summary skipped — market holiday or weekend ({_today_str}).")
                continue

            logger.info("[Scheduler] Sending day-end Telegram summaries...")
            try:
                today = datetime.now(IST).strftime("%Y-%m-%d")
                clients = db_fetchall(
                    "SELECT u.id, u.username, u.telegram_chat_id, "
                    "MIN(cbi.broker) as broker, MIN(cbi.instrument) as instrument "
                    "FROM users u LEFT JOIN client_broker_instances cbi ON cbi.client_id=u.id "
                    "WHERE u.role='client' AND u.is_active=1 AND u.telegram_chat_id IS NOT NULL "
                    "AND u.telegram_chat_id != '' "
                    "GROUP BY u.id, u.username, u.telegram_chat_id"
                )
                from utils.notifier import notify_day_end_summary
                for c in clients:
                    try:
                        trades = db_fetchall(
                            "SELECT pnl_pts, pnl_rs, direction FROM trade_history "
                            "WHERE client_id=? AND date(closed_at)=? AND UPPER(trading_mode)='LIVE'",
                            (c["id"], today)
                        )
                        total_pts = sum(t.get("pnl_pts") or 0 for t in trades)
                        total_rs  = sum(t.get("pnl_rs") or 0 for t in trades)
                        wins   = sum(1 for t in trades if (t.get("pnl_pts") or 0) > 0)
                        losses = sum(1 for t in trades if (t.get("pnl_pts") or 0) < 0)
                        # Always send summary (even 0-trade days show bot is active)
                        notify_day_end_summary(c["telegram_chat_id"], {
                            "date": today, "broker": c.get("broker", ""),
                            "total_trades": len(trades), "wins": wins, "losses": losses,
                            "total_pnl_pts": total_pts, "total_pnl_rs": total_rs,
                        })
                    except Exception as _ce:
                        logger.error(f"[Scheduler] Day-end error for {c['username']}: {_ce}")
            except Exception as e:
                logger.error(f"[Scheduler] Day-end scheduler inner error: {e}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Scheduler] Day-end scheduler error: {e}")
            await asyncio.sleep(60)


@app.get("/health")
async def health_check():
    """Public health check endpoint for monitoring tools and process managers.
    Returns bot uptime, active client session count, and timestamp.
    No authentication required — safe to expose behind a firewall/load balancer.
    """
    import os
    uptime_secs = time.time() - APP_START_TIME
    hrs, rem = divmod(int(uptime_secs), 3600)
    mins, secs = divmod(rem, 60)

    # Count running client bot instances from DB
    try:
        active_sessions = (db_fetchone(
            "SELECT COUNT(*) as c FROM client_broker_instances WHERE status='running'"
        ) or {}).get("c", 0)
    except Exception:
        active_sessions = 0

    return {
        "status": "ok",
        "uptime_seconds": round(uptime_secs, 1),
        "uptime": f"{hrs}h {mins}m {secs}s",
        "started_at": datetime.fromtimestamp(APP_START_TIME, tz=timezone.utc).isoformat(),
        "active_sessions": active_sessions,
        "pid": os.getpid(),
        "version": app.version,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _hub_schedule_reconnect_for_instance(row: dict):
    """
    Schedule a background reconnect for a single broker instance row if:
      - the token is stale for that broker, AND
      - auto-login credentials (password + totp) are stored, AND
      - a loop is not already active or in cooldown.
    Called at server startup and periodically to detect mid-day token expiry.
    """
    from hub.reconnect_manager import reconnect_manager
    from web.client_api import _is_token_fresh, _is_dhan_token_fresh, _make_headless_login_fn
    from utils.auth_manager_dhan import is_dhan_api_key_mode as _is_akm
    from web.auth import decrypt_secret

    user_id = row["client_id"]
    broker  = row["broker"]
    if reconnect_manager.is_active(user_id, broker) or reconnect_manager.is_cooldown(user_id, broker):
        return

    if not (row.get("password_encrypted") and row.get("totp_encrypted")):
        return  # no auto-login credentials

    token_ts = row.get("token_updated_at", "")
    if broker == "dhan":
        api_sec = decrypt_secret(row["api_secret_encrypted"]) if row.get("api_secret_encrypted") else ""
        stale = not _is_dhan_token_fresh(token_ts, api_key_mode=_is_akm({"api_secret": api_sec}))
    else:
        stale = not _is_token_fresh(token_ts)

    if not stale:
        return  # token is fresh — no reconnect needed

    fn = _make_headless_login_fn(user_id, broker)
    scheduled = reconnect_manager.schedule(user_id, broker, fn)
    if scheduled:
        logger.info(
            f"[HubReconnect] Stale token detected — scheduled background reconnect for "
            f"{broker} (user {user_id})"
        )


async def _hub_reconnect_scanner():
    """
    Hub-driven reconnect orchestrator.
    Runs at server startup then every 30 minutes, scanning all broker instances
    whose tokens are stale but have auto-login credentials stored.
    This ensures reconnect attempts happen even when the client dashboard is closed.
    """
    SCAN_INTERVAL_SECONDS = 30 * 60  # 30 minutes
    while True:
        try:
            rows = db_fetchall(
                """
                SELECT cbi.client_id, cbi.broker, cbi.token_updated_at,
                       cbi.password_encrypted, cbi.totp_encrypted,
                       cbi.api_key_encrypted, cbi.api_secret_encrypted
                FROM client_broker_instances cbi
                WHERE cbi.status != 'removed'
                """,
                ()
            )
            for row in rows:
                try:
                    _hub_schedule_reconnect_for_instance(dict(row))
                except Exception as exc:
                    logger.warning(f"[HubReconnect] Error scanning instance: {exc}")
        except Exception as exc:
            logger.warning(f"[HubReconnect] Scanner error: {exc}")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


@app.on_event("startup")
async def startup_event():
    # Auto-seed global provider credentials from credentials.ini (if not yet in DB)
    _seed_global_providers_from_ini()
    # Immediately try to connect both feeders in background (non-blocking)
    asyncio.create_task(_startup_auto_connect())
    # Background morning scheduler (runs daily at 08:30 AM IST)
    asyncio.create_task(_global_provider_scheduler())
    # Daily subscription expiry alerts (09:15 AM IST)
    asyncio.create_task(_subscription_expiry_scheduler())
    # Day-end Telegram summary (15:30 IST, weekdays only)
    asyncio.create_task(_day_end_summary_scheduler())
    # Dhan token auto-renewal (hourly, renews when token age > 22h)
    asyncio.create_task(_dhan_auto_renewal_scheduler())
    # Daily loss kill-switch enforcer (runs every 5 minutes during market hours)
    asyncio.create_task(_kill_switch_enforcer())
    # Hub-driven broker reconnect scanner (startup + every 30 min)
    asyncio.create_task(_hub_reconnect_scanner())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.server:app", host="0.0.0.0", port=5000, reload=True)
