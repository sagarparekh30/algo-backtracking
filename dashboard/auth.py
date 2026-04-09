"""
Authentication & authorisation for Trading HQ.

Supports two authentication modes:
  1. Admin  — env-var based (ADMIN_USERNAME / ADMIN_PASSWORD_HASH)
             No DB required. Always has full access.
  2. Users  — stored in the `users` DB table with subscription expiry.

JWT payload:
  { sub: username, role: "admin"|"user", exp: ... }

Dependencies:
  require_admin  — only admin token accepted
  require_user   — any valid, non-expired subscription token accepted
  get_current_user — returns full user dict (role + plan info)
"""

import os
import sys
from datetime import datetime, timedelta, timezone, date
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import (
    ADMIN_USERNAME,
    ADMIN_PASSWORD_HASH,
    SECRET_KEY,
    TOKEN_EXPIRE_HOURS,
)

ALGORITHM = "HS256"
_bearer = HTTPBearer(auto_error=True)


# ── Password helpers ────────────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


# ── JWT helpers ─────────────────────────────────────────────────────────

def create_access_token(username: str, role: str = "user") -> str:
    from jose import jwt
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": username, "role": role, "exp": expire},
        SECRET_KEY, algorithm=ALGORITHM,
    )


def decode_token(token: str) -> dict:
    """Decode JWT. Returns payload dict or raises 401."""
    from jose import jwt, JWTError
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if not payload.get("sub"):
            raise ValueError("no sub claim")
        return payload
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── DB user lookup ───────────────────────────────────────────────────────

def _get_db_user(identifier: str) -> Optional[dict]:
    """
    Fetch a user row by username OR mobile number.
    Returns None if not found.
    """
    try:
        from db.connection import get_conn
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, email, mobile, password_hash, role, plan_type, "
                "plan_start, plan_expiry, is_active FROM users "
                "WHERE username=%s OR mobile=%s",
                (identifier, identifier)
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = ["id", "username", "email", "mobile", "password_hash", "role",
                    "plan_type", "plan_start", "plan_expiry", "is_active"]
            return dict(zip(cols, row))
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _is_subscription_valid(user: dict) -> bool:
    """Return True if the user's subscription is active and not expired."""
    if not user.get("is_active"):
        return False
    expiry = user.get("plan_expiry")
    if expiry is None:
        return False
    if isinstance(expiry, str):
        expiry = date.fromisoformat(expiry)
    return expiry >= date.today()


# ── Login function (used by /api/auth/login) ────────────────────────────

def authenticate_user(username: str, password: str) -> Optional[dict]:
    """
    Authenticate a login attempt.
    Returns user dict with 'role' key, or None on failure.
    """
    # Check admin credentials first (env-var based, no DB)
    if username == ADMIN_USERNAME:
        if verify_password(password, ADMIN_PASSWORD_HASH):
            return {"username": username, "role": "admin", "plan_type": "admin"}
        return None

    # Check DB users
    user = _get_db_user(username)
    if not user:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    if not user.get("is_active"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account is deactivated. Contact support.",
        )
    if not _is_subscription_valid(user):
        expiry = user.get("plan_expiry", "unknown")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Subscription expired on {expiry}. Please renew your plan.",
        )
    return user


# ── FastAPI dependencies ────────────────────────────────────────────────

def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """
    Validate token and return the current user dict.
    Raises 401 if invalid, 403 if subscription expired.
    """
    payload = decode_token(credentials.credentials)
    username = payload["sub"]
    role = payload.get("role", "user")

    if role == "admin":
        return {"username": username, "role": "admin", "plan_type": "admin"}

    # For regular users, re-validate subscription on every request
    user = _get_db_user(username)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="User not found.")
    if not _is_subscription_valid(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Subscription expired. Please renew.")
    return user


def require_admin(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """Dependency — only admin tokens pass."""
    payload = decode_token(credentials.credentials)
    if payload.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="Admin access required.")
    return payload["sub"]


def require_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> dict:
    """
    Dependency — any valid subscribed user (including admin) can pass.
    Returns current user dict.
    """
    return get_current_user(credentials)
