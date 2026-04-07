"""
Admin authentication utilities for the dashboard.

Uses JWT (HS256) + bcrypt password hashing.

Environment variables required in .env:
  ADMIN_USERNAME      — login username          (default: "admin")
  ADMIN_PASSWORD_HASH — bcrypt hash of password (MUST be set)
  SECRET_KEY          — JWT signing secret      (MUST be set to something long + random)
  TOKEN_EXPIRE_HOURS  — token lifetime hours    (default: 12)

Generate a password hash (run once):
  python -c "import bcrypt; print(bcrypt.hashpw(b'your_password', bcrypt.gensalt()).decode())"
"""

import os
import sys
from datetime import datetime, timedelta, timezone

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


# ── Password helpers ────────────────────────────────────────────────────

def verify_password(plain: str, hashed: str) -> bool:
    """Verify a plain-text password against a stored bcrypt hash."""
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def hash_password(plain: str) -> str:
    """Hash a plain-text password (use this once to generate ADMIN_PASSWORD_HASH)."""
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt()).decode()


# ── JWT helpers ─────────────────────────────────────────────────────────

def create_access_token(username: str) -> str:
    """Create a signed JWT for the given username."""
    from jose import jwt
    expire = datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRE_HOURS)
    payload = {"sub": username, "exp": expire}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> str:
    """Decode and validate a JWT. Returns username or raises HTTPException."""
    from jose import jwt, JWTError
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            raise ValueError("no sub claim")
        return username
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Invalid or expired token: {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── FastAPI dependency ──────────────────────────────────────────────────

_bearer = HTTPBearer(auto_error=True)


def require_admin(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer),
) -> str:
    """
    FastAPI dependency — validates Bearer token and ensures the user is admin.

    Usage:
        @app.post("/api/ml/train")
        async def ml_train(bg: BackgroundTasks, _: str = Depends(require_admin)):
            ...
    """
    username = decode_token(credentials.credentials)
    if username != ADMIN_USERNAME:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return username
