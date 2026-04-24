import sqlite3
import os
import json
from pathlib import Path

DB_PATH = "config/algosoft.db"

def check_system():
    print("=== AlgoSoft Clear History Diagnostic ===")

    # 1. Database Check
    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
    else:
        print(f"[OK] Database found at {DB_PATH}")
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row

            # Check users
            users = conn.execute("SELECT id, username FROM users WHERE role='client'").fetchall()
            print(f"\nFound {len(users)} clients in database:")
            for u in users:
                c_id = u['id']
                trades = conn.execute("SELECT COUNT(*) as c FROM trade_history WHERE client_id=?", (c_id,)).fetchone()['c']
                failures = conn.execute("SELECT COUNT(*) as c FROM order_failures WHERE client_id=?", (c_id,)).fetchone()['c']
                instances = conn.execute("SELECT id, broker, status FROM client_broker_instances WHERE client_id=?", (c_id,)).fetchall()

                print(f"  - ID: {c_id} | Username: {u['username']}")
                print(f"    - Trades: {trades}")
                print(f"    - Failures: {failures}")
                print(f"    - Instances: {len(instances)}")
                for i in instances:
                    print(f"      * [{i['id']}] {i['broker']} - Status: {i['status']}")
            conn.close()
        except Exception as e:
            print(f"[ERROR] Database query failed: {e}")

    # 2. File Check
    print("\nChecking Strategy Files in config/:")
    status_files = list(Path('config').glob('bot_status_client_*.json'))
    state_files = list(Path('config').glob('sell_v3_state_*.json'))

    print(f"  - Bot Status Files: {len(status_files)}")
    for f in status_files:
        print(f"    * {f.name}")

    print(f"  - V3 State Files: {len(state_files)}")
    for f in state_files:
        print(f"    * {f.name}")

    print("\n=== Instructions ===")
    print("If the 'Clear Trade History' button is not working in the UI:")
    print("1. Ensure you are logged in as 'admin'.")
    print("2. Check server_log.txt for '[Admin] Error in clear_trade_history'.")
    print("3. You can manually clear a client (e.g. ID 1) by running:")
    print("   sqlite3 config/algosoft.db \"DELETE FROM trade_history WHERE client_id=1;\"")
    print("   rm config/sell_v3_state_1_*.json")

if __name__ == "__main__":
    check_system()
