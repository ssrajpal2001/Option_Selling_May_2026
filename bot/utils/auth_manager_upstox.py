import base64
import logging
import os
import random
import string
import time
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

_API_BASE     = "https://api.upstox.com"
_SERVICE_BASE = "https://service.upstox.com"
_LOGIN_BASE   = "https://login.upstox.com"
_INTERNAL_REDIRECT = "https://api-v2.upstox.com/login/authorization/redirect"

_DEFAULT_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en;q=0.9",
    "content-type": "application/json",
    "origin": _LOGIN_BASE,
    "priority": "u=1, i",
    "referer": _LOGIN_BASE,
    "sec-ch-ua": '"Chromium";v="140", "Not=A?Brand";v="24", "Google Chrome";v="140"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "x-device-details": (
        "platform=WEB|osName=Mac OS/10.15.7|osVersion=Chrome/140.0.0.0"
        "|appVersion=4.0.0|modelName=Chrome|manufacturer=Apple"
        "|uuid=3Z1IVTlV4rUUGbNp8KP0"
        "|userAgent=Upstox 3.0 Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
}


def _make_request_id():
    return "WPRO-" + "".join(random.choices(string.ascii_letters + string.digits, k=10))


def _parse_json_response(resp):
    """
    Parse an Upstox JSON response.
    Returns the data payload dict/value on success.
    Raises ValueError with a human-readable message on failure.

    Handles two response shapes:
    1. Wrapped:  {"success": true/false, "data": {...}, "error": {...}}
    2. Flat:     {"access_token": "...", "user_id": "...", ...}
                 (no "success" key — entire body IS the data, e.g. token exchange endpoint)
    """
    try:
        body = resp.json()
    except Exception:
        raise ValueError(f"Non-JSON response (HTTP {resp.status_code}): {resp.text[:300]}")

    if not isinstance(body, dict):
        raise ValueError(f"Unexpected response shape: {body!r}")

    if "success" not in body:
        return body

    success = body.get("success", True)
    data    = body.get("data")
    err     = body.get("error")

    if not success or (isinstance(data, dict) and data.get("status") == "error"):
        if isinstance(err, dict):
            code = err.get("errorCode") or err.get("code") or ""
            msg  = err.get("message") or err.get("msg") or str(err)
            raise ValueError(f"Upstox error {code}: {msg}".strip(": "))
        raise ValueError(f"Upstox login failed: {body}")

    return data


def handle_upstox_login_automated(credentials, return_error=False):
    """
    Automated Upstox login using Mobile Number, PIN and TOTP.
    Implements the 6-step Upstox OAuth flow directly using curl_cffi + pyotp.
    No third-party GitHub-only library required — all dependencies are on PyPI.

    Args:
        credentials: dict with api_key, api_secret, user_id/broker_user_id, password/pin,
                     totp/totp_secret/totp_key keys.
        return_error: If True, returns {"token": str|None, "error": str|None}.
                      If False (default), returns the raw access token string or None.
                      Use return_error=True only in admin contexts where the real error
                      message needs to be surfaced to the UI.

    Returns:
        str|None          when return_error=False
        {"token", "error"} when return_error=True
    """
    def _ret(token, error=None):
        if return_error:
            return {"token": token, "error": error}
        return token

    api_key    = credentials.get("api_key")
    api_secret = credentials.get("api_secret")
    user_id    = (
        credentials.get("user_id") or
        credentials.get("broker_user_id") or
        credentials.get("username")
    )
    password   = credentials.get("password") or credentials.get("pin")
    totp_secret = (
        credentials.get("totp") or
        credentials.get("totp_secret") or
        credentials.get("totp_key")
    )

    if not all([api_key, api_secret, user_id, password, totp_secret]):
        missing = [k for k, v in {
            "api_key": api_key, "api_secret": api_secret,
            "user_id": user_id, "password": password, "totp_secret": totp_secret,
        }.items() if not v]
        msg = f"Missing credentials: {missing}"
        logger.warning(f"Upstox Automated Login: {msg} for user: {user_id}")
        return _ret(None, msg)

    redirect_uri = (
        credentials.get("redirect_uri") or
        os.environ.get("UPSTOX_REDIRECT_URI") or
        "https://google.com"
    )

    try:
        import pyotp
        from curl_cffi import requests as cffi_requests
    except ImportError as e:
        logger.error(f"Missing dependency for Upstox login: {e}. Run: pip install curl_cffi pyotp")
        return _ret(None, f"Missing dependency: {e}. Install with: pip install curl_cffi pyotp")

    request_id = _make_request_id()
    headers = {**_DEFAULT_HEADERS, "x-request-id": request_id}

    try:
        logger.info(f"Attempting background Upstox login for {user_id}...")
        session = cffi_requests.Session(impersonate="chrome131", headers=headers)

        # ── Step 1: Authorization dialog → extract user_id param from redirect ─
        step1_url = f"{_API_BASE}/v2/login/authorization/dialog"
        step1_params = {
            "response_type": "code",
            "client_id": api_key,
            "redirect_uri": redirect_uri,
        }
        resp1 = session.get(step1_url, params=step1_params, allow_redirects=True)
        parsed1 = urlparse(resp1.url)
        qs1 = parse_qs(parsed1.query)

        upstox_user_id = (qs1.get("user_id") or [""])[0]
        dialog_client_id = (qs1.get("client_id") or [api_key])[0]

        if not upstox_user_id:
            raise ValueError(
                f"Step 1 failed — could not extract user_id from redirect URL. "
                f"Final URL: {resp1.url!r}  status={resp1.status_code}"
            )

        time.sleep(1)

        # ── Step 2: Generate OTP ──────────────────────────────────────────────
        step2_url = f"{_SERVICE_BASE}/login/open/v6/auth/1fa/otp/generate"
        step2_payload = {"data": {"mobileNumber": user_id, "userId": upstox_user_id}}
        resp2 = session.post(step2_url, json=step2_payload)
        data2 = _parse_json_response(resp2)
        validate_otp_token = data2.get("validateOTPToken") or data2.get("validateOtpToken")
        if not validate_otp_token:
            raise ValueError(f"Step 2 failed — validateOTPToken missing. Response: {data2}")

        time.sleep(1)

        # ── Step 3: Validate TOTP ─────────────────────────────────────────────
        step3_url = f"{_SERVICE_BASE}/login/open/v4/auth/1fa/otp-totp/verify"
        live_totp = pyotp.TOTP(totp_secret).now()
        step3_payload = {"data": {"otp": live_totp, "validateOtpToken": validate_otp_token}}
        resp3 = session.post(step3_url, json=step3_payload)
        _parse_json_response(resp3)

        time.sleep(1)

        # ── Step 4: Submit PIN (2FA) ──────────────────────────────────────────
        step4_url = f"{_SERVICE_BASE}/login/open/v3/auth/2fa"
        pin_b64 = base64.b64encode(password.encode()).decode()
        step4_params = {"client_id": dialog_client_id, "redirect_uri": _INTERNAL_REDIRECT}
        step4_payload = {"data": {"twoFAMethod": "SECRET_PIN", "inputText": pin_b64}}
        resp4 = session.post(step4_url, params=step4_params, json=step4_payload, allow_redirects=True)
        _parse_json_response(resp4)

        time.sleep(1)

        # ── Step 5: OAuth authorize → extract auth code ───────────────────────
        step5_url = f"{_SERVICE_BASE}/login/v2/oauth/authorize"
        step5_params = {
            "client_id": dialog_client_id,
            "redirect_uri": _INTERNAL_REDIRECT,
            "requestId": request_id,
            "response_type": "code",
        }
        step5_payload = {"data": {"userOAuthApproval": True}}
        resp5 = session.post(step5_url, params=step5_params, json=step5_payload, allow_redirects=True)
        data5 = _parse_json_response(resp5)
        oauth_redirect = (data5 or {}).get("redirectUri") or ""
        parsed5 = urlparse(oauth_redirect)
        qs5 = parse_qs(parsed5.query)
        auth_code = (qs5.get("code") or [""])[0]
        if not auth_code:
            raise ValueError(
                f"Step 5 failed — authorization code missing from redirectUri. "
                f"redirectUri={oauth_redirect!r}"
            )

        time.sleep(1)

        # ── Step 6: Exchange code for access token ────────────────────────────
        step6_url = f"{_API_BASE}/v2/login/authorization/token"
        step6_data = (
            f"code={auth_code}"
            f"&client_id={api_key}"
            f"&client_secret={api_secret}"
            f"&redirect_uri={redirect_uri}"
            f"&grant_type=authorization_code"
        )
        token_session = cffi_requests.Session(impersonate="chrome131")
        resp6 = token_session.post(
            step6_url,
            data=step6_data,
            headers={"accept": "application/json", "content-type": "application/x-www-form-urlencoded"},
        )
        data6 = _parse_json_response(resp6)
        access_token = (data6 or {}).get("access_token")
        if not access_token:
            raise ValueError(f"Step 6 failed — access_token missing. Response data: {data6}")

        logger.info(f"Background Upstox login SUCCESS for {user_id}")
        return _ret(access_token)

    except Exception as e:
        logger.error(f"Upstox background auth error for {user_id}: {e}")
        return _ret(None, str(e))
