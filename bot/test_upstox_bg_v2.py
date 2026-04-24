import os
from web.auth import decrypt_secret
from web.db import db_fetchone
from upstox_totp import UpstoxTOTP
import logging

logging.basicConfig(level=logging.INFO)

def test_upstox_automated():
    print("--- Testing Upstox Automated Login with New Redirect ---")
    row = db_fetchone("SELECT * FROM data_providers WHERE provider='upstox'")

    # Use the decrypted creds directly to be sure
    api_key = "abc08e59-814c-4aea-a3af-b4d8aac57bc0"
    api_secret = "s1tebltj0q"
    user_id = "8604928092"
    password = "121212"
    totp = "NFHSX64BBC7KJPBZTWBVH5MHP4NAO626"

    try:
        upx = UpstoxTOTP(
            username=user_id,
            password=password,
            pin_code=password,
            totp_secret=totp,
            client_id=api_key,
            client_secret=api_secret,
            redirect_uri="https://google.com" # As per user
        )

        resp = upx.app_token.get_access_token()
        if resp.success and resp.data:
            print(f"SUCCESS! New Access Token: {resp.data.access_token[:15]}...")
        else:
            print(f"FAILED: {resp.error}")
            # print(f"Raw data: {resp}")
    except Exception as e:
        print(f"Error during automated login: {e}")

if __name__ == "__main__":
    test_upstox_automated()
