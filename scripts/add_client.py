import sys
import os
import sqlite3
from pathlib import Path

# Add root to path so we can import auth
sys.path.append(str(Path(__file__).parent.parent))
from web.auth import hash_password, encrypt_secret

DB_PATH = "config/algosoft.db"

def add_client(username, email, password, broker, api_key, api_secret=None):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    try:
        # 1. Create User
        pwd_hash = hash_password(password)
        cursor.execute(
            "INSERT OR IGNORE INTO users (username, email, password_hash, role, is_active) VALUES (?, ?, ?, 'client', 1)",
            (username, email, pwd_hash)
        )
        conn.commit()

        user = cursor.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
        client_id = user['id']
        print(f"User {username} (ID: {client_id}) ensured.")

        # 2. Create Broker Instance
        enc_key = encrypt_secret(api_key)
        enc_secret = encrypt_secret(api_secret) if api_secret else None

        cursor.execute(
            """INSERT OR REPLACE INTO client_broker_instances
               (client_id, broker, api_key_encrypted, api_secret_encrypted, status, trading_mode)
               VALUES (?, ?, ?, ?, 'idle', 'paper')""",
            (client_id, broker, enc_key, enc_secret)
        )
        conn.commit()
        print(f"Broker {broker} added/updated for {username}.")
        print("\nSuccess! You can now log in to the UI and see your client.")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        conn.close()

import argparse

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Add or update a client in the AlgoSoft database.")
    parser.add_argument("--username", required=True, help="Client username")
    parser.add_argument("--email", required=True, help="Client email")
    parser.add_argument("--password", required=True, help="Client password")
    parser.add_argument("--broker", required=True, help="Broker name (e.g., upstox, zerodha, dhan, papertrade)")
    parser.add_argument("--api_key", required=True, help="Broker API Key")
    parser.add_argument("--api_secret", help="Broker API Secret (if applicable)")

    args = parser.parse_args()

    add_client(args.username, args.email, args.password, args.broker, args.api_key, args.api_secret)
