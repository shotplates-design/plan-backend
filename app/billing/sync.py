"""The one place Stripe objects are mapped onto our DB rows.

Callers pass FRESHLY RETRIEVED Stripe objects (never the webhook snapshot). Every transition is
monotonic (pending → paid → refunded; pending → failed/expired) and keyed on our
`credit_purchases` row, so duplicate, concurrent and out-of-order deliveries — plus the
success-page sync — all converge on one credit per paid Checkout Session.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing import stripe_client
from app.billing.packs import pack_by_key, pack_by_lookup_key
from app.billing.paywall import credit_tries
from app.models import BillingCustomer, CreditPurchase, User
from app.models.enums import LedgerKind, PurchaseStatus

log = logging.getLogger("plan.billing.sync")

PAID_PAYMENT_STATUSES = ("paid", "no_payment_required")


class IgnoreEventError(Exception):
    """Event is valid but not actionable (unknown user, not our product, …) → 200 'ignored'."""


def ts(value: int | None) -> datetime | None:
    return datetime.fromtimestamp(value, UTC) if value else None


def _as_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _id(value: Any) -> str | None:
    """Stripe fields may be an id string or an expanded object."""
    if isinstance(value, dict):
        return value.get("id")
    return value


# --------------------------------------------------------------------------- customers


def link_customer(db: Session, user_id: uuid.UUID, customer_id: str | None) -> None:
    """Record user ↔ Stripe customer, refusing to re-point an existing mapping."""
    if not customer_id:
        return
    by_user = db.scalars(select(BillingCustomer).where(BillingCustomer.user_id == user_id)).first()
    by_cust = db.scalars(
        select(BillingCustomer).where(BillingCustomer.stripe_customer_id == customer_id)
    ).first()
    if by_cust is not None and by_cust.user_id != user_id:
        raise IgnoreEventError(f"customer {customer_id} is linked to a different user")
    if by_user is not None and by_user.stripe_customer_id != customer_id:
        raise IgnoreEventError(f"user {user_id} already has customer {by_user.stripe_customer_id}")
    if by_user is None:
        db.add(BillingCustomer(user_id=user_id, stripe_customer_id=customer_id, livemode=False))
        db.flush()


# --------------------------------------------------------------------------- purchases


def session_lookup_key(session: dict[str, Any]) -> str | None:
    items = ((session.get("line_items") or {}).get("data")) or []
    if items:
        price = items[0].get("price") or {}
        if price.get("lookup_key"):
            return price["lookup_key"]
    return (session.get("metadata") or {}).get("lookup_key")


def _purchase_for_session(db: Session, session_id: str) -> CreditPurchase | None:
    return db.scalars(
        select(CreditPurchase)
        .where(CreditPurchase.stripe_checkout_session_id == session_id)
        .with_for_update()
    ).first()


def _new_purchase(db: Session, session: dict[str, Any]) -> CreditPurchase:
    """Purchase row for a session we didn't record at creation time (e.g. created elsewhere)."""
    meta = session.get("metadata") or {}
    user_id = _as_uuid(session.get("client_reference_id") or meta.get("user_id"))
    if user_id is None or db.get(User, user_id) is None:
        raise IgnoreEventError(f"checkout session {session.get('id')} does not map to a known user")
    lookup_key = session_lookup_key(session)
    pack = pack_by_lookup_key(lookup_key) or pack_by_key(meta.get("pack") or "")
    if pack is None:
        raise IgnoreEventError(f"checkout session {session.get('id')} is not for a configured credit pack")
    customer_id = _id(session.get("customer"))
    link_customer(db, user_id, customer_id)
    row = CreditPurchase(
        user_id=user_id,
        stripe_checkout_session_id=session["id"],
        stripe_customer_id=customer_id,
        pack_key=pack.key,
        lookup_key=pack.lookup_key,
        tries=pack.tries,
        currency=pack.currency,
        status=PurchaseStatus.PENDING,
        livemode=False,
        tries_revoked=0,
    )
    db.add(row)
    db.flush()
    return row


def sync_checkout_session(
    db: Session, session: dict[str, Any], *, event_type: str | None = None
) -> CreditPurchase:
    """Apply the CURRENT state of a Checkout Session. Credits tries exactly once. Caller commits."""
    if session.get("livemode"):
        raise IgnoreEventError("livemode checkout session ignored (test-mode-only build)")
    if session.get("mode") != "payment":
        raise IgnoreEventError(f"checkout session mode {session.get('mode')!r} is not a credit-pack payment")

    row = _purchase_for_session(db, session["id"]) or _new_purchase(db, session)
    pi = _id(session.get("payment_intent"))
    if pi and not row.stripe_payment_intent_id:
        row.stripe_payment_intent_id = pi
    if session.get("amount_total") is not None:
        row.amount_total = int(session["amount_total"])
    if not row.stripe_customer_id and _id(session.get("customer")):
        row.stripe_customer_id = _id(session.get("customer"))

    paid = session.get("payment_status") in PAID_PAYMENT_STATUSES
    if paid and row.status in (PurchaseStatus.PENDING, PurchaseStatus.FAILED, PurchaseStatus.EXPIRED):
        lk = session_lookup_key(session)
        if lk and row.lookup_key and lk != row.lookup_key:
            log.error(
                "Session %s price %s != recorded %s; crediting recorded pack", row.id, lk, row.lookup_key
            )
        row.status = PurchaseStatus.PAID
        row.paid_at = datetime.now(UTC)
        credit_tries(db, row.user_id, row.tries, kind=LedgerKind.PURCHASE, purchase_id=row.id,
                     note=f"{row.pack_key} pack ({session['id']})")  # fmt: skip
        log.info("Credited %s tries to user %s (purchase %s)", row.tries, row.user_id, row.id)
    elif not paid and row.status == PurchaseStatus.PENDING:
        if session.get("status") == "expired":
            row.status = PurchaseStatus.EXPIRED
        elif event_type == "checkout.session.async_payment_failed":
            row.status = PurchaseStatus.FAILED
    db.flush()
    return row


def apply_refund(db: Session, charge: dict[str, Any]) -> CreditPurchase:
    """Full refund → claw back the pack's tries (only what's unused; balance never goes < 0)."""
    if charge.get("livemode"):
        raise IgnoreEventError("livemode charge ignored (test-mode-only build)")
    pi = _id(charge.get("payment_intent"))
    if not pi:
        raise IgnoreEventError(f"charge {charge.get('id')} has no payment_intent")
    row = db.scalars(
        select(CreditPurchase).where(CreditPurchase.stripe_payment_intent_id == pi).with_for_update()
    ).first()
    if row is None:
        # Refund raced ahead of fulfilment: fulfil first so the claw-back nets out correctly.
        session = stripe_client.find_checkout_session_for_payment_intent(pi)
        if session is None:
            raise IgnoreEventError(f"no checkout session for payment_intent {pi}")
        row = sync_checkout_session(db, session)
    if not charge.get("refunded"):
        raise IgnoreEventError(f"partial refund on {charge.get('id')}; tries left unchanged (policy TBD)")
    if row.status != PurchaseStatus.PAID:
        return row  # already refunded, or never credited
    revoked = -credit_tries(db, row.user_id, -row.tries, kind=LedgerKind.REFUND, purchase_id=row.id,
                            note=f"refund {charge.get('id')}")  # fmt: skip
    row.status = PurchaseStatus.REFUNDED
    row.refunded_at = datetime.now(UTC)
    row.tries_revoked = revoked
    db.flush()
    return row
