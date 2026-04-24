from web.auth import decrypt_secret
from web.db import db_fetchall

def decrypt_all():
    rows = db_fetchall("SELECT * FROM data_providers")
    for r in rows:
        print(f"Provider: {r['provider']}")
        print(f"  API Key: {decrypt_secret(r['api_key_encrypted']) if r['api_key_encrypted'] else 'None'}")
        print(f"  API Secret: {decrypt_secret(r['api_secret_encrypted']) if r['api_secret_encrypted'] else 'None'}")
        print(f"  User ID: {decrypt_secret(r['user_id_encrypted']) if r['user_id_encrypted'] else 'None'}")
        print(f"  Password: {decrypt_secret(r['password_encrypted']) if r['password_encrypted'] else 'None'}")
        print(f"  TOTP: {decrypt_secret(r['totp_encrypted']) if r['totp_encrypted'] else 'None'}")
        print("-" * 20)

if __name__ == "__main__":
    decrypt_all()
