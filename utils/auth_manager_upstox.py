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
    totp_secret = credentials.get('totp')

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        logger.warning(f"Upstox Automated Login: Missing credentials. User: {user_id}")
        return None

    try:
        logger.info(f"Attempting background Upstox login for {user_id}...")
        # User specified redirect_uri is https://google.com
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
            logger.error(f"Background Upstox login FAILED for {user_id}: {resp.error}")
            # If it's a 500 error, it might be transient or a credential issue
            return None
    except Exception as e:
        logger.error(f"Upstox background auth error for {user_id}: {e}")
        return None
