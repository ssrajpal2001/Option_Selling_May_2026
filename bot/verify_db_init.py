import sqlite3
from web.auth import decrypt_secret

def verify():
    conn = sqlite3.connect('config/algosoft.db')
    conn.row_factory = sqlite3.Row

    # Check Dhan
    dhan = conn.execute("SELECT * FROM data_providers WHERE provider='dhan'").fetchone()
    if dhan:
        print(f"Dhan Status: {dhan['status']}")
        print(f"Dhan Key Decrypted: {decrypt_secret(dhan['api_key_encrypted']) == 'dba1ea52'}")

    # Check Upstox
    upstox = conn.execute("SELECT * FROM data_providers WHERE provider='upstox'").fetchone()
    if upstox:
        print(f"Upstox Status: {upstox['status']}")
        print(f"Upstox Key Decrypted: {decrypt_secret(upstox['api_key_encrypted']) == 'abc08e59-814c-4aea-a3af-b4d8aac57bc0'}")
        print(f"Upstox TOTP Decrypted: {decrypt_secret(upstox['totp_encrypted']) == 'NFHSX64BBC7KJPBZTWBVH5MHP4NAO626'}")

    conn.close()

if __name__ == "__main__":
    verify()
