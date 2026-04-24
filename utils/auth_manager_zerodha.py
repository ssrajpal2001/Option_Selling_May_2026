import sys

from kiteconnect import KiteConnect
from utils.logger import logger

def handle_zerodha_login(credentials_section, config_manager):
    """
    Manages the Zerodha login process for a specific account,
    including handling the access token.
    Returns an authenticated KiteConnect client instance.
    """
    logger.info(f"Starting Zerodha authentication for section: {credentials_section}...")
    
    # --- FIX: Fetch API key and secret from credentials.ini, not config.ini ---
    api_key = config_manager.get_credential(credentials_section, 'api_key')
    api_secret = config_manager.get_credential(credentials_section, 'api_secret')

    if not api_key or not api_secret:
        raise ValueError(f"API key or secret not found in section '{credentials_section}'.")

    # Read the access token from the credentials file using the same section name
    access_token = config_manager.get_credential(credentials_section, 'access_token')

    kite = KiteConnect(api_key=api_key)

    if access_token and access_token != 'YOUR_ACCESS_TOKEN':
        try:
            logger.info(f"Validating Zerodha access token for {credentials_section}...")
            kite.set_access_token(access_token)
            kite.profile()
            logger.info(f"Zerodha access token for {credentials_section} is valid.")
            return kite
        except Exception as e:
            logger.warning(f"Zerodha access token validation failed for {credentials_section}: {e}. Proceeding to login.")
            access_token = None

    logger.info(f"No valid Zerodha access token for {credentials_section}. Starting login flow.")

    if not sys.stdin or not sys.stdin.isatty():
        raise RuntimeError(
            f"Zerodha access token for [{credentials_section}] is invalid or expired. "
            "Please reconnect via the dashboard Settings page."
        )

    login_url = kite.login_url()
    print(f"\nPlease login to Zerodha for account '{credentials_section}' using this URL: {login_url}")

    request_token = input("Enter the request_token from the redirect URL: ")

    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data["access_token"]
        logger.info(f"Zerodha session for {credentials_section} generated successfully.")

        config_manager.set_credential(credentials_section, 'access_token', access_token)
        logger.info(f"New Zerodha access token for {credentials_section} saved.")

        kite.set_access_token(access_token)
        return kite

    except Exception as e:
        logger.error(f"Failed to generate Zerodha session for {credentials_section}", exc_info=True)
        raise

def handle_zerodha_login_automated(credentials):
    """
    Automated Zerodha login using User ID, Password and TOTP.
    Returns the access token.
    """
    import pyotp
    import requests
    import hashlib
    from urllib.parse import urlparse, parse_qs

    api_key = credentials.get('api_key')
    api_secret = credentials.get('api_secret')
    user_id = credentials.get('broker_user_id') or credentials.get('user_id')
    password = credentials.get('password')
    totp_secret = credentials.get('totp')

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        logger.warning(f"Zerodha Automated Login: Missing credentials for {user_id}")
        return None

    try:
        logger.info(f"Attempting automated Zerodha login for {user_id}...")
        session = requests.Session()

        # 1. Step 1: Login with UserID and Password
        resp1 = session.post("https://kite.zerodha.com/api/login", data={
            "user_id": user_id,
            "password": password
        })
        r1_data = resp1.json()
        if r1_data.get("status") != "success":
            logger.error(f"Zerodha Login Step 1 failed: {r1_data.get('message')}")
            return None

        request_id = r1_data["data"]["request_id"]

        # 2. Step 2: Two-Factor Authentication (TOTP)
        totp = pyotp.TOTP(totp_secret.replace(" ", "")).now()
        resp2 = session.post("https://kite.zerodha.com/api/twofa", data={
            "request_id": request_id,
            "twofa_value": totp,
            "user_id": user_id
        })
        r2_data = resp2.json()
        if r2_data.get("status") != "success":
            logger.error(f"Zerodha Login Step 2 failed: {r2_data.get('message')}")
            return None

        # 3. Step 3: Authorize for API
        # We need to hit the kite login URL to get the redirect with request_token
        auth_url = f"https://kite.trade/connect/login?v=3&api_key={api_key}"
        resp3 = session.get(auth_url, allow_redirects=True)

        # The final URL should contain the request_token
        final_url = resp3.url
        parsed = urlparse(final_url)
        params = parse_qs(parsed.query)

        if "request_token" not in params:
            logger.error(f"Zerodha Login Step 3 failed: Request token not found in redirect URL: {final_url}")
            return None

        request_token = params["request_token"][0]

        # 4. Step 4: Exchange Request Token for Access Token
        checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
        resp4 = requests.post("https://api.kite.trade/session/token", data={
            "api_key": api_key,
            "request_token": request_token,
            "checksum": checksum
        })
        r4_data = resp4.json()

        if r4_data.get("status") == "success":
            access_token = r4_data["data"]["access_token"]
            logger.info(f"Automated Zerodha login SUCCESS for {user_id}")
            return access_token
        else:
            logger.error(f"Zerodha Token Exchange failed: {r4_data.get('message')}")
            return None

    except Exception as e:
        logger.error(f"Automated Zerodha login error for {user_id}: {e}")
        return None
