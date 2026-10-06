"""Play-money wallet ledger. All balance changes go through `post_transaction`."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import Wallet, WalletTransaction
from app.models.enums import WalletTxType


def get_wallet(db: Session, user_id: uuid.UUID, *, for_update: bool = False) -> Wallet:
    stmt = select(Wallet).where(Wallet.user_id == user_id)
    if for_update:
        stmt = stmt.with_for_update()  # row lock on Postgres; no-op on SQLite
    wallet = db.scalars(stmt).one_or_none()
    if wallet is None:
        raise AppError(404, "WALLET_NOT_FOUND", "Wallet not found")
    return wallet


def post_transaction(
    db: Session,
    wallet: Wallet,
    tx_type: WalletTxType,
    amount_cents: int,
    *,
    bet_id: uuid.UUID | None = None,
    description: str | None = None,
) -> WalletTransaction:
    """Append a ledger entry and update the cached balance. Caller commits.

    The wallet row should have been loaded with `for_update=True` inside the same transaction.
    """
    new_balance = wallet.balance_cents + amount_cents
    if new_balance < 0:
        raise AppError(
            400,
            "INSUFFICIENT_FUNDS",
            "Insufficient play-money balance",
            {"balance_cents": wallet.balance_cents, "required_cents": -amount_cents},
        )
    wallet.balance_cents = new_balance
    tx = WalletTransaction(
        wallet_id=wallet.id,
        type=tx_type,
        amount_cents=amount_cents,
        balance_after_cents=new_balance,
        bet_id=bet_id,
        description=description,
    )
    db.add(tx)
    return tx
