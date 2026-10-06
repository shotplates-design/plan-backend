"""Stripe webhook processing: verify signature → idempotency by event id → re-fetch → apply.

Handled: checkout.session.completed / async_payment_succeeded / async_payment_failed / expired
(credit-pack purchases) and charge.refunded (claw back unused tries). Everything else is
recorded as ignored. The event snapshot is never trusted: the object is re-fetched from Stripe.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import stripe
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.billing import stripe_client
from app.billing.guard import StripeKeyError, configured_secret_key
from app.billing.sync import IgnoreEventError, apply_refund, sync_checkout_session, ts
from app.config import get_settings
from app.core.errors import AppError
from app.db import SessionLocal
from app.models import StripeEvent
from app.models.enums import StripeEventStatus

log = logging.getLogger("plan.billing.webhooks")

CHECKOUT_EVENTS = {
    "checkout.session.completed",
    "checkout.session.async_payment_succeeded",
    "checkout.session.async_payment_failed",
    "checkout.session.expired",
}
HANDLED_EVENTS = CHECKOUT_EVENTS | {"charge.refunded"}


def _dispatch(db: Session, event: dict[str, Any]) -> None:
    etype = event["type"]
    obj = event["data"]["object"]
    if etype not in HANDLED_EVENTS:
        raise IgnoreEventError(f"unhandled event type {etype}")
    if not obj.get("id"):
        raise IgnoreEventError(f"{etype} has no object id")
    # Re-fetch: StripeError propagates → 500 → Stripe redelivers (with backoff, up to 3 days).
    if etype in CHECKOUT_EVENTS:
        if obj.get("mode") not in (None, "payment"):
            raise IgnoreEventError(f"checkout session mode {obj.get('mode')!r} is not a credit-pack payment")
        sync_checkout_session(db, stripe_client.retrieve_checkout_session(obj["id"]), event_type=etype)
    else:
        apply_refund(db, stripe_client.retrieve_charge(obj["id"]))


def _record(event: dict[str, Any], status: StripeEventStatus, error: str | None = None) -> None:
    """Persist event outcome in a fresh transaction (used after a rollback)."""
    with SessionLocal() as db:
        row = db.get(StripeEvent, event["id"])
        if row is None:
            row = StripeEvent(
                id=event["id"],
                type=event["type"],
                livemode=bool(event.get("livemode")),
                stripe_created=ts(event.get("created")),
                payload=event,
                attempts=1,
                status=status,
            )
            db.add(row)
        row.status = status
        row.error = error
        if status in (StripeEventStatus.PROCESSED, StripeEventStatus.IGNORED):
            row.processed_at = datetime.now(UTC)
        db.commit()


def process_webhook(payload: bytes, sig_header: str | None) -> dict[str, Any]:
    """Sync handler (runs in a threadpool). Raises AppError for 400/500/503."""
    secret = get_settings().stripe_webhook_secret.get_secret_value()
    if not secret:
        raise AppError(503, "STRIPE_NOT_CONFIGURED", "STRIPE_WEBHOOK_SECRET is not set")
    if not sig_header:
        raise AppError(400, "INVALID_SIGNATURE", "Missing Stripe-Signature header")
    try:
        event = stripe_client.construct_event(payload, sig_header, secret)
    except (ValueError, stripe.SignatureVerificationError) as exc:
        raise AppError(400, "INVALID_SIGNATURE", "Invalid Stripe signature") from exc

    if event.get("livemode"):
        _record(event, StripeEventStatus.IGNORED, "livemode event refused (test-mode-only build)")
        return {"received": True, "ignored": True}

    try:
        configured_secret_key()  # need a valid test key to re-fetch from Stripe
    except StripeKeyError as exc:
        raise AppError(503, exc.code, exc.message) from exc

    with SessionLocal() as db:
        existing = db.get(StripeEvent, event["id"])
        if existing is not None and existing.status in (
            StripeEventStatus.PROCESSED,
            StripeEventStatus.IGNORED,
        ):
            return {"received": True, "duplicate": True}
        if existing is None:
            existing = StripeEvent(
                id=event["id"],
                type=event["type"],
                livemode=False,
                stripe_created=ts(event.get("created")),
                status=StripeEventStatus.PROCESSING,
                payload=event,
                attempts=1,
            )
            db.add(existing)
        else:
            existing.attempts += 1
            existing.status = StripeEventStatus.PROCESSING
        try:
            _dispatch(db, event)
            existing.status = StripeEventStatus.PROCESSED
            existing.processed_at = datetime.now(UTC)
            existing.error = None
            db.commit()
        except IgnoreEventError as exc:
            db.rollback()
            log.warning("Stripe event %s ignored: %s", event["id"], exc)
            _record(event, StripeEventStatus.IGNORED, str(exc))
            return {"received": True, "ignored": True}
        except IntegrityError as exc:
            db.rollback()
            dup = db.get(StripeEvent, event["id"])
            if dup is not None and dup.status == StripeEventStatus.PROCESSED:
                return {"received": True, "duplicate": True}
            log.exception("Stripe event %s failed (integrity)", event["id"])
            _record(event, StripeEventStatus.FAILED, repr(exc)[:2000])
            raise AppError(500, "WEBHOOK_FAILED", "Processing failed; Stripe will retry") from exc
        except Exception as exc:
            db.rollback()
            log.exception("Stripe event %s failed", event["id"])
            _record(event, StripeEventStatus.FAILED, repr(exc)[:2000])
            raise AppError(500, "WEBHOOK_FAILED", "Processing failed; Stripe will retry") from exc
    return {"received": True}
