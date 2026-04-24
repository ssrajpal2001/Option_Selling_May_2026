import sys
from web.auth import decrypt_secret
from web.db import db_fetchone
import requests
import configparser

def test_db_creds(provider):
    print(f"--- Testing {provider} from Database ---")
    row = db_fetchone("SELECT * FROM data_providers WHERE provider=?", (provider,))
    if not row:
        print(f"No database entry for {provider}")
        return

    api_key = decrypt_secret(row['api_key_encrypted']) if row['api_key_encrypted'] else None
    access_token = decrypt_secret(row['access_token_encrypted']) if row['access_token_encrypted'] else None

    if not access_token:
        print(f"No access token in DB for {provider}")
        return

    print(f"Provider: {provider}, Key: {api_key[:5] if api_key else 'None'}...")

    if provider == 'upstox':
        headers = {'accept': 'application/json', 'Authorization': f'Bearer {access_token}'}
        url = 'https://api.upstox.com/v2/user/profile'
    else:
        headers = {'access-token': access_token, 'Content-Type': 'application/json'}
        url = 'https://api.dhan.co/positions'

    try:
        resp = requests.get(url, headers=headers)
        if resp.status_code == 200:
            print(f"{provider.capitalize()} DB Connection SUCCESS")
        else:
            print(f"{provider.capitalize()} DB Connection FAILED: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Error testing {provider}: {e}")

if __name__ == "__main__":
    test_db_creds('upstox')
    print("\n")
    test_db_creds('dhan')
