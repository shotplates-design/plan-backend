"""Billing + paywall tables.

Generic Stripe infra: `billing_customers` (user <-> Stripe Customer), `stripe_events`
(webhook idempotency/audit). Paywall (credit packs): `usage_accounts` (cached counters, row-locked),
`usage_ledger` (append-only audit of every free/paid try, purchase, refund, grant),
`credit_purchases` (one per Checkout Session), `prop_unlocks` (which props a user opened, per ET day).
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TimestampMixin, new_uuid, str_enum, utcnow
from app.models.enums import LedgerKind, PurchaseStatus, StripeEventStatus, UsageBucket


class BillingCustomer(TimestampMixin, Base):
    """One Stripe Customer per user. Never deleted (accounting)."""

    __tablename__ = "billing_customers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), unique=True)
    stripe_customer_id: Mapped[str] = mapped_column(String(64), unique=True)
    livemode: Mapped[bool] = mapped_column(Boolean, default=False)


class StripeEvent(Base):
    """Webhook idempotency + audit log, keyed by Stripe event id."""

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # evt_...
    type: Mapped[str] = mapped_column(String(80), index=True)
    livemode: Mapped[bool] = mapped_column(Boolean, default=False)
    stripe_created: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[StripeEventStatus] = mapped_column(str_enum(StripeEventStatus, 16))
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON().with_variant(JSONB, "postgresql"))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageAccount(TimestampMixin, Base):
    """Per-user paywall counters. Mutated only under SELECT ... FOR UPDATE alongside a ledger row."""

    __tablename__ = "usage_accounts"
    __table_args__ = (
        CheckConstraint("free_used >= 0", name="ck_usage_free_used_nonneg"),
        CheckConstraint("paid_balance >= 0", name="ck_usage_paid_balance_nonneg"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    free_used: Mapped[int] = mapped_column(Integer, default=0)
    paid_balance: Mapped[int] = mapped_column(Integer, default=0)  # purchased tries remaining
    paid_purchased_total: Mapped[int] = mapped_column(Integer, default=0)
    paid_used_total: Mapped[int] = mapped_column(Integer, default=0)
    # Pluggable "paid entitlement" hook: comps today, a subscription model later.
    unlimited_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UsageLedger(Base):
    """Append-only audit trail. Sum of `delta` per bucket reconciles with usage_accounts."""

    __tablename__ = "usage_ledger"
    __table_args__ = (Index("ix_usage_ledger_user_created", "user_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[LedgerKind] = mapped_column(str_enum(LedgerKind, 16))
    bucket: Mapped[UsageBucket] = mapped_column(str_enum(UsageBucket, 16))
    # free bucket: +1 per free try used; paid bucket: +N purchased/granted, -1 per use, -N refund
    delta: Mapped[int] = mapped_column(Integer)
    prop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("props.id", ondelete="SET NULL"))
    purchase_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("credit_purchases.id"))
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CreditPurchase(TimestampMixin, Base):
    """One row per Checkout Session (mode=payment). Fulfilment is idempotent on this row."""

    __tablename__ = "credit_purchases"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    stripe_checkout_session_id: Mapped[str] = mapped_column(String(128), unique=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64))
    pack_key: Mapped[str] = mapped_column(String(32))
    lookup_key: Mapped[str | None] = mapped_column(String(64))
    tries: Mapped[int] = mapped_column(Integer)
    amount_total: Mapped[int | None] = mapped_column(Integer)  # cents actually charged
    currency: Mapped[str] = mapped_column(String(3), default="usd")
    status: Mapped[PurchaseStatus] = mapped_column(
        str_enum(PurchaseStatus, 16), default=PurchaseStatus.PENDING
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tries_revoked: Mapped[int] = mapped_column(Integer, default=0)
    livemode: Mapped[bool] = mapped_column(Boolean, default=False)


class PropUnlock(Base):
    """A prop detail the user opened on a given ET day (one try per user+prop+day by default;
    PAYWALL_REOPEN_SCOPE=forever makes any earlier row count)."""

    __tablename__ = "prop_unlocks"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    prop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("props.id", ondelete="CASCADE"), primary_key=True)
    unlock_date: Mapped[date] = mapped_column(Date, primary_key=True)  # America/New_York calendar day
    bucket: Mapped[UsageBucket] = mapped_column(str_enum(UsageBucket, 16))
    unlocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
