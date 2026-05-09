from fastapi import APIRouter, Request, Response, HTTPException, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
from typing import Optional
from web.auth import hash_password, verify_password, create_access_token
from web.db import db_fetchone, db_execute, db_fetchall
from web.deps import get_current_user, require_admin

router = APIRouter(prefix="/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    username: str
    email: str
    password: str
    broker: Optional[str] = None
    full_name: str
    phone_number: str


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/register")
async def register(body: RegisterRequest):
    if len(body.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if db_fetchone("SELECT id FROM users WHERE username=?", (body.username,)):
        raise HTTPException(400, "Username already taken")
    if db_fetchone("SELECT id FROM users WHERE email=?", (body.email,)):
        raise HTTPException(400, "Email already registered")

    hashed = hash_password(body.password)
    db_execute(
        "INSERT INTO users (username, email, password_hash, role, is_active, full_name, phone_number) VALUES (?,?,?,?,?,?,?)",
        (body.username, body.email, hashed, "client", 0, body.full_name, body.phone_number)
    )

    new_user = db_fetchone("SELECT id FROM users WHERE username=?", (body.username,))
    # Notify admin of new registration
    try:
        admin_email_row = db_fetchone("SELECT value FROM platform_settings WHERE key='admin_email'", ())
        if admin_email_row and admin_email_row["value"]:
            from utils.emailer import send_email
            send_email(
                to=admin_email_row["value"],
                subject=f"New Registration: {body.username}",
                body_html=(
                    f"A new user has registered and is awaiting activation.\n\n"
                    f"Username: {body.username}\n"
                    f"Email:    {body.email}\n"
                    f"Name:     {body.full_name}\n"
                    f"Phone:    {body.phone_number}\n\n"
                    f"Log in to the admin panel to activate this account."
                )
            )
    except Exception:
        pass

    return {"success": True, "message": "Registration successful. Awaiting admin activation."}


@router.post("/login")
async def login(body: LoginRequest, response: Response):
    user = db_fetchone("SELECT * FROM users WHERE username=?", (body.username,))
    if not user:
        raise HTTPException(401, "Invalid username or password")
    if not verify_password(body.password, user["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    if not user["is_active"]:
        raise HTTPException(403, "Account not activated. Contact admin.")

    token = create_access_token({"sub": str(user["id"]), "role": user["role"], "username": user["username"]})
    response.set_cookie("access_token", token, httponly=True, samesite="lax", max_age=86400)
    return {
        "success": True,
        "role": user["role"],
        "username": user["username"],
        "redirect": "/admin" if user["role"] == "admin" else "/dashboard"
    }


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token")
    return {"success": True}


@router.get("/me")
async def me(user=Depends(get_current_user)):
    return {
        "id": user["id"],
        "username": user["username"],
        "email": user["email"],
        "role": user["role"],
        "subscription_tier": user["subscription_tier"],
        "max_brokers": user["max_brokers"],
    }


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@router.post("/forgot-password")
async def forgot_password(body: ForgotPasswordRequest, request: Request):
    user = db_fetchone("SELECT id, username, email FROM users WHERE email=?", (body.email,))
    # Always return success to prevent user enumeration
    if not user:
        return {"success": True, "message": "If that email is registered, a reset link has been sent."}

    import secrets, datetime
    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    token = secrets.token_urlsafe(32)
    expires_at = (datetime.datetime.now(IST) + datetime.timedelta(hours=2)).isoformat()
    db_execute(
        "INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?,?,?)",
        (user["id"], token, expires_at)
    )

    try:
        base_url = str(request.base_url).rstrip("/")
        reset_link = f"{base_url}/reset-password?token={token}"
        from utils.emailer import send_email
        send_email(
            to=user["email"],
            subject="AlgoSoft — Password Reset",
            body_html=(
                f"Hi {user['username']},<br><br>"
                f"Click the link below to reset your password (valid for 2 hours):<br><br>"
                f"<a href='{reset_link}'>{reset_link}</a><br><br>"
                f"If you didn't request this, ignore this email."
            )
        )
    except Exception:
        pass

    return {"success": True, "message": "If that email is registered, a reset link has been sent."}


@router.post("/reset-password")
async def reset_password(body: ResetPasswordRequest):
    if len(body.new_password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")

    import datetime
    row = db_fetchone(
        "SELECT * FROM password_reset_tokens WHERE token=? AND used=0",
        (body.token,)
    )
    if not row:
        raise HTTPException(400, "Invalid or already used reset token")

    IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    expires_at = datetime.datetime.fromisoformat(row["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=IST)
    if datetime.datetime.now(IST) > expires_at:
        raise HTTPException(400, "Reset token has expired. Please request a new one.")

    hashed = hash_password(body.new_password)
    db_execute("UPDATE users SET password_hash=? WHERE id=?", (hashed, row["user_id"]))
    db_execute("UPDATE password_reset_tokens SET used=1 WHERE id=?", (row["id"],))

    return {"success": True, "message": "Password updated successfully. You may now log in."}
