import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.models.enums import PurchaseStatus
from app.schemas.common import APIModel


class UsageOut(APIModel):
    """Paywall counters for the current user (UI hints — the server enforces)."""

    paywall_enabled: bool
    free_limit: int = Field(description="Lifetime free tries per user (FREE_TRIES, default 10)")
    free_used: int
    free_remaining: int
    paid_balance: int = Field(description="Purchased tries remaining")
    tries_remaining: int | None = Field(description="free_remaining + paid_balance; null = unlimited")
    unlimited: bool = Field(description="Paywall off or an unlimited comp is active")
    unlimited_until: datetime | None = None
    props_unlocked: int = Field(description="Distinct props opened in the current re-open window")
    reopen_scope: str = Field(description="day | forever: re-opening a prop is free within this scope")
    unlocks_reset_at: datetime | None = Field(
        None, description="Next ET midnight when reopen_scope=day (opened props relock then)"
    )


class PackOut(APIModel):
    key: str = Field(examples=["small"])
    name: str = Field(examples=["30 tries"])
    tries: int
    unit_amount: int = Field(description="Cents")
    currency: str = "usd"
    lookup_key: str
    price_per_try_cents: float
    savings_pct: int | None = Field(None, description="Per-try saving vs the smallest pack")
    source: Literal["stripe", "config"] = Field(
        description="'stripe' if the price was resolved live from Stripe test mode, else configured"
    )


class BillingConfigOut(APIModel):
    enabled: bool = Field(description="Purchases on (BILLING_ENABLED)")
    stripe_ready: bool = Field(description="Billing enabled and a valid TEST key is configured")
    test_mode_only: bool = True
    approval_status: str
    publishable_key: str | None = Field(description="pk_test_… only; null if unset")
    offseason: dict[str, Any]


class PlansOut(APIModel):
    model: Literal["credit_packs"] = "credit_packs"
    try_unit: Literal["prop_unlock"] = Field(
        "prop_unlock",
        description="One try = opening one prop's hit-rate detail; re-opening it the same ET day is free",
    )
    free_tries: int
    packs: list[PackOut]
    billing: BillingConfigOut
    usage: UsageOut | None = Field(None, description="Present when authenticated")


class CheckoutIn(APIModel):
    pack: str = Field(
        pattern=r"^[a-z0-9_]{1,32}$", examples=["small"], description="Pack key from /billing/plans"
    )


class CheckoutOut(APIModel):
    checkout_url: str
    session_id: str
    pack: str
    tries: int
    unit_amount: int


class PurchaseOut(APIModel):
    id: uuid.UUID
    pack: str
    tries: int
    amount_total: int | None
    currency: str
    status: PurchaseStatus
    created_at: datetime
    paid_at: datetime | None
    refunded_at: datetime | None
    tries_revoked: int


class BillingUsageOut(APIModel):
    usage: UsageOut
    purchases: list[PurchaseOut] = Field(description="Most recent first (max 20)")
    synced_purchase: PurchaseOut | None = Field(None, description="Set when ?session_id= was synced")


class WebhookAck(APIModel):
    received: bool = True
    duplicate: bool | None = None
    ignored: bool | None = None
