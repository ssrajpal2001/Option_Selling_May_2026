from upstox_totp import UpstoxTOTP
import os

try:
    upx = UpstoxTOTP(
        username="dummy",
        password="dummy",
        pin_code="123456",
        totp_secret="ABCDEF1234567890",
        client_id="dummy",
        client_secret="dummy",
        redirect_uri="https://google.com"
    )
    print("UpstoxTOTP initialized successfully")
    # Check if we can call get_access_token (it will fail network but check structure)
    try:
        upx.app_token.get_access_token()
    except Exception as e:
        print(f"Call failed as expected: {e}")
except Exception as e:
    print(f"Initialization failed: {e}")
