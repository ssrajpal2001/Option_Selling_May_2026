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
            from utils.auth_manager_dhan import generate_dhan_token
            client_id  = decrypt_secret(dp.get("api_key_encrypted",  "") or "")
            app_id     = decrypt_secret(dp.get("user_id_encrypted",   "") or "")
            pin        = decrypt_secret(dp.get("password_encrypted",  "") or "")
            totp_sec   = decrypt_secret(dp.get("totp_encrypted",      "") or "")
            if not (client_id and app_id and pin):
                return {"success": False,
                        "message": "Dhan credentials incomplete. Save all 5 fields first (Client ID, API Key, API Secret, PIN, TOTP)."}
            token = generate_dhan_token(
                api_key=app_id,
                client_id=client_id,
                password=pin,
                totp_secret=totp_sec,
            )

        if token:
            enc_token = encrypt_secret(token)
            now = datetime.now(timezone.utc).isoformat()
            if provider == 'upstox':
                db_execute(
                    "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=?, token_issued_at=? WHERE provider=?",
                    (enc_token, now, now, provider)
                )
            else:
                db_execute(
                    "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=?, token_issued_at=? WHERE provider=?",
                    (enc_token, now, now, provider)
                )

            try:
                from hub.feed_registry import refresh_feed_credentials
                api_key_clear = decrypt_secret(dp.get("api_key_encrypted", ""))
                refreshed = refresh_feed_credentials(provider, token, api_key=api_key_clear)
                if refreshed:
                    logger.info(f"[Admin] Live {provider} feed signaled to refresh credentials.")
            except Exception as _rf_err:
                logger.warning(f"[Admin] Could not signal live feed refresh for {provider}: {_rf_err}")

            if provider == 'upstox':
                _sync_upstox_to_credentials(decrypt_secret(dp["api_key_encrypted"]), token, decrypt_secret(dp.get("api_secret_encrypted", "")))
            elif provider == 'dhan':
                _dhan_creds = configparser.ConfigParser()
                _dhan_creds.read(_CREDENTIALS_PATH)
                if not _dhan_creds.has_section('dhan_global'): _dhan_creds.add_section('dhan_global')
                _dhan_creds.set('dhan_global', 'client_id', decrypt_secret(dp["api_key_encrypted"]))
                _dhan_creds.set('dhan_global', 'access_token', token)
                with open(_CREDENTIALS_PATH, 'w') as f:
                    _dhan_creds.write(f)

            return {"success": True, "message": f"{provider.capitalize()} background login successful."}
        else:
            logger.warning(f"Background login for {provider} returned no token.")
            if provider == 'dhan':
                msg = ("Dhan token generation failed. Verify your Client ID, API Key, PIN and TOTP secret are correct.")
            elif provider == 'upstox':
                msg = "Upstox login failed. Check your User ID, Password, and TOTP secret in the configure panel."
            else:
                msg = f"{provider.capitalize()} login returned no token. Verify credentials."
            return {"success": False, "message": msg}

    except Exception as e:
        logger.error(f"[Admin] Background {provider} auth error: {e}")
        return {"success": False, "message": f"Error: {str(e)}"}


@router.post("/data-providers/connect-all")
async def connect_all_global_providers(admin=Depends(require_admin)):
    """Connect both Upstox and Dhan simultaneously and return per-provider results."""
    results = {}
    for provider in ("upstox", "dhan"):
        dp = db_fetchone("SELECT * FROM data_providers WHERE provider=?", (provider,))
        if not dp:
            results[provider] = {"success": False, "message": "Not configured in DB."}
            continue
        try:
            token = None
            if provider == "upstox":
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

            elif provider == "dhan":
                from utils.auth_manager_dhan import generate_dhan_token
                _client_id = decrypt_secret(dp.get("api_key_encrypted",  "") or "")
                _app_id    = decrypt_secret(dp.get("user_id_encrypted",   "") or "")
                _pin       = decrypt_secret(dp.get("password_encrypted",  "") or "")
                _totp_sec  = decrypt_secret(dp.get("totp_encrypted",      "") or "")
                if not (_client_id and _app_id and _pin):
                    results[provider] = {"success": False,
                                         "message": "Dhan credentials incomplete — save all 5 fields first."}
                    continue
                token = generate_dhan_token(
                    api_key=_app_id,
                    client_id=_client_id,
                    password=_pin,
                    totp_secret=_totp_sec,
                )

            if token:
                enc_token = encrypt_secret(token)
                now = datetime.now(timezone.utc).isoformat()
                if provider == "upstox":
                    db_execute(
                        "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=?, token_issued_at=? WHERE provider=?",
                        (enc_token, now, now, provider)
                    )
                else:
                    db_execute(
                        "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=?, token_issued_at=COALESCE(token_issued_at, ?) WHERE provider=?",
                        (enc_token, now, now, provider)
                    )
                # Signal any live feed to adopt new token immediately
                try:
                    from hub.feed_registry import refresh_feed_credentials
                    api_key_clear = decrypt_secret(dp.get("api_key_encrypted", ""))
                    refresh_feed_credentials(provider, token, api_key=api_key_clear)
                except Exception as _rf_err:
                    logger.warning(f"[Admin] Could not signal live feed refresh for {provider}: {_rf_err}")

                # Sync to credentials.ini for legacy compatibility (same as single-provider connect path)
                if provider == "dhan":
                    try:
                        _dhan_creds = configparser.ConfigParser()
                        _dhan_creds.read(_CREDENTIALS_PATH)
                        if not _dhan_creds.has_section('dhan_global'):
                            _dhan_creds.add_section('dhan_global')
                        _dhan_creds.set('dhan_global', 'client_id', decrypt_secret(dp["api_key_encrypted"]))
                        _dhan_creds.set('dhan_global', 'access_token', token)
                        with open(_CREDENTIALS_PATH, 'w') as _f:
                            _dhan_creds.write(_f)
                    except Exception as _ini_err:
                        logger.warning(f"[Admin] Could not sync Dhan token to credentials.ini: {_ini_err}")

                results[provider] = {"success": True, "message": f"{provider.capitalize()} connected."}
            else:
                results[provider] = {"success": False, "message": f"{provider.capitalize()} login returned no token."}

        except Exception as e:
            logger.error(f"[Admin] connect-all error for {provider}: {e}")
            results[provider] = {"success": False, "message": str(e)}

    overall = all(v["success"] for v in results.values())
    return {"success": overall, "results": results}


@router.post("/data-providers")
async def update_data_provider(body: ProviderConfigRequest, admin=Depends(require_admin)):
    try:
        enc_key = encrypt_secret(body.api_key)
        enc_secret = encrypt_secret(body.api_secret)
        enc_user = encrypt_secret(body.user_id) if body.user_id else None
        enc_pass = encrypt_secret(body.password) if body.password else None
        enc_totp = encrypt_secret(body.totp) if body.totp else None

        now = datetime.now(timezone.utc).isoformat()

        # Dhan: 5-field auto-login mode.
        # api_key  → api_key_encrypted  (Client ID / loginId)
        # user_id  → user_id_encrypted  (Application ID / applicationId)
        # api_secret → api_secret_encrypted (UUID permanent secret)
        # password → password_encrypted (PIN)
        # totp    → totp_encrypted     (TOTP secret)
        # access_token_encrypted is NOT touched here — it is written only by the /connect endpoint.
        if body.provider == 'dhan':
            db_execute(
                "UPDATE data_providers SET api_key_encrypted=?, api_secret_encrypted=?, user_id_encrypted=?, password_encrypted=?, totp_encrypted=?, status='configured', updated_at=?, updated_by=? WHERE provider=?",
                (enc_key, enc_secret, enc_user, enc_pass, enc_totp, now, admin["id"], body.provider)
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
            # Upstox: always reset token_issued_at (daily token replaced)
            db_execute(
                "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=?, token_issued_at=? WHERE provider='upstox'",
                (enc_token, now, now)
            )
            return {"success": True, "message": "Upstox global token updated via manual code."}

        elif provider == 'dhan':
            # Dhan: new token being manually entered — always reset token_issued_at (30-day countdown restarts)
            enc_token = encrypt_secret(code)
            now = datetime.now(timezone.utc).isoformat()
            db_execute(
                "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=?, token_issued_at=? WHERE provider='dhan'",
                (enc_token, now, now)
            )
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


def _audit(actor_id: int, actor_role: str, action: str, target_id: int, details: dict | None = None):
    """Insert one row into audit_log. Swallows all exceptions to never break the caller."""
    try:
        db_execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, target_type, target_id, details) VALUES (?,?,?,?,?,?)",
            (actor_id, actor_role, action, "user", target_id, json.dumps(details or {})),
        )
    except Exception as _ae:
        logger.warning(f"[Audit] Failed to write audit log: {_ae}")


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
    _audit(admin["id"], admin["role"], "activate_client", client_id, {"user": user["username"]})
    return {"success": True, "message": f"Client '{user['username']}' activated."}


@router.post("/clients/{client_id}/deactivate")
async def deactivate_client(client_id: int, admin=Depends(require_admin)):
    user = db_fetchone("SELECT * FROM users WHERE id=? AND role='client'", (client_id,))
    if not user:
        raise HTTPException(404, "Client not found")
    instance_manager.stop_all_for_client(client_id)
    db_execute("UPDATE users SET is_active=0 WHERE id=?", (client_id,))
    _audit(admin["id"], admin["role"], "deactivate_client", client_id, {"user": user["username"]})
    return {"success": True, "message": f"Client '{user['username']}' deactivated."}


def _resolve_effective_max_brokers(user: dict) -> int:
    """
    Returns the effective max_broker_instances for a user.
    Respects plan expiry: if plan_expiry_date is set and has passed, falls back to BASIC (1).
    """
    # Check plan expiry
    expiry_str = user.get("plan_expiry_date")
    if expiry_str:
        try:
            exp = datetime.fromisoformat(expiry_str)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                # Plan expired — fall back to BASIC (1 broker)
                return 1
        except Exception:
            pass
    return user.get("max_broker_instances") or 1


@router.post("/clients/{client_id}/set-tier")
async def set_subscription_tier(client_id: int, request: Request, admin=Depends(require_admin)):
    body = await request.json()
    plan_id = body.get("plan_id")
    tier = body.get("tier", "BASIC")
    plan_expiry_date = body.get("plan_expiry_date")  # ISO date string or None
    # Optional manual override — admin can explicitly cap slots independent of plan
    max_broker_override = body.get("max_broker_instances_override")

    # Lookup plan from DB
    if plan_id:
        plan = db_fetchone("SELECT * FROM subscription_plans WHERE id=? AND is_active=1", (plan_id,))
    else:
        plan = db_fetchone("SELECT * FROM subscription_plans WHERE plan_name=? AND is_active=1", (tier,))

    if plan:
        max_broker_instances = plan["max_broker_instances"]
        tier = plan["plan_name"]
        plan_id = plan["id"]
    else:
        max_broker_instances = {"FREE": 1, "BASIC": 1, "STANDARD": 2, "PREMIUM": 3, "PRO": 5, "ENTERPRISE": 999}.get(tier, 1)

    # Admin override wins if explicitly provided and valid
    if max_broker_override is not None:
        try:
            _ov = int(max_broker_override)
            if _ov > 0:
                max_broker_instances = _ov
        except (ValueError, TypeError):
            pass

    db_execute(
        "UPDATE users SET subscription_tier=?, max_broker_instances=?, plan_id=?, plan_expiry_date=? WHERE id=?",
        (tier, max_broker_instances, plan_id, plan_expiry_date, client_id)
    )
    # Audit log
    try:
        user = db_fetchone("SELECT username FROM users WHERE id=?", (client_id,))
        import json as _json
        _uname = user.get("username", "") if user else ""
        _details = _json.dumps({"plan": tier, "max_brokers": max_broker_instances, "expiry": plan_expiry_date, "user": _uname})
        db_execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, target_type, target_id, details) VALUES (?,?,?,?,?,?)",
            (admin["id"], "admin", "set_tier", "user", client_id, _details)
        )
    except Exception:
        pass
    return {"success": True, "tier": tier, "plan_id": plan_id, "max_broker_instances": max_broker_instances}


@router.get("/clients/{client_id}/detail")
async def client_detail(client_id: int, admin=Depends(require_admin)):
    user = db_fetchone("""
        SELECT u.id, u.username, u.email, u.is_active, u.subscription_tier,
               u.max_broker_instances, u.plan_id, u.plan_expiry_date, u.created_at,
               sp.display_name AS plan_display_name, sp.max_broker_instances AS plan_max_brokers,
               sp.plan_name AS plan_slug
        FROM users u
        LEFT JOIN subscription_plans sp ON sp.id = u.plan_id
        WHERE u.id=?
    """, (client_id,))
    if not user:
        raise HTTPException(404, "Client not found")
    # Effective broker cap (considers plan expiry)
    user["effective_max_brokers"] = _resolve_effective_max_brokers(user)
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
    _audit(admin["id"], admin["role"], "force_close", client_id, {"client_id": client_id})
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
        _audit(admin["id"], admin["role"], "clear_history", client_id, {"client_id": client_id})
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
    _audit(admin["id"], admin["role"], "approve_broker_request", req["client_id"],
           {"request_id": request_id, "from": req["current_broker"], "to": req["requested_broker"]})
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
    _audit(admin["id"], admin["role"], "deny_broker_request", req["client_id"],
           {"request_id": request_id, "from": req["current_broker"], "to": req["requested_broker"]})
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


# ── Subscription Plans CRUD ───────────────────────────────────────────────────

class SubscriptionPlanBody(BaseModel):
    plan_name: str
    display_name: str
    max_broker_instances: int = 1
    description: Optional[str] = ""
    is_active: Optional[int] = 1
    price_monthly: Optional[float] = None


@router.get("/plans")
async def list_subscription_plans(admin=Depends(require_admin)):
    plans = db_fetchall("""
        SELECT sp.*, COUNT(u.id) as client_count
        FROM subscription_plans sp
        LEFT JOIN users u ON u.subscription_tier = sp.plan_name AND u.role='client'
        GROUP BY sp.id
        ORDER BY sp.max_broker_instances ASC
    """)
    return plans


@router.post("/plans")
async def create_subscription_plan(body: SubscriptionPlanBody, admin=Depends(require_admin)):
    try:
        now = datetime.now(timezone.utc).isoformat()
        plan_id = db_execute("""
            INSERT INTO subscription_plans (plan_name, display_name, max_broker_instances, description, is_active, price_monthly, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (
            body.plan_name.upper(), body.display_name, body.max_broker_instances,
            body.description or "", body.is_active or 1, body.price_monthly, now, now
        ))
        return {"success": True, "plan_id": plan_id, "message": f"Plan '{body.display_name}' created."}
    except Exception as e:
        raise HTTPException(400, f"Failed to create plan: {str(e)}")


@router.put("/plans/{plan_id}")
async def update_subscription_plan(plan_id: int, body: SubscriptionPlanBody, admin=Depends(require_admin)):
    plan = db_fetchone("SELECT * FROM subscription_plans WHERE id=?", (plan_id,))
    if not plan:
        raise HTTPException(404, "Plan not found.")
    now = datetime.now(timezone.utc).isoformat()
    db_execute("""
        UPDATE subscription_plans
        SET plan_name=?, display_name=?, max_broker_instances=?, description=?, is_active=?, price_monthly=?, updated_at=?
        WHERE id=?
    """, (
        body.plan_name.upper(), body.display_name, body.max_broker_instances,
        body.description or "", body.is_active if body.is_active is not None else 1,
        body.price_monthly, now, plan_id
    ))
    # Sync existing users on this plan to the new broker limit
    db_execute(
        "UPDATE users SET max_broker_instances=? WHERE subscription_tier=?",
        (body.max_broker_instances, body.plan_name.upper())
    )
    return {"success": True, "message": f"Plan updated. {body.max_broker_instances} broker(s) per client on this plan."}


@router.delete("/plans/{plan_id}")
async def delete_subscription_plan(plan_id: int, admin=Depends(require_admin)):
    plan = db_fetchone("SELECT * FROM subscription_plans WHERE id=?", (plan_id,))
    if not plan:
        raise HTTPException(404, "Plan not found.")
    users_on_plan = db_fetchone(
        "SELECT COUNT(*) as c FROM users WHERE subscription_tier=? AND role='client'",
        (plan["plan_name"],)
    )
    if users_on_plan and users_on_plan["c"] > 0:
        raise HTTPException(400, f"Cannot delete plan '{plan['plan_name']}' — {users_on_plan['c']} client(s) are on it. Reassign them first.")
    db_execute("DELETE FROM subscription_plans WHERE id=?", (plan_id,))
    return {"success": True, "message": f"Plan '{plan['plan_name']}' deleted."}


# ── Data Provider Health ────────────────────────────────────────────────────

@router.get("/data-providers/health")
async def feeder_health_status(admin=Depends(require_admin)):
    """Returns token health AND live WebSocket connectivity state for global data providers."""
    from hub.feed_registry import get_ws_state
    providers = db_fetchall("SELECT * FROM data_providers WHERE provider IN ('upstox', 'dhan')")
    now = datetime.now(timezone.utc)
    result = {}
    for p in providers:
        ws = get_ws_state(p["provider"])
        info = {
            "provider": p["provider"],
            "status": p["status"],
            "updated_at": p.get("updated_at"),
            "has_token": bool(p.get("access_token_encrypted")),
            "has_credentials": bool(p.get("api_key_encrypted")) and (
                p["provider"] != "dhan" or bool(p.get("user_id_encrypted"))
            ),
            "token_age_hours": None,
            "token_fresh": False,
            # Live WebSocket state from feed_registry
            "ws_connected": ws["ws_connected"],
            "last_tick_time": ws["last_tick_time"],
        }
        if p.get("updated_at"):
            try:
                upd = datetime.fromisoformat(p["updated_at"])
                if upd.tzinfo is None:
                    upd = upd.replace(tzinfo=timezone.utc)
                age_s = (now - upd).total_seconds()
                info["token_age_hours"] = round(age_s / 3600, 1)
                if p["provider"] == "upstox":
                    # Upstox: daily token — freshness from updated_at (set on each new token fetch)
                    info["token_fresh"] = age_s < 86400
                    info["expires_in"] = f"{max(0, 24 - round(age_s/3600, 1))}h"
                else:
                    # Dhan: 30-day token — use token_issued_at if available; fall back to updated_at
                    issued_str = p.get("token_issued_at") or p.get("updated_at")
                    issued = datetime.fromisoformat(issued_str)
                    if issued.tzinfo is None:
                        issued = issued.replace(tzinfo=timezone.utc)
                    issued_age_s = (now - issued).total_seconds()
                    days_remaining = 30 - (issued_age_s / 86400)
                    info["token_fresh"] = days_remaining > 0
                    info["days_remaining"] = max(0, round(days_remaining, 1))
                    info["warn_expiry"] = 0 < days_remaining <= 5
            except Exception:
                pass

        # ── Dhan: live API token validation (quick check, timeout=5s) ──────
        if p["provider"] == "dhan" and p.get("access_token_encrypted"):
            try:
                import requests as _req
                _tok = decrypt_secret(p.get("access_token_encrypted", ""))
                _cid = decrypt_secret(p.get("api_key_encrypted", ""))
                _r = _req.get(
                    'https://api.dhan.co/v2/fundlimit',
                    headers={'access-token': _tok, 'client-id': _cid, 'Content-Type': 'application/json'},
                    timeout=5
                )
                info["token_api_valid"] = (_r.status_code == 200)
                info["token_api_status"] = _r.status_code
                if _r.status_code == 401:
                    info["token_fresh"] = False  # override — token is confirmed dead
                    info["token_expired"] = True
                    info["warn_expiry"] = False  # expired, not just warn
            except Exception as _ve:
                info["token_api_valid"] = None  # couldn't check (network/timeout)

        result[p["provider"]] = info
    return result


# ── Audit Log ────────────────────────────────────────────────────────────────

@router.get("/audit-log")
async def get_audit_log(
    admin=Depends(require_admin),
    page: int = 1,
    per_page: int = 50,
    action: str = None,
    username: str = None,
    actor_username: str = None,
    from_date: str = None,
    to_date: str = None,
):
    """Paginated audit log with filters for action, target/actor username, and date range."""
    offset = (page - 1) * per_page
    conditions = []
    params: list = []

    if action:
        conditions.append("al.action LIKE ?")
        params.append(f"%{action}%")
    if username:
        conditions.append("tu.username LIKE ?")
        params.append(f"%{username}%")
    if actor_username:
        conditions.append("au.username LIKE ?")
        params.append(f"%{actor_username}%")
    if from_date:
        conditions.append("DATE(al.created_at) >= ?")
        params.append(from_date)
    if to_date:
        conditions.append("DATE(al.created_at) <= ?")
        params.append(to_date)

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    rows = db_fetchall(f"""
        SELECT al.id, al.action, al.target_type, al.target_id, al.details,
               al.created_at, al.actor_role, al.ip_address,
               au.username AS admin_username,
               tu.username AS target_username,
               al.target_id  AS target_client_id
        FROM audit_log al
        LEFT JOIN users au ON au.id = al.actor_id
        LEFT JOIN users tu ON tu.id = al.target_id
        {where}
        ORDER BY al.created_at DESC
        LIMIT ? OFFSET ?
    """, params + [per_page, offset])

    total_row = db_fetchone(
        f"""SELECT COUNT(*) as c FROM audit_log al
            LEFT JOIN users au ON au.id = al.actor_id
            LEFT JOIN users tu ON tu.id = al.target_id
            {where}""", params
    )
    total = total_row["c"] if total_row else 0

    from datetime import date as _date
    today_str = str(_date.today())
    today_row = db_fetchone("SELECT COUNT(*) as c FROM audit_log WHERE DATE(created_at)=?", (today_str,))
    today_count = today_row["c"] if today_row else 0

    return {
        "items": rows,
        "total": total,
        "today_count": today_count,
        "page": page,
        "per_page": per_page,
    }


# ── System Health ─────────────────────────────────────────────────────────────

@router.get("/system-health")
async def system_health(admin=Depends(require_admin)):
    """Returns server uptime, process metrics, event loop state, and feed tick times."""
    import os, time, datetime as _dt
    import psutil

    info: dict = {}

    # Uptime
    try:
        proc = psutil.Process(os.getpid())
        started_at = proc.create_time()
        uptime_secs = time.time() - started_at
        hrs, rem = divmod(int(uptime_secs), 3600)
        mins, secs = divmod(rem, 60)
        info["uptime"] = f"{hrs}h {mins}m {secs}s"
        info["uptime_seconds"] = uptime_secs
        info["started_at"] = _dt.datetime.fromtimestamp(started_at, tz=timezone.utc).isoformat()
    except Exception as _e:
        info["uptime"] = "unavailable"

    # CPU / Memory
    try:
        info["cpu_percent"] = psutil.cpu_percent(interval=0.1)
        vm = psutil.virtual_memory()
        info["memory_used_mb"] = round(vm.used / 1024 / 1024, 1)
        info["memory_total_mb"] = round(vm.total / 1024 / 1024, 1)
        info["memory_percent"] = vm.percent
    except Exception:
        pass

    # Python process count
    try:
        info["python_process_count"] = sum(
            1 for p in psutil.process_iter(['name']) if 'python' in (p.info.get('name') or '').lower()
        )
    except Exception:
        info["python_process_count"] = None

    # Event loop health
    import asyncio
    loop = asyncio.get_event_loop()
    info["event_loop_running"] = loop.is_running()
    info["event_loop_closed"] = loop.is_closed()

    # Feed tick times (from feed_registry)
    try:
        from hub.feed_registry import get_ws_state
        for feed_name in ("upstox", "dhan"):
            ws = get_ws_state(feed_name)
            last = ws.get("last_tick_time")
            if last:
                age = time.time() - last
                info[f"{feed_name}_last_tick_secs_ago"] = round(age, 1)
                info[f"{feed_name}_ws_connected"] = ws["ws_connected"]
            else:
                info[f"{feed_name}_last_tick_secs_ago"] = None
                info[f"{feed_name}_ws_connected"] = ws["ws_connected"]
    except Exception:
        pass

    # Active client bots
    try:
        running = db_fetchone("SELECT COUNT(*) as c FROM client_broker_instances WHERE status='running'")
        info["active_bots"] = running["c"] if running else 0
    except Exception:
        info["active_bots"] = 0

    # Total clients + active
    try:
        tot = db_fetchone("SELECT COUNT(*) as c FROM users WHERE role='client'")
        act = db_fetchone("SELECT COUNT(*) as c FROM users WHERE role='client' AND is_active=1")
        info["total_clients"] = tot["c"] if tot else 0
        info["active_clients"] = act["c"] if act else 0
    except Exception:
        pass

    info["timestamp"] = _dt.datetime.now(timezone.utc).isoformat()
    return info


# ── Platform Settings (SMTP / Telegram) ──────────────────────────────────────

class PlatformSettingsBatch(BaseModel):
    settings: dict  # key → value


@router.get("/platform-settings")
async def get_platform_settings(admin=Depends(require_admin)):
    """Return all platform_settings (masks sensitive values)."""
    rows = db_fetchall("SELECT key, value FROM platform_settings ORDER BY key")
    safe = {}
    mask_keys = {"smtp_password", "telegram_bot_token"}
    for r in rows:
        k = r["key"]
        safe[k] = "••••••••" if (k in mask_keys and r["value"]) else (r["value"] or "")
    return {"settings": safe}


@router.post("/platform-settings")
async def save_platform_settings(body: PlatformSettingsBatch, admin=Depends(require_admin)):
    """Upsert platform settings. Pass empty string to clear a key."""
    now = _dt.datetime.now(timezone.utc).isoformat()
    for k, v in body.settings.items():
        # Do not overwrite password with mask placeholder
        if v == "••••••••":
            continue
        db_execute(
            "INSERT INTO platform_settings (key, value, updated_at, updated_by) VALUES (?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at, "
            "updated_by=excluded.updated_by",
            (k, v, now, admin["id"])
        )
    _audit(admin, "platform_settings_update", "system", 0, {
        "keys": [k for k in body.settings if body.settings[k] != "••••••••"]
    })
    return {"success": True, "message": "Platform settings saved."}


@router.post("/platform-settings/test-email")
async def test_email(admin=Depends(require_admin)):
    """Send a test email to the admin's registered email address."""
    admin_row = db_fetchone("SELECT email, username FROM users WHERE id=?", (admin["id"],))
    if not admin_row:
        raise HTTPException(400, "Admin user not found.")

    from utils.emailer import send_email
    ok = send_email(
        to=admin_row["email"],
        subject="[AlgoSoft] Test Email — SMTP Working",
        body_html="""
        <div style="font-family:Arial,sans-serif;max-width:500px;margin:auto;
                    background:#0f172a;color:#e2e8f0;padding:32px;border-radius:12px;">
          <h2 style="color:#00d4aa;margin-top:0">✅ Test Email Successful</h2>
          <p>Hello <strong>{name}</strong>,</p>
          <p>Your SMTP configuration is working correctly.
             AlgoSoft will use these settings for all subscription expiry and
             trade alert emails.</p>
          <hr style="border-color:#334155;margin:24px 0">
          <p style="font-size:12px;color:#64748b">AlgoSoft Automated Trading Platform</p>
        </div>
        """.replace("{name}", admin_row.get("username", "Admin")),
    )
    if ok:
        return {"success": True, "message": f"Test email sent to {admin_row['email']}"}
    raise HTTPException(500, "Failed to send test email. Check SMTP settings.")


@router.get("/clients/{client_id}/static-ip")
async def get_client_static_ip(client_id: int, admin=Depends(require_admin)):
    row = db_fetchone("SELECT static_ip FROM users WHERE id=?", (client_id,))
    if not row:
        raise HTTPException(404, "Client not found.")
    return {"static_ip": row.get("static_ip") or ""}


class StaticIPBody(BaseModel):
    static_ip: str = ""


@router.patch("/clients/{client_id}/static-ip")
async def update_client_static_ip(client_id: int, body: StaticIPBody,
                                   admin=Depends(require_admin)):
    row = db_fetchone("SELECT id FROM users WHERE id=?", (client_id,))
    if not row:
        raise HTTPException(404, "Client not found.")
    db_execute("UPDATE users SET static_ip=? WHERE id=?", (body.static_ip.strip() or None, client_id))
    _audit(admin, "client_static_ip_update", "user", client_id, {"static_ip": body.static_ip})
    return {"success": True}


@router.get("/clients/{client_id}/risk-overrides")
async def get_client_risk_overrides(client_id: int, admin=Depends(require_admin)):
    """Return client's strategy overrides and risk params from active broker instance."""
    inst = db_fetchone(
        "SELECT id, client_strategy_overrides, daily_loss_limit, trading_locked_until "
        "FROM client_broker_instances WHERE client_id=? ORDER BY id DESC LIMIT 1",
        (client_id,)
    )
    if not inst:
        return {"overrides": {}, "daily_loss_limit": 0, "trading_locked_until": None}
    overrides = {}
    try:
        if inst.get("client_strategy_overrides"):
            overrides = json.loads(inst["client_strategy_overrides"])
    except Exception:
        pass
    return {
        "overrides": overrides,
        "daily_loss_limit": inst.get("daily_loss_limit") or 0,
        "trading_locked_until": inst.get("trading_locked_until"),
    }
