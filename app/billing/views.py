"""Builders shared by /billing/* and /auth/me."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.billing.paywall import Access, props_unlocked_count, unlocks_reset_at
from app.config import get_settings
from app.models import CreditPurchase
from app.schemas.billing import PurchaseOut, UsageOut


def usage_out(db: Session, access: Access) -> UsageOut:
    st = access.state
    return UsageOut(
        paywall_enabled=st.paywall_enabled,
        free_limit=st.free_limit,
        free_used=min(st.free_used, st.free_limit),
        free_remaining=st.free_remaining,
        paid_balance=st.paid_balance,
        tries_remaining=st.tries_remaining,
        unlimited=st.unlimited,
        unlimited_until=st.unlimited_until,
        props_unlocked=props_unlocked_count(db, access.user_id) if access.user_id else 0,
        reopen_scope=get_settings().paywall_reopen_scope,
        unlocks_reset_at=unlocks_reset_at(),
    )


def purchase_out(p: CreditPurchase) -> PurchaseOut:
    return PurchaseOut(
        id=p.id,
        pack=p.pack_key,
        tries=p.tries,
        amount_total=p.amount_total,
        currency=p.currency,
        status=p.status,
        created_at=p.created_at,
        paid_at=p.paid_at,
        refunded_at=p.refunded_at,
        tries_revoked=p.tries_revoked or 0,
    )
