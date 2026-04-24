import configparser
import requests
import json

def test_upstox():
    creds = configparser.ConfigParser()
    creds.read('config/credentials.ini')

    # Try upstox_1
    api_key = creds.get('upstox_1', 'api_key')
    access_token = creds.get('upstox_1', 'access_token')

    print(f"Testing Upstox with api_key: {api_key[:5]}...")

    headers = {
        'accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }

    try:
        # Get profile as a test
        resp = requests.get('https://api.upstox.com/v2/user/profile', headers=headers)
        if resp.status_code == 200:
            print("Upstox Connection SUCCESS")
            print(f"Profile: {resp.json().get('data', {}).get('user_name')}")
        else:
            print(f"Upstox Connection FAILED: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Upstox Test Error: {e}")

def test_dhan():
    creds = configparser.ConfigParser()
    creds.read('config/credentials.ini')

    # Try Dhan_1
    client_id = creds.get('Dhan_1', 'client_id')
    access_token = creds.get('Dhan_1', 'access_token')

    print(f"Testing Dhan with client_id: {client_id}")

    headers = {
        'access-token': access_token,
        'Content-Type': 'application/json'
    }

    try:
        # Get holdings or positions as a test
        resp = requests.get('https://api.dhan.co/positions', headers=headers)
        if resp.status_code == 200:
            print("Dhan Connection SUCCESS")
            # print(f"Positions: {len(resp.json())}")
        else:
            print(f"Dhan Connection FAILED: {resp.status_code} {resp.text}")
    except Exception as e:
        print(f"Dhan Test Error: {e}")

if __name__ == "__main__":
    print("--- Testing Credentials from credentials.ini ---")
    test_upstox()
    print("\n---")
    test_dhan()
