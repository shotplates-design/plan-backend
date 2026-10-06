"""Password hashing (argon2id) and JWT helpers."""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import get_settings

_hasher = PasswordHasher()  # argon2id with library defaults


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def create_access_token(user_id: uuid.UUID, session_id: uuid.UUID) -> tuple[str, datetime]:
    s = get_settings()
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=s.access_token_ttl_minutes)
    payload = {
        "sub": str(user_id),
        "jti": str(session_id),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "typ": "access",
    }
    token = jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)
    return token, expires


def decode_access_token(token: str) -> dict:
    s = get_settings()
    return jwt.decode(
        token,
        s.jwt_secret,
        algorithms=[s.jwt_algorithm],
        options={"require": ["sub", "jti", "exp"]},
    )
