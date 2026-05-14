import urllib.parse
import asyncio
import csv
import io
import json
import logging
import os
import re
import time
from pathlib import Path
import subprocess
import configparser
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, field_validator
from typing import Optional
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

from web.deps import require_admin, get_current_user
from web.db import db_fetchone, db_fetchall, db_execute
from web.auth import encrypt_secret, decrypt_secret
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
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
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    user_id: Optional[str] = None
    password: Optional[str] = None
    totp: Optional[str] = None

class ManualTokenRequest(BaseModel):
    provider: str
    raw_value: str
    redirect_uri: Optional[str] = None

@router.get("/data-providers")
async def list_data_providers(admin=Depends(require_admin)):
    providers = db_fetchall("SELECT provider, status, updated_at FROM data_providers")
    return providers

@router.get("/data-providers/{provider}/config")
async def get_data_provider_config(provider: str, admin=Depends(require_admin)):
    """Return decrypted credentials for a provider so the configure modal can pre-fill fields."""
    dp = db_fetchone("SELECT * FROM data_providers WHERE provider=?", (provider,))
    if not dp:
        raise HTTPException(404, f"Provider '{provider}' not found.")
    return {
        "api_key":    decrypt_secret(dp.get("api_key_encrypted")    or "") or "",
        "api_secret": decrypt_secret(dp.get("api_secret_encrypted") or "") or "",
        "user_id":    decrypt_secret(dp.get("user_id_encrypted")    or "") or "",
        "password":   decrypt_secret(dp.get("password_encrypted")   or "") or "",
        "totp":       decrypt_secret(dp.get("totp_encrypted")       or "") or "",
    }

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
        raw_host = request.headers.get('host') or str(request.base_url).split('/')[2]
        proto = request.headers.get('x-forwarded-proto', 'http')
        if 'localhost' not in raw_host and '127.0.0.1' not in raw_host and proto != 'http':
            proto = 'https'
        _cb_uri = f"{proto}://{raw_host}/auth/upstox/callback"
        # Upstox rejects HTTP redirect URIs for non-localhost hosts.
        # Fall back to google.com (manual paste flow) when running over plain HTTP.
        _saved_redirect = dp.get("redirect_uri") or ""
        if proto == 'http' or _saved_redirect.startswith("https://www.google.com") or _saved_redirect.startswith("https://google.com"):
            redirect_uri = "https://www.google.com"
        else:
            redirect_uri = _cb_uri
        auth_dialog = "https://api.upstox.com/v2/login/authorization/dialog"
        url = f"{auth_dialog}?response_type=code&client_id={api_key}&redirect_uri={urllib.parse.quote(redirect_uri)}&state={urllib.parse.quote(state_encrypted)}"
        return RedirectResponse(url)
    elif provider == 'dhan':
        from web.auth import _fernet
        state_payload = f"admin:{int(time.time())}"
        state_encrypted = _fernet.encrypt(state_payload.encode()).decode()
        # Include applicationId so Dhan generates the correct app-scoped token
        app_id = decrypt_secret(dp.get("user_id_encrypted", "") or "") if dp else ""
        base_url = f"https://login.dhan.co/?state={urllib.parse.quote(state_encrypted)}"
        if app_id:
            base_url += f"&applicationId={urllib.parse.quote(app_id)}"
        return RedirectResponse(base_url)
    raise HTTPException(400, f"OAuth not supported for {provider}")


@router.post("/data-providers/{provider}/connect")
async def global_provider_connect_background(provider: str, admin=Depends(require_admin)):
    """JSON endpoint for background automated login."""
    dp = db_fetchone("SELECT * FROM data_providers WHERE provider=?", (provider,))
    if not dp: return {"success": False, "message": "Provider not configured."}

    try:
        token = None
        _dhan_error = None
        _upstox_error = None
        if provider == 'upstox':
            # Skip re-login if a valid token exists, was issued within the last 30 minutes,
            # AND the WebSocket is currently connected (token is actually working).
            _issued_at = dp.get("token_issued_at")
            _has_token = bool(dp.get("access_token_encrypted"))
            if _issued_at and _has_token:
                try:
                    import pytz as _pytz
                    _IST = _pytz.timezone('Asia/Kolkata')
                    _issued_dt = datetime.fromisoformat(_issued_at)
                    # token_issued_at is stored in IST without tzinfo — attach IST, convert to UTC
                    if _issued_dt.tzinfo is None:
                        _issued_dt = _IST.localize(_issued_dt)
                    _age_minutes = (datetime.now(IST) - _issued_dt.astimezone(IST)).total_seconds() / 60
                    # Only skip if fresh AND the WS is actually connected (not returning 401).
                    # Also check FeedServer directly in case feed_registry hasn't updated yet
                    # (race condition: registry updates asynchronously after WS connects).
                    from hub.feed_registry import get_ws_state as _gws
                    _ws_live = _gws('upstox').get('ws_connected', False)
                    if not _ws_live:
                        try:
                            from hub.feed_server import get_feed_server as _gfs
                            _fs = _gfs()
                            _df = getattr(_fs, '_dual_feed', None)
                            _up = getattr(_df, 'upstox', None) if _df else None
                            if _up and getattr(_up, 'is_connected', False):
                                _ws_live = True
                        except Exception:
                            pass
                    if _age_minutes < 30 and _ws_live:
                        logger.info(f"[Admin] Upstox token is fresh ({_age_minutes:.1f} min old) and WS connected — skipping re-login.")
                        return {"success": True, "message": f"Upstox token is fresh ({_age_minutes:.1f} min old). No re-login needed."}
                    elif _age_minutes < 30 and not _ws_live:
                        logger.info(f"[Admin] Upstox token appears fresh ({_age_minutes:.1f} min old) but WS is offline — attempting re-login.")
                except Exception as _ts_err:
                    logger.debug(f"[Admin] Could not parse Upstox token_issued_at ({_issued_at!r}): {_ts_err}")

            # Before attempting TOTP re-login, check if any Upstox broker instance has
            # a fresher token (auto-generated at bot startup). Using the broker token
            # avoids burning TOTP attempts and hitting the OTP rate limit.
            if not token:
                try:
                    _upstox_broker = db_fetchone(
                        "SELECT access_token_encrypted, token_updated_at FROM client_broker_instances "
                        "WHERE broker='upstox' AND access_token_encrypted IS NOT NULL "
                        "ORDER BY token_updated_at DESC LIMIT 1",
                        ()
                    )
                    if _upstox_broker and _upstox_broker['access_token_encrypted']:
                        _broker_token = decrypt_secret(_upstox_broker['access_token_encrypted'])
                        _broker_updated = _upstox_broker.get('token_updated_at', '')
                        _dp_updated = dp.get('updated_at', '')
                        if _broker_token and _broker_updated and (_broker_updated > _dp_updated or not _dp_updated):
                            token = _broker_token
                            logger.info(f"[Admin] Upstox: using fresher token from broker instance (updated {_broker_updated}).")
                            # Also update data_providers so FeedServer's re-init uses the fresh token
                            try:
                                db_execute(
                                    "UPDATE data_providers SET access_token_encrypted=?, updated_at=? WHERE provider='upstox'",
                                    (_upstox_broker['access_token_encrypted'], _broker_updated)
                                )
                                logger.info(f"[Admin] Upstox token updated in data_providers (synced from broker instance).")
                            except Exception as _sync_err:
                                logger.warning(f"[Admin] Could not sync Upstox token to data_providers: {_sync_err}")
                except Exception as _broker_tok_err:
                    logger.debug(f"[Admin] Could not fetch fresher Upstox token from broker instance: {_broker_tok_err}")

            # Only attempt TOTP re-login if no fresh token was found from broker
            if not token:
                from utils.auth_manager_upstox import handle_upstox_login_automated
                creds = {
                    "api_key": decrypt_secret(dp["api_key_encrypted"]),
                    "api_secret": decrypt_secret(dp.get("api_secret_encrypted", "")),
                    "user_id": decrypt_secret(dp.get("user_id_encrypted", "")),
                    "password": decrypt_secret(dp.get("password_encrypted", "")),
                    "totp": decrypt_secret(dp.get("totp_encrypted", "")),
                    # Pass the saved redirect_uri so the token exchange step matches what is
                    # registered in the Upstox Developer Portal exactly. Falls back to the
                    # internal Upstox URI if not yet recorded (see auth_manager_upstox.py).
                    "redirect_uri": dp.get("redirect_uri") or "",
                }
                _result = handle_upstox_login_automated(creds, return_error=True)
                token = (_result or {}).get("token")
                _upstox_error = (_result or {}).get("error")
                if token:
                    _sync_upstox_to_credentials(creds["api_key"], token, creds["api_secret"])

        elif provider == 'dhan':
            from utils.auth_manager_dhan import generate_dhan_token
            client_id  = decrypt_secret(dp.get("api_key_encrypted",    "") or "")
            pin        = decrypt_secret(dp.get("password_encrypted",    "") or "")
            totp_sec   = decrypt_secret(dp.get("totp_encrypted",        "") or "")
            missing = [f for f, v in [
                ("Client ID",    client_id),
                ("PIN",          pin),
                ("TOTP Secret",  totp_sec),
            ] if not v]
            if missing:
                return {"success": False,
                        "message": f"Dhan credentials incomplete — missing: {', '.join(missing)}. Save all fields first."}

            # Before attempting TOTP re-login, check if any Dhan broker instance has
            # a fresher token (auto-generated at bot startup). Using the broker token
            # avoids burning TOTP attempts and hitting the 2-minute rate limit.
            token = None
            try:
                _dhan_broker = db_fetchone(
                    "SELECT access_token_encrypted, token_updated_at FROM client_broker_instances "
                    "WHERE broker='dhan' AND access_token_encrypted IS NOT NULL "
                    "ORDER BY token_updated_at DESC LIMIT 1",
                    ()
                )
                if _dhan_broker and _dhan_broker['access_token_encrypted']:
                    _broker_token = decrypt_secret(_dhan_broker['access_token_encrypted'])
                    _broker_updated = _dhan_broker.get('token_updated_at', '')
                    _dp_updated = dp.get('updated_at', '')
                    if _broker_token and _broker_updated and (_broker_updated > _dp_updated or not _dp_updated):
                        token = _broker_token
                        logger.info(f"[Admin] Dhan: using fresher token from broker instance (updated {_broker_updated}).")
                        # Also update data_providers so FeedServer's re-init uses the fresh token
                        try:
                            db_execute(
                                "UPDATE data_providers SET access_token_encrypted=?, updated_at=? WHERE provider='dhan'",
                                (_dhan_broker['access_token_encrypted'], _broker_updated)
                            )
                            logger.info(f"[Admin] Dhan token updated in data_providers (synced from broker instance).")
                        except Exception as _sync_err:
                            logger.warning(f"[Admin] Could not sync Dhan token to data_providers: {_sync_err}")
            except Exception as _broker_tok_err:
                logger.debug(f"[Admin] Could not fetch fresher Dhan token from broker instance: {_broker_tok_err}")

            # Only attempt fresh token generation if no fresh token was found from broker
            if not token:
                _dhan_result = generate_dhan_token(
                    api_key=client_id,
                    client_id=client_id,
                    password=pin,
                    totp_secret=totp_sec,
                )
                token = _dhan_result['token']
                _dhan_error = _dhan_result['error']
            else:
                _dhan_error = None

        if token:
            enc_token = encrypt_secret(token)
            now = datetime.now(IST).isoformat()
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

            # After token refresh, ensure the WebSocket actually reconnects.
            # refresh_feed_credentials() only works when the WS is already running.
            # If it was offline (disabled at startup or task died), trigger FeedServer reconnect.
            try:
                from hub.feed_server import get_feed_server
                from hub.feed_registry import get_ws_state
                srv = get_feed_server()
                if srv._started and not get_ws_state(provider).get('ws_connected'):
                    await srv.reconnect_provider(provider)
                    logger.info(f"[Admin] FeedServer WebSocket reconnect triggered for {provider}.")
            except Exception as _srv_err:
                logger.warning(f"[Admin] Could not trigger FeedServer reconnect for {provider}: {_srv_err}")

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
                msg = f"Dhan: {_dhan_error}" if _dhan_error else "Dhan token generation failed. Verify your Client ID, PIN and TOTP secret."
            elif provider == 'upstox':
                msg = f"Upstox login failed: {_upstox_error}" if _upstox_error else "Upstox login failed. Check your User ID, Password, and TOTP secret."
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
            _dhan_error = None
            _upstox_error = None
            if provider == "upstox":
                # Skip re-login if a valid token exists, was issued within the last 30 minutes,
                # AND the WebSocket is currently live (token is actually working).
                _issued_at = dp.get("token_issued_at")
                _has_token = bool(dp.get("access_token_encrypted"))
                if _issued_at and _has_token:
                    try:
                        import pytz as _pytz
                        _IST = _pytz.timezone('Asia/Kolkata')
                        _issued_dt = datetime.fromisoformat(_issued_at)
                        if _issued_dt.tzinfo is None:
                            _issued_dt = _IST.localize(_issued_dt)
                        _age_minutes = (datetime.now(IST) - _issued_dt.astimezone(IST)).total_seconds() / 60
                        from hub.feed_registry import get_ws_state as _gws2
                        _ws_live2 = _gws2('upstox').get('ws_connected', False)
                        if not _ws_live2:
                            try:
                                from hub.feed_server import get_feed_server as _gfs2
                                _fs2 = _gfs2()
                                _df2 = getattr(_fs2, '_dual_feed', None)
                                _up2 = getattr(_df2, 'upstox', None) if _df2 else None
                                if _up2 and getattr(_up2, 'is_connected', False):
                                    _ws_live2 = True
                            except Exception:
                                pass
                        if _age_minutes < 30 and _ws_live2:
                            logger.info(f"[Admin] Upstox token is fresh ({_age_minutes:.1f} min old) and WS connected — skipping re-login.")
                            results[provider] = {"success": True, "message": f"Upstox token is fresh ({_age_minutes:.1f} min old). No re-login needed."}
                            continue
                        elif _age_minutes < 30 and not _ws_live2:
                            logger.info(f"[Admin] Upstox token appears fresh ({_age_minutes:.1f} min old) but WS offline — attempting re-login.")
                    except Exception as _ts_err:
                        logger.debug(f"[Admin] Could not parse Upstox token_issued_at ({_issued_at!r}): {_ts_err}")

                # Before attempting TOTP re-login, check if any Upstox broker instance has
                # a fresher token (auto-generated at bot startup). Avoids burning TOTP attempts.
                if not token:
                    try:
                        _upstox_broker = db_fetchone(
                            "SELECT access_token_encrypted, token_updated_at FROM client_broker_instances "
                            "WHERE broker='upstox' AND access_token_encrypted IS NOT NULL "
                            "ORDER BY token_updated_at DESC LIMIT 1",
                            ()
                        )
                        if _upstox_broker and _upstox_broker['access_token_encrypted']:
                            _broker_token = decrypt_secret(_upstox_broker['access_token_encrypted'])
                            _broker_updated = _upstox_broker.get('token_updated_at', '')
                            _dp_updated = dp.get('updated_at', '')
                            if _broker_token and _broker_updated and (_broker_updated > _dp_updated or not _dp_updated):
                                token = _broker_token
                                logger.info(f"[Admin] Upstox: using fresher token from broker instance (updated {_broker_updated}).")
                                # Also update data_providers so FeedServer's re-init uses the fresh token
                                try:
                                    db_execute(
                                        "UPDATE data_providers SET access_token_encrypted=?, updated_at=? WHERE provider='upstox'",
                                        (_upstox_broker['access_token_encrypted'], _broker_updated)
                                    )
                                    logger.info(f"[Admin] Upstox token updated in data_providers (synced from broker instance).")
                                except Exception as _sync_err:
                                    logger.warning(f"[Admin] Could not sync Upstox token to data_providers: {_sync_err}")
                    except Exception as _broker_tok_err:
                        logger.debug(f"[Admin] Could not fetch fresher Upstox token from broker instance: {_broker_tok_err}")

                if not token:
                    from utils.auth_manager_upstox import handle_upstox_login_automated
                    creds = {
                        "api_key": decrypt_secret(dp["api_key_encrypted"]),
                        "api_secret": decrypt_secret(dp.get("api_secret_encrypted", "")),
                        "user_id": decrypt_secret(dp.get("user_id_encrypted", "")),
                        "password": decrypt_secret(dp.get("password_encrypted", "")),
                        "totp": decrypt_secret(dp.get("totp_encrypted", "")),
                        "redirect_uri": dp.get("redirect_uri") or "",
                    }
                    _result = handle_upstox_login_automated(creds, return_error=True)
                    token = (_result or {}).get("token")
                    _upstox_error = (_result or {}).get("error")
                    if token:
                        _sync_upstox_to_credentials(creds["api_key"], token, creds["api_secret"])

            elif provider == "dhan":
                from utils.auth_manager_dhan import generate_dhan_token
                _client_id = decrypt_secret(dp.get("api_key_encrypted",  "") or "")
                _pin       = decrypt_secret(dp.get("password_encrypted",  "") or "")
                _totp_sec  = decrypt_secret(dp.get("totp_encrypted",      "") or "")
                _missing = [f for f, v in [
                    ("Client ID",   _client_id),
                    ("PIN",         _pin),
                    ("TOTP Secret", _totp_sec),
                ] if not v]
                if _missing:
                    results[provider] = {"success": False,
                                         "message": f"Dhan credentials incomplete — missing: {', '.join(_missing)}."}
                    continue

                # Before attempting TOTP re-login, check if any Dhan broker instance has
                # a fresher token (auto-generated at bot startup). Avoids the 2-minute rate limit.
                token = None
                try:
                    _dhan_broker = db_fetchone(
                        "SELECT access_token_encrypted, token_updated_at FROM client_broker_instances "
                        "WHERE broker='dhan' AND access_token_encrypted IS NOT NULL "
                        "ORDER BY token_updated_at DESC LIMIT 1",
                        ()
                    )
                    if _dhan_broker and _dhan_broker['access_token_encrypted']:
                        _broker_token = decrypt_secret(_dhan_broker['access_token_encrypted'])
                        _broker_updated = _dhan_broker.get('token_updated_at', '')
                        _dp_updated = dp.get('updated_at', '')
                        if _broker_token and _broker_updated and (_broker_updated > _dp_updated or not _dp_updated):
                            token = _broker_token
                            logger.info(f"[Admin] Dhan: using fresher token from broker instance (updated {_broker_updated}).")
                            # Also update data_providers so FeedServer's re-init uses the fresh token
                            try:
                                db_execute(
                                    "UPDATE data_providers SET access_token_encrypted=?, updated_at=? WHERE provider='dhan'",
                                    (_dhan_broker['access_token_encrypted'], _broker_updated)
                                )
                                logger.info(f"[Admin] Dhan token updated in data_providers (synced from broker instance).")
                            except Exception as _sync_err:
                                logger.warning(f"[Admin] Could not sync Dhan token to data_providers: {_sync_err}")
                except Exception as _broker_tok_err:
                    logger.debug(f"[Admin] Could not fetch fresher Dhan token from broker instance: {_broker_tok_err}")

                # Only generate fresh token if no fresh token was found from broker
                if not token:
                    _dhan_result = generate_dhan_token(
                        api_key=_client_id,
                        client_id=_client_id,
                        password=_pin,
                        totp_secret=_totp_sec,
                    )
                    token = _dhan_result['token']
                    _dhan_error = _dhan_result['error']
                else:
                    _dhan_error = None

            if token:
                enc_token = encrypt_secret(token)
                now = datetime.now(IST).isoformat()
                db_execute(
                    "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=?, token_issued_at=? WHERE provider=?",
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
                if provider == "dhan":
                    _fail_msg = f"Dhan: {_dhan_error}" if _dhan_error else "Dhan login returned no token."
                elif provider == "upstox":
                    _fail_msg = f"Upstox login failed: {_upstox_error}" if _upstox_error else "Upstox login returned no token."
                else:
                    _fail_msg = f"{provider.capitalize()} login returned no token."
                results[provider] = {"success": False, "message": _fail_msg}

        except Exception as e:
            logger.error(f"[Admin] connect-all error for {provider}: {e}")
            results[provider] = {"success": False, "message": str(e)}

    overall = all(v["success"] for v in results.values())
    return {"success": overall, "results": results}


@router.post("/data-providers")
async def update_data_provider(request: Request, body: ProviderConfigRequest, admin=Depends(require_admin)):
    try:
        now = datetime.now(IST).isoformat()

        # Build a dynamic SET clause — only update fields that have non-empty submitted values.
        # This prevents accidental credential wipes when the admin only changes one field.
        field_map = [
            ("api_key_encrypted",    body.api_key),
            ("api_secret_encrypted", body.api_secret),
            ("user_id_encrypted",    body.user_id),
            ("password_encrypted",   body.password),
            ("totp_encrypted",       body.totp),
        ]
        set_parts = []
        params = []
        for col, val in field_map:
            if val and val.strip():
                set_parts.append(f"{col}=?")
                params.append(encrypt_secret(val))

        if not set_parts:
            return {"success": False, "message": "No credentials provided to save."}

        set_parts.append("updated_at=?")
        params.append(now)
        set_parts.append("updated_by=?")
        params.append(admin["id"])

        if body.provider == 'dhan':
            set_parts.append("status='configured'")
            # For Dhan, 'api_secret' IS the access token when entered manually.
            # Mirror it to access_token_encrypted so provider_factory can read it.
            if body.api_secret and body.api_secret.strip():
                set_parts.append("access_token_encrypted=?")
                params.append(encrypt_secret(body.api_secret))
                set_parts.append("token_issued_at=?")
                params.append(datetime.now(IST).isoformat())

        # For Upstox, record the server's callback URL alongside credentials so Background
        # Connect always uses the exact redirect_uri registered in the Upstox Developer Portal.
        saved_redirect_uri = None
        if body.provider == 'upstox':
            raw_host = request.headers.get('host', '')
            proto = request.headers.get('x-forwarded-proto', 'http')
            if 'localhost' not in raw_host and '127.0.0.1' not in raw_host and proto != 'http':
                proto = 'https'
            _cb = f"{proto}://{raw_host}/auth/upstox/callback"
            # Upstox rejects HTTP redirect URIs — use google.com flow when on plain HTTP
            saved_redirect_uri = "https://www.google.com" if proto == 'http' else _cb
            set_parts.append("redirect_uri=?")
            params.append(saved_redirect_uri)
            logger.info(f"[Admin] Recorded Upstox redirect_uri: {saved_redirect_uri}")

        params.append(body.provider)
        db_execute(
            f"UPDATE data_providers SET {', '.join(set_parts)} WHERE provider=?",
            tuple(params)
        )

        logger.info(f"Admin updated global provider {body.provider} fields: {[f[0] for f in field_map if f[1]]}")
        result = {"success": True}
        if saved_redirect_uri:
            result["redirect_uri"] = saved_redirect_uri
        return result
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

            # redirect_uri must match exactly what was used in the original auth dialog.
            # Callers using the server-callback flow pass redirect_uri explicitly;
            # legacy google.com flows omit it and we fall back to the old hardcoded value.
            redirect_uri = body.redirect_uri or "https://www.google.com"

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
            now = datetime.now(IST).isoformat()
            # Upstox: always reset token_issued_at (daily token replaced)
            db_execute(
                "UPDATE data_providers SET access_token_encrypted=?, status='configured', updated_at=?, token_issued_at=? WHERE provider='upstox'",
                (enc_token, now, now)
            )
            return {"success": True, "message": "Upstox global token updated via manual code."}

        elif provider == 'dhan':
            # Dhan: new token being manually entered — always reset token_issued_at (30-day countdown restarts)
            enc_token = encrypt_secret(code)
            now = datetime.now(IST).isoformat()
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
    now = datetime.now(IST).isoformat()
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
    import hub.instance_manager
    instance_manager = hub.instance_manager.instance_manager
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
                exp = exp.replace(tzinfo=IST)
            if datetime.now(IST) > exp:
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
               u.full_name, u.phone_number, u.telegram_chat_id,
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
    import hub.instance_manager
    instance_manager = hub.instance_manager.instance_manager
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
        import hub.instance_manager
        instance_manager = hub.instance_manager.instance_manager
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


# ── Trade History CSV Export ─────────────────────────────────────────────────

@router.get("/clients/{client_id}/trades/export")
async def export_trade_history_csv(
    client_id: int,
    from_date: Optional[str] = Query(None, description="Start date YYYY-MM-DD"),
    to_date: Optional[str] = Query(None, description="End date YYYY-MM-DD"),
    admin=Depends(require_admin)
):
    """Export a client's trade history as a CSV file."""
    user = db_fetchone("SELECT id, username FROM users WHERE id=?", (client_id,))
    if not user:
        raise HTTPException(404, "Client not found")

    params = [client_id]
    date_clause = ""
    if from_date:
        date_clause += " AND date(closed_at) >= ?"
        params.append(from_date)
    if to_date:
        date_clause += " AND date(closed_at) <= ?"
        params.append(to_date)

    trades = db_fetchall(f"""
        SELECT closed_at, trade_type, direction, strike, instrument, broker,
               trading_mode, entry_price, exit_price, quantity,
               pnl_pts, pnl_rs, exit_reason, entry_index_price
        FROM trade_history
        WHERE client_id=? {date_clause}
        ORDER BY closed_at DESC
    """, params)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Date/Time", "Type", "Direction", "Strike", "Instrument", "Broker",
        "Mode", "Entry Price", "Exit Price", "Qty",
        "P&L (pts)", "P&L (₹)", "Exit Reason", "Index Price at Entry"
    ])
    for t in trades:
        writer.writerow([
            t["closed_at"], t["trade_type"], t["direction"], t["strike"],
            t["instrument"], t["broker"], t["trading_mode"],
            t["entry_price"], t["exit_price"], t["quantity"],
            t["pnl_pts"], t["pnl_rs"], t["exit_reason"], t["entry_index_price"]
        ])

    output.seek(0)
    filename = f"trades_{user['username']}_{from_date or 'all'}_{to_date or 'all'}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


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

    import hub.instance_manager
    instance_manager = hub.instance_manager.instance_manager
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
        import hub.instance_manager
        instance_manager = hub.instance_manager.instance_manager
        instance_manager.stop_all_for_client(req["client_id"])
        db_execute("UPDATE client_broker_instances SET status='idle', bot_pid=NULL WHERE client_id=? AND status='running'", (req["client_id"],))

    now = datetime.now(IST).isoformat()
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
    now = datetime.now(IST).isoformat()
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
                "exit_details": v3_extras.get('exit_details', []),
                "current_ts": data.get('updated_at', '')
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
        now = datetime.now(IST).isoformat()
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
    now = datetime.now(IST).isoformat()
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
    now = datetime.now(IST)
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
                    upd = upd.replace(tzinfo=IST)
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
                        issued = issued.replace(tzinfo=IST)
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
        conditions.append("al.action = ?")
        params.append(action)
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
        info["started_at"] = _dt.datetime.fromtimestamp(started_at, tz=IST).isoformat()
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

    # Rust engine status
    try:
        from hub.sell_v3.rust_bridge import RUST_AVAILABLE
        info["rust_available"] = RUST_AVAILABLE
    except Exception:
        info["rust_available"] = False

    info["timestamp"] = _dt.datetime.now(IST).isoformat()
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


_VALID_THEMES = {"dark", "light", "midnight", "saffron"}

@router.post("/platform-settings")
async def save_platform_settings(body: PlatformSettingsBatch, admin=Depends(require_admin)):
    """Upsert platform settings. Pass empty string to clear a key."""
    if "default_theme" in body.settings and body.settings["default_theme"] not in _VALID_THEMES:
        raise HTTPException(400, f"Invalid theme. Allowed values: {sorted(_VALID_THEMES)}")
    now = _dt.datetime.now(IST).isoformat()
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
    _audit(admin["id"], admin["role"], "platform_settings_update", 0, {
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


@router.post("/platform-settings/test-telegram")
async def test_telegram(admin=Depends(require_admin)):
    """Send a test Telegram message to the admin's registered Chat ID."""
    admin_row = db_fetchone("SELECT telegram_chat_id, username FROM users WHERE id=?", (admin["id"],))
    chat_id = (admin_row.get("telegram_chat_id") or "").strip() if admin_row else ""
    if not chat_id:
        raise HTTPException(400, "No Telegram Chat ID set for your admin account. Add it in Admin profile or users table.")
    from utils.notifier import send_telegram
    ok = send_telegram(
        chat_id,
        "✅ <b>AlgoSoft — Test Message</b>\n"
        "Your Telegram bot is correctly configured.\n"
        "Clients will receive live trade alerts and day-end summaries at their Chat IDs.",
        force=True
    )
    if ok:
        return {"success": True, "message": f"Test message sent to Chat ID {chat_id}"}
    raise HTTPException(
        500,
        "Failed to send. Check: (1) Bot token is saved correctly, "
        "(2) You have sent /start to the bot on Telegram first. "
        "(Note: test messages bypass the global alert toggle.)"
    )


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


@router.get("/clients/{client_id}/logs")
async def list_client_logs(client_id: int, admin=Depends(require_admin)):
    """List all log files on disk that belong to this client."""
    user = db_fetchone("SELECT id, username FROM users WHERE id=? AND role='client'", (client_id,))
    if not user:
        raise HTTPException(404, "Client not found.")
    log_dir = os.path.join(os.getcwd(), "logs")
    log_files = []
    if os.path.isdir(log_dir):
        prefix = f"client_{client_id}_"
        for fname in os.listdir(log_dir):
            if fname.startswith(prefix) and fname.endswith(".log"):
                fpath = os.path.join(log_dir, fname)
                try:
                    stat = os.stat(fpath)
                    broker = fname[len(prefix):-len(".log")]
                    log_files.append({
                        "filename": fname,
                        "broker": broker,
                        "size_bytes": stat.st_size,
                        "size_human": _human_size(stat.st_size),
                        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                    })
                except Exception:
                    pass
    log_files.sort(key=lambda x: x["modified_at"], reverse=True)
    return {"client_id": client_id, "username": user["username"], "logs": log_files}


@router.get("/clients/{client_id}/logs/download")
async def download_client_log(
    client_id: int,
    broker: str = Query(...),
    admin=Depends(require_admin),
):
    """Stream/download a client's full log file."""
    user = db_fetchone("SELECT id FROM users WHERE id=? AND role='client'", (client_id,))
    if not user:
        raise HTTPException(404, "Client not found.")
    safe_broker = broker.replace("/", "").replace("..", "").replace("\\", "").strip()
    log_path = os.path.join(os.getcwd(), "logs", f"client_{client_id}_{safe_broker}.log")
    if not os.path.isfile(log_path):
        raise HTTPException(404, "Log file not found.")

    def _iter():
        with open(log_path, "rb") as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk

    filename = f"client_{client_id}_{safe_broker}.log"
    return StreamingResponse(
        _iter(),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/clients/{client_id}/logs/tail")
async def tail_client_log(
    client_id: int,
    broker: str = Query(...),
    lines: int = Query(default=100, ge=10, le=2000),
    admin=Depends(require_admin),
):
    """Return the last N lines of a client log file for in-dashboard viewing."""
    from collections import deque
    user = db_fetchone("SELECT id FROM users WHERE id=? AND role='client'", (client_id,))
    if not user:
        raise HTTPException(404, "Client not found.")
    safe_broker = broker.replace("/", "").replace("..", "").replace("\\", "").strip()
    log_path = os.path.join(os.getcwd(), "logs", f"client_{client_id}_{safe_broker}.log")
    if not os.path.isfile(log_path):
        return {"lines": [], "broker": safe_broker, "total_lines": 0, "size_bytes": 0}
    try:
        dq: deque = deque(maxlen=lines)
        total = 0
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                dq.append(line.rstrip("\n"))
                total += 1
        return {
            "lines": list(dq),
            "broker": safe_broker,
            "total_lines": total,
            "size_bytes": os.path.getsize(log_path),
        }
    except Exception as e:
        raise HTTPException(500, f"Error reading log file: {e}")


@router.get("/elastic-ips")
async def list_elastic_ips(admin=Depends(require_admin)):
    """Return all EC2 Elastic IPs from AWS, annotated with which client is using each one."""
    aws_key = os.environ.get("AWS_ACCESS_KEY_ID", "").strip()
    aws_secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "").strip()
    aws_region = (os.environ.get("AWS_DEFAULT_REGION") or os.environ.get("AWS_REGION") or "").strip()

    if not aws_key or not aws_secret:
        return {
            "success": False,
            "error": "AWS credentials not configured. Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY secrets.",
            "ips": [],
        }

    # Build a map of ip → list[client info] so shared IPs show all users
    assigned_rows = db_fetchall(
        "SELECT id, username, email, static_ip FROM users WHERE role='client' AND static_ip IS NOT NULL AND static_ip != ''"
    )
    assigned_map: dict[str, list] = {}
    for row in assigned_rows:
        ip_key = (row.get("static_ip") or "").strip()
        if not ip_key:
            continue
        assigned_map.setdefault(ip_key, []).append(
            {"id": row["id"], "username": row["username"], "email": row["email"]}
        )

    try:
        import boto3
        kwargs: dict = {"aws_access_key_id": aws_key, "aws_secret_access_key": aws_secret}
        if aws_region:
            kwargs["region_name"] = aws_region
        ec2 = boto3.client("ec2", **kwargs)
        response = ec2.describe_addresses()
        addresses = response.get("Addresses", [])
    except Exception as e:
        logger.warning(f"[Admin] Failed to fetch EC2 Elastic IPs: {e}")
        return {
            "success": False,
            "error": "Could not retrieve Elastic IPs from AWS. Check that your credentials are correct and have EC2 read permissions.",
            "ips": [],
        }

    result = []
    for addr in addresses:
        public_ip = addr.get("PublicIp", "")
        allocation_id = addr.get("AllocationId", "")
        instance_id = addr.get("InstanceId")
        clients_using = assigned_map.get(public_ip)  # list or None
        result.append({
            "public_ip": public_ip,
            "allocation_id": allocation_id,
            "instance_id": instance_id,
            "ec2_associated": bool(instance_id),
            "in_use_by": clients_using,        # list[{id,username,email}] or None
            "shared_count": len(clients_using) if clients_using else 0,
        })

    result.sort(key=lambda x: (x["shared_count"] == 0, x["public_ip"]))
    return {"success": True, "ips": result}


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
    ip_to_save = body.static_ip.strip() or None
    # Sharing the same EIP across clients is explicitly allowed — all major Indian
    # brokers support multiple accounts whitelisting the same source IP.
    # Count how many OTHER clients already use this IP so we can surface the info.
    shared_with = 0
    if ip_to_save:
        shared_rows = db_fetchall(
            "SELECT id FROM users WHERE static_ip=? AND id != ? AND role='client'",
            (ip_to_save, client_id),
        )
        shared_with = len(shared_rows)
    db_execute("UPDATE users SET static_ip=? WHERE id=?", (ip_to_save, client_id))
    _audit(admin["id"], admin["role"], "client_static_ip_update", client_id, {"static_ip": body.static_ip})
    return {"success": True, "shared_with": shared_with}


# ── Broker Credential Management ─────────────────────────────────────────────

@router.get("/clients/{client_id}/broker-credentials-status")
async def get_broker_credentials_status(client_id: int, admin=Depends(require_admin)):
    """
    Return boolean flags indicating which credential fields are set for the
    client's most-recently configured broker instance. No secret values
    are returned — only True/False per field.
    """
    if not db_fetchone("SELECT id FROM users WHERE id=? AND role='client'", (client_id,)):
        raise HTTPException(404, "Client not found")
    row = db_fetchone(
        """SELECT broker, api_key_encrypted, api_secret_encrypted,
                  broker_user_id_encrypted, password_encrypted, totp_encrypted
           FROM client_broker_instances
           WHERE client_id=? AND status != 'removed'
           ORDER BY id DESC LIMIT 1""",
        (client_id,),
    )
    if not row:
        return {"broker": None, "api_key": False, "api_secret": False,
                "broker_user_id": False, "password": False, "totp": False}
    return {
        "broker":         row.get("broker"),
        "api_key":        bool(row.get("api_key_encrypted")),
        "api_secret":     bool(row.get("api_secret_encrypted")),
        "broker_user_id": bool(row.get("broker_user_id_encrypted")),
        "password":       bool(row.get("password_encrypted")),
        "totp":           bool(row.get("totp_encrypted")),
    }


class BrokerCredentialsBody(BaseModel):
    broker: str
    api_key: str = ""
    api_secret: str = ""
    broker_user_id: str = ""
    password: str = ""
    totp: str = ""


@router.put("/clients/{client_id}/broker-credentials")
async def put_broker_credentials(client_id: int, body: BrokerCredentialsBody,
                                  admin=Depends(require_admin)):
    """
    Admin-initiated upsert of broker credentials for a client.
    Only fields that are non-empty strings are updated; existing encrypted
    values are preserved for blank fields (same semantics as client_api.py).
    """
    _KNOWN_BROKERS = {
        "angelone", "dhan", "zerodha", "aliceblue", "groww", "fyers", "upstox",
    }
    if not db_fetchone("SELECT id FROM users WHERE id=? AND role='client'", (client_id,)):
        raise HTTPException(404, "Client not found")
    if not body.broker:
        raise HTTPException(400, "broker is required")
    if body.broker not in _KNOWN_BROKERS:
        raise HTTPException(400, f"Unknown broker '{body.broker}'. Must be one of: {sorted(_KNOWN_BROKERS)}")

    existing = db_fetchone(
        """SELECT api_key_encrypted, api_secret_encrypted, broker_user_id_encrypted,
                  password_encrypted, totp_encrypted, trading_mode, instrument,
                  quantity, strategy_version
           FROM client_broker_instances
           WHERE client_id=? AND broker=?""",
        (client_id, body.broker),
    )

    def _enc(val: str, fallback):
        return encrypt_secret(val) if val.strip() else fallback

    enc_key    = _enc(body.api_key,        existing["api_key_encrypted"]        if existing else None)
    enc_secret = _enc(body.api_secret,     existing["api_secret_encrypted"]     if existing else None)
    enc_uid    = _enc(body.broker_user_id, existing["broker_user_id_encrypted"] if existing else None)
    enc_pwd    = _enc(body.password,       existing["password_encrypted"]       if existing else None)
    enc_totp   = _enc(body.totp,           existing["totp_encrypted"]           if existing else None)

    # Preserve non-credential settings from any existing row
    mode     = (existing or {}).get("trading_mode", "paper")
    instr    = (existing or {}).get("instrument", "NIFTY")
    qty      = (existing or {}).get("quantity", 1)
    strat    = (existing or {}).get("strategy_version", "v1")

    db_execute(
        """INSERT INTO client_broker_instances
             (client_id, broker, api_key_encrypted, api_secret_encrypted,
              broker_user_id_encrypted, password_encrypted, totp_encrypted,
              trading_mode, instrument, quantity, strategy_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(client_id, broker) DO UPDATE SET
             api_key_encrypted        = excluded.api_key_encrypted,
             api_secret_encrypted     = excluded.api_secret_encrypted,
             broker_user_id_encrypted = excluded.broker_user_id_encrypted,
             password_encrypted       = excluded.password_encrypted,
             totp_encrypted           = excluded.totp_encrypted,
             instrument               = excluded.instrument""",
        (client_id, body.broker, enc_key, enc_secret, enc_uid,
         enc_pwd, enc_totp, mode, instr, qty, strat),
    )

    fields_updated = [f for f, v in [
        ("api_key", body.api_key), ("api_secret", body.api_secret),
        ("broker_user_id", body.broker_user_id),
        ("password", body.password), ("totp", body.totp),
    ] if v.strip()]

    _audit(admin["id"], admin["role"], "admin_broker_credentials_update", client_id, {
        "broker": body.broker,
        "fields_updated": fields_updated,
    })

    return {
        "success": True,
        "message": (
            f"{body.broker.capitalize()} credentials saved by admin "
            f"({', '.join(fields_updated) or 'no fields changed'})."
        ),
    }


@router.get("/clients/{client_id}/risk-overrides")
async def get_client_risk_overrides(client_id: int, admin=Depends(require_admin)):
    """Return client's full risk & money management parameters from active broker instance."""
    inst = db_fetchone(
        """SELECT id, client_strategy_overrides, daily_loss_limit, trading_locked_until,
                  capital_allocated, max_position_size, max_open_positions, max_daily_trades,
                  per_trade_loss_limit, max_drawdown_pct, risk_per_trade_pct,
                  daily_pnl, daily_trade_count, pnl_reset_date
           FROM client_broker_instances WHERE client_id=? ORDER BY id DESC LIMIT 1""",
        (client_id,)
    )
    if not inst:
        return {
            "instance_id": None,
            "overrides": {},
            "daily_loss_limit": 0,
            "trading_locked_until": None,
            "capital_allocated": 0,
            "max_position_size": 1,
            "max_open_positions": 1,
            "max_daily_trades": 0,
            "per_trade_loss_limit": 0,
            "max_drawdown_pct": 0,
            "risk_per_trade_pct": 1.0,
            "daily_pnl": 0,
            "daily_trade_count": 0,
            "pnl_reset_date": None,
        }
    overrides = {}
    try:
        if inst.get("client_strategy_overrides"):
            overrides = json.loads(inst["client_strategy_overrides"])
    except Exception:
        pass
    return {
        "instance_id": inst.get("id"),
        "overrides": overrides,
        "daily_loss_limit":    inst.get("daily_loss_limit") or 0,
        "trading_locked_until": inst.get("trading_locked_until"),
        "capital_allocated":   inst.get("capital_allocated") or 0,
        "max_position_size":   inst.get("max_position_size") or 1,
        "max_open_positions":  inst.get("max_open_positions") or 1,
        "max_daily_trades":    inst.get("max_daily_trades") or 0,
        "per_trade_loss_limit": inst.get("per_trade_loss_limit") or 0,
        "max_drawdown_pct":    inst.get("max_drawdown_pct") or 0,
        "risk_per_trade_pct":  inst.get("risk_per_trade_pct") or 1.0,
        "daily_pnl":           inst.get("daily_pnl") or 0,
        "daily_trade_count":   inst.get("daily_trade_count") or 0,
        "pnl_reset_date":      inst.get("pnl_reset_date"),
    }


class RiskParamsBody(BaseModel):
    instance_id: int | None = None
    daily_loss_limit: float = 0
    capital_allocated: float = 0
    max_position_size: int = 1
    max_open_positions: int = 1
    max_daily_trades: int = 0
    per_trade_loss_limit: float = 0
    max_drawdown_pct: float = 0
    risk_per_trade_pct: float = 1.0
    lock_trading: bool | None = None
    unlock_trading: bool | None = None


@router.post("/clients/{client_id}/risk-params")
async def save_client_risk_params(client_id: int, body: RiskParamsBody,
                                  admin=Depends(require_admin)):
    """Save risk & money management parameters for a client's broker instance."""
    inst = db_fetchone(
        "SELECT id FROM client_broker_instances WHERE client_id=? ORDER BY id DESC LIMIT 1",
        (client_id,)
    ) if not body.instance_id else {"id": body.instance_id}

    if not inst:
        raise HTTPException(404, "No broker instance found for client. Connect a broker first.")

    inst_id = inst["id"]
    now_ist_str = datetime.now(IST).isoformat()

    lock_val = None
    if body.lock_trading is True:
        # Lock for 24 hours
        lock_val = (datetime.now(IST) + timedelta(hours=24)).isoformat()
    elif body.unlock_trading is True:
        lock_val = None
    else:
        existing = db_fetchone("SELECT trading_locked_until FROM client_broker_instances WHERE id=?", (inst_id,))
        lock_val = existing.get("trading_locked_until") if existing else None

    db_execute("""
        UPDATE client_broker_instances SET
            daily_loss_limit=?, capital_allocated=?, max_position_size=?,
            max_open_positions=?, max_daily_trades=?, per_trade_loss_limit=?,
            max_drawdown_pct=?, risk_per_trade_pct=?, trading_locked_until=?
        WHERE id=?
    """, (
        body.daily_loss_limit, body.capital_allocated, body.max_position_size,
        body.max_open_positions, body.max_daily_trades, body.per_trade_loss_limit,
        body.max_drawdown_pct, body.risk_per_trade_pct, lock_val, inst_id
    ))
    _audit(admin["id"], admin["role"], "client_risk_params_update", inst_id, {
        "daily_loss_limit": body.daily_loss_limit,
        "capital_allocated": body.capital_allocated,
        "max_position_size": body.max_position_size,
        "lock_trading": body.lock_trading,
    })
    return {"success": True, "instance_id": inst_id}


@router.post("/clients/{client_id}/reset-daily-pnl")
async def reset_client_daily_pnl(client_id: int, admin=Depends(require_admin)):
    """Manually reset the daily P&L counter and trade count for a client."""
    inst = db_fetchone(
        "SELECT id FROM client_broker_instances WHERE client_id=? ORDER BY id DESC LIMIT 1",
        (client_id,)
    )
    if not inst:
        raise HTTPException(404, "No broker instance found.")
    db_execute(
        "UPDATE client_broker_instances SET daily_pnl=0, daily_trade_count=0, pnl_reset_date=? WHERE id=?",
        (datetime.now(IST).date().isoformat(), inst["id"])
    )
    _audit(admin["id"], admin["role"], "client_daily_pnl_reset", inst["id"], {})
    return {"success": True}


# ── Log File Management ────────────────────────────────────────────────────────

class LogCleanupRequest(BaseModel):
    max_backup_age_days: int = 7
    max_inactive_age_days: int = 14
    dry_run: bool = False

    @field_validator("max_backup_age_days", "max_inactive_age_days")
    @classmethod
    def _must_be_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("must be at least 1 day")
        return v


@router.get("/logs/disk-usage")
async def get_log_disk_usage(admin=Depends(require_admin)):
    """Return a summary of current disk usage in the logs/ directory."""
    from utils.log_cleanup import get_log_disk_usage as _usage
    return _usage()


@router.post("/logs/cleanup")
async def trigger_log_cleanup(body: LogCleanupRequest, admin=Depends(require_admin)):
    """
    Manually trigger a log file cleanup.

    Deletes rotated backup files older than max_backup_age_days and primary log files
    for inactive clients that have not been touched in max_inactive_age_days.
    Set dry_run=true to preview what would be removed without deleting anything.
    """
    from utils.log_cleanup import cleanup_old_logs
    result = cleanup_old_logs(
        max_backup_age_days=body.max_backup_age_days,
        max_inactive_age_days=body.max_inactive_age_days,
        dry_run=body.dry_run,
    )
    _audit(admin["id"], admin["role"], "log_cleanup", None, {
        "dry_run": body.dry_run,
        "deleted_count": len(result["deleted"]),
        "max_backup_age_days": body.max_backup_age_days,
        "max_inactive_age_days": body.max_inactive_age_days,
    })
    return {"success": True, **result}


# ── Global merged log stream ───────────────────────────────────────────────────

_LOG_LINE_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[,.]?\d*)\s+'
    r'(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\b'
)


@router.get("/logs/all")
async def global_logs_all(
    lines: int = Query(default=200, ge=10, le=2000,
                       description="Last N lines to read from each client log file"),
    admin=Depends(require_admin),
):
    """
    Read the last `lines` entries from every client_{id}_{broker}.log,
    merge them all, sort by timestamp descending, and return the combined set.
    Reading N lines per-file ensures every client is fairly represented even
    when one log is much chattier than others — the result size is at most
    N × (number of log files).
    Each entry: ts, level, client_id, broker, message (+ text for compat).
    """
    from collections import deque

    log_dir = os.path.join(os.getcwd(), "logs")
    entries: list[dict] = []

    if os.path.isdir(log_dir):
        for fname in os.listdir(log_dir):
            # Match pattern: client_<id>_<broker>.log
            if not (fname.startswith("client_") and fname.endswith(".log")):
                continue
            parts = fname[len("client_"):-len(".log")].split("_", 1)
            if len(parts) != 2:
                continue
            try:
                cid = int(parts[0])
            except ValueError:
                continue
            broker = parts[1]
            fpath = os.path.join(log_dir, fname)
            try:
                dq: deque = deque(maxlen=lines)
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for raw in f:
                        dq.append(raw.rstrip("\n"))
                for raw_line in dq:
                    m = _LOG_LINE_RE.match(raw_line)
                    ts = m.group(1).replace(",", ".") if m else ""
                    lvl = m.group(2) if m else "INFO"
                    if lvl == "WARNING":
                        lvl = "WARN"
                    entries.append({
                        "ts": ts,
                        "level": lvl,
                        "client_id": cid,
                        "broker": broker,
                        "message": raw_line,   # canonical field name
                        "text": raw_line,       # backward-compat alias
                    })
            except Exception:
                pass

    # Sort by timestamp descending; entries without a ts sort to the bottom
    entries.sort(key=lambda e: e["ts"], reverse=True)
    return {"entries": entries, "total": len(entries)}


# ── IP-conflict detection ──────────────────────────────────────────────────────


@router.get("/ip-conflicts")
async def get_ip_conflicts(
    hours: int = Query(default=6, ge=1, le=72),
    admin=Depends(require_admin),
):
    """
    Return broker instances whose static-IP binding check failed within the
    last `hours` hours, joined with the owning user's username.
    """
    rows = db_fetchall(
        """
        SELECT cbi.id, cbi.client_id, cbi.broker,
               cbi.ip_last_failed_at,
               u.username
        FROM client_broker_instances cbi
        JOIN users u ON u.id = cbi.client_id
        WHERE cbi.ip_last_failed_at IS NOT NULL
          AND datetime(cbi.ip_last_failed_at) >= datetime('now', ? || ' hours')
        ORDER BY cbi.ip_last_failed_at DESC
        """,
        (f"-{hours}",),
    )
    conflicts = [dict(r) for r in rows] if rows else []
    return {"conflicts": conflicts, "hours": hours}


# ── Crash alerts ──────────────────────────────────────────────────────────────


@router.get("/crash-alerts")
async def get_crash_alerts(admin=Depends(require_admin)):
    """Return broker instances that have crashed (status='crashed' or stopped unexpectedly)."""
    rows = db_fetchall(
        """
        SELECT cbi.id, cbi.client_id, cbi.broker, cbi.status,
               cbi.last_heartbeat, u.username
        FROM client_broker_instances cbi
        JOIN users u ON u.id = cbi.client_id
        WHERE cbi.status IN ('crashed', 'error')
        ORDER BY cbi.last_heartbeat DESC
        """,
        (),
    )
    crashed = []
    for r in (rows or []):
        crashed.append({
            "id": r["id"],
            "client_id": r["client_id"],
            "username": r["username"],
            "broker": r["broker"],
            "status": r["status"],
            "crashed_at": r["last_heartbeat"],
        })
    return {"crashed": crashed}


# ── Bulk operations ───────────────────────────────────────────────────────────


@router.post("/bulk-action")
async def bulk_action(request: Request, admin=Depends(require_admin)):
    """
    Bulk admin operations on multiple clients.
    Body: {action: 'activate'|'deactivate'|'push_strategy', client_ids: [...], payload: {...}}
    """
    body = await request.json()
    action = body.get("action")
    client_ids = body.get("client_ids") or []
    payload = body.get("payload") or {}

    if not action or not client_ids:
        raise HTTPException(status_code=400, detail="action and client_ids required")
    if action not in ("activate", "deactivate", "push_strategy"):
        raise HTTPException(status_code=400, detail="Invalid action")

    results = {"ok": [], "failed": []}
    for cid in client_ids:
        try:
            if action == "activate":
                db_execute("UPDATE users SET status='active' WHERE id=?", (cid,))
                results["ok"].append(cid)
            elif action == "deactivate":
                db_execute("UPDATE users SET status='inactive' WHERE id=?", (cid,))
                results["ok"].append(cid)
            elif action == "push_strategy":
                import json as _json
                overrides_json = _json.dumps(payload)
                db_execute(
                    "UPDATE client_broker_instances SET client_strategy_overrides=? WHERE client_id=?",
                    (overrides_json, cid),
                )
                results["ok"].append(cid)
        except Exception as e:
            results["failed"].append({"id": cid, "error": str(e)})

    return {"action": action, "results": results}
