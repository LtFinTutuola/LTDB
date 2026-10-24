"""
src/core/security.py
--------------------
Cryptographic helpers for password hashing (using stdlib hashlib.scrypt)
and JWT generation/verification (using PyJWT).
"""
import os
import hmac
import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
from pathlib import Path
import jwt
import yaml

from src.core.config import settings, BASE_DIR

# Scrypt parameters
_SCRYPT_N = 16384
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_LEN = 64
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    """
    Hashes a password using hashlib.scrypt with a cryptographically secure random salt.
    Format: scrypt$N$r$p$salt_hex$hash_hex
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        maxmem=0,
        dklen=_KEY_LEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verifies a plain password against an scrypt hash string using hmac.compare_digest.
    """
    try:
        parts = hashed_password.split("$")
        if len(parts) != 6 or parts[0] != "scrypt":
            return False
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = bytes.fromhex(parts[4])
        expected_hash = bytes.fromhex(parts[5])

        actual_hash = hashlib.scrypt(
            plain_password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=0,
            dklen=len(expected_hash),
        )
        return hmac.compare_digest(actual_hash, expected_hash)
    except Exception:
        return False


def ensure_jwt_secret() -> str:
    """
    Ensures that settings.AUTH_JWT_SECRET is populated.
    If empty, generates a cryptographically secure 48-byte token,
    persists it into src/config.yaml under auth.jwt_secret,
    and updates settings.AUTH_JWT_SECRET in memory.
    """
    secret = settings.AUTH_JWT_SECRET
    if secret and secret.strip():
        return secret.strip()

    new_secret = secrets.token_urlsafe(48)
    config_path = BASE_DIR / "src" / "config.yaml"
    try:
        cfg = {}
        if config_path.exists():
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}

        if "auth" not in cfg or not isinstance(cfg["auth"], dict):
            cfg["auth"] = {}
        cfg["auth"]["jwt_secret"] = new_secret

        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(cfg, f, sort_keys=False, default_flow_style=False, allow_unicode=True)

        settings.AUTH_JWT_SECRET = new_secret
        return new_secret
    except Exception:
        # Fallback in-memory if disk write fails
        settings.AUTH_JWT_SECRET = new_secret
        return new_secret


def create_access_token(
    user_id: str,
    username: str,
    role: str,
    token_version: int,
    ttl_days: Optional[int] = None,
) -> str:
    """
    Generates a signed JWT access token containing subject, username, role, and token_version.
    """
    secret = ensure_jwt_secret()
    days = ttl_days if ttl_days is not None else settings.AUTH_TOKEN_TTL_DAYS
    now = datetime.now(timezone.utc)
    exp = now + timedelta(days=days)

    payload = {
        "sub": str(user_id),
        "username": str(username),
        "role": str(role),
        "tv": int(token_version),
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """
    Decodes and validates a JWT token. Raises PyJWT exceptions on error.
    """
    secret = ensure_jwt_secret()
    return jwt.decode(token, secret, algorithms=[ALGORITHM])
