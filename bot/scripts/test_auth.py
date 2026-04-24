import sys
import os
import bcrypt

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from web.auth import hash_password, verify_password

def test_auth_logic():
    print("Testing Authentication Logic...")

    password = "Admin@123"
    hashed = hash_password(password)
    print(f"Password: {password}")
    print(f"Hash: {hashed}")

    # Test verification
    if verify_password(password, hashed):
        print("Verification: SUCCESS")
    else:
        print("Verification: FAILED")
        return False

    # Test negative verification
    if not verify_password("WrongPassword", hashed):
        print("Negative Verification: SUCCESS")
    else:
        print("Negative Verification: FAILED")
        return False

    print("All Auth Logic Tests Passed!")
    return True

if __name__ == "__main__":
    if test_auth_logic():
        sys.exit(0)
    else:
        sys.exit(1)
