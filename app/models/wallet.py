import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._types import TimestampMixin, new_uuid, str_enum, utcnow
from app.models.enums import WalletTxType

if TYPE_CHECKING:
    from app.models.user import User


class Wallet(TimestampMixin, Base):
    """Play-money wallet. Amounts are integer cents. `balance_cents` is a cached sum of the ledger."""

    __tablename__ = "wallets"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    currency: Mapped[str] = mapped_column(String(8), default="PLAY")
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)

    user: Mapped["User"] = relationship(back_populates="wallet")
    transactions: Mapped[list["WalletTransaction"]] = relationship(back_populates="wallet")


class WalletTransaction(Base):
    """Append-only ledger entry. amount_cents is signed (+credit / -debit)."""

    __tablename__ = "wallet_transactions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id", ondelete="CASCADE"), index=True)
    type: Mapped[WalletTxType] = mapped_column(str_enum(WalletTxType, 16))
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    balance_after_cents: Mapped[int] = mapped_column(BigInteger)
    bet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bets.id"), index=True)
    description: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    wallet: Mapped[Wallet] = relationship(back_populates="transactions")
