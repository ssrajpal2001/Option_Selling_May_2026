import os
from web.auth import decrypt_secret
from web.db import db_fetchone
from upstox_totp import UpstoxTOTP
import logging

logging.basicConfig(level=logging.INFO)

def test_upstox_automated():
    print("--- Testing Upstox Automated Login ---")
    row = db_fetchone("SELECT * FROM data_providers WHERE provider='upstox'")
    if not row:
        print("No Upstox entry in DB")
        return

    creds = {
        "api_key": decrypt_secret(row['api_key_encrypted']),
        "api_secret": decrypt_secret(row['api_secret_encrypted']),
        "user_id": decrypt_secret(row['user_id_encrypted']),
        "password": decrypt_secret(row['password_encrypted']),
        "totp": decrypt_secret(row['totp_encrypted'])
    }

    print(f"Attempting login for User ID: {creds['user_id']} with Redirect: https://google.com")

    try:
        upx = UpstoxTOTP(
            username=creds['user_id'],
            password=creds['password'],
            pin_code=creds['password'],
            totp_secret=creds['totp'],
            client_id=creds['api_key'],
            client_secret=creds['api_secret'],
            redirect_uri="https://google.com"
        )

        resp = upx.app_token.get_access_token()
        if resp.success and resp.data:
            print(f"SUCCESS! New Access Token: {resp.data.access_token[:10]}...")
        else:
            print(f"FAILED: {resp.error}")
    except Exception as e:
        print(f"Error during automated login: {e}")

if __name__ == "__main__":
    test_upstox_automated()
