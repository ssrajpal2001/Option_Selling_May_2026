import re
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
    Returns True when the stored api_secret is a Dhan API Secret (UUID format)
    rather than a 24-hour/30-day access token.

    API Key mode allows fully automated token generation; the API Secret is a
    permanent credential from DhanHQ → DhanHQ Trading APIs → API Key tab.
    """
    api_secret = (credentials.get('api_secret') or '').strip()
    return bool(api_secret and _UUID_RE.match(api_secret))


def generate_dhan_token(api_key: str, client_id: str, password: str,
                        totp_secret: str = '') -> str | None:
    """
    Generates a fresh Dhan access token using the DhanHQ API.

    Endpoint: POST https://api.dhan.co/token
    Required: applicationId (api_key), loginId (client_id), password
    Optional: 2FA (TOTP code derived from totp_secret)

    Returns the new access token string, or None on failure.
    """
    totp_code = ''
    if totp_secret:
        try:
            import pyotp
            totp_code = pyotp.TOTP(totp_secret.replace(' ', '')).now()
        except Exception as e:
            logger.warning(f"[Dhan] TOTP generation failed: {e}")

    payload: dict = {
        'loginId': client_id,
        'password': password,
        'applicationId': api_key,
    }
    if totp_code:
        payload['2FA'] = totp_code

    try:
        logger.info(f"[Dhan] Generating access token for client {client_id} via API Key …")
        resp = requests.post(
            'https://api.dhan.co/token',
            json=payload,
            headers={'Content-Type': 'application/json'},
            timeout=15,
        )
        if resp.status_code == 200:
            data = resp.json()
            token = data.get('accessToken') or data.get('access_token')
            if token:
                logger.info(f"[Dhan] Access token generated successfully for {client_id}.")
                return token
            logger.error(f"[Dhan] Token response missing accessToken field: {data}")
        else:
            logger.error(
                f"[Dhan] Token generation failed for {client_id}: "
                f"HTTP {resp.status_code} — {resp.text[:300]}"
            )
    except Exception as e:
        logger.error(f"[Dhan] Token generation error for {client_id}: {e}")

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
        dhan = dhanhq(client_id, access_token)
        logger.info(f'[Dhan] Client initialised for {client_id}.')
        return dhan
    except Exception as e:
        logger.error(f'[Dhan] Failed to initialise client: {e}')
        return None


def _estimate_dhan_token_expiry(token_updated_at: str, api_key_mode: bool = False) -> dict:
    """
    Estimates remaining validity of the Dhan access token.

    API Key mode  → tokens are generated with 24-hour validity.
    Direct Token  → user-generated tokens may be up to 30 days.
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
        elif resp.status_code in (401, 403):
            logger.error(f'[Dhan] Token EXPIRED for {client_id}: HTTP {resp.status_code}')
            return None
        else:
            logger.warning(
                f'[Dhan] Token check returned {resp.status_code} for {client_id}: '
                f'{resp.text[:200]}'
            )
            return access_token

    except Exception as e:
        logger.error(f'[Dhan] Automated login error: {e}')
        return None
