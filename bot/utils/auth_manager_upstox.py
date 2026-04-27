import logging
import os
from web.auth import encrypt_secret, decrypt_secret

logger = logging.getLogger(__name__)

def handle_upstox_login_automated(credentials):
    """
    Automated Upstox login using Mobile Number, PIN and TOTP.
    Returns a dict: {"token": <str>, "error": None} on success,
                    {"token": None, "error": <str>} on failure.
    Returns None only for hard pre-flight failures (missing library / credentials).
    """
    try:
        from upstox_totp import UpstoxTOTP
    except ImportError:
        logger.debug("upstox-totp not available; automated Upstox TOTP login skipped")
        return None

    api_key = credentials.get('api_key')
    api_secret = credentials.get('api_secret')
    user_id = credentials.get('user_id') or credentials.get('broker_user_id') or credentials.get('username')
    password = credentials.get('password') or credentials.get('pin')
    totp_secret = (
        credentials.get('totp') or
        credentials.get('totp_secret') or
        credentials.get('totp_key')
    )

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        missing = [k for k, v in {
            'api_key': api_key, 'api_secret': api_secret,
            'user_id': user_id, 'password': password, 'totp_secret': totp_secret
        }.items() if not v]
        msg = f"Missing credentials: {missing}"
        logger.warning(f"Upstox Automated Login: {msg} for user: {user_id}")
        return {"token": None, "error": msg}

    try:
        logger.info(f"Attempting background Upstox login for {user_id}...")
        redirect_uri = (
            credentials.get('redirect_uri') or
            os.environ.get('UPSTOX_REDIRECT_URI') or
            "https://google.com"
        )

        upx = UpstoxTOTP(
            username=user_id,
            password=password,
            pin_code=password,
            totp_secret=totp_secret,
            client_id=api_key,
            client_secret=api_secret,
            redirect_uri=redirect_uri
        )

        resp = upx.app_token.get_access_token()
        if resp.success and resp.data:
            logger.info(f"Background Upstox login SUCCESS for {user_id}")
            return {"token": resp.data.access_token, "error": None}
        else:
            err = (
                getattr(resp, 'error', None) or
                getattr(resp, 'message', None) or
                "Unknown error from Upstox"
            )
            logger.error(f"Background Upstox login FAILED for {user_id}: {err}")
            return {"token": None, "error": str(err)}

    except Exception as e:
        logger.error(f"Upstox background auth error for {user_id}: {e}")
        return {"token": None, "error": str(e)}
