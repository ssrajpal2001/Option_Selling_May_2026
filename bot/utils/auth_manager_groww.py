import requests
from utils.logger import logger


def handle_groww_login(credentials: dict):
    """
    Initialises a Groww session using a stored access token.
    Groww's trading API requires a session token; this validates the stored one.
    Returns the token string if valid, None on failure.
    """
    try:
        client_id = credentials.get("broker_user_id") or credentials.get("client_id")
        access_token = credentials.get("access_token")

        if not access_token:
            logger.warning("[Groww] No access token stored.")
            return None

        resp = requests.get(
            "https://groww.in/v1/api/user/profile",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )

        if resp.status_code == 200:
            logger.info(f"[Groww] Token validated for client {client_id}.")
            return access_token
        elif resp.status_code in (401, 403):
            logger.warning(f"[Groww] Token expired/invalid for {client_id}: HTTP {resp.status_code}")
            return None
        else:
            logger.warning(f"[Groww] Unexpected status {resp.status_code} — treating token as present.")
            return access_token
    except Exception as e:
        logger.error(f"[Groww] Login validation error: {e}")
        return None


def handle_groww_login_automated(credentials: dict) -> str | None:
    """
    Attempts to refresh a Groww session using stored credentials.
    Groww does not have a public third-party trading API; this is a best-effort
    token re-validation that returns the existing token if it is still valid.
    """
    return handle_groww_login(credentials)
