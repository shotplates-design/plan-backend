import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import AppError
from app.core.security import create_access_token, hash_password, verify_password
from app.models import AuthSession, User, Wallet
from app.models.enums import WalletTxType
from app.services.wallet import post_transaction

# Pre-computed hash so login timing is similar whether or not the email exists.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


def signup(
    db: Session,
    email: str,
    password: str,
    display_name: str | None,
    theme=None,
) -> User:
    if db.scalars(select(User.id).where(User.email == email)).first():
        raise AppError(409, "EMAIL_TAKEN", "An account with this email already exists")
    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        theme=theme,
    )
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise AppError(409, "EMAIL_TAKEN", "An account with this email already exists") from exc
    wallet = Wallet(user_id=user.id, balance_cents=0)
    db.add(wallet)
    db.flush()
    bonus = get_settings().signup_bonus_cents
    if bonus:
        post_transaction(
            db, wallet, WalletTxType.SIGNUP_BONUS, bonus, description="Play-money welcome credits"
        )
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalars(select(User).where(User.email == email)).one_or_none()
    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise AppError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    if not verify_password(password, user.password_hash):
        raise AppError(401, "INVALID_CREDENTIALS", "Invalid email or password")
    return user


def start_session(db: Session, user: User, *, user_agent: str | None, ip: str | None) -> tuple[str, datetime]:
    session_id = uuid.uuid4()
    token, expires = create_access_token(user.id, session_id)
    db.add(
        AuthSession(
            id=session_id,
            user_id=user.id,
            expires_at=expires,
            user_agent=(user_agent or "")[:512] or None,
            ip_address=ip,
        )
    )
    db.commit()
    return token, expires


def revoke_session(db: Session, session_id: uuid.UUID) -> None:
    sess = db.get(AuthSession, session_id)
    if sess and sess.revoked_at is None:
        sess.revoked_at = datetime.now(UTC)
        db.commit()
