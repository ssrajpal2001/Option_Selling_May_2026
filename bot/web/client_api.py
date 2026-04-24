import json
import time
import hashlib
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
from utils.logger import logger

router = APIRouter(prefix="/client", tags=["client"])

IST = timezone(timedelta(hours=5, minutes=30))


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
               api_secret_encrypted,
               trading_mode, instrument, quantity, strategy_version,
               status, last_heartbeat, token_updated_at
        FROM client_broker_instances
        WHERE client_id=? AND status != 'removed'
    """, (user["id"],))
    from utils.auth_manager_dhan import is_dhan_api_key_mode as _is_akm_cfg
    result = []
    for r in rows:
        d = dict(r)
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

        del d["api_secret_encrypted"]
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


@router.delete("/broker/{broker}")
async def delete_broker_config(broker: str, user=Depends(get_current_user)):
    if broker not in ("zerodha", "dhan", "angelone", "upstox"):
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
    if body.broker not in ("zerodha", "dhan", "angelone", "upstox"):
        raise HTTPException(400, "Broker must be 'zerodha', 'dhan', 'angelone', or 'upstox'.")

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
        "SELECT api_key_encrypted, api_secret_encrypted, access_token_encrypted, password_encrypted, totp_encrypted, broker_user_id_encrypted FROM client_broker_instances WHERE client_id=? AND broker=?",
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

    # For Dhan, we might want to store api_secret as access_token if provided as such
    access_token_val = body.access_token
    if (not access_token_val or access_token_val == "unchanged") and body.api_secret and body.api_secret != "unchanged":
        access_token_val = body.api_secret
        is_new_token = True

    enc_token = encrypt_secret(access_token_val) if is_new_token else (existing_row["access_token_encrypted"] if existing_row else None)

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
                "api_key": decrypt_secret(instance["api_key_encrypted"]),
                "api_secret": decrypt_secret(instance["api_secret_encrypted"]),
                "broker_user_id": decrypt_secret(instance["broker_user_id_encrypted"]),
                "password": decrypt_secret(instance["password_encrypted"]),
                "totp": decrypt_secret(instance["totp_encrypted"])
            }
            token = handle_zerodha_login_automated(creds)
            if token:
                enc_token = encrypt_secret(token)
                now_ist = datetime.now(IST).isoformat()
                db_execute("UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='zerodha'", (enc_token, now_ist, user["id"]))
                from hub.event_bus import event_bus
                await event_bus.publish('BROKER_TOKEN_UPDATED', {'user_id': user["id"], 'broker': 'zerodha', 'access_token': token})
                return {"success": True, "automated": True, "message": "Zerodha background login successful."}
        except Exception as e:
            logger.error(f"Zerodha automated login failed: {e}")

    # 2. Fallback to Browser OAuth
    api_key = decrypt_secret(instance["api_key_encrypted"])
    state_payload = f'{user["id"]}:{int(time.time())}'
    state_encrypted = _fernet.encrypt(state_payload.encode()).decode()
    login_url = KITE_LOGIN_URL.format(api_key=api_key)
    raw_host = request.headers.get('host') or str(request.base_url).split('/')[2]
    proto = request.headers.get('x-forwarded-proto', 'http')
    if 'localhost' not in raw_host and '127.0.0.1' not in raw_host and proto != 'http': proto = 'https'
    redirect_uri = f"{proto}://{raw_host}/auth/zerodha/callback"
    login_url += "&redirect_uri=" + urllib.parse.quote(redirect_uri) + "&state=" + urllib.parse.quote(state_encrypted)
    return {"success": True, "login_url": login_url}


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
        token = generate_dhan_token(
            api_key=api_key,
            client_id=dhan_client_id,
            password=password,
            totp_secret=totp_sec,
        )
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
        raise HTTPException(
            400,
            "Dhan token generation failed. Check your API Key, Dhan Client ID, and password in Settings."
        )

    # ── Path 2: Direct token mode — validate existing token ───────────────
    if password and totp_sec:
        try:
            token = handle_dhan_login_automated(creds)
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

    # 1. Attempt Automated Login (AngelOne is naturally TOTP based)
    if instance.get("password_encrypted") and instance.get("totp_encrypted"):
        try:
            from utils.auth_manager_angelone import handle_angelone_login
            creds = {
                "api_key": decrypt_secret(instance["api_key_encrypted"]),
                "client_code": decrypt_secret(instance["broker_user_id_encrypted"]),
                "pin": decrypt_secret(instance["password_encrypted"]),
                "totp": decrypt_secret(instance["totp_encrypted"])
            }
            smart_api = handle_angelone_login(creds)
            if smart_api and smart_api.access_token:
                enc_token = encrypt_secret(smart_api.access_token)
                now_ist = datetime.now(IST).isoformat()
                db_execute("UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='angelone'", (enc_token, now_ist, user["id"]))
                from hub.event_bus import event_bus
                await event_bus.publish('BROKER_TOKEN_UPDATED', {'user_id': user["id"], 'broker': 'angelone', 'access_token': smart_api.access_token})
                return {"success": True, "automated": True, "message": "AngelOne session generated."}
        except Exception as e:
            logger.error(f"AngelOne automated login failed: {e}")

    url = "https://smartapi.angelbroking.com/publisher-login"
    return {"success": True, "login_url": url}


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

@router.post("/bot/toggle-trading")
async def toggle_trading(body: TradingToggleRequest, user=Depends(get_current_user)):
    instance = _get_active_instance(user["id"])
    if not instance or instance["status"] != "running":
        raise HTTPException(400, "Broker connection must be active first.")

    # We use a signal file to communicate with the subprocess
    toggle_file = Path(f'config/trading_enabled_{user["id"]}.json')
    with open(toggle_file, 'w') as f:
        json.dump({"enabled": body.enabled, "updated_at": time.time()}, f)

    msg = "Trading enabled." if body.enabled else "Trading disabled. Active trades will be closed."
    logger.info(f"[Client] User {user['id']} toggled trading to {body.enabled}")
    action = "trading_start" if body.enabled else "trading_stop"
    _audit_client(user["id"], action, {"broker": instance.get("broker")})
    return {"success": True, "message": msg}

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


@router.post("/bot/start")
async def start_bot(body: BotStartRequest = BotStartRequest(), user=Depends(get_current_user)):
    # ── Plan expiry enforcement (hard cap: only 1 permitted broker slot) ─
    _plan_expiry_warning = None
    _plan_expired = False
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
        # When expired, only allow starting the single permitted broker instance
        # (the lowest-id configured instance).  Any additional brokers are blocked.
        all_instances = db_fetchall(
            "SELECT id, broker FROM client_broker_instances "
            "WHERE client_id=? AND status != 'removed' ORDER BY id ASC",
            (user["id"],)
        )
        requested_broker = body.broker if body.broker and body.broker in ("zerodha", "dhan", "angelone", "upstox") else None
        if len(all_instances) > 1 and requested_broker:
            permitted_broker = all_instances[0]["broker"]
            if requested_broker != permitted_broker:
                raise HTTPException(
                    403,
                    f"Your subscription expired on {_exp_str_s[:10]}. "
                    f"Only your primary broker ({permitted_broker}) can be started on an expired plan. "
                    "Contact admin to renew or remove extra broker configurations."
                )
        _plan_expiry_warning = (
            f"Your subscription expired on {_exp_str_s[:10]}. "
            "You are limited to 1 broker slot. Contact admin to renew."
        )
    # ─────────────────────────────────────────────────────────────────────

    requested_broker = body.broker if body.broker and body.broker in ("zerodha", "dhan", "angelone", "upstox") else None  # noqa (may duplicate from expiry block; safe)
    instance = _get_active_instance(user["id"], broker=requested_broker)
    if not instance:
        raise HTTPException(400, "No broker configured. Please set up your broker first.")
    if not instance.get("api_key_encrypted"):
        raise HTTPException(400, "Broker credentials missing. Please re-enter your credentials.")

    broker_name = instance["broker"]

    # ── Kill-switch check ─────────────────────────────────────────────────
    locked_until = instance.get("trading_locked_until")
    if locked_until:
        try:
            locked_dt = datetime.fromisoformat(locked_until)
            if locked_dt.tzinfo is None:
                locked_dt = locked_dt.replace(tzinfo=IST)
            if datetime.now(IST) < locked_dt:
                raise HTTPException(
                    423,
                    f"Daily loss kill-switch is active. Bot is locked until "
                    f"{locked_until[:16].replace('T', ' ')} IST. "
                    "It will auto-unlock at the next trading session (9:15 AM)."
                )
        except HTTPException:
            raise
        except Exception:
            pass
    # ─────────────────────────────────────────────────────────────────────

    has_auto_login = instance.get("password_encrypted") and instance.get("totp_encrypted")

    # ── Headless Login Integration ──
    # If auto-login is possible, we attempt it BEFORE starting the bot to ensure valid tokens in DB
    if has_auto_login:
        try:
            logger.info(f"[Bot Start] Attempting headless login for {broker_name} (User {user['id']})...")
            creds = {
                "api_key": decrypt_secret(instance["api_key_encrypted"]),
                "api_secret": decrypt_secret(instance.get("api_secret_encrypted", "")),
                "broker_user_id": decrypt_secret(instance.get("broker_user_id_encrypted", "")),
                "password": decrypt_secret(instance["password_encrypted"]),
                "totp": decrypt_secret(instance["totp_encrypted"])
            }

            token = None
            if broker_name == 'zerodha':
                from utils.auth_manager_zerodha import handle_zerodha_login_automated
                token = handle_zerodha_login_automated(creds)
            elif broker_name == 'dhan':
                from utils.auth_manager_dhan import handle_dhan_login_automated
                token = handle_dhan_login_automated(creds)
            elif broker_name == 'angelone':
                from utils.auth_manager_angelone import handle_angelone_login
                creds["client_code"] = creds["broker_user_id"]
                creds["pin"] = creds["password"]
                smart_api = handle_angelone_login(creds)
                if smart_api: token = smart_api.access_token
            elif broker_name == 'upstox':
                from utils.auth_manager_upstox import handle_upstox_login_automated
                token = handle_upstox_login_automated(creds)

            if token:
                enc_token = encrypt_secret(token)
                now_ist = datetime.now(IST).isoformat()
                db_execute(f"UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE id=?", (enc_token, now_ist, instance["id"]))
                # Refresh instance data for start_instance call
                instance["access_token_encrypted"] = enc_token
                logger.info(f"[Bot Start] Headless login SUCCESS for {broker_name}")
            else:
                logger.warning(f"[Bot Start] Headless login failed for {broker_name}, will attempt with existing token if any.")
        except Exception as e:
            logger.error(f"[Bot Start] Headless login error: {e}")

    # Validation
    if not instance.get("access_token_encrypted"):
        raise HTTPException(400, f"Connection failed. Please provide Password/TOTP for One-Click Connect or manual access token in Settings.")

    if broker_name in ("zerodha", "upstox", "angelone"):
        if not _is_token_fresh(instance.get("token_updated_at")):
            raise HTTPException(400, f"{broker_name.capitalize()} session expired. Update Password/TOTP or reconnect in Settings.")
    elif broker_name == "dhan":
        _dhan_api_secret = decrypt_secret(instance["api_secret_encrypted"]) if instance.get("api_secret_encrypted") else ""
        from utils.auth_manager_dhan import is_dhan_api_key_mode as _is_akm
        _dhan_is_api_mode = _is_akm({"api_secret": _dhan_api_secret})
        if not _is_dhan_token_fresh(instance.get("token_updated_at"), api_key_mode=_dhan_is_api_mode):
            if _dhan_is_api_mode:
                raise HTTPException(400, "Dhan access token expired (24-hr limit). Click 'Connect Now' to auto-refresh it.")
            raise HTTPException(400, "Dhan access token expired. Reconnect in Settings.")

    pending_change = db_fetchone(
        "SELECT id FROM broker_change_requests WHERE client_id=? AND status='pending'",
        (user["id"],)
    )
    if pending_change:
        raise HTTPException(400, "You have a pending broker change request. Please wait for admin approval before starting the bot.")

    # Removed hardcoded Upstox data provider check to allow unified client-broker data feeds.

    if instance["status"] == "running":
        return {"success": False, "message": "Bot is already running."}

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
    response = {"success": ok, "message": msg}
    if _plan_expiry_warning:
        response["plan_warning"] = _plan_expiry_warning
    return response


@router.post("/bot/stop")
async def stop_bot(user=Depends(get_current_user)):
    instance = _get_active_instance(user["id"])
    if not instance:
        raise HTTPException(400, "No broker configured.")

    ok, msg = instance_manager.stop_instance(instance["id"])
    if ok:
        db_execute("UPDATE client_broker_instances SET status='idle', bot_pid=NULL WHERE id=?", (instance["id"],))
        _audit_client(user["id"], "bot_deactivate", {"broker": instance.get("broker")})
    return {"success": ok, "message": msg}


@router.post("/bot/restart")
async def restart_bot(body: BotStartRequest = BotStartRequest(), user=Depends(get_current_user)):
    await stop_bot(user=user)
    return await start_bot(body=body, user=user)


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
                "api_key": decrypt_secret(instance["api_key_encrypted"]),
                "api_secret": decrypt_secret(instance["api_secret_encrypted"]),
                "broker_user_id": decrypt_secret(instance["broker_user_id_encrypted"]),
                "password": decrypt_secret(instance["password_encrypted"]),
                "totp": decrypt_secret(instance["totp_encrypted"])
            }
            token = handle_upstox_login_automated(creds)
            if token:
                enc_token = encrypt_secret(token)
                now_ist = datetime.now(IST).isoformat()
                db_execute("UPDATE client_broker_instances SET access_token_encrypted=?, token_updated_at=? WHERE client_id=? AND broker='upstox'", (enc_token, now_ist, user["id"]))
                from hub.event_bus import event_bus
                await event_bus.publish('BROKER_TOKEN_UPDATED', {'user_id': user["id"], 'broker': 'upstox', 'access_token': token})
                return {"success": True, "automated": True, "message": "Upstox background login successful."}
        except Exception as e:
            logger.error(f"Upstox automated login failed: {e}")

    # 2. Fallback to Browser OAuth
    api_key = decrypt_secret(instance["api_key_encrypted"])
    state_payload = f'{user["id"]}:{int(time.time())}'
    state_encrypted = _fernet.encrypt(state_payload.encode()).decode()
    raw_host = request.headers.get('host') or str(request.base_url).split('/')[2]
    proto = request.headers.get('x-forwarded-proto', 'http')
    if 'localhost' not in raw_host and '127.0.0.1' not in raw_host and proto != 'http': proto = 'https'
    actual_redirect = f"{proto}://{raw_host}/auth/upstox/callback"
    auth_dialog = "https://api.upstox.com/v2/login/authorization/dialog"
    url = f"{auth_dialog}?response_type=code&client_id={api_key}&redirect_uri={urllib.parse.quote(actual_redirect)}&state={urllib.parse.quote(state_encrypted)}"
    return {"success": True, "login_url": url}

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

    return {
        "configured": True,
        "instance": inst_safe,
        "live": live_status,
        "trade_history": trades,
        "bot_data": bot_data,
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

class BacktestStartRequest(BaseModel):
    instrument: str
    date: str
    quantity: int = 1

@router.post("/backtest/start")
async def start_client_backtest(body: BacktestStartRequest, user=Depends(get_current_user)):
    from web.admin_api import start_backtest, BacktestStartRequest as AdminBSR
    # Reuse admin logic but scoped to client
    # For now, we use a single global backtest process per server instance
    return await start_backtest(AdminBSR(instrument=body.instrument, date=body.date, quantity=body.quantity), user)

@router.get("/backtest/status")
async def get_client_backtest_status(user=Depends(get_current_user)):
    from web.admin_api import get_backtest_status
    return await get_backtest_status(user)

@router.post("/backtest/stop")
async def stop_client_backtest(user=Depends(get_current_user)):
    from web.admin_api import stop_backtest
    return await stop_backtest(user)

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
        "referral_code, referred_by_id, created_at FROM users WHERE id=?",
        (user["id"],)
    )
    if not row:
        raise HTTPException(404, "User not found.")
    return dict(row)


@router.patch("/profile")
async def update_profile(body: ProfileUpdate, user=Depends(get_current_user)):
    updates, params = [], []
    if body.full_name is not None:
        updates.append("full_name=?"); params.append(body.full_name.strip())
    if body.phone_number is not None:
        updates.append("phone_number=?"); params.append(body.phone_number.strip())
    if body.telegram_chat_id is not None:
        updates.append("telegram_chat_id=?"); params.append(body.telegram_chat_id.strip() or None)
    if not updates:
        return {"success": True, "message": "Nothing to update."}
    params.append(user["id"])
    db_execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", params)
    _audit_client(user["id"], "profile_update", {
        "fields": [u.split("=")[0] for u in updates]
    })
    return {"success": True, "message": "Profile updated."}


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
        }
    else:
        mins_to_close = max(0, int((market_close_time - now).total_seconds() / 60))
        return {
            "status": "OPEN", "reason": "Market is live",
            "is_market_hours": True, "minutes_to_open": 0,
            "minutes_to_close": mins_to_close,
            "next_open": None, "server_time": now.isoformat(),
        }
