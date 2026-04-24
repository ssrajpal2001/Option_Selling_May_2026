from utils.logger import logger


def generate_fyers_auth_url(app_id: str, secret_id: str) -> str:
    """
    Generates the Fyers OAuth login URL.
    User completes browser login and gets a redirect URL containing auth_code.
    """
    try:
        from fyers_apiv3 import fyersModel
        session = fyersModel.SessionModel(
            client_id=app_id,
            secret_key=secret_id,
            redirect_uri="https://trade.fyers.in/api-login/redirect-uri/index.html",
            response_type="code",
            grant_type="authorization_code",
        )
        url = session.generate_authcode()
        logger.info(f"[Fyers] Auth URL generated for App ID: {app_id}")
        return url
    except Exception as e:
        logger.error(f"[Fyers] Failed to generate auth URL: {e}")
        return None


def exchange_fyers_auth_code(app_id: str, secret_id: str, auth_code: str) -> dict:
    """
    Exchanges a Fyers auth code for an access token.
    auth_code is extracted from the redirect URL after browser login.
    Returns {'token': str, 'error': None} on success.
    """
    _fail = lambda msg: {"token": None, "error": msg}
    try:
        from fyers_apiv3 import fyersModel
        session = fyersModel.SessionModel(
            client_id=app_id,
            secret_key=secret_id,
            redirect_uri="https://trade.fyers.in/api-login/redirect-uri/index.html",
            response_type="code",
            grant_type="authorization_code",
        )
        session.set_token(auth_code)
        response = session.generate_token()

        if response.get("s") == "ok" or response.get("code") == 200:
            token = (
                response.get("access_token") or
                (response.get("data") or {}).get("access_token")
            )
            if token:
                logger.info("[Fyers] Access token generated successfully.")
                return {"token": token, "error": None}
            return _fail("Token not found in Fyers response.")

        msg = response.get("message") or str(response)
        logger.error(f"[Fyers] Token exchange failed: {msg}")
        return _fail(msg)
    except Exception as e:
        logger.error(f"[Fyers] Token exchange error: {e}")
        return _fail(str(e))


def handle_fyers_login(credentials: dict):
    """
    Initialises a FyersModel client using a stored access token.
    Returns the FyersModel instance on success, None on failure.
    """
    try:
        from fyers_apiv3 import fyersModel
        app_id = credentials.get("broker_user_id") or credentials.get("api_key")
        access_token = credentials.get("access_token")

        if not app_id or not access_token:
            logger.warning("[Fyers] Missing App ID or access token.")
            return None

        fyers = fyersModel.FyersModel(client_id=app_id, token=access_token, log_path="")
        profile = fyers.get_profile()
        if profile.get("s") == "ok" or profile.get("code") == 200:
            logger.info(f"[Fyers] Client initialised for App ID: {app_id}.")
            return fyers

        logger.warning(f"[Fyers] Token validation failed: {profile.get('message', 'unknown error')}")
        return None
    except Exception as e:
        logger.error(f"[Fyers] Login error: {e}")
        return None
