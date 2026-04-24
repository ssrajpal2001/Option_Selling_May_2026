from web.auth import verify_password
from web.db import db_fetchone

def verify_reset():
    username = "testclient"
    password = "AlgoSoft@123"

    user = db_fetchone("SELECT * FROM users WHERE username=?", (username,))
    if not user:
        print("FAIL: User not found")
        return

    if verify_password(password, user["password_hash"]):
        print("SUCCESS: New password verified successfully.")
    else:
        print("FAIL: New password verification failed.")

if __name__ == "__main__":
    verify_reset()
