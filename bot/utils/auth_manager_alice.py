import pyotp
from utils.logger import logger


def handle_alice_login(credentials: dict):
    """
    Initialises an AliceBlue client using a stored session token.
    Returns the Aliceblue instance on success, None on failure.
    """
    try:
        from pya3 import Aliceblue
        user_id = credentials.get("broker_user_id") or credentials.get("client_id")
        api_key = credentials.get("api_key")
        access_token = credentials.get("access_token")

        if not user_id or not api_key or not access_token:
            logger.warning("[AliceBlue] Missing Client ID, API key, or session token.")
            return None

        alice = Aliceblue(user_id=user_id, api_key=api_key)
        alice.session_id = access_token
        logger.info(f"[AliceBlue] Client initialised with existing token for {user_id}.")
        return alice
    except Exception as e:
        logger.error(f"[AliceBlue] Login error: {e}")
        return None


def handle_alice_login_automated(credentials: dict) -> str | None:
    """
    Generates a fresh Alice Blue session token via background login.
    Requires: Client ID, API Key, PIN, TOTP secret.
    Returns the session token string on success, None on failure.
    """
    try:
        from pya3 import Aliceblue
        user_id = credentials.get("broker_user_id") or credentials.get("client_id")
        api_key = credentials.get("api_key")
        password = credentials.get("password") or credentials.get("pin")
        totp_secret = (credentials.get("totp") or credentials.get("totp_secret") or "").strip()

        if not all([user_id, api_key, password]):
            logger.warning("[AliceBlue] Missing credentials for automated login (need Client ID, API key, PIN).")
            return None

        totp = ""
        if totp_secret:
            try:
                totp = pyotp.TOTP(totp_secret.replace(" ", "")).now()
            except Exception as te:
                logger.warning(f"[AliceBlue] TOTP generation failed: {te}")

        alice = Aliceblue(user_id=user_id, api_key=api_key)
        logger.info(f"[AliceBlue] Attempting background login for {user_id}...")

        session = alice.get_session_id(password=password, totp=totp)

        if isinstance(session, dict):
            token = session.get("sessionID") or session.get("session_id") or session.get("data")
            if token:
                logger.info(f"[AliceBlue] Session token generated for {user_id}.")
                return str(token)
            logger.error(f"[AliceBlue] Login failed — response: {session}")
            return None

        if isinstance(session, str) and len(session) > 10:
            logger.info(f"[AliceBlue] Session token generated for {user_id}.")
            return session

        logger.error(f"[AliceBlue] Login returned unexpected response: {session}")
        return None
    except Exception as e:
        logger.error(f"[AliceBlue] Automated login error: {e}")
        return None
