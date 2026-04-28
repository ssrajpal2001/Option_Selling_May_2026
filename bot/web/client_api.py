import json
import time
import hashlib
import asyncio
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from typing import Optional
from web.deps import get_current_user
from web.db import db_fetchone, db_fetchall, db_execute
from web.auth import encrypt_secret, decrypt_secret, _fernet
from hub.instance_manager import instance_manager
from hub.reconnect_manager import reconnect_manager
from utils.logger import logger

router = APIRouter(prefix="/client", tags=["client"])

IST = timezone(timedelta(hours=5, minutes=30))
# Brokers that support fully-automated (headless) login — fyers excluded
HEADLESS_LOGIN_BROKERS = ("zerodha", "dhan", "angelone", "upstox", "aliceblue", "groww")


def _compute_has_credentials(row: dict) -> bool:
    """
    Returns True when the stored credentials are sufficient to *identify* this
    broker instance.  The check is broker-specific because different brokers
    use different primary credential fields.
    """
    broker = row.get("broker", "")
    if broker == "fyers":
        # App ID stored as broker_user_id + Secret ID stored as api_secret
        return bool(row.get("broker_user_id_encrypted") and row.get("api_secret_encrypted"))
    elif broker == "groww":
        # Client ID (broker_user_id) is the minimum required field
        return bool(row.get("broker_user_id_encrypted"))
    elif broker in ("angelone", "aliceblue"):
        # SmartAPI key / Alice API key + Client Code/ID
        return bool(row.get("api_key_encrypted") and row.get("broker_user_id_encrypted"))
    elif broker == "dhan":
        # Dhan: applicationId (api_key) is primary; api_secret UUID is alternate
        return bool(row.get("api_key_encrypted") or row.get("api_secret_encrypted"))
    else:  # zerodha, upstox
        return bool(row.get("api_key_encrypted"))


def _make_headless_login_fn(user_id: int, broker: str):
    """
    Returns a *synchronous* callable suitable for use with asyncio.to_thread().
    When called, it reads fresh credentials from the DB, attempts headless login,
    and — on success — writes the new token back to the DB.
    Returns the token string on success, or None on failure.
    """
    def _fn():
        from web.db import db_fetchone as _dbf, db_execute as _dbe
        from web.auth import decrypt_secret as _dec, encrypt_secret as _enc

        instance = _dbf(
            "SELECT * FROM client_broker_instances "
            "WHERE client_id=? AND broker=? AND status != 'removed'",
            (user_id, broker)
        )
        if not instance:
            return None
        if not (instance.get("password_encrypted") and instance.get("totp_encrypted")):
            return None

        creds = {
            "api_key":        _dec(instance["api_key_encrypted"]) if instance.get("api_key_encrypted") else "",
            "api_secret":     _dec(instance["api_secret_encrypted"]) if instance.get("api_secret_encrypted") else "",
            "broker_user_id": _dec(instance["broker_user_id_encrypted"]) if instance.get("broker_user_id_encrypted") else "",
            "password":       _dec(instance["password_encrypted"]),
            "totp":           _dec(instance["totp_encrypted"]),
        }

        token = None
        # fyers has no automated (headless) login path — skip silently
        if broker == "fyers":
            logger.debug(f"[ReconnectFn] fyers does not support headless login (user {user_id}); skipping")
            return None
        try:
            if broker == "zerodha":
                from utils.auth_manager_zerodha import handle_zerodha_login_automated
                token = handle_zerodha_login_automated(creds)
            elif broker == "dhan":
                from utils.auth_manager_dhan import handle_dhan_login_automated
                token = handle_dhan_login_automated(creds)
            elif broker == "angelone":
                from utils.auth_manager_angelone import handle_angelone_login
                creds["client_code"] = creds["broker_user_id"]
                creds["pin"] = creds["password"]
                smart_api = handle_angelone_login(creds)
                if smart_api:
                    token = smart_api.access_token
            elif broker == "upstox":
                from utils.auth_manager_upstox import handle_upstox_login_automated
                token = handle_upstox_login_automated(creds)
            elif broker == "aliceblue":
                from utils.auth_manager_alice import handle_alice_login_automated
                token = handle_alice_login_automated(creds)
            elif broker == "groww":
                from utils.auth_manager_groww import handle_groww_login_automated
                token = handle_groww_login_automated(creds)
        except Exception as exc:
            logger.warning(f"[ReconnectFn] {broker} login error for user {user_id}: {exc}")
            return None

        if token:
            enc_token = _enc(token)
            now_ist = datetime.now(IST).isoformat()
            _dbe(
                "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE id=?",
                (enc_token, now_ist, instance["id"])
            )
            logger.info(f"[ReconnectFn] {broker} token saved for user {user_id}")
        return token

    return _fn


def _audit_client(actor_id: int, action: str, details: dict | None = None):
    """Write an audit log entry for client-initiated actions."""
    try:
        db_execute(
            "INSERT INTO audit_log (actor_id, actor_role, action, target_type, target_id, details) "
            "VALUES (?,?,?,?,?,?)",
            (actor_id, "client", action, "user", actor_id, json.dumps(details or {})),
        )
    except Exception as _ae:
        logger.warning(f"[Audit] Client audit failed: {_ae}")


KITE_LOGIN_URL = "https://kite.trade/connect/login?v=3&api_key={api_key}"


def _is_token_fresh(token_updated_at: str) -> bool:
    if not token_updated_at:
        return False
    try:
        updated = datetime.fromisoformat(token_updated_at).replace(tzinfo=IST)
        now_ist = datetime.now(IST)
        today_6am = now_ist.replace(hour=6, minute=0, second=0, microsecond=0)
        if now_ist < today_6am:
            today_6am -= timedelta(days=1)
        return updated > today_6am
    except Exception:
        return False


def _is_dhan_token_fresh(token_updated_at: str, api_key_mode: bool = False) -> bool:
    if not token_updated_at:
        return False
    try:
        updated = datetime.fromisoformat(token_updated_at).replace(tzinfo=IST)
        now_ist = datetime.now(IST)
        elapsed = (now_ist - updated).total_seconds()
        if api_key_mode:
            return elapsed < 23 * 3600
        return elapsed < 30 * 86400
    except Exception:
        return False


def _get_active_instance(user_id: int, broker: str = None):
    if broker:
        instance = db_fetchone(
            "SELECT * FROM client_broker_instances WHERE client_id=? AND broker=? AND status != 'removed' ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, id DESC LIMIT 1",
            (user_id, broker)
        )
    else:
        instance = db_fetchone(
            "SELECT * FROM client_broker_instances WHERE client_id=? AND status != 'removed' ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, id DESC LIMIT 1",
            (user_id,)
        )
    return instance


# ── Broker Setup ─────────────────────────────────────────────────────────────

class BrokerSetup(BaseModel):
    broker: str = "zerodha"
    broker_user_id: Optional[str] = ""
    api_key: str
    api_secret: Optional[str] = ""
    access_token: Optional[str] = ""
    password: Optional[str] = ""
    totp: Optional[str] = ""
    trading_mode: str = "paper"
    instrument: str = "NIFTY"
    quantity: int = 25
    strategy_version: str = "V3"


@router.get("/broker")
async def get_broker_config(user=Depends(get_current_user)):
    rows = db_fetchall("""
        SELECT id, broker, broker_user_id_encrypted, password_encrypted, totp_encrypted,
               api_key_encrypted, api_secret_encrypted, access_token_encrypted,
               trading_mode, instrument, quantity, strategy_version,
               status, last_heartbeat, token_updated_at
        FROM client_broker_instances
        WHERE client_id=? AND status != 'removed'
    """, (user["id"],))
    from utils.auth_manager_dhan import is_dhan_api_key_mode as _is_akm_cfg
    result = []
    for r in rows:
        d = dict(r)
        # Presence flags (broker-specific credential completeness)
        d["has_credentials"] = _compute_has_credentials(d)
        d["has_api_key"] = d["has_credentials"]  # backwards-compat alias
        d["has_auto_login"] = bool(d.get("password_encrypted") and d.get("totp_encrypted"))
        # Masked display fields
        d["broker_user_id"] = "..." if d.get("broker_user_id_encrypted") else ""
        d["password"] = "..." if d.get("password_encrypted") else ""
        d["totp"] = "..." if d.get("totp_encrypted") else ""

        if d["broker"] == "dhan":
            _api_sec = decrypt_secret(d["api_secret_encrypted"]) if d.get("api_secret_encrypted") else ""
            _api_key_mode = _is_akm_cfg({"api_secret": _api_sec})
            d["dhan_api_key_mode"] = _api_key_mode
            d["token_fresh"] = _is_dhan_token_fresh(d.get("token_updated_at"), api_key_mode=_api_key_mode)
        else:
            d["token_fresh"] = _is_token_fresh(d.get("token_updated_at"))

        # Background reconnect status from the hub manager
        d["reconnect_status"] = reconnect_manager.get_status(user["id"], d["broker"])

        # Remove all raw encrypted fields from the API response
        for _f in ("api_key_encrypted", "api_secret_encrypted", "broker_user_id_encrypted",
                   "password_encrypted", "totp_encrypted", "access_token_encrypted"):
            d.pop(_f, None)

        result.append(d)
    # Resolve plan info + effective broker cap (handles plan expiry)
    from datetime import datetime, timezone as _tz
    now_utc = datetime.now(_tz.utc)
    expiry_str = user.get("plan_expiry_date")
    plan_expired = False
    if expiry_str:
        try:
            exp = datetime.fromisoformat(expiry_str)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_tz.utc)
            plan_expired = now_utc > exp
        except Exception:
            pass
    effective_max = 1 if plan_expired else (user.get("max_broker_instances") or 1)

    # Fetch plan display info
    plan_info = None
    if user.get("plan_id"):
        from web.db import db_fetchone as _dbf
        plan_info = _dbf("SELECT plan_name, display_name, max_broker_instances FROM subscription_plans WHERE id=?", (user["plan_id"],))

    return {
        "instances": result,
        "max_brokers": effective_max,
        "plan": {
            "name": plan_info["display_name"] if plan_info else (user.get("subscription_tier") or "Basic"),
            "slug": plan_info["plan_name"] if plan_info else (user.get("subscription_tier") or "BASIC"),
            "max_brokers": effective_max,
            "expiry_date": expiry_str,
            "expired": plan_expired,
        }
    }


@router.get("/broker-status")
async def get_broker_status(user=Depends(get_current_user)):
    """
    Alias of GET /broker — returns the same instances payload.
    Exists as the canonical polling endpoint for 30-second status refresh on the dashboard.
    """
    return await get_broker_config(user=user)


@router.post("/broker/{broker}/reconnect")
async def reconnect_broker(broker: str, user=Depends(get_current_user)):
    """
    Attempt a headless (automated) re-login for the given broker when the session
    token has expired. Only succeeds when Password + TOTP are stored.
    Called by the auto-reconnect loop on the client dashboard (max 5 retries, 60s gap).
    """
    VALID_BROKERS = ("zerodha", "dhan", "angelone", "upstox", "fyers", "aliceblue", "groww")
    if broker not in VALID_BROKERS:
        raise HTTPException(400, "Invalid broker.")

    instance = db_fetchone(
        "SELECT * FROM client_broker_instances WHERE client_id=? AND broker=? AND status != 'removed'",
        (user["id"], broker)
    )
    if not instance:
        raise HTTPException(404, "Broker not configured.")

    if not instance.get("api_key_encrypted"):
        raise HTTPException(400, "missing_credentials")

    has_auto_login = instance.get("password_encrypted") and instance.get("totp_encrypted")
    if not has_auto_login:
        raise HTTPException(400, "no_auto_login")

    logger.info(f"[Reconnect] Attempting headless re-login for {broker} (user {user['id']})...")

    creds = {
        "api_key":        decrypt_secret(instance["api_key_encrypted"]),
        "api_secret":     decrypt_secret(instance["api_secret_encrypted"]) if instance.get("api_secret_encrypted") else "",
        "broker_user_id": decrypt_secret(instance["broker_user_id_encrypted"]) if instance.get("broker_user_id_encrypted") else "",
        "password":       decrypt_secret(instance["password_encrypted"]),
        "totp":           decrypt_secret(instance["totp_encrypted"]),
    }

    token = None
    try:
        if broker == "zerodha":
            from utils.auth_manager_zerodha import handle_zerodha_login_automated
            token = await asyncio.to_thread(handle_zerodha_login_automated, creds)
        elif broker == "dhan":
            from utils.auth_manager_dhan import handle_dhan_login_automated
            token = await asyncio.to_thread(handle_dhan_login_automated, creds)
        elif broker == "angelone":
            from utils.auth_manager_angelone import handle_angelone_login
            creds["client_code"] = creds["broker_user_id"]
            creds["pin"] = creds["password"]
            smart_api = await asyncio.to_thread(handle_angelone_login, creds)
            if smart_api:
                token = smart_api.access_token
        elif broker == "upstox":
            from utils.auth_manager_upstox import handle_upstox_login_automated
            token = await asyncio.to_thread(handle_upstox_login_automated, creds)
        elif broker == "aliceblue":
            from utils.auth_manager_alice import handle_alice_login_automated
            token = await asyncio.to_thread(handle_alice_login_automated, creds)
        elif broker == "groww":
            from utils.auth_manager_groww import handle_groww_login_automated
            token = await asyncio.to_thread(handle_groww_login_automated, creds)
        elif broker == "fyers":
            raise HTTPException(400, "no_auto_login")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"[Reconnect] {broker} headless login error for user {user['id']}: {e}", exc_info=True)
        raise HTTPException(503, "reconnect_failed")

    if not token:
        logger.warning(f"[Reconnect] {broker} headless login FAILED for user {user['id']} (no token returned)")
        raise HTTPException(503, "reconnect_failed")

    logger.info(f"[Reconnect] {broker} headless login SUCCESS for user {user['id']}")
    enc_token = encrypt_secret(token)
    now_ist = datetime.now(IST).isoformat()
    db_execute(
        "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE id=?",
        (enc_token, now_ist, instance["id"])
    )
    _audit_client(user["id"], "BROKER_TOKEN_REFRESH", {"broker": broker, "method": "auto_reconnect"})
    return {"success": True, "message": f"{broker.capitalize()} reconnected successfully."}


@router.post("/broker/{broker}/reconnect/start")
async def start_broker_reconnect(broker: str, force: bool = False,
                                 user=Depends(get_current_user)):
    """
    Schedule a background reconnect loop for the given broker.
    The hub manager will attempt headless login every 60 s, up to 5 retries.
    Pass ?force=true to override the post-exhaustion cooldown (explicit user retry).
    Poll GET /broker/{broker}/reconnect-status for live progress.
    Only brokers with a headless login path are supported (fyers is excluded).
    """
    if broker not in HEADLESS_LOGIN_BROKERS:
        raise HTTPException(400, "Invalid broker.")
    if reconnect_manager.is_active(user["id"], broker):
        return {"started": False, "message": "Reconnect loop already running."}
    instance = db_fetchone(
        "SELECT password_encrypted, totp_encrypted FROM client_broker_instances "
        "WHERE client_id=? AND broker=? AND status != 'removed'",
        (user["id"], broker)
    )
    if not instance:
        raise HTTPException(404, "Broker not configured.")
    if not (instance.get("password_encrypted") and instance.get("totp_encrypted")):
        raise HTTPException(400, "no_auto_login")
    # Allow user to bypass cooldown with explicit force flag
    if force:
        reconnect_manager.clear_exhausted(user["id"], broker)
    fn = _make_headless_login_fn(user["id"], broker)
    started = reconnect_manager.schedule(user["id"], broker, fn, force=force)
    if not started:
        return {"started": False, "message": "In post-exhaustion cooldown. Pass ?force=true to override."}
    logger.info(f"[Reconnect] Background loop started for {broker} (user {user['id']}, force={force})")
    return {"started": True, "message": f"Reconnect loop started for {broker}."}


@router.post("/broker/{broker}/reconnect/cancel")
async def cancel_broker_reconnect(broker: str, user=Depends(get_current_user)):
    """Cancel an active background reconnect loop for the given broker."""
    if broker not in HEADLESS_LOGIN_BROKERS:
        raise HTTPException(400, "Invalid broker.")
    cancelled = reconnect_manager.cancel(user["id"], broker)
    return {"cancelled": cancelled, "message": "Cancelled." if cancelled else "No active loop found."}


@router.get("/broker/{broker}/reconnect-status")
async def get_broker_reconnect_status(broker: str, user=Depends(get_current_user)):
    """Return the current background reconnect state for the given broker."""
    if broker not in HEADLESS_LOGIN_BROKERS:
        raise HTTPException(400, "Invalid broker.")
    return reconnect_manager.get_status(user["id"], broker)


@router.delete("/broker/{broker}")
async def delete_broker_config(broker: str, user=Depends(get_current_user)):
    if broker not in ("zerodha", "dhan", "angelone", "upstox", "fyers", "aliceblue", "groww"):
        raise HTTPException(400, "Invalid broker.")

    instance = db_fetchone(
        "SELECT id, status FROM client_broker_instances WHERE client_id=? AND broker=? AND status != 'removed'",
        (user["id"], broker)
    )
    if not instance:
        raise HTTPException(404, "Broker configuration not found.")

    if instance["status"] == "running":
        raise HTTPException(400, "Stop the bot before removing the broker.")

    # Mark as removed instead of hard delete to preserve history (or we could delete)
    # But since we have a UNIQUE constraint, we should probably either hard delete or update to 'removed' and handle conflict
    db_execute(
        "DELETE FROM client_broker_instances WHERE id=?",
        (instance["id"],)
    )

    # Clean up status files
    status_file = Path(f'config/bot_status_client_{user["id"]}.json')
    if status_file.exists(): status_file.unlink()

    toggle_file = Path(f'config/trading_enabled_{user["id"]}.json')
    if toggle_file.exists(): toggle_file.unlink()

    # Clean up strategy state files
    for p in Path('config').glob(f'sell_v3_state_{user["id"]}_*.json'):
        try: p.unlink()
        except: pass

    logger.info(f"[Client] User {user['id']} removed broker configuration: {broker}")
    return {"success": True, "message": f"{broker.capitalize()} configuration removed."}


@router.post("/broker")
async def save_broker_config(body: BrokerSetup, user=Depends(get_current_user)):
    VALID_BROKERS = ("zerodha", "dhan", "angelone", "upstox", "fyers", "aliceblue", "groww")
    if body.broker not in VALID_BROKERS:
        raise HTTPException(400, f"Broker must be one of: {', '.join(VALID_BROKERS)}.")

    existing = db_fetchall(
        "SELECT id FROM client_broker_instances WHERE client_id=? AND status != 'removed'", (user["id"],)
    )
    # Effective max brokers — respects plan expiry
    from datetime import datetime, timezone as _tz2
    _exp_str = user.get("plan_expiry_date")
    _plan_expired = False
    if _exp_str:
        try:
            _exp = datetime.fromisoformat(_exp_str)
            if _exp.tzinfo is None: _exp = _exp.replace(tzinfo=_tz2.utc)
            _plan_expired = datetime.now(_tz2.utc) > _exp
        except Exception: pass
    max_b = 1 if _plan_expired else (user.get("max_broker_instances") or 1)

    if len(existing) >= max_b:
        existing_broker = db_fetchone(
            "SELECT id FROM client_broker_instances WHERE client_id=? AND broker=? AND status != 'removed'",
            (user["id"], body.broker)
        )
        if not existing_broker:
            if _plan_expired:
                raise HTTPException(400, "Your plan has expired. Broker slots are capped at 1. Contact admin to renew.")
            raise HTTPException(400, f"Your plan allows {max_b} broker(s). Contact admin to upgrade your plan.")

    if body.trading_mode not in ("paper", "live"):
        raise HTTPException(400, "trading_mode must be 'paper' or 'live'")

    existing_row = db_fetchone(
        "SELECT api_key_encrypted, api_secret_encrypted, access_token_encrypted, password_encrypted, totp_encrypted, broker_user_id_encrypted, token_updated_at FROM client_broker_instances WHERE client_id=? AND broker=?",
        (user["id"], body.broker)
    )

    is_new_key = body.api_key and body.api_key != "unchanged"
    is_new_secret = body.api_secret and body.api_secret != "unchanged"
    is_new_token = body.access_token and body.access_token != "unchanged"
    is_new_pwd = body.password and body.password != "unchanged"
    is_new_totp = body.totp and body.totp != "unchanged"
    is_new_user_id = body.broker_user_id and body.broker_user_id != "unchanged"

    # Universal save logic for all brokers
    enc_key = encrypt_secret(body.api_key) if is_new_key else (existing_row["api_key_encrypted"] if existing_row else None)
    enc_secret = encrypt_secret(body.api_secret) if is_new_secret else (existing_row["api_secret_encrypted"] if existing_row else None)
    enc_pwd = encrypt_secret(body.password) if is_new_pwd else (existing_row["password_encrypted"] if existing_row else None)
    enc_totp = encrypt_secret(body.totp) if is_new_totp else (existing_row["totp_encrypted"] if existing_row else None)
    enc_uid = encrypt_secret(body.broker_user_id) if is_new_user_id else (existing_row["broker_user_id_encrypted"] if existing_row else None)

    # access_token is only provided by manual-token brokers (fyers, groww).
    # Automated brokers omit it from the request; their token is set via the Connect endpoint.
    enc_token = encrypt_secret(body.access_token) if is_new_token else (existing_row["access_token_encrypted"] if existing_row else None)

    token_ts = existing_row["token_updated_at"] if existing_row else None
    if is_new_token:
        token_ts = datetime.now(IST).isoformat()

    db_execute("""
        INSERT INTO client_broker_instances
          (client_id, broker, api_key_encrypted, api_secret_encrypted, access_token_encrypted,
           password_encrypted, totp_encrypted, broker_user_id_encrypted,
           token_updated_at, trading_mode, instrument, quantity, strategy_version)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(client_id, broker) DO UPDATE SET
          api_key_encrypted=excluded.api_key_encrypted,
          api_secret_encrypted=excluded.api_secret_encrypted,
          access_token_encrypted=excluded.access_token_encrypted,
          password_encrypted=excluded.password_encrypted,
          totp_encrypted=excluded.totp_encrypted,
          broker_user_id_encrypted=excluded.broker_user_id_encrypted,
          token_updated_at=COALESCE(excluded.token_updated_at, client_broker_instances.token_updated_at),
          trading_mode=excluded.trading_mode,
          instrument=excluded.instrument,
          quantity=excluded.quantity,
          strategy_version=excluded.strategy_version
    """, (user["id"], body.broker, enc_key, enc_secret, enc_token, enc_pwd, enc_totp, enc_uid,
          token_ts, body.trading_mode, body.instrument, body.quantity, body.strategy_version))

    msg = f"{body.broker.capitalize()} credentials saved."
    if enc_pwd and enc_totp:
        msg += " One-Click Connect is enabled."

    _audit_client(user["id"], "broker_save", {"broker": body.broker, "mode": body.trading_mode})
    return {"success": True, "message": msg}


# ── Zerodha OAuth ────────────────────────────────────────────────────────────

@router.get("/zerodha/login-url")
async def zerodha_login_url(request: Request, user=Depends(get_current_user)):
    instance = db_fetchone("SELECT * FROM client_broker_instances WHERE client_id=? AND broker='zerodha'", (user["id"],))
    if not instance or not instance.get("api_key_encrypted"):
        raise HTTPException(400, "Save your API Key and API Secret in Settings first.")

    # 1. Attempt Automated Headless Login if credentials exist
    if instance.get("password_encrypted") and instance.get("totp_encrypted"):
        try:
            from utils.auth_manager_zerodha import handle_zerodha_login_automated
            creds = {
                "api_key":        decrypt_secret(instance.get("api_key_encrypted") or ""),
                "api_secret":     decrypt_secret(instance.get("api_secret_encrypted") or ""),
                "broker_user_id": decrypt_secret(instance.get("broker_user_id_encrypted") or ""),
                "password":       decrypt_secret(instance.get("password_encrypted") or ""),
                "totp":           decrypt_secret(instance.get("totp_encrypted") or ""),
            }
            token = await asyncio.to_thread(handle_zerodha_login_automated, creds)
            if token:
                enc_token = encrypt_secret(token)
                now_ist = datetime.now(IST).isoformat()
                db_execute("UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='zerodha'", (enc_token, now_ist, user["id"]))
                from hub.event_bus import event_bus
                await event_bus.publish('BROKER_TOKEN_UPDATED', {'user_id': user["id"], 'broker': 'zerodha', 'access_token': token})
                return {"success": True, "automated": True, "message": "Zerodha background login successful."}
            logger.warning(f"[Zerodha] Automated login returned no token for user {user['id']} — falling back to OAuth")
        except Exception as e:
            logger.error(f"[Zerodha] Automated login failed for user {user['id']}: {e}", exc_info=True)

    # 2. Fallback to Browser OAuth
    try:
        api_key = decrypt_secret(instance.get("api_key_encrypted") or "")
        state_payload = f'{user["id"]}:{int(time.time())}'
        state_encrypted = _fernet.encrypt(state_payload.encode()).decode()
        login_url = KITE_LOGIN_URL.format(api_key=api_key)
        raw_host = request.headers.get('host') or str(request.base_url).split('/')[2]
        proto = request.headers.get('x-forwarded-proto', 'http')
        if 'localhost' not in raw_host and '127.0.0.1' not in raw_host and proto != 'http':
            proto = 'https'
        redirect_uri = f"{proto}://{raw_host}/auth/zerodha/callback"
        login_url += "&redirect_uri=" + urllib.parse.quote(redirect_uri) + "&state=" + urllib.parse.quote(state_encrypted)
        return {"success": True, "login_url": login_url}
    except Exception as e:
        logger.error(f"[Zerodha] OAuth URL generation failed for user {user['id']}: {e}", exc_info=True)
        raise HTTPException(400, f"Could not generate Zerodha login URL: {str(e)[:200]}")


@router.get("/dhan/login-url")
async def dhan_login_url(user=Depends(get_current_user)):
    instance = db_fetchone("SELECT * FROM client_broker_instances WHERE client_id=? AND broker='dhan'", (user["id"],))
    if not instance or not instance.get("api_key_encrypted"):
        raise HTTPException(400, "Enter your Dhan credentials in Settings first.")

    api_key    = decrypt_secret(instance["api_key_encrypted"])
    api_secret = decrypt_secret(instance["api_secret_encrypted"]) if instance.get("api_secret_encrypted") else ""
    broker_uid = decrypt_secret(instance["broker_user_id_encrypted"]) if instance.get("broker_user_id_encrypted") else ""
    password   = decrypt_secret(instance["password_encrypted"]) if instance.get("password_encrypted") else ""
    totp_sec   = decrypt_secret(instance["totp_encrypted"]) if instance.get("totp_encrypted") else ""

    creds = {
        "api_key": api_key, "api_secret": api_secret,
        "broker_user_id": broker_uid, "password": password, "totp": totp_sec,
    }

    from utils.auth_manager_dhan import is_dhan_api_key_mode, generate_dhan_token, handle_dhan_login_automated

    # ── Path 1: API Key mode — auto-generate a fresh 24-hr token ──────────
    if is_dhan_api_key_mode(creds):
        if not password:
            raise HTTPException(
                400,
                "API Key mode requires your Dhan login password. "
                "Add it in Settings so tokens can be generated automatically."
            )
        dhan_client_id = broker_uid or api_key
        try:
            _dhan_result = await asyncio.to_thread(
                generate_dhan_token,
                api_key=api_key,
                client_id=dhan_client_id,
                password=password,
                totp_secret=totp_sec,
            )
        except Exception as e:
            logger.error(f"[Dhan] generate_dhan_token raised: {e}", exc_info=True)
            raise HTTPException(500, f"Dhan token generation error: {str(e)[:200]}")
        token = _dhan_result['token']
        if token:
            enc_token = encrypt_secret(token)
            now_ist = datetime.now(IST).isoformat()
            db_execute(
                "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? "
                "WHERE client_id=? AND broker='dhan'",
                (enc_token, now_ist, user["id"]),
            )
            from hub.event_bus import event_bus
            await event_bus.publish('BROKER_TOKEN_UPDATED',
                                    {'user_id': user["id"], 'broker': 'dhan', 'access_token': token})
            return {
                "success": True, "automated": True,
                "message": "Dhan access token generated automatically via API Key! Valid for 24 hours."
            }
        _dhan_err = _dhan_result['error'] or "Check your API Key, Dhan Client ID, and password."
        raise HTTPException(400, f"Dhan token generation failed. {_dhan_err}")

    # ── Path 2: Direct token mode — validate existing token ───────────────
    if password and totp_sec:
        try:
            token = await asyncio.to_thread(handle_dhan_login_automated, creds)
            if token:
                enc_token = encrypt_secret(token)
                now_ist = datetime.now(IST).isoformat()
                db_execute(
                    "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? "
                    "WHERE client_id=? AND broker='dhan'",
                    (enc_token, now_ist, user["id"]),
                )
                from hub.event_bus import event_bus
                await event_bus.publish('BROKER_TOKEN_UPDATED',
                                        {'user_id': user["id"], 'broker': 'dhan', 'access_token': token})
                return {"success": True, "automated": True, "message": "Dhan token validated."}
        except Exception as e:
            logger.error(f"[Dhan] Token validation failed: {e}")

    # ── Path 3: Fallback → open Dhan web login ───────────────────────────
    state_payload = f'{user["id"]}:{int(time.time())}'
    state_encrypted = _fernet.encrypt(state_payload.encode()).decode()
    url = f"https://login.dhan.co/?state={urllib.parse.quote(state_encrypted)}"
    return {"success": True, "login_url": url}


@router.get("/angelone/login-url")
async def angelone_login_url(user=Depends(get_current_user)):
    instance = db_fetchone("SELECT * FROM client_broker_instances WHERE client_id=? AND broker='angelone'", (user["id"],))
    if not instance:
        raise HTTPException(400, "AngelOne configuration missing.")

    if not instance.get("api_key_encrypted"):
        raise HTTPException(400, "Enter your AngelOne API Key and Client Code in Settings first.")

    # AngelOne requires automated (TOTP) login — there is no OAuth browser flow.
    if not instance.get("password_encrypted") or not instance.get("totp_encrypted"):
        raise HTTPException(
            400,
            "One-click connect requires your PIN/MPIN and TOTP Secret. "
            "Save them in the Credentials section above, then try again."
        )

    try:
        from utils.auth_manager_angelone import handle_angelone_login
        creds = {
            "api_key":     decrypt_secret(instance.get("api_key_encrypted") or ""),
            "client_code": decrypt_secret(instance.get("broker_user_id_encrypted") or ""),
            "pin":         decrypt_secret(instance.get("password_encrypted") or ""),
            "totp":        decrypt_secret(instance.get("totp_encrypted") or ""),
        }
        smart_api = await asyncio.to_thread(handle_angelone_login, creds)
        if smart_api and smart_api.access_token:
            enc_token = encrypt_secret(smart_api.access_token)
            now_ist = datetime.now(IST).isoformat()
            db_execute("UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='angelone'", (enc_token, now_ist, user["id"]))
            from hub.event_bus import event_bus
            await event_bus.publish('BROKER_TOKEN_UPDATED', {'user_id': user["id"], 'broker': 'angelone', 'access_token': smart_api.access_token})
            return {"success": True, "automated": True, "message": "AngelOne session generated."}
        raise HTTPException(400, "AngelOne login failed — verify your Client Code, PIN/MPIN, and TOTP secret.")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[AngelOne] Automated login failed for user {user['id']}: {e}", exc_info=True)
        raise HTTPException(400, f"AngelOne login error: {str(e)[:200]}")


# ── Zerodha Manual Token Exchange ─────────────────────────────────────────────

class ZerodhaManualToken(BaseModel):
    request_token: str

@router.post("/zerodha/exchange-token")
async def zerodha_exchange_token(body: ZerodhaManualToken, user=Depends(get_current_user)):
    request_token = body.request_token.strip()
    if not request_token:
        raise HTTPException(400, "request_token is required.")

    instance = db_fetchone(
        "SELECT * FROM client_broker_instances WHERE client_id=? AND broker='zerodha' AND status != 'removed'",
        (user["id"],)
    )
    if not instance or not instance.get("api_key_encrypted"):
        raise HTTPException(400, "Save your API Key and API Secret in Settings first.")

    api_key = decrypt_secret(instance["api_key_encrypted"])
    api_secret = decrypt_secret(instance.get("api_secret_encrypted", ""))
    if not api_key or not api_secret:
        raise HTTPException(400, "Could not decrypt credentials. Please re-enter your API Key and Secret.")

    try:
        checksum = hashlib.sha256(
            (api_key + request_token + api_secret).encode()
        ).hexdigest()

        import requests as http_requests
        resp = http_requests.post(
            "https://api.kite.trade/session/token",
            data={
                "api_key": api_key,
                "request_token": request_token,
                "checksum": checksum,
            },
        )
        resp_data = resp.json()

        if resp.status_code != 200 or resp_data.get("status") == "error":
            error_msg = resp_data.get("message", "Token exchange failed")
            raise HTTPException(400, f"Zerodha error: {error_msg}")

        access_token = resp_data.get("data", {}).get("access_token", "")
        if not access_token:
            raise HTTPException(400, "No access token in Zerodha response.")

        enc_token = encrypt_secret(access_token)
        now_ist = datetime.now(IST).isoformat()
        db_execute(
            "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='zerodha'",
            (enc_token, now_ist, user["id"])
        )
        return {"success": True, "message": "Zerodha access token generated successfully!", "token_updated_at": now_ist}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Token exchange failed: {str(e)[:200]}")


# ── Bot Control ───────────────────────────────────────────────────────────────

class BotStartRequest(BaseModel):
    broker: Optional[str] = None

class TradingToggleRequest(BaseModel):
    enabled: bool
    broker: Optional[str] = None

class BrokerQuickUpdateRequest(BaseModel):
    broker: str
    trading_mode: str = "paper"
    quantity: int = 25

@router.post("/bot/toggle-trading")
async def toggle_trading(body: TradingToggleRequest, user=Depends(get_current_user)):
    instance = _get_active_instance(user["id"])
    if not instance or instance["status"] != "running":
        raise HTTPException(400, "Broker connection must be active first.")

    # Per-broker file (new) + legacy global file (backward compat)
    broker = body.broker or instance.get("broker", "")
    _write_broker_trading_file(user["id"], broker, body.enabled)

    msg = "Trading enabled." if body.enabled else "Trading disabled. Active trades will be closed."
    logger.info(f"[Client] User {user['id']} toggled trading to {body.enabled}")
    action = "trading_start" if body.enabled else "trading_stop"
    _audit_client(user["id"], action, {"broker": broker})
    return {"success": True, "message": msg}


def _write_broker_trading_file(client_id: int, broker: str, enabled: bool):
    """Write per-broker and legacy global toggle files, and update DB."""
    import json as _json
    payload = {"enabled": enabled, "updated_at": time.time()}
    # Per-broker file (read by subprocess)
    if broker:
        Path(f'config/trading_enabled_{client_id}_{broker}.json').write_text(
            _json.dumps(payload)
        )
    # Legacy global file (backward compat for old subprocesses)
    Path(f'config/trading_enabled_{client_id}.json').write_text(
        _json.dumps(payload)
    )
    # Persist in DB
    if broker:
        db_execute(
            "UPDATE client_broker_instances SET trading_active=? WHERE client_id=? AND broker=?",
            (1 if enabled else 0, client_id, broker)
        )


@router.post("/bot/toggle-broker-trading")
async def toggle_broker_trading(body: TradingToggleRequest, user=Depends(get_current_user)):
    """Per-broker trading toggle — enables/disables order placement for one broker only."""
    if not body.broker:
        raise HTTPException(400, "broker field is required.")
    instance = db_fetchone(
        "SELECT id, status FROM client_broker_instances WHERE client_id=? AND broker=?",
        (user["id"], body.broker)
    )
    if not instance:
        raise HTTPException(404, "Broker not configured.")

    _write_broker_trading_file(user["id"], body.broker, body.enabled)

    msg = f"{body.broker.capitalize()} trading {'enabled' if body.enabled else 'disabled'}."
    logger.info(f"[Client] User {user['id']} toggled {body.broker} trading to {body.enabled}")
    action = "trading_start" if body.enabled else "trading_stop"
    _audit_client(user["id"], action, {"broker": body.broker})
    return {"success": True, "message": msg}


@router.post("/broker/quick-update")
async def broker_quick_update(body: BrokerQuickUpdateRequest, user=Depends(get_current_user)):
    """Inline update of trading_mode and quantity for a broker — no credentials required."""
    _VALID_BROKERS = ("zerodha", "dhan", "angelone", "upstox", "fyers", "aliceblue", "groww")
    if body.broker not in _VALID_BROKERS:
        raise HTTPException(400, f"Broker must be one of: {', '.join(_VALID_BROKERS)}.")
    if body.trading_mode not in ("paper", "live"):
        raise HTTPException(400, "trading_mode must be 'paper' or 'live'")
    if body.quantity < 1:
        raise HTTPException(400, "quantity must be >= 1")
    # Verify broker ownership before any write
    _inst = db_fetchone(
        "SELECT id FROM client_broker_instances WHERE client_id=? AND broker=?",
        (user["id"], body.broker)
    )
    if not _inst:
        raise HTTPException(404, "Broker not configured for this account.")

    db_execute(
        "UPDATE client_broker_instances SET trading_mode=?, quantity=? WHERE client_id=? AND broker=?",
        (body.trading_mode, body.quantity, user["id"], body.broker)
    )
    # Write runtime config file so running subprocess picks up changes within ~5 s
    cfg_file = Path(f"config/broker_config_{user['id']}_{body.broker}.json")
    with open(cfg_file, "w") as _f:
        json.dump({
            "trading_mode": body.trading_mode,
            "quantity": body.quantity,
            "updated_at": time.time()
        }, _f)
    _audit_client(user["id"], "broker_quick_update", {
        "broker": body.broker, "mode": body.trading_mode, "qty": body.quantity
    })
    return {"success": True, "message": f"{body.broker.capitalize()} updated: {body.trading_mode.upper()}, Qty {body.quantity}"}

@router.post("/bot/square-off-all")
async def square_off_all_positions(user=Depends(get_current_user)):
    instance = _get_active_instance(user["id"])
    if not instance or instance["status"] != "running":
        raise HTTPException(400, "Broker connection must be active first.")

    # We use a signal file to communicate with the subprocess
    # A dedicated signal file for square off
    sq_file = Path(f'config/square_off_signal_{user["id"]}.json')
    with open(sq_file, 'w') as f:
        json.dump({"triggered_at": time.time()}, f)

    logger.info(f"[Client] User {user['id']} requested SQUARE OFF ALL.")
    return {"success": True, "message": "Square off signal sent to bot."}


@router.post("/bot/square-off-leg")
async def square_off_one_leg(request: Request, user=Depends(get_current_user)):
    instance = _get_active_instance(user["id"])
    if not instance or instance["status"] != "running":
        raise HTTPException(400, "Broker connection must be active first.")
    body = await request.json()
    side = str(body.get("side", "")).upper()
    if side not in ("CE", "PE"):
        raise HTTPException(400, "side must be CE or PE.")
    leg_file = Path(f'config/square_off_leg_{user["id"]}_{side}.json')
    with open(leg_file, 'w') as f:
        json.dump({"side": side, "triggered_at": time.time()}, f)
    logger.info(f"[Client] User {user['id']} requested SQUARE OFF LEG {side}.")
    return {"success": True, "message": f"{side} square off signal sent to bot."}


async def _start_one_broker_instance(instance: dict, user: dict, permitted_broker: str = None) -> dict:
    """
    Attempt to start a single configured broker instance.

    Returns a dict:
      {"broker": str, "status": "started"|"skipped"|"already_running"|"failed", "message": str}

    'skipped'  — ineligible (stale token, kill-switch, plan restriction)
    'started'  — subprocess launched successfully
    'already_running' — was already running, no action taken
    'failed'   — subprocess failed to launch
    """
    broker_name = instance["broker"]

    # Plan expiry: only the primary (lowest-id) broker is permitted
    if permitted_broker and broker_name != permitted_broker:
        return {"broker": broker_name, "status": "skipped",
                "message": f"Plan expired — only {permitted_broker} allowed"}

    # Credentials check
    if not instance.get("api_key_encrypted"):
        return {"broker": broker_name, "status": "skipped", "message": "Credentials missing"}

    # Kill-switch check
    locked_until = instance.get("trading_locked_until")
    if locked_until:
        try:
            locked_dt = datetime.fromisoformat(locked_until)
            if locked_dt.tzinfo is None:
                locked_dt = locked_dt.replace(tzinfo=IST)
            if datetime.now(IST) < locked_dt:
                return {"broker": broker_name, "status": "skipped",
                        "message": f"Kill-switch active until {locked_until[:16].replace('T', ' ')} IST"}
        except Exception:
            pass

    # Token freshness / headless login
    has_auto_login = instance.get("password_encrypted") and instance.get("totp_encrypted")
    _token_ts = instance.get("token_updated_at", "")
    if broker_name == "dhan":
        token_needs_refresh = not _is_dhan_token_fresh(_token_ts)
    else:
        token_needs_refresh = not _is_token_fresh(_token_ts)

    if has_auto_login and token_needs_refresh:
        try:
            logger.info(f"[Bot Start] Headless login for {broker_name} (User {user['id']})...")
            creds = {
                "api_key": decrypt_secret(instance["api_key_encrypted"]),
                "api_secret": decrypt_secret(instance.get("api_secret_encrypted", "")),
                "broker_user_id": decrypt_secret(instance.get("broker_user_id_encrypted", "")),
                "password": decrypt_secret(instance["password_encrypted"]),
                "totp": decrypt_secret(instance["totp_encrypted"]),
            }
            token = None
            if broker_name == "zerodha":
                from utils.auth_manager_zerodha import handle_zerodha_login_automated
                token = handle_zerodha_login_automated(creds)
            elif broker_name == "dhan":
                from utils.auth_manager_dhan import handle_dhan_login_automated
                token = handle_dhan_login_automated(creds)
            elif broker_name == "angelone":
                from utils.auth_manager_angelone import handle_angelone_login
                creds["client_code"] = creds["broker_user_id"]
                creds["pin"] = creds["password"]
                smart_api = handle_angelone_login(creds)
                if smart_api:
                    token = smart_api.access_token
            elif broker_name == "upstox":
                from utils.auth_manager_upstox import handle_upstox_login_automated
                token = handle_upstox_login_automated(creds)
            elif broker_name == "aliceblue":
                from utils.auth_manager_alice import handle_alice_login_automated
                token = handle_alice_login_automated(creds)
            elif broker_name == "groww":
                from utils.auth_manager_groww import handle_groww_login_automated
                token = handle_groww_login_automated(creds)

            if token:
                enc_token = encrypt_secret(token)
                now_ist = datetime.now(IST).isoformat()
                db_execute(
                    "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE id=?",
                    (enc_token, now_ist, instance["id"]),
                )
                instance["access_token_encrypted"] = enc_token
                logger.info(f"[Bot Start] Headless login SUCCESS for {broker_name}")
            else:
                logger.warning(f"[Bot Start] Headless login failed for {broker_name}")
        except Exception as exc:
            logger.error(f"[Bot Start] Headless login error for {broker_name}: {exc}")
    elif has_auto_login:
        logger.info(
            f"[Bot Start] Skipping headless login for {broker_name} (User {user['id']}) "
            f"— token fresh (updated_at={_token_ts[:19] if _token_ts else 'unknown'})"
        )

    # Token presence / freshness validation
    if not instance.get("access_token_encrypted"):
        return {"broker": broker_name, "status": "skipped",
                "message": "No access token — save Password/TOTP or connect manually"}

    if broker_name in ("zerodha", "upstox", "angelone", "fyers", "aliceblue"):
        if not _is_token_fresh(instance.get("token_updated_at")):
            return {"broker": broker_name, "status": "skipped",
                    "message": f"{broker_name.capitalize()} session expired — reconnect in Settings"}
    elif broker_name == "dhan":
        _dhan_api_secret = decrypt_secret(instance["api_secret_encrypted"]) if instance.get("api_secret_encrypted") else ""
        from utils.auth_manager_dhan import is_dhan_api_key_mode as _is_akm_h
        _dhan_api_mode = _is_akm_h({"api_secret": _dhan_api_secret})
        if not _is_dhan_token_fresh(instance.get("token_updated_at"), api_key_mode=_dhan_api_mode):
            return {"broker": broker_name, "status": "skipped",
                    "message": "Dhan token expired — click Connect Now or reconnect in Settings"}

    # Already running?
    if instance["status"] == "running":
        live = instance_manager.get_instance_status(instance["id"])
        if live.get("running"):
            return {"broker": broker_name, "status": "already_running", "message": "Already running"}
        # DB says running but process is dead — allow restart
        db_execute("UPDATE client_broker_instances SET status='idle', bot_pid=NULL WHERE id=?", (instance["id"],))

    # Launch subprocess
    ok, msg, pid = instance_manager.start_instance(
        instance_id=instance["id"],
        client_id=user["id"],
        username=user["username"],
        broker=instance["broker"],
        instrument=instance["instrument"],
        quantity=instance["quantity"],
        strategy_version=instance["strategy_version"],
        trading_mode=instance["trading_mode"],
        api_key=decrypt_secret(instance["api_key_encrypted"]),
        access_token=decrypt_secret(instance["access_token_encrypted"]) if instance.get("access_token_encrypted") else "",
    )
    if ok:
        db_execute("UPDATE client_broker_instances SET status='running', bot_pid=? WHERE id=?", (pid, instance["id"]))
        _audit_client(user["id"], "bot_activate", {"broker": broker_name, "mode": instance.get("trading_mode")})
        return {"broker": broker_name, "status": "started", "message": msg}
    return {"broker": broker_name, "status": "failed", "message": msg}


@router.post("/bot/start")
async def start_bot(body: BotStartRequest = BotStartRequest(), user=Depends(get_current_user)):
    _valid_brokers = ("zerodha", "dhan", "angelone", "upstox", "fyers", "aliceblue", "groww")
    if body.broker and body.broker not in _valid_brokers and body.broker != "all":
        raise HTTPException(400, f"Unknown broker '{body.broker}'. Valid brokers: {', '.join(_valid_brokers)} or 'all'")

    # ── Plan expiry enforcement ───────────────────────────────────────────
    _plan_expiry_warning = None
    _plan_expired = False
    _permitted_broker = None  # None = all brokers allowed
    from datetime import datetime, timezone as _tz_start
    _exp_str_s = user.get("plan_expiry_date")
    if _exp_str_s:
        try:
            _exp_s = datetime.fromisoformat(_exp_str_s)
            if _exp_s.tzinfo is None:
                _exp_s = _exp_s.replace(tzinfo=_tz_start.utc)
            if datetime.now(_tz_start.utc) > _exp_s:
                _plan_expired = True
        except Exception:
            pass

    if _plan_expired:
        _all_inst_exp = db_fetchall(
            "SELECT id, broker FROM client_broker_instances "
            "WHERE client_id=? AND status != 'removed' ORDER BY id ASC",
            (user["id"],)
        )
        if _all_inst_exp:
            _permitted_broker = _all_inst_exp[0]["broker"]
        _plan_expiry_warning = (
            f"Your subscription expired on {_exp_str_s[:10]}. "
            "You are limited to 1 broker slot. Contact admin to renew."
        )
    # ─────────────────────────────────────────────────────────────────────

    requested_broker = body.broker if body.broker and body.broker != "all" else None

    # ── Single-broker path (explicit broker name provided) ────────────────
    if requested_broker:
        # Expired plan: block non-primary broker requests explicitly
        if _plan_expired and _permitted_broker and requested_broker != _permitted_broker:
            raise HTTPException(
                403,
                f"Your subscription expired on {_exp_str_s[:10]}. "
                f"Only your primary broker ({_permitted_broker}) can be started on an expired plan. "
                "Contact admin to renew or remove extra broker configurations."
            )

        instance = _get_active_instance(user["id"], broker=requested_broker)
        if not instance:
            raise HTTPException(400, "No broker configured. Please set up your broker first.")

        # Check for pending broker change request
        pending_change = db_fetchone(
            "SELECT id FROM broker_change_requests WHERE client_id=? AND status='pending'",
            (user["id"],)
        )
        if pending_change:
            raise HTTPException(400, "You have a pending broker change request. Please wait for admin approval before starting the bot.")

        result = await _start_one_broker_instance(instance, user, permitted_broker=_permitted_broker)
        if result["status"] == "skipped":
            raise HTTPException(400, result["message"])
        if result["status"] == "failed":
            raise HTTPException(500, result["message"])
        if result["status"] == "already_running":
            response = {"success": False, "message": "Bot is already running."}
        else:
            response = {"success": True, "message": result["message"]}
        if _plan_expiry_warning:
            response["plan_warning"] = _plan_expiry_warning
        return response

    # ── Start-all path (no broker specified — start every configured broker) ─
    all_instances = db_fetchall(
        "SELECT * FROM client_broker_instances WHERE client_id=? AND status != 'removed' ORDER BY id ASC",
        (user["id"],)
    )
    if not all_instances:
        raise HTTPException(400, "No broker configured. Please set up your broker first.")

    # Check for pending broker change request (blocks all start attempts)
    pending_change = db_fetchone(
        "SELECT id FROM broker_change_requests WHERE client_id=? AND status='pending'",
        (user["id"],)
    )
    if pending_change:
        raise HTTPException(400, "You have a pending broker change request. Please wait for admin approval before starting the bot.")

    results = []
    for inst in all_instances:
        r = await _start_one_broker_instance(dict(inst), user, permitted_broker=_permitted_broker)
        results.append(r)

    started = [r["broker"] for r in results if r["status"] == "started"]
    already  = [r["broker"] for r in results if r["status"] == "already_running"]
    skipped  = [r for r in results if r["status"] == "skipped"]
    failed   = [r for r in results if r["status"] == "failed"]

    if not started and not already:
        # Nothing launched — return summary as 200 so the frontend toast
        # shows the skip/fail reasons rather than a generic error.
        first_reason = (skipped + failed)[0]["message"] if (skipped + failed) else "No brokers could be started."
        parts = []
        if skipped:
            skip_detail = "; ".join(f"{r['broker'].capitalize()} ({r['message']})" for r in skipped)
            parts.append(f"{len(skipped)} skipped — {skip_detail}")
        if failed:
            fail_detail = "; ".join(f"{r['broker'].capitalize()}: {r['message']}" for r in failed)
            parts.append(f"{len(failed)} failed — {fail_detail}")
        summary_msg = ". ".join(parts) or first_reason
        return {
            "success": False,
            "message": summary_msg,
            "started": [],
            "already_running": [],
            "skipped": [r["broker"] for r in skipped],
            "failed": [r["broker"] for r in failed],
            "details": results,
        }

    parts = []
    if started:
        parts.append(f"{len(started)} started: {', '.join(b.capitalize() for b in started)}")
    if already:
        parts.append(f"{len(already)} already running: {', '.join(b.capitalize() for b in already)}")
    if skipped:
        skip_detail = "; ".join(f"{r['broker'].capitalize()} ({r['message']})" for r in skipped)
        parts.append(f"{len(skipped)} skipped — {skip_detail}")
    if failed:
        fail_detail = "; ".join(f"{r['broker'].capitalize()}: {r['message']}" for r in failed)
        parts.append(f"{len(failed)} failed — {fail_detail}")

    summary = ". ".join(parts)
    response = {
        "success": True,
        "message": summary,
        "started": started,
        "already_running": already,
        "skipped": [r["broker"] for r in skipped],
        "failed": [r["broker"] for r in failed],
        "details": results,
    }
    if _plan_expiry_warning:
        response["plan_warning"] = _plan_expiry_warning
    return response


@router.post("/bot/stop")
async def stop_bot(user=Depends(get_current_user)):
    instances = db_fetchall(
        "SELECT id, broker, status FROM client_broker_instances WHERE client_id=? AND status != 'removed'",
        (user["id"],)
    )
    if not instances:
        raise HTTPException(400, "No broker configured.")

    actually_stopped = []
    already_idle = []
    for inst in instances:
        live = instance_manager.get_instance_status(inst["id"])
        was_running = live.get("running") or inst.get("status") == "running"
        instance_manager.stop_instance(inst["id"])
        db_execute("UPDATE client_broker_instances SET status='idle', bot_pid=NULL WHERE id=?", (inst["id"],))
        if was_running:
            actually_stopped.append(inst["broker"])
        else:
            already_idle.append(inst["broker"])

    _audit_client(user["id"], "bot_deactivate", {"brokers": actually_stopped})
    parts = []
    if actually_stopped:
        parts.append(f"Stopped: {', '.join(b.capitalize() for b in actually_stopped)}")
    if already_idle:
        parts.append(f"Already idle: {', '.join(b.capitalize() for b in already_idle)}")
    msg = ". ".join(parts) if parts else "No configured broker instances found."
    return {"success": True, "message": msg, "stopped": actually_stopped, "already_idle": already_idle}


@router.post("/bot/restart")
async def restart_bot(body: BotStartRequest = BotStartRequest(), user=Depends(get_current_user)):
    await stop_bot(user=user)
    return await start_bot(body=body, user=user)


class BotStopOneRequest(BaseModel):
    broker: str


@router.post("/bot/stop-one")
async def stop_one_broker_bot(body: BotStopOneRequest, user=Depends(get_current_user)):
    """Stop a single named broker instance without affecting others."""
    valid_brokers = ("zerodha", "dhan", "angelone", "upstox", "fyers", "aliceblue", "groww")
    if body.broker not in valid_brokers:
        raise HTTPException(400, f"Unknown broker '{body.broker}'.")

    instance = db_fetchone(
        "SELECT id, broker FROM client_broker_instances WHERE client_id=? AND broker=? AND status != 'removed'",
        (user["id"], body.broker)
    )
    if not instance:
        raise HTTPException(400, f"{body.broker.capitalize()} is not configured.")

    instance_manager.stop_instance(instance["id"])
    db_execute("UPDATE client_broker_instances SET status='idle', bot_pid=NULL WHERE id=?", (instance["id"],))
    _audit_client(user["id"], "bot_deactivate", {"broker": body.broker})
    return {"success": True, "message": f"{body.broker.capitalize()} bot stopped."}


@router.get("/upstox/login-url")
async def upstox_login_url(request: Request, user=Depends(get_current_user)):
    instance = db_fetchone("SELECT * FROM client_broker_instances WHERE client_id=? AND broker='upstox'", (user["id"],))
    if not instance or not instance.get("api_key_encrypted"):
        raise HTTPException(400, "Enter your Upstox API Key in Settings first.")

    # 1. Attempt Automated Login
    if instance.get("password_encrypted") and instance.get("totp_encrypted"):
        try:
            from utils.auth_manager_upstox import handle_upstox_login_automated
            creds = {
                "api_key":        decrypt_secret(instance.get("api_key_encrypted") or ""),
                "api_secret":     decrypt_secret(instance.get("api_secret_encrypted") or ""),
                "broker_user_id": decrypt_secret(instance.get("broker_user_id_encrypted") or ""),
                "password":       decrypt_secret(instance.get("password_encrypted") or ""),
                "totp":           decrypt_secret(instance.get("totp_encrypted") or ""),
            }
            token = await asyncio.to_thread(handle_upstox_login_automated, creds)
            if token:
                enc_token = encrypt_secret(token)
                now_ist = datetime.now(IST).isoformat()
                db_execute("UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='upstox'", (enc_token, now_ist, user["id"]))
                from hub.event_bus import event_bus
                await event_bus.publish('BROKER_TOKEN_UPDATED', {'user_id': user["id"], 'broker': 'upstox', 'access_token': token})
                return {"success": True, "automated": True, "message": "Upstox background login successful."}
        except Exception as e:
            logger.error(f"[Upstox] Automated login failed for user {user['id']}: {e}", exc_info=True)

    # 2. Fallback to Browser OAuth
    api_key = decrypt_secret(instance.get("api_key_encrypted") or "")
    state_payload = f'{user["id"]}:{int(time.time())}'
    state_encrypted = _fernet.encrypt(state_payload.encode()).decode()
    raw_host = request.headers.get('host') or str(request.base_url).split('/')[2]
    proto = request.headers.get('x-forwarded-proto', 'http')
    if 'localhost' not in raw_host and '127.0.0.1' not in raw_host and proto != 'http': proto = 'https'
    actual_redirect = f"{proto}://{raw_host}/auth/upstox/callback"
    auth_dialog = "https://api.upstox.com/v2/login/authorization/dialog"
    url = f"{auth_dialog}?response_type=code&client_id={api_key}&redirect_uri={urllib.parse.quote(actual_redirect)}&state={urllib.parse.quote(state_encrypted)}"
    return {"success": True, "login_url": url}


@router.get("/fyers/login-url")
async def fyers_login_url(user=Depends(get_current_user)):
    """
    Returns the Fyers OAuth login URL.
    The user opens it in a browser, logs in, and pastes the redirect URL back.
    """
    instance = db_fetchone(
        "SELECT api_key_encrypted, api_secret_encrypted FROM client_broker_instances WHERE client_id=? AND broker='fyers' AND status != 'removed'",
        (user["id"],)
    )
    if not instance:
        raise HTTPException(400, "No Fyers broker configured.")
    if not instance.get("api_key_encrypted"):
        raise HTTPException(400, "Fyers App ID not configured.")

    from utils.auth_manager_fyers import generate_fyers_auth_url
    app_id = decrypt_secret(instance["api_key_encrypted"])
    secret_id = decrypt_secret(instance.get("api_secret_encrypted") or "")
    url = generate_fyers_auth_url(app_id=app_id, secret_id=secret_id)
    if not url:
        raise HTTPException(500, "Could not generate Fyers login URL.")
    return {"success": True, "login_url": url}


@router.post("/fyers/exchange-token")
async def fyers_exchange_token(body: dict, user=Depends(get_current_user)):
    """
    Exchanges the Fyers auth code (extracted from the redirect URL) for an access token.
    Body: { "auth_code": "..." }
    """
    from utils.auth_manager_fyers import exchange_fyers_auth_code
    auth_code = (body.get("auth_code") or "").strip()
    if not auth_code:
        raise HTTPException(400, "auth_code is required.")

    instance = db_fetchone(
        "SELECT id, api_key_encrypted, api_secret_encrypted FROM client_broker_instances WHERE client_id=? AND broker='fyers' AND status != 'removed'",
        (user["id"],)
    )
    if not instance:
        raise HTTPException(400, "No Fyers broker configured.")

    app_id = decrypt_secret(instance["api_key_encrypted"])
    secret_id = decrypt_secret(instance.get("api_secret_encrypted") or "")
    result = exchange_fyers_auth_code(app_id=app_id, secret_id=secret_id, auth_code=auth_code)
    if result["error"]:
        raise HTTPException(400, f"Fyers token exchange failed: {result['error']}")

    enc_token = encrypt_secret(result["token"])
    now_ist = datetime.now(IST).isoformat()
    db_execute(
        "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE id=?",
        (enc_token, now_ist, instance["id"])
    )
    _audit(user_id=user["id"], action="BROKER_TOKEN_REFRESH", detail="Fyers access token updated via OAuth exchange.")
    return {"success": True, "message": "Fyers connected successfully."}


@router.get("/aliceblue/login-url")
async def aliceblue_login_url(user=Depends(get_current_user)):
    """
    Alice Blue uses background (headless) login — no OAuth redirect URL.
    Returns instructions to trigger One-Click Connect instead.
    """
    instance = db_fetchone(
        "SELECT id, api_key_encrypted, broker_user_id_encrypted, password_encrypted, totp_encrypted FROM client_broker_instances WHERE client_id=? AND broker='aliceblue' AND status != 'removed'",
        (user["id"],)
    )
    if not instance:
        raise HTTPException(400, "No Alice Blue broker configured.")

    from utils.auth_manager_alice import handle_alice_login_automated
    creds = {
        "broker_user_id": decrypt_secret(instance.get("broker_user_id_encrypted") or ""),
        "api_key": decrypt_secret(instance["api_key_encrypted"]) if instance.get("api_key_encrypted") else "",
        "password": decrypt_secret(instance.get("password_encrypted") or ""),
        "totp": decrypt_secret(instance.get("totp_encrypted") or ""),
    }
    try:
        token = await asyncio.to_thread(handle_alice_login_automated, creds)
    except Exception as e:
        logger.error(f"[AliceBlue] handle_alice_login_automated raised: {e}", exc_info=True)
        raise HTTPException(500, f"Alice Blue login error: {str(e)[:200]}")
    if not token:
        raise HTTPException(400, "Alice Blue automated login failed. Please verify your Client ID, API Key, PIN, and TOTP seed.")

    enc_token = encrypt_secret(token)
    now_ist = datetime.now(IST).isoformat()
    db_execute(
        "UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE id=?",
        (enc_token, now_ist, instance["id"])
    )
    _audit(user_id=user["id"], action="BROKER_TOKEN_REFRESH", detail="Alice Blue session refreshed via background login.")
    return {"success": True, "automated": True, "message": "Alice Blue connected successfully."}


@router.get("/groww/login-url")
async def groww_login_url(user=Depends(get_current_user)):
    """
    Groww requires a manual Bearer access token from the Groww developer portal.
    This endpoint validates the existing stored token.
    """
    instance = db_fetchone(
        "SELECT id, broker_user_id_encrypted, access_token_encrypted, token_updated_at FROM client_broker_instances WHERE client_id=? AND broker='groww' AND status != 'removed'",
        (user["id"],)
    )
    if not instance:
        raise HTTPException(400, "No Groww broker configured.")
    if not instance.get("access_token_encrypted"):
        raise HTTPException(400, "No Groww access token stored. Please paste your Bearer token in Settings.")

    from utils.auth_manager_groww import handle_groww_login
    creds = {
        "broker_user_id": decrypt_secret(instance.get("broker_user_id_encrypted") or ""),
        "access_token": decrypt_secret(instance["access_token_encrypted"]),
    }
    try:
        token = await asyncio.to_thread(handle_groww_login, creds)
    except Exception as e:
        logger.error(f"[Groww] handle_groww_login raised: {e}", exc_info=True)
        raise HTTPException(500, f"Groww login error: {str(e)[:200]}")
    if not token:
        raise HTTPException(400, "Groww token is invalid or expired. Please update your access token in Settings.")

    now_ist = datetime.now(IST).isoformat()
    db_execute(
        "UPDATE client_broker_instances SET token_updated_at=? WHERE id=?",
        (now_ist, instance["id"])
    )
    _audit(user_id=user["id"], action="BROKER_TOKEN_REFRESH", detail="Groww token validated.")
    return {"success": True, "automated": True, "message": "Groww token validated successfully."}


@router.get("/bot/status")
async def bot_status(instrument: Optional[str] = None, user=Depends(get_current_user)):
    instance = _get_active_instance(user["id"])
    if not instance:
        return {"configured": False}

    live_status = instance_manager.get_instance_status(instance["id"])

    # If instrument is provided, filter trade history
    if instrument:
        trades = db_fetchall("""
            SELECT trade_type, direction, strike, entry_price, exit_price,
                   pnl_pts, pnl_rs, exit_reason, closed_at, entry_index_price,
                   entry_indicators, exit_indicators
            FROM trade_history WHERE instance_id=? AND instrument=? ORDER BY closed_at DESC LIMIT 50
        """, (instance["id"], instrument))
    else:
        trades = db_fetchall("""
            SELECT trade_type, direction, strike, entry_price, exit_price,
                   pnl_pts, pnl_rs, exit_reason, closed_at, entry_index_price,
                   entry_indicators, exit_indicators
            FROM trade_history WHERE instance_id=? ORDER BY closed_at DESC LIMIT 50
        """, (instance["id"],))

    bot_data = {}

    # Try instrument-specific status file first
    if instrument:
        status_file = Path(f'config/bot_status_client_{user["id"]}_{instrument}.json')
    else:
        status_file = Path(f'config/bot_status_client_{user["id"]}.json')

    if not status_file.exists() and instrument:
        # Fallback to main file if specific instrument file not found
        status_file = Path(f'config/bot_status_client_{user["id"]}.json')

    if status_file.exists():
        try:
            with open(status_file, 'r') as f:
                bot_data = json.load(f)
            heartbeat = float(bot_data.get('heartbeat') or 0)
            age = time.time() - heartbeat
            bot_data['stale'] = age > 30
            bot_data['stale_seconds'] = round(age)

            # Multi-tenant logic: If we have a fresh heartbeat in the file,
            # consider the bot running even if it's not in this web worker's memory.
            if not live_status["running"] and heartbeat > 0 and age < 30:
                live_status["running"] = True
                live_status["pid"] = bot_data.get("pid")
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            bot_data = {}

    inst_dict = dict(instance)
    safe_keys = ["id", "broker", "status", "trading_mode", "instrument", "quantity", "strategy_version", "last_heartbeat", "token_updated_at"]
    inst_safe = {k: inst_dict.get(k) for k in safe_keys}

    # Per-broker running status for all configured instances
    all_broker_instances = db_fetchall(
        "SELECT id, broker, status, trading_active, trading_mode, quantity FROM client_broker_instances WHERE client_id=? AND status != 'removed' ORDER BY id ASC",
        (user["id"],)
    )
    brokers_status = []
    for bi in all_broker_instances:
        b_live = instance_manager.get_instance_status(bi["id"])
        running = b_live["running"] or bi["status"] == "running"
        brokers_status.append({
            "broker": bi["broker"],
            "instance_id": bi["id"],
            "running": running,
            "pid": b_live.get("pid"),
            "trading_active": bool(bi["trading_active"]),
            "trading_mode": bi["trading_mode"] or "paper",
            "quantity": bi["quantity"] or 25,
        })

    return {
        "configured": True,
        "instance": inst_safe,
        "live": live_status,
        "trade_history": trades,
        "bot_data": bot_data,
        "brokers": brokers_status,
    }


# ── Broker Change Requests ────────────────────────────────────────────────

class BrokerChangeRequest(BaseModel):
    current_broker: str
    requested_broker: str
    reason: Optional[str] = ""


@router.post("/broker-change-request")
async def submit_broker_change_request(body: BrokerChangeRequest, user=Depends(get_current_user)):
    if body.current_broker not in ("zerodha", "dhan") or body.requested_broker not in ("zerodha", "dhan"):
        raise HTTPException(400, "Invalid broker name.")
    if body.current_broker == body.requested_broker:
        raise HTTPException(400, "Current and requested broker cannot be the same.")

    current_instance = db_fetchone(
        "SELECT id FROM client_broker_instances WHERE client_id=? AND broker=? AND status != 'removed'",
        (user["id"], body.current_broker)
    )
    if not current_instance:
        raise HTTPException(400, f"You don't have {body.current_broker} configured as a broker.")

    requested_instance = db_fetchone(
        "SELECT id FROM client_broker_instances WHERE client_id=? AND broker=? AND status != 'removed'",
        (user["id"], body.requested_broker)
    )
    if requested_instance:
        raise HTTPException(400, f"You already have {body.requested_broker} configured. No change request needed.")

    existing = db_fetchone(
        "SELECT id FROM broker_change_requests WHERE client_id=? AND status='pending'",
        (user["id"],)
    )
    if existing:
        raise HTTPException(400, "You already have a pending broker change request. Please wait for admin approval.")

    running = db_fetchone(
        "SELECT id FROM client_broker_instances WHERE client_id=? AND status='running'",
        (user["id"],)
    )
    if running:
        raise HTTPException(400, "Please stop your bot before requesting a broker change.")

    db_execute(
        "INSERT INTO broker_change_requests (client_id, current_broker, requested_broker, reason) VALUES (?,?,?,?)",
        (user["id"], body.current_broker, body.requested_broker, body.reason or "")
    )
    return {"success": True, "message": "Broker change request submitted. Admin will review it shortly."}


@router.get("/broker-change-request")
async def get_broker_change_request(user=Depends(get_current_user)):
    pending = db_fetchone(
        "SELECT id, current_broker, requested_broker, reason, status, created_at FROM broker_change_requests WHERE client_id=? AND status='pending' LIMIT 1",
        (user["id"],)
    )
    recent = db_fetchall(
        "SELECT id, current_broker, requested_broker, reason, status, created_at, resolved_at FROM broker_change_requests WHERE client_id=? ORDER BY created_at DESC LIMIT 5",
        (user["id"],)
    )
    return {"pending": pending, "recent": recent}


# ── Backtest Engine ──────────────────────────────────────────────────────────

import uuid as _uuid

class BacktestStartRequest(BaseModel):
    instrument: str
    start_date: str
    end_date: str
    quantity: int = 1


_current_bt_job_id: Optional[str] = None


def _compute_backtest_summary(trades: list) -> dict:
    """Compute aggregate metrics from a list of trade-leg dicts."""
    if not trades:
        return None
    total_pts = sum(float(t.get('pnl_pts') or 0) for t in trades)
    total_rs  = sum(float(t.get('pnl_rs')  or 0) for t in trades)
    wins      = [t for t in trades if float(t.get('pnl_pts') or 0) > 0]
    losses    = [t for t in trades if float(t.get('pnl_pts') or 0) < 0]
    n = len(trades)
    avg_pts = total_pts / n if n else 0

    # Max drawdown: worst peak-to-trough cumulative PnL (trades list is newest-first)
    running = 0.0
    peak    = 0.0
    max_dd  = 0.0
    for t in reversed(trades):
        running += float(t.get('pnl_pts') or 0)
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades":      n,
        "total_pnl_pts":     round(total_pts, 2),
        "total_pnl_rs":      round(total_rs,  2),
        "wins":              len(wins),
        "losses":            len(losses),
        "win_rate":          round(len(wins) / n * 100, 1) if n else 0,
        "avg_pnl_pts":       round(avg_pts, 2),
        "max_drawdown_pts":  round(max_dd, 2),
    }



@router.get("/backtest/available-dates")
async def backtest_available_dates(instrument: str = "NIFTY", user=Depends(get_current_user)):
    """Return sorted list of dates that have recorded CSV data for the given instrument."""
    import re as _re
    data_dir = Path("backtest_data")
    if not data_dir.exists():
        return {"dates": [], "instrument": instrument.upper()}

    pattern = _re.compile(
        rf"^market_data_{_re.escape(instrument.upper())}_(\d{{4}}-\d{{2}}-\d{{2}})\.csv$",
        _re.IGNORECASE,
    )
    dates = []
    for f in data_dir.iterdir():
        m = pattern.match(f.name)
        if m:
            dates.append(m.group(1))
    dates.sort()
    return {"dates": dates, "instrument": instrument.upper()}


@router.post("/backtest/run")
async def run_client_backtest(body: BacktestStartRequest, user=Depends(get_current_user)):
    """Start a backtest run; returns a job_id for polling via /backtest/status/{job_id}."""
    global _current_bt_job_id
    from web.admin_api import start_backtest, BacktestStartRequest as AdminBSR
    import web.admin_api as _admin_mod

    _proc = _admin_mod._backtest_proc_handle
    if _proc is not None and _proc.poll() is None:
        return {
            "success": False,
            "message": "A backtest is already running.",
            "job_id": _current_bt_job_id,
        }

    date_str = body.start_date if body.start_date == body.end_date \
               else f"{body.start_date} to {body.end_date}"
    result = await start_backtest(
        AdminBSR(instrument=body.instrument, date=date_str, quantity=body.quantity), user
    )
    if result.get("success", True) is not False:
        _current_bt_job_id = _uuid.uuid4().hex[:8]
        result["job_id"] = _current_bt_job_id
    return result


@router.get("/backtest/status/{job_id}")
async def get_client_backtest_status_by_job(job_id: str, user=Depends(get_current_user)):
    """Poll status for a specific backtest job_id."""
    from web.admin_api import get_backtest_status
    data = await get_backtest_status(user)
    data["job_id"] = job_id
    trades = data.get("trades", [])
    if not data.get("running"):
        if trades:
            data["summary"] = _compute_backtest_summary(trades)
        data["no_trades"] = len(trades) == 0
    return data


@router.post("/backtest/stop")
async def stop_client_backtest(user=Depends(get_current_user)):
    from web.admin_api import stop_backtest
    return await stop_backtest(user)


# Legacy aliases kept for backward compatibility
@router.post("/backtest/start")
async def start_client_backtest(body: BacktestStartRequest, user=Depends(get_current_user)):
    return await run_client_backtest(body, user)


@router.get("/backtest/status")
async def get_client_backtest_status(user=Depends(get_current_user)):
    job_id = _current_bt_job_id or "current"
    return await get_client_backtest_status_by_job(job_id, user)

# ── Trade History ─────────────────────────────────────────────────────────────

@router.get("/trades")
async def get_trades(user=Depends(get_current_user)):
    trades = db_fetchall("""
        SELECT t.trade_type, t.direction, t.strike, t.entry_price, t.exit_price,
               t.pnl_pts, t.pnl_rs, t.exit_reason, t.instrument, t.trading_mode,
               t.closed_at, cbi.broker, t.entry_index_price,
               t.entry_indicators, t.exit_indicators
        FROM trade_history t
        JOIN client_broker_instances cbi ON cbi.id=t.instance_id
        WHERE t.client_id=?
        ORDER BY t.closed_at DESC LIMIT 100
    """, (user["id"],))
    return {"trades": trades}


# ── Profile ───────────────────────────────────────────────────────────────────

class ProfileUpdate(BaseModel):
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    telegram_chat_id: Optional[str] = None


@router.get("/profile")
async def get_profile(user=Depends(get_current_user)):
    row = db_fetchone(
        "SELECT id, username, email, full_name, phone_number, telegram_chat_id, "
        "referral_code, referred_by_id, created_at, static_ip FROM users WHERE id=?",
        (user["id"],)
    )
    if not row:
        raise HTTPException(404, "User not found.")
    return dict(row)


@router.patch("/profile")
async def update_profile(body: ProfileUpdate, user=Depends(get_current_user)):
    # Read current chat_id before saving to detect actual changes
    existing = db_fetchone("SELECT telegram_chat_id FROM users WHERE id=?", (user["id"],))
    old_chat_id = (existing.get("telegram_chat_id") or "").strip() if existing else ""

    updates, params = [], []
    if body.full_name is not None:
        updates.append("full_name=?"); params.append(body.full_name.strip())
    if body.phone_number is not None:
        updates.append("phone_number=?"); params.append(body.phone_number.strip())
    new_chat_id = None
    if body.telegram_chat_id is not None:
        new_chat_id = body.telegram_chat_id.strip() or None
        updates.append("telegram_chat_id=?"); params.append(new_chat_id)
    if not updates:
        return {"success": True, "message": "Nothing to update."}
    params.append(user["id"])
    db_execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", params)
    _audit_client(user["id"], "profile_update", {
        "fields": [u.split("=")[0] for u in updates]
    })
    # Auto-verify Telegram only when chat ID is set to a NEW value
    if new_chat_id and new_chat_id != old_chat_id:
        try:
            from utils.notifier import send_telegram
            send_telegram(
                new_chat_id,
                "✅ <b>AlgoSoft — Telegram Connected!</b>\n"
                "You will receive live trade alerts and a day-end PnL summary here.\n"
                "Bot: Connected ✅",
                force=True
            )
        except Exception:
            pass
    return {"success": True, "message": "Profile updated."}


@router.post("/settings/test-telegram")
async def test_telegram_client(user=Depends(get_current_user)):
    """Send a test Telegram message to verify the client's Chat ID is working."""
    row = db_fetchone("SELECT telegram_chat_id FROM users WHERE id=?", (user["id"],))
    chat_id = (row.get("telegram_chat_id") or "").strip() if row else ""
    if not chat_id:
        raise HTTPException(400, "No Telegram Chat ID saved yet. Enter your Chat ID and save first.")
    from utils.notifier import send_telegram
    ok = send_telegram(
        chat_id,
        "✅ <b>AlgoSoft — Telegram Connected!</b>\n"
        "You will receive live trade alerts and a day-end PnL summary here.\n"
        "Bot: Connected ✅",
        force=True
    )
    if ok:
        return {"success": True, "message": "Test message sent! Check your Telegram."}
    raise HTTPException(
        500,
        "Failed to send. Ensure the bot token is configured by admin and you have sent /start to the bot."
    )


# ── Referral ──────────────────────────────────────────────────────────────────

def _ensure_referral_code(user_id: int) -> str:
    """Auto-generate a referral code for user if not yet assigned."""
    import secrets, string
    row = db_fetchone("SELECT referral_code FROM users WHERE id=?", (user_id,))
    if row and row.get("referral_code"):
        return row["referral_code"]
    alphabet = string.ascii_uppercase + string.digits
    for _ in range(10):
        code = "AS" + "".join(secrets.choice(alphabet) for _ in range(6))
        try:
            db_execute("UPDATE users SET referral_code=? WHERE id=?", (code, user_id))
            return code
        except Exception:
            continue
    return ""


@router.get("/referral")
async def get_referral(user=Depends(get_current_user)):
    code = _ensure_referral_code(user["id"])
    referred = db_fetchall(
        "SELECT username, created_at FROM users WHERE referred_by_id=? ORDER BY created_at DESC",
        (user["id"],)
    )
    return {
        "referral_code": code,
        "referred_count": len(referred),
        "referred_users": [{"username": r["username"], "joined": r["created_at"][:10]} for r in referred],
    }


# ── Per-Broker Settings ───────────────────────────────────────────────────────

_VALID_BROKERS = {"zerodha", "dhan", "angelone", "upstox", "fyers", "aliceblue", "groww"}

# Mapping: UI field name → (broker_cfg key in client_strategy_overrides, python type)
_BCK = {
    "start_time":              ("v3.start_time",                     str),
    "entry_end_time":          ("v3.entry_end_time",                  str),
    "square_off_time":         ("v3.square_off_time",                 str),
    "session_pnl_enabled":     ("v3.guardrail_pnl.enabled",           bool),
    "session_pnl_target_pts":  ("v3.guardrail_pnl.target_pts",        float),
    "session_pnl_sl_pts":      ("v3.guardrail_pnl.stoploss_pts",      float),
    "single_trade_target_pts": ("v3.single_trade_target_pts",         float),
    "single_trade_sl_pts":     ("v3.single_trade_stoploss_pts",       float),
    "tsl_enabled":             ("v3.tsl_scalable.enabled",            bool),
    "tsl_base_profit":         ("v3.tsl_scalable.base_profit",        float),
    "tsl_base_lock":           ("v3.tsl_scalable.base_lock",          float),
    "tsl_step_profit":         ("v3.tsl_scalable.step_profit",        float),
    "tsl_step_lock":           ("v3.tsl_scalable.step_lock",          float),
    "max_trades_per_day":      ("v3.max_trades_per_day",              int),
    "smart_rolling_enabled":   ("v3.smart_rolling_enabled",           bool),
    "profit_target_enabled":   ("v3.profit_target_enabled",           bool),
    "profit_target_pct":       ("v3.profit_target_pct",               float),
    "ltp_decay_enabled":       ("v3.ltp_decay_enabled",               bool),
    "ltp_exit_min":            ("v3.ltp_exit_min",                    float),
    "ratio_exit_enabled":      ("v3.ratio_exit.enabled",              bool),
    "ratio_exit_threshold":    ("v3.ratio_exit.threshold",            float),
}
_DAY_MAP = {"MON": "monday", "TUE": "tuesday", "WED": "wednesday", "THU": "thursday", "FRI": "friday"}


def _read_admin_v3_defaults() -> dict:
    """Read admin strategy defaults for the settings GET response."""
    try:
        import json as _json
        with open("config/strategy_logic.json") as _f:
            _strat = _json.load(_f)
        _v3 = _strat.get("NIFTY", {}).get("sell", {}).get("v3", {}) or {}
        _pnl = _v3.get("guardrail_pnl") or {}
        _tsl = _v3.get("tsl_scalable") or {}
        return {
            "start_time":              _v3.get("start_time") or _strat.get("NIFTY", {}).get("sell", {}).get("start_time") or "09:20",
            "entry_end_time":          _v3.get("entry_end_time") or "14:00",
            "square_off_time":         _v3.get("square_off_time") or "15:15",
            "session_pnl_enabled":     bool(_pnl.get("enabled", False)),
            "session_pnl_target_pts":  _pnl.get("target_pts"),
            "session_pnl_sl_pts":      _pnl.get("stoploss_pts"),
            "single_trade_target_pts": _v3.get("single_trade_target_pts") or None,
            "single_trade_sl_pts":     _v3.get("single_trade_stoploss_pts") or None,
            "tsl_enabled":             bool(_tsl.get("enabled", False)),
            "tsl_base_profit":         _tsl.get("base_profit"),
            "tsl_base_lock":           _tsl.get("base_lock"),
            "tsl_step_profit":         _tsl.get("step_profit"),
            "tsl_step_lock":           _tsl.get("step_lock"),
            "max_trades_per_day":      _v3.get("max_trades_per_day"),
            "smart_rolling_enabled":   bool(_v3.get("smart_rolling_enabled", True)),
            "profit_target_enabled":   bool(_v3.get("profit_target_enabled", False)),
            "profit_target_pct":       _v3.get("profit_target_pct"),
            "ltp_decay_enabled":       bool(_v3.get("ltp_decay_enabled", False)),
            "ltp_exit_min":            _v3.get("ltp_exit_min"),
            "ratio_exit_enabled":      bool((_v3.get("ratio_exit") or {}).get("enabled", False)),
            "ratio_exit_threshold":    (_v3.get("ratio_exit") or {}).get("threshold"),
            "day_wise":                {
                d: {
                    "target": (_v3.get(long) or {}).get("single_trade_target_pts"),
                    "sl":     (_v3.get(long) or {}).get("single_trade_stoploss_pts"),
                }
                for d, long in _DAY_MAP.items()
            },
        }
    except Exception:
        return {}


def _get_broker_instance(client_id: int, broker: str):
    return db_fetchone(
        "SELECT id, client_strategy_overrides, daily_loss_limit, max_daily_trades, capital_allocated, "
        "       trading_locked_until, per_trade_loss_limit, max_position_size, max_open_positions, "
        "       max_drawdown_pct, risk_per_trade_pct "
        "FROM client_broker_instances "
        "WHERE client_id=? AND broker=? AND status!='removed' LIMIT 1",
        (client_id, broker)
    )


def _parse_broker_cfg(inst) -> dict:
    """Return broker_cfg sub-dict from client_strategy_overrides."""
    try:
        if inst and inst.get("client_strategy_overrides"):
            raw = json.loads(inst["client_strategy_overrides"])
            return raw.get("broker_cfg", {})
    except Exception:
        pass
    return {}


def _broker_cfg_to_ui(broker_cfg: dict) -> dict:
    """Convert broker_cfg internal keys to UI field names."""
    out = {}
    for field, (key, _) in _BCK.items():
        if key in broker_cfg:
            out[field] = broker_cfg[key]
    # Day-wise overrides
    day_wise = {}
    for d, long in _DAY_MAP.items():
        t_key = f"v3.{long}.single_trade_target_pts"
        sl_key = f"v3.{long}.single_trade_stoploss_pts"
        if t_key in broker_cfg or sl_key in broker_cfg:
            day_wise[d] = {
                "target": broker_cfg.get(t_key),
                "sl":     broker_cfg.get(sl_key),
            }
    if day_wise:
        out["day_wise"] = day_wise
    return out


@router.get("/broker/{broker}/settings")
async def get_broker_settings(broker: str, user=Depends(get_current_user)):
    if broker not in _VALID_BROKERS:
        raise HTTPException(400, "Invalid broker.")
    inst = _get_broker_instance(user["id"], broker)
    if not inst:
        return {"configured": False, "broker": broker, "settings": {}, "admin_defaults": _read_admin_v3_defaults()}

    broker_cfg = _parse_broker_cfg(inst)
    client_settings = _broker_cfg_to_ui(broker_cfg)

    # Merge DB columns
    if inst.get("daily_loss_limit") is not None:
        client_settings.setdefault("daily_loss_limit", inst["daily_loss_limit"])
    if inst.get("max_daily_trades") is not None:
        client_settings.setdefault("max_trades_per_day", inst["max_daily_trades"])

    # Capital deploy pct from overrides JSON top-level
    try:
        raw_over = json.loads(inst["client_strategy_overrides"]) if inst.get("client_strategy_overrides") else {}
    except Exception:
        raw_over = {}
    if "capital_deploy_pct" in raw_over:
        client_settings["capital_deploy_pct"] = raw_over["capital_deploy_pct"]

    if inst.get("trading_locked_until"):
        client_settings["trading_locked_until"] = inst["trading_locked_until"]

    return {
        "configured": True,
        "broker":         broker,
        "instance_id":    inst["id"],
        "settings":       client_settings,
        "admin_defaults": _read_admin_v3_defaults(),
        "capital_allocated": inst.get("capital_allocated") or 0,
        "per_trade_loss_limit": inst.get("per_trade_loss_limit") or 0,
        "max_position_size":    inst.get("max_position_size") or 1,
        "max_open_positions":   inst.get("max_open_positions") or 1,
        "max_drawdown_pct":     inst.get("max_drawdown_pct") or 0,
        "risk_per_trade_pct":   inst.get("risk_per_trade_pct") or 1.0,
    }


class BrokerSettingsUpdate(BaseModel):
    start_time:              Optional[str]   = None
    entry_end_time:          Optional[str]   = None
    square_off_time:         Optional[str]   = None
    capital_deploy_pct:      Optional[float] = None
    session_pnl_enabled:     Optional[bool]  = None
    session_pnl_target_pts:  Optional[float] = None
    session_pnl_sl_pts:      Optional[float] = None
    single_trade_target_pts: Optional[float] = None
    single_trade_sl_pts:     Optional[float] = None
    day_wise:                Optional[dict]  = None
    tsl_enabled:             Optional[bool]  = None
    tsl_base_profit:         Optional[float] = None
    tsl_base_lock:           Optional[float] = None
    tsl_step_profit:         Optional[float] = None
    tsl_step_lock:           Optional[float] = None
    daily_loss_limit:        Optional[float] = None
    max_trades_per_day:      Optional[int]   = None
    smart_rolling_enabled:   Optional[bool]  = None
    profit_target_enabled:   Optional[bool]  = None
    profit_target_pct:       Optional[float] = None
    ltp_decay_enabled:       Optional[bool]  = None
    ltp_exit_min:            Optional[float] = None
    ratio_exit_enabled:      Optional[bool]  = None
    ratio_exit_threshold:    Optional[float] = None


@router.post("/broker/{broker}/settings")
async def save_broker_settings(broker: str, body: BrokerSettingsUpdate, user=Depends(get_current_user)):
    if broker not in _VALID_BROKERS:
        raise HTTPException(400, "Invalid broker.")
    inst = _get_broker_instance(user["id"], broker)
    if not inst:
        raise HTTPException(400, f"No {broker} instance configured.")

    # Load current overrides
    try:
        raw_over = json.loads(inst["client_strategy_overrides"]) if inst.get("client_strategy_overrides") else {}
    except Exception:
        raw_over = {}

    broker_cfg = raw_over.get("broker_cfg", {})

    # Apply each field → broker_cfg key
    for field, (cfg_key, typ) in _BCK.items():
        val = getattr(body, field, None)
        if val is not None:
            if typ == bool:
                broker_cfg[cfg_key] = bool(val)
            elif typ == float:
                broker_cfg[cfg_key] = float(val)
            elif typ == int:
                broker_cfg[cfg_key] = int(val)
            else:
                broker_cfg[cfg_key] = val

    # Day-wise overrides
    if body.day_wise is not None:
        for d_upper, vals in body.day_wise.items():
            long = _DAY_MAP.get(d_upper.upper())
            if not long:
                continue
            target = vals.get("target")
            sl     = vals.get("sl")
            t_key  = f"v3.{long}.single_trade_target_pts"
            sl_key = f"v3.{long}.single_trade_stoploss_pts"
            if target is not None:
                broker_cfg[t_key] = float(target)
            else:
                broker_cfg.pop(t_key, None)
            if sl is not None:
                broker_cfg[sl_key] = float(sl)
            else:
                broker_cfg.pop(sl_key, None)

    raw_over["broker_cfg"] = broker_cfg

    # Capital deploy pct — stored at overrides top-level
    if body.capital_deploy_pct is not None:
        raw_over["capital_deploy_pct"] = float(body.capital_deploy_pct)

    # Build SQL update
    sql_sets  = ["client_strategy_overrides=?"]
    sql_vals  = [json.dumps(raw_over)]

    if body.daily_loss_limit is not None:
        sql_sets.append("daily_loss_limit=?")
        sql_vals.append(body.daily_loss_limit)
    if body.max_trades_per_day is not None:
        sql_sets.append("max_daily_trades=?")
        sql_vals.append(body.max_trades_per_day)

    sql_vals.append(inst["id"])
    db_execute(f"UPDATE client_broker_instances SET {', '.join(sql_sets)} WHERE id=?", sql_vals)
    _audit_client(user["id"], "broker_settings_save", {"broker": broker})
    return {"success": True, "message": f"{broker.capitalize()} settings saved."}


@router.post("/broker/{broker}/stop")
async def stop_broker_bot(broker: str, user=Depends(get_current_user)):
    """Stop the bot for a specific broker instance."""
    if broker not in _VALID_BROKERS:
        raise HTTPException(400, "Invalid broker.")
    inst = db_fetchone(
        "SELECT id, broker, status FROM client_broker_instances "
        "WHERE client_id=? AND broker=? AND status!='removed' LIMIT 1",
        (user["id"], broker)
    )
    if not inst:
        raise HTTPException(400, f"No {broker} instance configured.")

    ok, msg = instance_manager.stop_instance(inst["id"])
    db_execute(
        "UPDATE client_broker_instances SET status='idle', bot_pid=NULL WHERE id=?",
        (inst["id"],)
    )
    _audit_client(user["id"], "bot_deactivate", {"broker": broker})
    return {"success": True, "message": msg or f"{broker.capitalize()} bot stopped."}


# ── Risk Parameters ───────────────────────────────────────────────────────────

class RiskParamsUpdate(BaseModel):
    daily_loss_limit: Optional[float] = None       # ₹ daily loss kill-switch
    max_trades_per_day: Optional[int] = None
    profit_target_pct: Optional[float] = None      # % of capital
    guardrail_pnl_target: Optional[float] = None   # pts
    guardrail_pnl_sl: Optional[float] = None       # pts
    single_trade_target: Optional[float] = None    # pts
    single_trade_sl: Optional[float] = None        # pts


@router.get("/risk-params")
async def get_risk_params(user=Depends(get_current_user)):
    inst = db_fetchone(
        """SELECT id, daily_loss_limit, client_strategy_overrides,
                  capital_allocated, max_position_size, max_open_positions,
                  per_trade_loss_limit, max_drawdown_pct, risk_per_trade_pct,
                  trading_locked_until, daily_pnl, daily_trade_count, max_daily_trades
           FROM client_broker_instances
           WHERE client_id=? AND status!='removed'
           ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, id DESC LIMIT 1""",
        (user["id"],)
    )
    if not inst:
        return {"configured": False, "params": {}}
    overrides = {}
    try:
        if inst.get("client_strategy_overrides"):
            overrides = json.loads(inst["client_strategy_overrides"])
    except Exception:
        pass
    return {
        "configured": True,
        "instance_id": inst["id"],
        "daily_loss_limit":    inst.get("daily_loss_limit") or 0,
        "params": overrides,
        # Admin-set risk limits (read-only for client)
        "capital_allocated":   inst.get("capital_allocated") or 0,
        "max_position_size":   inst.get("max_position_size") or 1,
        "max_open_positions":  inst.get("max_open_positions") or 1,
        "per_trade_loss_limit": inst.get("per_trade_loss_limit") or 0,
        "max_drawdown_pct":    inst.get("max_drawdown_pct") or 0,
        "risk_per_trade_pct":  inst.get("risk_per_trade_pct") or 1.0,
        "trading_locked_until": inst.get("trading_locked_until"),
        "daily_pnl":           inst.get("daily_pnl") or 0,
        "daily_trade_count":   inst.get("daily_trade_count") or 0,
        "max_daily_trades":    inst.get("max_daily_trades") or 0,
    }


@router.post("/risk-params")
async def save_risk_params(body: RiskParamsUpdate, user=Depends(get_current_user)):
    inst = db_fetchone(
        "SELECT id, client_strategy_overrides FROM client_broker_instances "
        "WHERE client_id=? AND status!='removed' ORDER BY CASE WHEN status='running' THEN 0 ELSE 1 END, id DESC LIMIT 1",
        (user["id"],)
    )
    if not inst:
        raise HTTPException(400, "No broker configured. Set up a broker first.")

    overrides = {}
    try:
        if inst.get("client_strategy_overrides"):
            overrides = json.loads(inst["client_strategy_overrides"])
    except Exception:
        pass

    if body.max_trades_per_day is not None:
        overrides["max_trades_per_day"] = max(0, body.max_trades_per_day)
    if body.profit_target_pct is not None:
        overrides["profit_target_pct"] = body.profit_target_pct
    if body.guardrail_pnl_target is not None:
        overrides["guardrail_pnl_target"] = body.guardrail_pnl_target
    if body.guardrail_pnl_sl is not None:
        overrides["guardrail_pnl_sl"] = body.guardrail_pnl_sl
    if body.single_trade_target is not None:
        overrides["single_trade_target"] = body.single_trade_target
    if body.single_trade_sl is not None:
        overrides["single_trade_sl"] = body.single_trade_sl

    db_execute(
        "UPDATE client_broker_instances SET client_strategy_overrides=? "
        + (", daily_loss_limit=?" if body.daily_loss_limit is not None else "")
        + " WHERE id=?",
        ([json.dumps(overrides)]
         + ([body.daily_loss_limit] if body.daily_loss_limit is not None else [])
         + [inst["id"]])
    )
    _audit_client(user["id"], "risk_params_save", overrides)
    return {"success": True, "message": "Risk parameters saved."}


# ── Market Status ─────────────────────────────────────────────────────────────

_NSE_HOLIDAYS_2026 = {
    "2026-01-26", "2026-03-02", "2026-03-20", "2026-04-02", "2026-04-14",
    "2026-04-15", "2026-05-01", "2026-06-29", "2026-08-15", "2026-09-02",
    "2026-10-02", "2026-10-21", "2026-10-22", "2026-11-04", "2026-11-25",
    "2026-12-25",
}
_NSE_HOLIDAYS_2025 = {
    "2025-01-26", "2025-02-26", "2025-03-14", "2025-03-31", "2025-04-10",
    "2025-04-14", "2025-04-18", "2025-05-01", "2025-08-15", "2025-10-02",
    "2025-10-02", "2025-10-20", "2025-10-21", "2025-11-05", "2025-12-25",
}
_NSE_HOLIDAYS = _NSE_HOLIDAYS_2025 | _NSE_HOLIDAYS_2026


@router.get("/market-status")
async def get_market_status(user=Depends(get_current_user)):
    """Return NSE market open/closed status and timing info."""
    now = datetime.now(IST)
    today_str = now.strftime("%Y-%m-%d")
    weekday = now.weekday()  # 0=Mon, 6=Sun

    try:
        from hub.sell_v3.rust_bridge import RUST_AVAILABLE as _rust
        rust_available = bool(_rust)
    except Exception:
        rust_available = False

    is_holiday = today_str in _NSE_HOLIDAYS
    is_weekend = weekday >= 5
    market_open_time  = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close_time = now.replace(hour=15, minute=30, second=0, microsecond=0)

    if is_holiday or is_weekend:
        status = "CLOSED"
        reason = "Holiday" if is_holiday else "Weekend"
        next_open = None
        # Find next trading day
        candidate = now + timedelta(days=1)
        for _ in range(10):
            c_str = candidate.strftime("%Y-%m-%d")
            if candidate.weekday() < 5 and c_str not in _NSE_HOLIDAYS:
                next_open = candidate.replace(hour=9, minute=15, second=0, microsecond=0).isoformat()
                break
            candidate += timedelta(days=1)
        return {
            "status": status,
            "reason": reason,
            "next_open": next_open,
            "is_market_hours": False,
            "minutes_to_open": None,
            "minutes_to_close": None,
            "server_time": now.isoformat(),
            "rust_available": rust_available,
        }

    before_open  = now < market_open_time
    after_close  = now > market_close_time
    in_session   = not before_open and not after_close
    pre_open     = (market_open_time - timedelta(minutes=15)) <= now < market_open_time

    if before_open:
        status = "PRE-OPEN" if pre_open else "CLOSED"
        reason = "Before market open"
        mins_to_open = max(0, int((market_open_time - now).total_seconds() / 60))
        return {
            "status": status, "reason": reason, "is_market_hours": False,
            "minutes_to_open": mins_to_open, "minutes_to_close": None,
            "next_open": market_open_time.isoformat(),
            "server_time": now.isoformat(),
            "rust_available": rust_available,
        }
    elif after_close:
        # Find next trading day
        candidate = now + timedelta(days=1)
        next_open = None
        for _ in range(10):
            c_str = candidate.strftime("%Y-%m-%d")
            if candidate.weekday() < 5 and c_str not in _NSE_HOLIDAYS:
                next_open = candidate.replace(hour=9, minute=15, second=0, microsecond=0).isoformat()
                break
            candidate += timedelta(days=1)
        return {
            "status": "CLOSED", "reason": "Market closed for today",
            "is_market_hours": False, "minutes_to_open": None, "minutes_to_close": None,
            "next_open": next_open, "server_time": now.isoformat(),
            "rust_available": rust_available,
        }
    else:
        mins_to_close = max(0, int((market_close_time - now).total_seconds() / 60))
        return {
            "status": "OPEN", "reason": "Market is live",
            "is_market_hours": True, "minutes_to_open": 0,
            "minutes_to_close": mins_to_close,
            "next_open": None, "server_time": now.isoformat(),
            "rust_available": rust_available,
        }
