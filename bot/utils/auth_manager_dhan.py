import re
import time
import requests
from dhanhq import dhanhq
from utils.logger import logger
from datetime import datetime, timezone, timedelta

_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE
)


def is_dhan_api_key_mode(credentials: dict) -> bool:
    """
    Returns True when TOTP secret is stored — meaning fully automated background
    token generation is possible via auth.dhan.co/app/generateAccessToken.

    Requires: Client ID (api_key), PIN (password), TOTP Secret (totp_secret or totp).
    """
    totp_secret = (
        credentials.get('totp_secret') or
        credentials.get('totp') or ''
    ).strip()
    password = (credentials.get('password') or credentials.get('pin') or '').strip()
    api_key   = (credentials.get('api_key') or credentials.get('client_id') or '').strip()
    return bool(api_key and password and totp_secret)


def generate_dhan_token(api_key: str, client_id: str, password: str,
                        totp_secret: str = '') -> dict:
    """
    Generates a fresh Dhan access token using the official auth endpoint.

    Endpoint: POST https://auth.dhan.co/app/generateAccessToken
    Body (form-encoded): dhanClientId, pin, totp
    Requires TOTP to be enabled for the account.

    Returns a dict: {'token': str, 'error': None} on success,
                    {'token': None, 'error': '<Dhan error message>'} on failure.

    NOTE: `api_key` parameter retained for API compatibility — client_id is
    used as dhanClientId.
    """
    _fail = lambda msg: {'token': None, 'error': msg}

    totp_code = ''
    if totp_secret:
        try:
            import pyotp
            totp_code = pyotp.TOTP(totp_secret.replace(' ', '')).now()
        except Exception as e:
            logger.warning(f"[Dhan] TOTP generation failed: {e}")

    if not totp_code:
        logger.error("[Dhan] TOTP code is required for background token generation.")
        return _fail("TOTP code could not be generated. Check your TOTP secret.")

    login_id = client_id or api_key

    def _attempt(code: str) -> dict:
        """Single POST attempt. Returns the standard {'token', 'error'} dict."""
        try:
            resp = requests.post(
                'https://auth.dhan.co/app/generateAccessToken',
                data={
                    'dhanClientId': login_id,
                    'pin': password,
                    'totp': code,
                },
                headers={'Content-Type': 'application/x-www-form-urlencoded'},
                timeout=15,
            )
            try:
                data = resp.json()
            except Exception:
                logger.error(
                    f"[Dhan] Token response not JSON (HTTP {resp.status_code}): {resp.text[:300]}"
                )
                return _fail(f"Dhan returned an unexpected response (HTTP {resp.status_code}).")

            if resp.status_code == 200:
                token = data.get('accessToken') or data.get('access_token')
                if token:
                    logger.info(f"[Dhan] Access token generated successfully for {login_id}.")
                    return {'token': token, 'error': None}
                dhan_msg = data.get('message') or data.get('errorMessage') or str(data)
                logger.error(f"[Dhan] Token response missing accessToken field: {data}")
                return _fail(dhan_msg)
            else:
                dhan_msg = data.get('message') or data.get('errorMessage') or resp.text[:200]
                logger.error(
                    f"[Dhan] Token generation failed for {login_id}: "
                    f"HTTP {resp.status_code} — {dhan_msg}"
                )
                return _fail(dhan_msg)

        except Exception as e:
            logger.error(f"[Dhan] Token generation error for {login_id}: {e}")
            return _fail("Could not reach Dhan servers. Check your internet connection and try again.")

    logger.info(f"[Dhan] Generating access token for client {login_id} …")
    result = _attempt(totp_code)

    # Retry once if Dhan rejected the TOTP (race at 30s window boundary)
    if result['error'] and 'totp' in result['error'].lower():
        logger.warning(
            f"[Dhan] TOTP rejected for {login_id} — waiting 1 s for next window, retrying …"
        )
        time.sleep(1)
        try:
            import pyotp
            fresh_totp = pyotp.TOTP(totp_secret.replace(' ', '')).now()
        except Exception as te:
            logger.error(f"[Dhan] TOTP regeneration failed on retry: {te}")
            return result  # return original error
        logger.info(f"[Dhan] Retrying token generation for {login_id} with fresh TOTP …")
        result = _attempt(fresh_totp)

    return result


def renew_dhan_token(access_token: str, client_id: str) -> str | None:
    """
    Renews an active 24-hour Dhan access token for another 24 hours.
    Only works on tokens generated via Dhan Web / generateAccessToken.
    Returns the new token string, or None on failure.

    Endpoint: POST https://api.dhan.co/v2/RenewToken
    """
    try:
        logger.info(f"[Dhan] Renewing token for {client_id} …")
        resp = requests.post(
            'https://api.dhan.co/v2/RenewToken',
            headers={
                'access-token': access_token,
                'dhanClientId': client_id,
                'Content-Type': 'application/json',
            },
            timeout=15,
        )
        if resp.status_code == 200:
            try:
                data = resp.json()
                new_token = data.get('accessToken') or data.get('access_token') or access_token
                logger.info(f"[Dhan] Token renewed successfully for {client_id}.")
                return new_token
            except Exception:
                logger.error(f"[Dhan] Renew response not JSON: {resp.text[:200]}")
        else:
            logger.warning(f"[Dhan] Renew failed for {client_id}: HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"[Dhan] Token renewal error: {e}")
    return None


def handle_dhan_login(credentials, config_manager=None):
    """
    Initialises and returns a dhanhq client instance.

    In API Key mode the generated/stored access_token (from DB) is used.
    In Direct Token mode the api_secret (manual token) is used.
    """
    if isinstance(credentials, str):
        client_id = config_manager.get_credential(credentials, 'client_id', fallback='')
        access_token = config_manager.get_credential(credentials, 'access_token', fallback=None)
    else:
        if is_dhan_api_key_mode(credentials):
            client_id = (
                credentials.get('broker_user_id') or
                credentials.get('client_id') or
                credentials.get('api_key')
            )
            access_token = credentials.get('access_token')
        else:
            client_id = credentials.get('api_key') or credentials.get('client_id')
            access_token = credentials.get('access_token') or credentials.get('api_secret')

    if not access_token:
        logger.warning('[Dhan] Login: access token not found.')
        return None
    if not client_id:
        logger.warning('[Dhan] Login: client ID not found.')
        client_id = ''

    try:
        try:
            from dhanhq import DhanContext
        except ImportError:
            DhanContext = None
        dhan = (dhanhq(DhanContext(client_id, access_token))
                if DhanContext else dhanhq(client_id, access_token))
        logger.info(f'[Dhan] Client initialised for {client_id}.')
        return dhan
    except Exception as e:
        logger.error(f'[Dhan] Failed to initialise client: {e}')
        return None


def _estimate_dhan_token_expiry(token_updated_at: str, api_key_mode: bool = False) -> dict:
    """
    Estimates remaining validity of the Dhan access token.

    Tokens generated via generateAccessToken have 24-hour validity.
    Manual/OAuth tokens may be up to 30 days.
    """
    result = {'hours_remaining': None, 'days_remaining': None,
              'is_valid': False, 'warn_soon': False}
    if not token_updated_at:
        return result
    try:
        updated = datetime.fromisoformat(token_updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        elapsed_seconds = (now - updated).total_seconds()

        if api_key_mode:
            total_seconds = 24 * 3600
            remaining_seconds = total_seconds - elapsed_seconds
            hours_remaining = max(0, remaining_seconds / 3600)
            result['hours_remaining'] = round(hours_remaining, 1)
            result['is_valid'] = remaining_seconds > 0
            result['warn_soon'] = 0 < remaining_seconds <= 2 * 3600
        else:
            days_elapsed = elapsed_seconds / 86400
            days_remaining = 30 - days_elapsed
            result['days_remaining'] = max(0, round(days_remaining, 1))
            result['is_valid'] = days_remaining > 0
            result['warn_soon'] = 0 < days_remaining <= 5

    except Exception as e:
        logger.warning(f'[Dhan] Could not parse token date: {e}')
    return result


def handle_dhan_login_automated(credentials):
    """
    Validates existing Dhan access token via a lightweight API call.
    Returns the access token if still valid, None otherwise.
    """
    try:
        client_id = credentials.get('api_key') or credentials.get('client_id')
        access_token = (
            credentials.get('access_token') or
            credentials.get('api_secret') or
            credentials.get('token')
        )

        if not access_token:
            logger.warning(f'[Dhan] Automated: No access token for client {client_id}')
            return None

        logger.info(f'[Dhan] Validating token for {client_id} …')
        resp = requests.get(
            'https://api.dhan.co/v2/fundlimit',
            headers={
                'access-token': access_token,
                'client-id': client_id or '',
                'Content-Type': 'application/json',
            },
            timeout=10,
        )

        if resp.status_code == 200:
            logger.info(f'[Dhan] Token VALID for {client_id}.')
            return access_token
        else:
            # ANY non-200 response (401, 403, 400 DH-906, etc.) means the token
            # is rejected.  Previously 400s were silently returned as "valid"
            # which let an invalid token slip through and broke contract loading.
            logger.warning(
                f'[Dhan] Token check returned {resp.status_code} for {client_id}: '
                f'{resp.text[:200]}'
            )
            return None

    except Exception as e:
        logger.error(f'[Dhan] Automated login error: {e}')
        return None
