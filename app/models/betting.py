import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._types import new_uuid, str_enum, utcnow
from app.models.enums import BetStatus, BetType, SelectionResult
from app.models.sports import Prop, Selection


class Bet(Base):
    __tablename__ = "bets"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_bet_idempotency"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[BetType] = mapped_column(str_enum(BetType, 8))
    status: Mapped[BetStatus] = mapped_column(str_enum(BetStatus, 8), default=BetStatus.PENDING)
    stake_cents: Mapped[int] = mapped_column(BigInteger)
    odds_american: Mapped[int] = mapped_column(Integer)
    odds_decimal: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    potential_payout_cents: Mapped[int] = mapped_column(BigInteger)
    payout_cents: Mapped[int | None] = mapped_column(BigInteger)
    idempotency_key: Mapped[str | None] = mapped_column(String(64))
    placed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    legs: Mapped[list["BetLeg"]] = relationship(
        back_populates="bet", cascade="all, delete-orphan", lazy="selectin"
    )


class BetLeg(Base):
    """One locked-in selection. Exactly one of selection_id / prop_id is set."""

    __tablename__ = "bet_legs"
    __table_args__ = (
        CheckConstraint(
            "(selection_id IS NOT NULL AND prop_id IS NULL) OR "
            "(selection_id IS NULL AND prop_id IS NOT NULL)",
            name="ck_leg_exactly_one_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    bet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bets.id", ondelete="CASCADE"), index=True)
    selection_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("selections.id"), index=True, nullable=True
    )
    prop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("props.id"), index=True, nullable=True)
    odds_american: Mapped[int] = mapped_column(Integer)
    line: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    line_label: Mapped[str | None] = mapped_column(String(32))
    result: Mapped[SelectionResult] = mapped_column(
        str_enum(SelectionResult, 8), default=SelectionResult.PENDING
    )

    bet: Mapped[Bet] = relationship(back_populates="legs")
    selection: Mapped[Selection | None] = relationship(lazy="joined")
    prop: Mapped[Prop | None] = relationship(lazy="joined")
