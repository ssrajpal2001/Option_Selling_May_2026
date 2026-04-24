import os
from web.auth import decrypt_secret
from web.db import db_fetchone
from Dhan_Tradehull.Dhan_Tradehull import Tradehull
import logging

logging.basicConfig(level=logging.INFO)

def test_dhan_automated():
    print("--- Testing Dhan Automated Login ---")
    row = db_fetchone("SELECT * FROM data_providers WHERE provider='dhan'")
    if not row:
        print("No Dhan entry in DB")
        return

    creds = {
        "api_key": decrypt_secret(row['api_key_encrypted']),
        "user_id": decrypt_secret(row['user_id_encrypted']),
        "password": decrypt_secret(row['password_encrypted']),
        "totp": decrypt_secret(row['totp_encrypted'])
    }

    print(f"Attempting login for Customer ID: {creds['user_id']} with Client ID: {creds['api_key']}")

    try:
        # Based on Tradehull signature (self, ClientCode: str, token_id: str)
        # It seems it might NOT do the background login itself but take a token?
        # Let's check if there's a get_token or similar.
        print("Initializing Tradehull class...")
        # ts = Tradehull(creds['user_id'], "dummy")
        # Actually let's try the parameters from the previous auth_manager_dhan.py
        # which used ts = Dhan_Tradehull(client_id, user_id, password, totp_secret)
        # But our inspect showed (self, ClientCode: str, token_id: str)

        from Dhan_Tradehull import Tradehull
        # If it only takes ClientCode and token_id, maybe it's already logged in?
        # Let's see if there is another class.
        pass
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    test_dhan_automated()
