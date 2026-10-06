import uuid
from datetime import UTC, datetime
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import AppError
from app.core.security import decode_access_token
from app.db import get_db
from app.models import AuthSession, User

_bearer = HTTPBearer(auto_error=False, description="JWT from /auth/login (or use the cookie)")

DB = Annotated[Session, Depends(get_db)]


def get_current_user(
    request: Request,
    db: DB,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    token = creds.credentials if creds else request.cookies.get(get_settings().auth_cookie_name)
    if not token:
        raise AppError(401, "UNAUTHORIZED", "Authentication required")
    try:
        claims = decode_access_token(token)
        session_id = uuid.UUID(claims["jti"])
        user_id = uuid.UUID(claims["sub"])
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise AppError(401, "INVALID_TOKEN", "Invalid or expired token") from exc

    sess = db.get(AuthSession, session_id)
    expires = sess.expires_at if sess else None
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if (
        sess is None
        or sess.user_id != user_id
        or sess.revoked_at is not None
        or (expires is not None and expires <= datetime.now(UTC))
    ):
        raise AppError(401, "SESSION_REVOKED", "Session is no longer valid")
    user = db.get(User, user_id)
    if user is None:
        raise AppError(401, "UNAUTHORIZED", "User no longer exists")
    request.state.session_id = session_id
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_optional_user(
    request: Request,
    db: DB,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User | None:
    """Like get_current_user but returns None for anonymous/invalid credentials (public routes)."""
    has_token = creds is not None or request.cookies.get(get_settings().auth_cookie_name)
    if not has_token:
        return None
    try:
        return get_current_user(request, db, creds)
    except AppError:
        return None


OptionalUser = Annotated[User | None, Depends(get_optional_user)]
