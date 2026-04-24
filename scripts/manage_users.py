import sys
import os
import argparse

# Add the project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def create_or_update_user(username, email, password, role='client'):
    from web.auth import hash_password
    from web.db import db_execute, db_fetchone

    user = db_fetchone("SELECT id FROM users WHERE username=?", (username,))
    if user:
        print(f"User '{username}' already exists. Updating email, password and activation status.")
        hashed = hash_password(password)
        db_execute("UPDATE users SET email=?, password_hash=?, is_active=1, role=? WHERE username=?", (email, hashed, role, username))
        print(f"Successfully updated user '{username}'.")
    else:
        hashed = hash_password(password)
        db_execute(
            "INSERT INTO users (username, email, password_hash, role, is_active) VALUES (?,?,?,?,?)",
            (username, email, hashed, role, 1)
        )
        print(f"Successfully created user '{username}' with role '{role}'.")

def delete_user(username):
    from web.db import db_execute, db_fetchone

    user = db_fetchone("SELECT id FROM users WHERE username=?", (username,))
    if not user:
        print(f"Error: User '{username}' not found.")
        return False

    user_id = user['id']
    # Removing linked records to ensure a clean deletion
    print(f"Removing associated records for user '{username}' (ID: {user_id})...")
    db_execute("DELETE FROM trade_history WHERE client_id=?", (user_id,))
    db_execute("DELETE FROM order_failures WHERE client_id=?", (user_id,))
    db_execute("DELETE FROM broker_change_requests WHERE client_id=?", (user_id,))
    db_execute("DELETE FROM client_broker_instances WHERE client_id=?", (user_id,))
    db_execute("DELETE FROM users WHERE id=?", (user_id,))

    print(f"Successfully deleted user '{username}' and all associated records from all tables.")
    return True

def list_users():
    from web.db import db_fetchall, DB_PATH

    users = db_fetchall("SELECT username, email, role, is_active FROM users")
    print(f"\nUsing database at: {os.path.abspath(DB_PATH)}")
    print("--- Current Users in Database ---")
    if not users:
        print("No users found.")
    for u in users:
        status = "ACTIVE" if u['is_active'] else "INACTIVE"
        print(f"Username: {u['username']:<15} | Email: {u['email']:<25} | Role: {u['role']:<10} | Status: {status}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AlgoSoft User Management Tool")
    parser.add_argument("--db", help="Path to the algosoft.db file")

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Upsert command
    upsert_parser = subparsers.add_parser("upsert", help="Add or update a user")
    upsert_parser.add_argument("username", help="The username")
    upsert_parser.add_argument("email", help="The email address")
    upsert_parser.add_argument("password", help="The new password")
    upsert_parser.add_argument("role", nargs="?", default="client", help="The user role (default: client)")

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete a user and all their records")
    delete_parser.add_argument("username", help="The username to delete")

    # List command
    list_parser = subparsers.add_parser("list", help="List all users in the database")

    args = parser.parse_args()

    if args.command:
        if args.db:
            os.environ["ALGOSOFT_DB_PATH"] = args.db

        if args.command == "upsert":
            create_or_update_user(args.username, args.email, args.password, args.role)
        elif args.command == "delete":
            delete_user(args.username)
        elif args.command == "list":
            list_users()
    else:
        parser.print_help()
