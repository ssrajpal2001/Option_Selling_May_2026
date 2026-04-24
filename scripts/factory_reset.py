import os
import sys
import glob
import argparse
from pathlib import Path

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def factory_reset():
    from web.db import db_execute, db_fetchone, DB_PATH
    from web.auth import hash_password

    print(f"--- AlgoSoft System Factory Reset ---")
    print(f"Targeting database: {os.path.abspath(DB_PATH)}")

    confirm = input("Are you SURE you want to clear all users, brokers, and trade history? (y/n): ")
    if confirm.lower() != 'y':
        print("Reset cancelled.")
        return

    try:
        # 1. Clear Tables
        print("\nClearing database tables...")
        tables_to_clear = [
            "trade_history",
            "order_failures",
            "audit_log",
            "broker_change_requests",
            "client_broker_instances",
            "users"
        ]

        for table in tables_to_clear:
            db_execute(f"DELETE FROM {table}")
            print(f" - {table}: CLEARED")

        # 2. Reset Auto-increments
        print("\nResetting ID sequences...")
        db_execute("DELETE FROM sqlite_sequence")
        print(" - All sequences reset to 1")

        # 3. Seed Default Admin
        print("\nRe-seeding default admin...")
        ph = hash_password("Admin@123")
        db_execute(
            "INSERT INTO users (username, email, password_hash, role, is_active) VALUES (?,?,?,?,?)",
            ("admin", "admin@algosoft.com", ph, "admin", 1)
        )
        print(" - Default admin created (admin / Admin@123)")

        # 4. Preserve Upstox in data_providers (Upsert)
        print("\nPreserving Upstox data provider...")
        existing = db_fetchone("SELECT id FROM data_providers WHERE provider='upstox'")
        if not existing:
            db_execute("INSERT INTO data_providers (provider, status) VALUES ('upstox', 'not_configured')")
            print(" - Upstox entry created")
        else:
            print(" - Upstox entry preserved")

        # 5. Clean up config files (JSON status/state)
        print("\nCleaning up status/state files in config/...")
        config_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config')
        patterns = [
            'bot_status_client_*.json',
            'sell_v3_state_*.json',
            'backtest_status_ui.json',
            'bot.pid'
        ]

        for pattern in patterns:
            files = glob.glob(os.path.join(config_dir, pattern))
            for f in files:
                try:
                    os.remove(f)
                    print(f" - Deleted: {os.path.basename(f)}")
                except Exception as e:
                    print(f" - Failed to delete {os.path.basename(f)}: {e}")

        print("\n--- Factory Reset Complete! ---")
        print("You can now start the web server and log in as 'admin' with 'Admin@123'.")

    except Exception as e:
        print(f"\nERROR during reset: {str(e)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AlgoSoft Factory Reset Tool")
    parser.add_argument("--db", help="Path to the algosoft.db file to reset")
    args = parser.parse_args()

    if args.db:
        os.environ["ALGOSOFT_DB_PATH"] = args.db

    factory_reset()
