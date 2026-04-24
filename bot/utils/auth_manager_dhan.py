from dhanhq import dhanhq
from utils.logger import logger
from datetime import datetime, timezone, timedelta


def handle_dhan_login(credentials, config_manager=None):
    """
    Manages the Dhan login process for a specific account.
    Returns an authenticated dhanhq client instance.
    """
    if isinstance(credentials, str):
        client_id = config_manager.get_credential(credentials, 'client_id', fallback='')
        access_token = config_manager.get_credential(credentials, 'access_token', fallback=None)
    else:
        client_id = credentials.get('api_key') or credentials.get('client_id')
        access_token = credentials.get('access_token') or credentials.get('api_secret')

    if not access_token:
        logger.warning(f"Dhan Login: Access token not found.")
        return None

    if not client_id:
        logger.warning(f"Dhan Login: Client ID not found.")
        client_id = ""

    try:
        dhan = dhanhq(client_id, access_token)
        logger.info(f"Dhan client initialized for {client_id}.")
        return dhan
    except Exception as e:
        logger.error(f"Failed to initialize Dhan client: {e}")
        return None


def _estimate_dhan_token_expiry(token_updated_at: str) -> dict:
    """
    Estimates days remaining for Dhan access token.
    Dhan tokens are valid for 30 days.
    Returns dict with days_remaining, is_valid, warn_soon.
    """
    result = {"days_remaining": None, "is_valid": False, "warn_soon": False}
    if not token_updated_at:
        return result
    try:
        updated = datetime.fromisoformat(token_updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        days_elapsed = (now - updated).days
        days_remaining = 30 - days_elapsed
        result["days_remaining"] = max(0, days_remaining)
        result["is_valid"] = days_remaining > 0
        result["warn_soon"] = 0 < days_remaining <= 5
    except Exception as e:
        logger.warning(f"Could not parse Dhan token date: {e}")
    return result


def handle_dhan_login_automated(credentials):
    """
    Validates existing Dhan access token.
    Dhan does not support programmatic headless login — tokens must be
    generated manually on the Dhan portal and are valid for 30 days.

    Returns the access token if valid, None otherwise.
    """
    try:
        client_id = credentials.get('api_key') or credentials.get('client_id')
        # Check all possible key names for the access token
        access_token = (
            credentials.get('access_token') or
            credentials.get('api_secret') or
            credentials.get('token')
        )

        if not access_token:
            logger.warning(f"Dhan Automated Login: No access token provided for client {client_id}")
            return None

        logger.info(f"Validating Dhan access token for client {client_id}...")

        import requests
        headers = {
            'access-token': access_token,
            'client-id': client_id or '',
            'Content-Type': 'application/json'
        }
        resp = requests.get('https://api.dhan.co/v2/fundlimit', headers=headers, timeout=10)

        if resp.status_code == 200:
            logger.info(f"Dhan token VALID for client {client_id}")
            return access_token
        elif resp.status_code == 401:
            logger.error(f"Dhan token EXPIRED for client {client_id}: HTTP 401")
            return None
        else:
            # Non-401 errors (e.g. market closed, rate limits) — token may still be valid
            logger.warning(f"Dhan token check returned {resp.status_code} for {client_id}: {resp.text[:200]}")
            # Treat as valid if not explicitly unauthorized
            if resp.status_code not in (401, 403):
                return access_token
            return None

    except Exception as e:
        logger.error(f"Dhan automated login error: {e}")
        return None
