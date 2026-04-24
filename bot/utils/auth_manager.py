from .logger import logger
from .rest_api_client import RestApiClient
from .interactive_auth import get_new_token_interactively

class AuthHandler:
    def __init__(self, config_manager, credentials_section, access_token=None):
        self.config_manager = config_manager
        self.credentials_section = credentials_section
        self.access_token = access_token
        self.api_client_manager = None

    def get_access_token(self):
        if self.access_token:
            return self.access_token
        return self.config_manager.get(self.credentials_section, 'access_token')

    def switch_client(self):
        if self.api_client_manager:
            return self.api_client_manager.switch_to_next_client()
        return False

async def _background_upstox_login(auth_handler, api_key, api_secret, mobile_no, pin, totp_secret):
    """Attempt background login for Upstox using API credentials + Mobile/PIN/TOTP."""
    import pyotp
    import aiohttp
    import urllib.parse
    from web.auth import encrypt_secret, decrypt_secret
    from web.db import db_execute, db_fetchone
    from datetime import datetime, timezone

    try:
        logger.info(f"Upstox: Background login for {auth_handler.credentials_section}...")

        # Note: Upstox does not provide a public REST API for background login (ClientID + PWD -> Token).
        # It strictly mandates OAuth2 Redirect Flow.
        # Background automation would require Playwright/Selenium which is resource heavy on EC2.

        # Strategy: We rely on the "One-Click" logic in the UI but ensure the bot
        # uses the latest token from the DB.

        dp = db_fetchone("SELECT access_token_encrypted FROM data_providers WHERE provider='upstox'")
        if dp and dp['access_token_encrypted']:
             return decrypt_secret(dp['access_token_encrypted'])

        return None
    except Exception as e:
        logger.error(f"Upstox background lookup failed: {e}")
        return None

async def _background_dhan_login(client_id, api_key, api_secret, totp_secret):
    """Attempt background login for Dhan."""
    # Dhan tokens are long-lived (30 days). Automation usually involves storing the access_token.
    from web.db import db_fetchone
    from web.auth import decrypt_secret

    dp = db_fetchone("SELECT access_token_encrypted FROM data_providers WHERE provider='dhan'")
    if dp and dp['access_token_encrypted']:
        return decrypt_secret(dp['access_token_encrypted'])
    return None

async def handle_login(config_manager, credentials_section='upstox_trading'):
    """
    Handles the Upstox authentication and verifies the access token's validity asynchronously.
    If the token is invalid, it attempts background login if credentials exist,
    otherwise it prompts the user for a new one.
    """
    logger.info(f"Attempting to authenticate Upstox account: [{credentials_section}]")

    # 1. Try background login if credentials are in DB
    try:
        from web.db import db_fetchone
        from web.auth import decrypt_secret
        from datetime import datetime, timezone

        dp = db_fetchone("SELECT * FROM data_providers WHERE provider='upstox'")
        if dp and dp['status'] == 'configured':
            api_key = decrypt_secret(dp['api_key_encrypted'])
            api_secret = decrypt_secret(dp['api_secret_encrypted'])
            totp_secret = decrypt_secret(dp['totp_encrypted'])

            # Check if current token is fresh (e.g. updated today)
            # Upstox tokens expire daily.
            # If we don't have a fresh token, we might need a manual refresh
            # UNLESS we have a custom automation scraper.
    except: pass

    while True:
        try:
            auth_handler = AuthHandler(config_manager, credentials_section)
            api_client = RestApiClient(auth_handler)

            is_valid = await api_client.verify_authentication()

            if is_valid:
                logger.info(f"Authentication successful and token verified for [{credentials_section}].")
                return api_client
            else:
                logger.critical(f"AUTHENTICATION FAILED for account [{credentials_section}].")

                # Check for background credentials in DB as a rescue
                try:
                    from web.db import db_fetchone
                    from web.auth import decrypt_secret
                    dp = db_fetchone("SELECT * FROM data_providers WHERE provider='upstox'")
                    if dp and dp['totp_encrypted']:
                         logger.info("Background credentials found. Please use the 'One-Click Connect' in the Admin Panel to refresh the token.")
                except: pass

                new_token = get_new_token_interactively(config_manager, credentials_section)
                if not new_token:
                    return None

        except Exception as e:
            logger.error(f"An unexpected error occurred during authentication for [{credentials_section}]: {e}", exc_info=True)
            return None
