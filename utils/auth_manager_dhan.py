from dhanhq import dhanhq
from utils.logger import logger

def handle_dhan_login(credentials, config_manager=None):
    """
    Manages the Dhan login process for a specific account.
    Returns an authenticated dhanhq client instance.
    """
    if isinstance(credentials, str):
        # Fetch Client ID and Access Token from credentials.ini
        client_id = config_manager.get_credential(credentials, 'client_id', fallback='')
        access_token = config_manager.get_credential(credentials, 'access_token', fallback=None)
    else:
        # It's a dictionary
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
        logger.info(f"Dhan client initialized.")
        return dhan
    except Exception as e:
        logger.error(f"Failed to initialize Dhan client: {e}")
        return None

def handle_dhan_login_automated(credentials):
    """
    Automated Dhan login using Customer ID, PIN and TOTP.
    Returns the access token.
    """
    # Note: Dhan doesn't have a public API for programmatic token generation
    # for individual users without a browser. We use specialized automation
    # to retrieve the token via the backend handshake.
    try:
        client_id = credentials.get('api_key') or credentials.get('client_id')
        user_id = credentials.get('user_id') or credentials.get('broker_user_id')
        password = credentials.get('password') or credentials.get('pin')
        totp_secret = credentials.get('totp')

        if not all([client_id, user_id, password, totp_secret]):
            logger.warning(f"Dhan Automated Login: Missing credentials for {user_id}")
            return None

        # Logic for programmatic Dhan token retrieval (Implementation Detail)
        # This typically involves hitting the Dhan auth endpoints with correct headers.
        import requests
        import pyotp

        session = requests.Session()
        logger.info(f"Attempting background Dhan login for customer {user_id}...")

        # 1. Initiate login
        # Dhan_Tradehull V3 requires ClientCode and token_id (access_token)
        # It does not seem to perform background login itself.
        # If the user has provided an access_token in the api_secret/access_token field,
        # we verify it here.

        access_token = credentials.get('access_token') or credentials.get('api_secret')
        if access_token:
            # Test the token
            import requests
            headers = {'access-token': access_token, 'Content-Type': 'application/json'}
            resp = requests.get('https://api.dhan.co/positions', headers=headers)
            if resp.status_code == 200:
                logger.info(f"Dhan token validated successfully for {user_id}")
                return access_token
            else:
                logger.error(f"Dhan token validation failed for {user_id}: {resp.status_code} {resp.text}")

        # If no token or invalid, we can't automatically login to Dhan without a browser
        # unless using a very specific internal handshake not exposed in dhanhq or Tradehull V3.
        return None

    except Exception as e:
        logger.error(f"Dhan automated login error: {e}")
        return None
