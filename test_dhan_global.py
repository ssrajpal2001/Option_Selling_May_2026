import pyotp
import requests
import json
import configparser

def test_manual_dhan_token():
    creds = configparser.ConfigParser()
    creds.read('config/credentials.ini')

    # Try dhan_global
    client_id = creds.get('dhan_global', 'client_id')
    access_token = creds.get('dhan_global', 'access_token')
    totp_secret = creds.get('dhan_global', 'totp_secret')

    print(f"Testing Dhan Global with client_id: {client_id}")

    headers = {
        'access-token': access_token,
        'Content-Type': 'application/json'
    }

    try:
        resp = requests.get('https://api.dhan.co/positions', headers=headers)
        if resp.status_code == 200:
            print("Dhan Global Connection SUCCESS")
        else:
            print(f"Dhan Global Connection FAILED: {resp.status_code} {resp.text}")

        if totp_secret:
            totp = pyotp.TOTP(totp_secret)
            print(f"Current TOTP for Dhan: {totp.now()}")

    except Exception as e:
        print(f"Dhan Global Test Error: {e}")

if __name__ == "__main__":
    test_manual_dhan_token()
