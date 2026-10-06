"""/billing/* routes: credit-pack pricing, Stripe Checkout (one-time payments), usage.

Stripe TEST MODE ONLY. The paywall itself never calls Stripe (see app.billing.paywall).
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any

import stripe
from fastapi import APIRouter, Query
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, OptionalUser
from app.billing import stripe_client
from app.billing.guard import StripeKeyError, require_stripe_ready, safe_publishable_key, secret_key_status
from app.billing.offseason import offseason_policy
from app.billing.packs import pack_by_key, packs
from app.billing.paywall import resolve_access
from app.billing.sync import IgnoreEventError, sync_checkout_session
from app.billing.views import purchase_out, usage_out
from app.config import get_settings
from app.core.errors import ERROR_RESPONSES, AppError
from app.models import BillingCustomer, CreditPurchase, User
from app.models.enums import PurchaseStatus
from app.schemas.billing import (
    BillingConfigOut,
    BillingUsageOut,
    CheckoutIn,
    CheckoutOut,
    PackOut,
    PlansOut,
)

log = logging.getLogger("plan.billing.api")

_BILLING_ERRORS = {
    **ERROR_RESPONSES,
    502: {"model": ERROR_RESPONSES[400]["model"], "description": "STRIPE_ERROR"},
    503: {
        "model": ERROR_RESPONSES[400]["model"],
        "description": "BILLING_DISABLED / STRIPE_NOT_CONFIGURED / STRIPE_LIVE_KEY_REFUSED / "
        "STRIPE_KEY_INVALID / PRICE_NOT_CONFIGURED",
    },
}
router = APIRouter(prefix="/billing", tags=["billing"], responses=_BILLING_ERRORS)


def _stripe_ready() -> bool:
    return get_settings().billing_enabled and secret_key_status() == "ok"


def _stripe_prices() -> dict[str, dict[str, Any]]:
    if not _stripe_ready():
        return {}
    try:
        return stripe_client.get_prices_by_lookup_keys([p.lookup_key for p in packs()])
    except (stripe.StripeError, StripeKeyError) as exc:
        log.warning("Falling back to configured pack prices: %s", exc)
        return {}


@router.get("/plans", response_model=PlansOut)
def list_plans(db: DB, user: OptionalUser) -> PlansOut:
    """Pricing: free tries + credit packs. Works without Stripe keys (falls back to config prices).
    Includes the caller's `usage` when authenticated."""
    s = get_settings()
    live = _stripe_prices()
    rows: list[PackOut] = []
    for p in packs():
        sp = live.get(p.lookup_key)
        amount = int(sp["unit_amount"]) if sp and sp.get("unit_amount") is not None else p.unit_amount
        rows.append(
            PackOut(
                key=p.key,
                name=p.name,
                tries=p.tries,
                unit_amount=amount,
                currency=(sp or {}).get("currency", p.currency),
                lookup_key=p.lookup_key,
                price_per_try_cents=round(amount / p.tries, 2),
                source="stripe" if sp else "config",
            )
        )
    if rows:
        base = max(r.price_per_try_cents for r in rows)
        for r in rows:
            saving = math.floor((1 - r.price_per_try_cents / base) * 100) if base else 0
            r.savings_pct = saving or None
    return PlansOut(
        free_tries=s.free_tries,
        packs=rows,
        billing=BillingConfigOut(
            enabled=s.billing_enabled,
            stripe_ready=_stripe_ready(),
            approval_status=s.stripe_approval_status,
            publishable_key=safe_publishable_key(),
            offseason=offseason_policy(),
        ),
        usage=usage_out(db, resolve_access(db, user)) if user else None,
    )


def _get_or_create_customer(db: DB, user: User) -> str:
    bc = db.scalars(select(BillingCustomer).where(BillingCustomer.user_id == user.id)).first()
    if bc is not None:
        return bc.stripe_customer_id
    cust = stripe_client.create_customer(email=user.email, user_id=str(user.id))
    if cust.get("livemode"):
        raise AppError(503, "STRIPE_LIVE_KEY_REFUSED", "Live-mode customer refused")
    db.add(BillingCustomer(user_id=user.id, stripe_customer_id=cust["id"], livemode=False))
    db.commit()
    return cust["id"]


@router.post("/checkout-session", response_model=CheckoutOut)
def create_checkout_session(body: CheckoutIn, user: CurrentUser, db: DB) -> CheckoutOut:
    """Hosted Stripe Checkout for a credit pack (one-time payment, `mode=payment`).

    The client sends only a pack key; the server maps it to a Price lookup key. Tries are
    credited by the webhook (or the success-page sync), never by this call.
    """
    require_stripe_ready()
    s = get_settings()
    pack = pack_by_key(body.pack)
    if pack is None:
        raise AppError(
            422, "UNKNOWN_PACK", f"Unknown pack {body.pack!r}", {"packs": [p.key for p in packs()]}
        )
    try:
        price = stripe_client.get_prices_by_lookup_keys([pack.lookup_key]).get(pack.lookup_key)
        if price is None:
            raise AppError(
                503,
                "PRICE_NOT_CONFIGURED",
                f"No active Stripe price for lookup key {pack.lookup_key}; "
                "run `python -m app.cli stripe-setup`",
            )
        if price.get("unit_amount") != pack.unit_amount:
            log.warning("Stripe price %s amount %s != configured %s", pack.lookup_key,
                        price.get("unit_amount"), pack.unit_amount)  # fmt: skip
        customer_id = _get_or_create_customer(db, user)
        meta = {
            "user_id": str(user.id),
            "pack": pack.key,
            "tries": str(pack.tries),
            "lookup_key": pack.lookup_key,
        }
        params = {
            "mode": "payment",
            "customer": customer_id,
            "client_reference_id": str(user.id),
            "line_items": [{"price": price["id"], "quantity": 1}],
            "metadata": meta,
            "payment_intent_data": {"metadata": meta, "description": f"Plan: {pack.name}"},
            "success_url": s.checkout_success_url,
            "cancel_url": s.checkout_cancel_url,
        }
        bucket = int(datetime.now(UTC).timestamp() // 300)
        session = stripe_client.create_checkout_session(
            params, idempotency_key=f"checkout:{user.id}:{pack.key}:{bucket}"
        )
    except stripe.StripeError as exc:
        raise stripe_client.stripe_error(exc) from exc

    existing = db.scalars(
        select(CreditPurchase).where(CreditPurchase.stripe_checkout_session_id == session["id"])
    ).first()
    if existing is None:  # idempotent replay within the 5-min bucket returns the same session
        db.add(
            CreditPurchase(
                user_id=user.id,
                stripe_checkout_session_id=session["id"],
                stripe_customer_id=customer_id,
                pack_key=pack.key,
                lookup_key=pack.lookup_key,
                tries=pack.tries,
                amount_total=price.get("unit_amount"),
                currency=price.get("currency", pack.currency),
                status=PurchaseStatus.PENDING,
                livemode=False,
                tries_revoked=0,
            )
        )
        db.commit()
    return CheckoutOut(
        checkout_url=session["url"],
        session_id=session["id"],
        pack=pack.key,
        tries=pack.tries,
        unit_amount=int(price.get("unit_amount") or pack.unit_amount),
    )


@router.get("/usage", response_model=BillingUsageOut)
def get_usage(
    user: CurrentUser,
    db: DB,
    session_id: str | None = Query(
        None, description="cs_… from the success page; syncs that purchase from Stripe immediately"
    ),
) -> BillingUsageOut:
    """Free tries, purchased balance and recent purchases from OUR DB.

    With `session_id` (success page), the Checkout Session is fetched from Stripe and fulfilled
    if paid — idempotent with the webhook, so tries are credited exactly once.
    """
    synced = None
    if session_id:
        require_stripe_ready()
        try:
            cs = stripe_client.retrieve_checkout_session(session_id)
        except stripe.InvalidRequestError as exc:
            raise AppError(404, "CHECKOUT_SESSION_NOT_FOUND", "Checkout session not found") from exc
        except stripe.StripeError as exc:
            raise stripe_client.stripe_error(exc) from exc
        bc = db.scalars(select(BillingCustomer).where(BillingCustomer.user_id == user.id)).first()
        owns = cs.get("client_reference_id") == str(user.id) or (
            bc is not None and cs.get("customer") == bc.stripe_customer_id
        )
        if not owns:
            raise AppError(404, "CHECKOUT_SESSION_NOT_FOUND", "Checkout session not found")
        try:
            synced = purchase_out(sync_checkout_session(db, cs))
            db.commit()
        except IgnoreEventError as exc:
            db.rollback()
            raise AppError(404, "CHECKOUT_SESSION_NOT_FOUND", "Not a credit-pack checkout session") from exc
    access = resolve_access(db, user)
    purchases = db.scalars(
        select(CreditPurchase)
        .where(CreditPurchase.user_id == user.id)
        .order_by(CreditPurchase.created_at.desc())
        .limit(20)
    ).all()
    return BillingUsageOut(
        usage=usage_out(db, access), purchases=[purchase_out(p) for p in purchases], synced_purchase=synced
    )
