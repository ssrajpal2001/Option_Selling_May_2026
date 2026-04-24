import sys
import os
from web.auth import hash_password
from web.db import db_execute, db_fetchone

def reset_user_password(username, new_password):
    user = db_fetchone("SELECT id FROM users WHERE username=?", (username,))
    if not user:
        print(f"Error: User '{username}' not found.")
        return False

    hashed = hash_password(new_password)
    db_execute("UPDATE users SET password_hash=? WHERE username=?", (hashed, username))
    print(f"Successfully reset password for user '{username}'.")
    return True

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: PYTHONPATH=. python3 scripts/reset_password.py <username> <new_password>")
    else:
        # Set PYTHONPATH to current directory if not already set or run from shell
        reset_user_password(sys.argv[1], sys.argv[2])
