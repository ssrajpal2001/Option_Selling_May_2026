import urllib.parse
import asyncio
import json
import logging
import os
import time
from pathlib import Path
import subprocess
import configparser
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timezone

from web.deps import require_admin, get_current_user
from web.db import db_fetchone, db_fetchall, db_execute
from web.auth import encrypt_secret, decrypt_secret
from hub.instance_manager import instance_manager
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])

_CREDENTIALS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'config', 'credentials.ini')


def _sync_upstox_to_credentials(api_key: str, access_token: str, api_secret: str = None):
    creds = configparser.ConfigParser()
    creds.read(_CREDENTIALS_PATH)
    updated_sections = []
    for section in creds.sections():
        if section.lower().startswith('upstox'):
            if api_key:
                creds.set(section, 'api_key', api_key)
            if api_secret:
                creds.set(section, 'api_secret', api_secret)
            creds.set(section, 'access_token', access_token)
            updated_sections.append(section)
    if updated_sections:
        with open(_CREDENTIALS_PATH, 'w') as f:
            creds.write(f)
        logger.info(f"Synced Upstox credentials to credentials.ini sections: {updated_sections}")




# ── Data Provider Management ──────────────────────────────────────────────────

class ProviderConfigRequest(BaseModel):
    provider: str
    api_key: str
    api_secret: str
    user_id: Optional[str] = None
    password: Optional[str] = None
    totp: Optional[str] = None

class ManualTokenRequest(BaseModel):
    provider: str
    raw_value: str

@router.get("/data-providers")
async def list_data_providers(admin=Depends(require_admin)):
    providers = db_fetchall("SELECT provider, status, updated_at FROM data_providers")
    return providers

@router.get("/data-providers/{provider}/auth")
async def global_provider_auth(provider: str, request: Request, admin=Depends(require_admin)):
    """Legacy endpoint for browser-based OAuth fallback."""
    dp = db_fetchone("SELECT * FROM data_providers WHERE provider=?", (provider,))
    if not dp: raise HTTPException(400, f"Configure {provider} in the system first.")

    if provider == 'upstox':
        api_key = decrypt_secret(dp["api_key_encrypted"])
        from web.auth import _fernet
        state_payload = f"admin:{int(time.time())}"
        state_encrypted = _fernet.encrypt(state_payload.encode()).decode()
        redirect_uri = "https://google.com"
        auth_dialog = "https://api.upstox.com/v2/login/authorization/dialog"
        url = f"{auth_dialog}?response_type=code&client_id={api_key}&redirect_uri={urllib.parse.quote(redirect_uri)}&state={urllib.parse.quote(state_encrypted)}"
        return RedirectResponse(url)
    elif provider == 'dhan':
        from web.auth import _fernet
        state_payload = f"admin:{int(time.time())}"
        state_encrypted = _fernet.encrypt(state_payload.encode()).decode()
        url = f"https://login.dhan.co/?state={urllib.parse.quote(state_encrypted)}"
        return RedirectResponse(url)
    raise HTTPException(400, f"OAuth not supported for {provider}")


@router.post("/data-providers/{provider}/connect")
async def global_provider_connect_background(provider: str, admin=Depends(require_admin)):
    """JSON endpoint for background automated login."""
    dp = db_fetchone("SELECT * FROM data_providers WHERE provider=?", (provider,))
    if not dp: return {"success": False, "message": "Provider not configured."}

    try:
        token = None
        if provider == 'upstox':
            from utils.auth_manager_upstox import handle_upstox_login_automated
            creds = {
                "api_key": decrypt_secret(dp["api_key_encrypted"]),
                "api_secret": decrypt_secret(dp.get("api_secret_encrypted", "")),
                "user_id": decrypt_secret(dp.get("user_id_encrypted", "")),
                "password": decrypt_secret(dp.get("password_encrypted", "")),
                "totp": decrypt_secret(dp.get("totp_encrypted", ""))
            }
            token = handle_upstox_login_automated(creds)
            if token:
                _sync_upstox_to_credentials(creds["api_key"], token, creds["api_secret"])

        elif provider == 'dhan':
            from utils.auth_manager_dhan import handle_dhan_login_automated
            creds = {
                "api_key": decrypt_secret(dp["api_key_encrypted"]),
                "user_id": decrypt_secret(dp.get("user_id_encrypted", "")),
                "password": decrypt_secret(dp.get("password_encrypted", "")),
                "totp": decrypt_secret(dp.get("totp_encrypted", ""))
            }
            token = handle_dhan_login_automated(creds)

        if token:
            enc_token = encrypt_secret(token)
            now = datetime.now(timezone.utc).isoformat()
            db_execute("UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=? WHERE provider=?", (enc_token, now, provider))

            # Also sync to credentials.ini for compatibility with legacy parts of the bot
            if provider == 'upstox':
                _sync_upstox_to_credentials(decrypt_secret(dp["api_key_encrypted"]), token, decrypt_secret(dp.get("api_secret_encrypted", "")))
            elif provider == 'dhan':
                # Sync Dhan to credentials.ini [dhan_global]
                creds = configparser.ConfigParser()
                creds.read(_CREDENTIALS_PATH)
                if not creds.has_section('dhan_global'): creds.add_section('dhan_global')
                creds.set('dhan_global', 'client_id', decrypt_secret(dp["api_key_encrypted"]))
                creds.set('dhan_global', 'access_token', token)
                with open(_CREDENTIALS_PATH, 'w') as f:
                    creds.write(f)

            return {"success": True, "message": f"{provider.capitalize()} background login successful."}
        else:
            logger.warning(f"Background login for {provider} returned no token.")
            return {"success": False, "message": f"Background login failed for {provider.capitalize()}. Check credentials and TOTP seed."}

    except Exception as e:
        logger.error(f"[Admin] Background {provider} auth error: {e}")
        return {"success": False, "message": f"Error: {str(e)}"}

@router.post("/data-providers")
async def update_data_provider(body: ProviderConfigRequest, admin=Depends(require_admin)):
    try:
        enc_key = encrypt_secret(body.api_key)
        enc_secret = encrypt_secret(body.api_secret)
        enc_user = encrypt_secret(body.user_id) if body.user_id else None
        enc_pass = encrypt_secret(body.password) if body.password else None
        enc_totp = encrypt_secret(body.totp) if body.totp else None

        now = datetime.now(timezone.utc).isoformat()

        # Logic Fix: Only treat api_secret as access_token for Dhan (if access_token not explicitly provided)
        # For Upstox, api_secret is the App Secret needed for OAuth/OTP flow.
        if body.provider == 'dhan':
            db_execute(
                "UPDATE data_providers SET api_key_encrypted=?, access_token_encrypted=?, api_secret_encrypted=?, user_id_encrypted=?, password_encrypted=?, totp_encrypted=?, status='configured', updated_at=?, updated_by=? WHERE provider=?",
                (enc_key, enc_secret, enc_secret, enc_user, enc_pass, enc_totp, now, admin["id"], body.provider)
            )
        else:
            # For Upstox/Others, keep access_token separate.
            # We don't overwrite access_token here unless we are intentionally clearing it.
            db_execute(
                "UPDATE data_providers SET api_key_encrypted=?, api_secret_encrypted=?, user_id_encrypted=?, password_encrypted=?, totp_encrypted=?, updated_at=?, updated_by=? WHERE provider=?",
                (enc_key, enc_secret, enc_user, enc_pass, enc_totp, now, admin["id"], body.provider)
            )

        # Global providers are now read directly from DB by the bot.
        # No need to sync to credentials.ini anymore.
        logger.info(f"Admin updated global provider {body.provider} in database.")
        return {"success": True}
    except Exception as e:
        return {"success": False, "message": str(e)}

@router.post("/data-providers/exchange-token")
async def exchange_manual_token(body: ManualTokenRequest, admin=Depends(require_admin)):
    """Exchanges a manual 'code' or raw redirect URL for an access token for global providers."""
    try:
        provider = body.provider.lower()
        raw_val = body.raw_value.strip()

        # 1. Extract code from URL if provided
        import urllib.parse
        code = raw_val
        if '?' in raw_val:
            parsed = urllib.parse.urlparse(raw_val)
            query = urllib.parse.parse_qs(parsed.query)
            if 'code' in query:
                code = query['code'][0]
            elif 'access_token' in query:
                # Dhan sometimes passes access_token directly in redirect if not standard OAuth
                code = query['access_token'][0]

        # 2. Perform exchange if it's Upstox
        if provider == 'upstox':
            dp = db_fetchone("SELECT * FROM data_providers WHERE provider='upstox'")
            if not dp: return {"success": False, "message": "Provider not found"}

            api_key = decrypt_secret(dp["api_key_encrypted"])
            api_secret = decrypt_secret(dp.get("api_secret_encrypted", ""))

            # Since the user was redirected to Google, the 'redirect_uri' used in the original
            # auth dialog must be used for the exchange.
            redirect_uri = "https://google.com"

            import requests as http_requests
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
                return {"success": False, "message": resp_data.get("errors", [{}])[0].get("message", "Token exchange failed.")}

            access_token = resp_data.get("access_token", "")
            enc_token = encrypt_secret(access_token)
            now = datetime.now(timezone.utc).isoformat()
            db_execute("UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=? WHERE provider='upstox'", (enc_token, now))
            return {"success": True, "message": "Upstox global token updated via manual code."}

        elif provider == 'dhan':
            # For Dhan, we usually just store the token directly
            enc_token = encrypt_secret(code)
            now = datetime.now(timezone.utc).isoformat()
            db_execute("UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=? WHERE provider='dhan'", (enc_token, now))
            return {"success": True, "message": "Dhan global token updated."}

        return {"success": False, "message": f"Manual exchange not implemented for {provider}"}
    except Exception as e:
        logger.error(f"Manual token exchange failed: {e}", exc_info=True)
        return {"success": False, "message": str(e)}

# ── Client Management ────────────────────────────────────────────────────────

@router.get("/clients")
async def list_clients(admin=Depends(require_admin)):
    clients = db_fetchall("""
        SELECT u.id, u.username, u.email, u.is_active, u.subscription_tier,
               u.max_broker_instances, u.created_at, u.activated_at,
               SUM(CASE WHEN cbi.status != 'removed' THEN 1 ELSE 0 END) as broker_count,
               SUM(CASE WHEN cbi.status='running' THEN 1 ELSE 0 END) as running_count,
               GROUP_CONCAT(DISTINCT CASE WHEN cbi.status != 'removed' THEN cbi.broker END) as brokers
        FROM users u
        LEFT JOIN client_broker_instances cbi ON cbi.client_id=u.id
        WHERE u.role='client'
        GROUP BY u.id
        ORDER BY u.created_at DESC
    """)
    return clients


@router.post("/clients/{client_id}/activate")
async def activate_client(client_id: int, admin=Depends(require_admin)):
    user = db_fetchone("SELECT * FROM users WHERE id=? AND role='client'", (client_id,))
    if not user:
        raise HTTPException(404, "Client not found")
    now = datetime.now(timezone.utc).isoformat()
    db_execute(
        "UPDATE users SET is_active=1, activated_at=?, activated_by=? WHERE id=?",
        (now, admin["id"], client_id)
    )
    return {"success": True, "message": f"Client '{user['username']}' activated."}


@router.post("/clients/{client_id}/deactivate")
async def deactivate_client(client_id: int, admin=Depends(require_admin)):
    user = db_fetchone("SELECT * FROM users WHERE id=? AND role='client'", (client_id,))
    if not user:
        raise HTTPException(404, "Client not found")
    instance_manager.stop_all_for_client(client_id)
    db_execute("UPDATE users SET is_active=0 WHERE id=?", (client_id,))
    return {"success": True, "message": f"Client '{user['username']}' deactivated."}


@router.post("/clients/{client_id}/set-tier")
async def set_subscription_tier(client_id: int, request: Request, admin=Depends(require_admin)):
    body = await request.json()
    tier = body.get("tier", "FREE")
    max_broker_instances = {"FREE": 1, "PREMIUM": 3}.get(tier, 1)
    db_execute(
        "UPDATE users SET subscription_tier=?, max_broker_instances=? WHERE id=?",
        (tier, max_broker_instances, client_id)
    )
    return {"success": True, "tier": tier, "max_broker_instances": max_broker_instances}


@router.get("/clients/{client_id}/detail")
async def client_detail(client_id: int, admin=Depends(require_admin)):
    user = db_fetchone("SELECT id, username, email, is_active, subscription_tier, max_broker_instances, created_at FROM users WHERE id=?", (client_id,))
    if not user:
        raise HTTPException(404, "Client not found")
    instances = db_fetchall("""
        SELECT id, broker, trading_mode, instrument, quantity, strategy_version,
               status, bot_pid, last_heartbeat, token_updated_at, created_at
        FROM client_broker_instances WHERE client_id=? AND status != 'removed'
    """, (client_id,))
    failures = db_fetchall("""
        SELECT order_side, failure_reason, retry_attempt, paired_leg_closed, created_at
        FROM order_failures WHERE client_id=? ORDER BY created_at DESC LIMIT 20
    """, (client_id,))
    trades = db_fetchall("""
        SELECT trade_type, direction, strike, entry_price, exit_price,
               pnl_pts, pnl_rs, exit_reason, closed_at, entry_index_price
        FROM trade_history WHERE client_id=? ORDER BY closed_at DESC LIMIT 50
    """, (client_id,))
    broker_requests = db_fetchall("""
        SELECT id, current_broker, requested_broker, reason, status, created_at, resolved_at
        FROM broker_change_requests WHERE client_id=? ORDER BY created_at DESC LIMIT 10
    """, (client_id,))
    return {"client": user, "instances": instances, "failures": failures, "trades": trades, "broker_requests": broker_requests}


@router.post("/clients/{client_id}/force-close")
async def force_close_positions(client_id: int, admin=Depends(require_admin)):
    instance_manager.stop_all_for_client(client_id)
    # Also update DB status to ensure UI reflects it immediately
    db_execute("UPDATE client_broker_instances SET status='idle', bot_pid=NULL WHERE client_id=?", (client_id,))
    return {"success": True, "message": "All bot instances stopped for client."}


@router.post("/clients/{client_id}/clear-history")
async def clear_trade_history(client_id: int, admin=Depends(require_admin)):
    logger.info(f"[Admin] Clearing trade history for client {client_id} requested by admin {admin['username']}")
    try:
        # 0. Stop any running instances first to prevent old status overwriting the reset
        instance_manager.stop_all_for_client(client_id)

        # 1. Clear database history
        logger.info(f"[Admin] Deleting records from trade_history and order_failures for client {client_id}")
        db_execute("DELETE FROM trade_history WHERE client_id=?", (client_id,))
        db_execute("DELETE FROM order_failures WHERE client_id=?", (client_id,))

        # Verify deletion
        from web.db import db_fetchone
        remain = db_fetchone("SELECT COUNT(*) as c FROM trade_history WHERE client_id=?", (client_id,))
        logger.info(f"[Admin] Database history cleared for client {client_id}. Remaining: {remain['c'] if remain else 0}")

        # 2. Reset session PnL in bot status files (if they exist)
        # We use a precise glob to avoid matching client 1 for client 10/100
        # Pattern: bot_status_client_1.json or bot_status_client_1_*.json
        status_files = list(Path('config').glob(f'bot_status_client_{client_id}.json'))
        status_files.extend(list(Path('config').glob(f'bot_status_client_{client_id}_*.json')))

        logger.debug(f"[Admin] Resetting session data in {len(status_files)} status files for client {client_id}")
        for sf in status_files:
            try:
                if sf.exists():
                    with open(sf, 'r') as f:
                        data = json.load(f)
                    data['session_pnl'] = 0
                    data['trade_count'] = 0
                    data['trade_history'] = [] # Clear order book history

                    # Reset position statuses if they exist
                    if 'buy' in data:
                        for side in data['buy']:
                            if isinstance(data['buy'][side], dict):
                                data['buy'][side]['status'] = 'IDLE'
                    if 'sell' in data:
                        for side in ['CE', 'PE']:
                            if side in data['sell'] and isinstance(data['sell'][side], dict):
                                data['sell'][side]['placed'] = False
                        if 'v3_extras' in data['sell']:
                            data['sell']['v3_extras'] = {}

                    with open(sf, 'w') as f:
                        json.dump(data, f, indent=2)
                    logger.debug(f"[Admin] Reset status file: {sf.name}")
            except Exception as e:
                logger.warning(f"[Admin] Failed to reset status file {sf}: {e}")

        # 3. Delete V3 state files to ensure re-entry logic resets
        state_files = list(Path('config').glob(f'sell_v3_state_{client_id}_*.json'))
        # Include non-instrument specific state file if it exists
        state_files.extend(list(Path('config').glob(f'sell_v3_state_{client_id}.json')))

        logger.debug(f"[Admin] Deleting {len(state_files)} state files for client {client_id}")
        for vf in state_files:
            try:
                if vf.exists():
                    vf.unlink()
                    logger.debug(f"[Admin] Deleted state file: {vf.name}")
            except Exception as e:
                logger.warning(f"[Admin] Failed to delete state file {vf}: {e}")

        # 4. Wipe orchestrator memory if bot is running (optional, but good for immediate UI feedback)
        # Note: This only works if the bot process is the same as the web server,
        # which it isn't (they are separate PIDs). So we rely on file reset.

        logger.info(f"[Admin] Successfully cleared all history and state for client {client_id}")
        return {"success": True, "message": "Trade history, session PnL, and V3 state cleared."}
    except Exception as e:
        logger.error(f"[Admin] Error in clear_trade_history for client {client_id}: {e}", exc_info=True)
        return {"success": False, "message": f"Failed to clear history: {str(e)}"}


# ── Admin live overview ──────────────────────────────────────────────────────

@router.get("/overview")
async def overview(admin=Depends(require_admin)):
    total_clients = db_fetchone("SELECT COUNT(*) as c FROM users WHERE role='client'")["c"]
    active_clients = db_fetchone("SELECT COUNT(*) as c FROM users WHERE role='client' AND is_active=1")["c"]
    pending_clients = db_fetchone("SELECT COUNT(*) as c FROM users WHERE role='client' AND is_active=0")["c"]
    running_instances = db_fetchone("SELECT COUNT(*) as c FROM client_broker_instances WHERE status='running'")["c"]
    failures_today = db_fetchone(
        "SELECT COUNT(*) as c FROM order_failures WHERE date(created_at)=date('now')"
    )["c"]

    live_details = []
    running_rows = db_fetchall("""
        SELECT cbi.id, cbi.client_id, cbi.broker, cbi.instrument, cbi.quantity,
               cbi.trading_mode, cbi.strategy_version, cbi.bot_pid, u.username
        FROM client_broker_instances cbi
        JOIN users u ON u.id = cbi.client_id
        WHERE cbi.status = 'running'
    """)
    for row in running_rows:
        entry = dict(row)
        status_file = Path(f'config/bot_status_client_{row["client_id"]}.json')
        if status_file.exists():
            try:
                with open(status_file, 'r') as f:
                    bd = json.load(f)
                entry["session_pnl"] = float(bd.get("session_pnl") or 0)
                entry["trade_count"] = int(bd.get("trade_count") or 0)
                entry["heartbeat"] = float(bd.get("heartbeat") or 0)
                hb_age = time.time() - entry["heartbeat"]
                entry["stale"] = hb_age > 30
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                entry["session_pnl"] = 0
                entry["stale"] = True
        else:
            entry["session_pnl"] = 0
            entry["stale"] = True
        live_details.append(entry)

    pending_broker_requests = db_fetchall("""
        SELECT bcr.id, bcr.client_id, bcr.current_broker, bcr.requested_broker,
               bcr.reason, bcr.created_at, u.username
        FROM broker_change_requests bcr
        JOIN users u ON u.id = bcr.client_id
        WHERE bcr.status='pending'
        ORDER BY bcr.created_at DESC
    """)

    return {
        "total_clients": total_clients,
        "active_clients": active_clients,
        "pending_clients": pending_clients,
        "running_instances": running_instances,
        "failures_today": failures_today,
        "live_instances": live_details,
        "pending_broker_requests": pending_broker_requests,
    }


# ── Admin bot monitor for any client ─────────────────────────────────────

@router.get("/clients/{client_id}/bot-status")
async def client_bot_status(client_id: int, admin=Depends(require_admin)):
    user = db_fetchone("SELECT id, username FROM users WHERE id=?", (client_id,))
    if not user:
        raise HTTPException(404, "Client not found")

    instance = db_fetchone(
        "SELECT id, broker, status, trading_mode, instrument, quantity, strategy_version FROM client_broker_instances WHERE client_id=? ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, id DESC LIMIT 1",
        (client_id,)
    )
    if not instance:
        return {"configured": False, "bot_data": {}}

    live_status = instance_manager.get_instance_status(instance["id"])
    bot_data = {}
    status_file = Path(f'config/bot_status_client_{client_id}.json')
    if status_file.exists():
        try:
            with open(status_file, 'r') as f:
                bot_data = json.load(f)
            heartbeat = float(bot_data.get('heartbeat') or 0)
            age = time.time() - heartbeat
            bot_data['stale'] = age > 30
            bot_data['stale_seconds'] = round(age)
            if not live_status["running"] and heartbeat > 0 and age < 30:
                live_status["running"] = True
                live_status["pid"] = bot_data.get("pid")
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            bot_data = {}

    return {
        "configured": True,
        "instance": dict(instance),
        "live": live_status,
        "bot_data": bot_data,
        "username": user["username"],
    }


# ── Broker Change Requests ─────────────────────────────────────────────────

@router.get("/broker-requests")
async def list_broker_requests(admin=Depends(require_admin)):
    rows = db_fetchall("""
        SELECT bcr.*, u.username, u.email
        FROM broker_change_requests bcr
        JOIN users u ON u.id = bcr.client_id
        ORDER BY CASE WHEN bcr.status='pending' THEN 0 ELSE 1 END, bcr.created_at DESC
        LIMIT 50
    """)
    return rows


@router.post("/broker-requests/{request_id}/approve")
async def approve_broker_request(request_id: int, admin=Depends(require_admin)):
    req = db_fetchone("SELECT * FROM broker_change_requests WHERE id=?", (request_id,))
    if not req:
        raise HTTPException(404, "Request not found")
    if req["status"] != "pending":
        raise HTTPException(400, "Request already resolved")

    running = db_fetchone(
        "SELECT id FROM client_broker_instances WHERE client_id=? AND broker=? AND status='running'",
        (req["client_id"], req["current_broker"])
    )
    if running:
        instance_manager.stop_all_for_client(req["client_id"])
        db_execute("UPDATE client_broker_instances SET status='idle', bot_pid=NULL WHERE client_id=? AND status='running'", (req["client_id"],))

    now = datetime.now(timezone.utc).isoformat()
    db_execute(
        "UPDATE client_broker_instances SET status='removed' WHERE client_id=? AND broker=?",
        (req["client_id"], req["current_broker"])
    )
    db_execute(
        "UPDATE broker_change_requests SET status='approved', resolved_at=?, resolved_by_id=? WHERE id=?",
        (now, admin["id"], request_id)
    )
    return {"success": True, "message": f"Broker change approved. Old {req['current_broker']} instance disabled. Client can now set up {req['requested_broker']}."}


@router.post("/broker-requests/{request_id}/deny")
async def deny_broker_request(request_id: int, admin=Depends(require_admin)):
    req = db_fetchone("SELECT * FROM broker_change_requests WHERE id=?", (request_id,))
    if not req:
        raise HTTPException(404, "Request not found")
    if req["status"] != "pending":
        raise HTTPException(400, "Request already resolved")
    now = datetime.now(timezone.utc).isoformat()
    db_execute(
        "UPDATE broker_change_requests SET status='denied', resolved_at=?, resolved_by_id=? WHERE id=?",
        (now, admin["id"], request_id)
    )
    return {"success": True, "message": "Broker change request denied."}


# ── V3 Backtest Engine ───────────────────────────────────────────────────

class BacktestStartRequest(BaseModel):
    instrument: str
    date: str
    quantity: int = 1


_backtest_proc_handle: Optional[subprocess.Popen] = None
_backtest_status_path = Path("config/backtest_status_ui.json")


def _get_bt_log_path(user_id):
    return Path(f"logs/backtest_client_{user_id}.log")


@router.post("/backtest/start")
async def start_backtest(body: BacktestStartRequest, user=Depends(get_current_user)):
    global _backtest_proc_handle

    if _backtest_proc_handle and _backtest_proc_handle.poll() is None:
        return {"success": False, "message": "A backtest is already running."}

    # Reset status file
    status_path = Path(f"config/backtest_status_ui_{user['id']}.json")
    if status_path.exists():
        status_path.unlink()

    # Create temporary config
    try:
        # Ensure logs directory exists and define log path early
        log_path = _get_bt_log_path(user['id'])
        log_path.parent.mkdir(parents=True, exist_ok=True)

        base_cfg_path = Path('config/config_trader.ini')
        temp_cfg_path = Path(f'config/backtest_ui_temp_{user["id"]}.ini')

        cp = configparser.ConfigParser()
        if base_cfg_path.exists():
            cp.read(base_cfg_path)

        if not cp.has_section('app'): cp.add_section('app')
        if not cp.has_section('settings'): cp.add_section('settings')

        cp.set('app', 'log_file', str(log_path))
        cp.set('settings', 'backtest_enabled', 'True')
        cp.set('settings', 'instrument_to_trade', body.instrument)
        cp.set('settings', 'backtest_date', body.date)
        cp.set('settings', 'trading_mode', 'paper')

        # Override quantity for all brokers in the temp config
        for section in cp.sections():
            # Apply to all sections that are not standard app/settings/database
            if section.lower() not in ['app', 'settings', 'database', 'logging', 'data_provider', 'DEFAULT']:
                cp.set(section, 'quantity', str(body.quantity))

        with open(temp_cfg_path, 'w') as f:
            cp.write(f)

        # Clear old log
        with open(log_path, 'w') as f:
            f.write(f"--- Starting UI Backtest for {body.instrument} on {body.date} ---\n")

        # Spawn subprocess
        env = os.environ.copy()
        env['UI_BACKTEST_MODE'] = 'True'
        env['CLIENT_ID'] = str(user['id'])
        env['PYTHONPATH'] = env.get('PYTHONPATH', '') + os.pathsep + os.getcwd()

        log_file_handle = open(log_path, 'a')
        _backtest_proc_handle = subprocess.Popen(
            ["python3", "main.py", "--config", str(temp_cfg_path)],
            stdout=log_file_handle,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=os.getcwd()
        )
        log_file_handle.close()

        return {"success": True, "message": f"Backtest started for {body.instrument} on {body.date}. Logs: {log_path}"}

    except Exception as e:
        logger.error(f"Failed to start UI Backtest subprocess: {e}", exc_info=True)
        return {"success": False, "message": f"Error: {str(e)}"}


@router.get("/backtest/status")
async def get_backtest_status(user=Depends(get_current_user)):
    running = _backtest_proc_handle is not None and _backtest_proc_handle.poll() is None
    exit_code = _backtest_proc_handle.poll() if _backtest_proc_handle else None

    log_tail = []
    log_path = _get_bt_log_path(user['id'])
    if log_path.exists():
        try:
            with open(log_path, 'r') as f:
                # Read last 100 lines
                log_tail = f.readlines()[-100:]
                log_tail = [line.strip() for line in log_tail]
        except Exception:
            pass

    status_path = Path(f"config/backtest_status_ui_{user['id']}.json")
    if not status_path.exists():
        return {
            "running": running,
            "exit_code": exit_code,
            "logs": log_tail,
            "stats": {"pnl": 0, "trade_count": 0},
            "trades": []
        }

    try:
        with open(status_path, 'r') as f:
            data = json.load(f)

        # Check if actually finished
        finished = data.get('finished', False) or (not running and _backtest_proc_handle is not None)

        v3_extras = data.get('sell', {}).get('v3_extras', {})

        return {
            "running": running,
            "finished": finished,
            "exit_code": exit_code,
            "logs": log_tail or data.get('log_tail', []),
            "stats": {
                "pnl": data.get('session_pnl', 0),
                "trade_count": data.get('trade_count', 0),
                "rsi": v3_extras.get('combined_rsi'),
                "roc": v3_extras.get('combined_roc'),
                "slope": v3_extras.get('slope_status'),
                "price": v3_extras.get('combined_price'),
                "vwap": v3_extras.get('combined_vwap'),
                "entry_reason": v3_extras.get('entry_reason', 'SCANNING'),
                "tsl_lock": v3_extras.get('tsl_lock'),
                "exit_rule_status": v3_extras.get('exit_rule_status'),
                "entry_details": v3_extras.get('entry_details', []),
                "exit_details": v3_extras.get('exit_details', [])
            },
            "trades": data.get('trade_history', [])
        }
    except Exception:
        return {"running": running, "error": "Failed to read status", "logs": log_tail}


@router.post("/backtest/stop")
async def stop_backtest(user=Depends(get_current_user)):
    global _backtest_proc_handle
    if _backtest_proc_handle and _backtest_proc_handle.poll() is None:
        _backtest_proc_handle.terminate()
        try:
            _backtest_proc_handle.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _backtest_proc_handle.kill()
        return {"success": True, "message": "Backtest subprocess terminated."}
    return {"success": False, "message": "No backtest running."}
