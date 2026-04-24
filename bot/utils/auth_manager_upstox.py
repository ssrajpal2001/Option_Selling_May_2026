import logging
from web.auth import encrypt_secret, decrypt_secret

logger = logging.getLogger(__name__)

def handle_upstox_login_automated(credentials):
    """
    Automated Upstox login using Mobile Number, PIN and TOTP.
    Returns the access token.
    """
    try:
        from upstox_totp import UpstoxTOTP
    except ImportError:
        logger.error("upstox-totp package not installed. Run 'pip install upstox-totp'")
        return None

    api_key = credentials.get('api_key')
    api_secret = credentials.get('api_secret')
    user_id = credentials.get('user_id') or credentials.get('broker_user_id') or credentials.get('username')
    password = credentials.get('password') or credentials.get('pin')
    # Support both 'totp' (from DB flow) and 'totp_secret' (from credentials.ini direct read)
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
        logger.warning(f"Upstox Automated Login: Missing credentials {missing} for user: {user_id}")
        return None

    try:
        logger.info(f"Attempting background Upstox login for {user_id}...")
        redirect_uri = credentials.get('redirect_uri') or "https://google.com"

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
            return resp.data.access_token
        else:
            logger.error(f"Background Upstox login FAILED for {user_id}: {getattr(resp, 'error', 'unknown error')}")
            return None
    except Exception as e:
        logger.error(f"Upstox background auth error for {user_id}: {e}")
        return None
