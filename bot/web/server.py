import time
import hashlib
import urllib.parse
from datetime import datetime, timezone, timedelta

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
                           status='configured', updated_at=?
                       WHERE provider='dhan'""",
                    (enc_key, enc_token, enc_token, enc_totp, now)
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

@app.on_event("startup")
async def startup_event():
    # Auto-seed global provider credentials from credentials.ini (if not yet in DB)
    _seed_global_providers_from_ini()
    # Background morning scheduler (runs daily at 08:30 AM IST)
    asyncio.create_task(_global_provider_scheduler())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("web.server:app", host="0.0.0.0", port=5000, reload=True)
