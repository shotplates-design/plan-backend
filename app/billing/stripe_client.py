"""Thin Stripe SDK boundary. The ONLY module that talks to Stripe's API.

Every call goes through `_client()`, which enforces the test-mode key guard. Tests mock the
public functions in this module; `_client()` is additionally patched to fail in tests so an
unmocked call can never reach the network.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import stripe

from app.billing.guard import configured_secret_key
from app.config import get_settings
from app.core.errors import AppError

log = logging.getLogger("plan.billing.stripe")

_PRICE_CACHE_TTL_SECONDS = 600
_price_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _client() -> stripe.StripeClient:
    key = configured_secret_key()  # raises StripeKeyError for missing/live/invalid keys
    return stripe.StripeClient(key, stripe_version=get_settings().stripe_api_version, max_network_retries=2)


def plain(obj: Any) -> Any:
    """Recursively convert StripeObject/ListObject into plain dicts/lists."""
    if isinstance(obj, stripe.StripeObject):
        return {k: plain(v) for k, v in obj.to_dict().items()}
    if isinstance(obj, dict):
        return {k: plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [plain(v) for v in obj]
    return obj


def stripe_error(exc: stripe.StripeError) -> AppError:
    request_id = getattr(exc, "request_id", None)
    log.error("Stripe error: %s (request_id=%s)", exc.user_message or str(exc), request_id)
    return AppError(
        502,
        "STRIPE_ERROR",
        "Payment provider error; please retry",
        {"request_id": request_id, "stripe_code": getattr(exc, "code", None)},
    )


def clear_price_cache() -> None:
    _price_cache.clear()


# --------------------------------------------------------------------------- reads


def get_prices_by_lookup_keys(lookup_keys: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve lookup keys → active Price dicts (cached ~10 min in-process)."""
    now = time.monotonic()
    result: dict[str, dict[str, Any]] = {}
    missing = []
    for key in lookup_keys:
        hit = _price_cache.get(key)
        if hit and hit[0] > now:
            result[key] = hit[1]
        else:
            missing.append(key)
    if missing:
        listing = _client().v1.prices.list(
            params={"lookup_keys": missing, "active": True, "expand": ["data.product"], "limit": 100}
        )
        for price in listing.data:
            p = plain(price)
            _price_cache[p["lookup_key"]] = (now + _PRICE_CACHE_TTL_SECONDS, p)
            result[p["lookup_key"]] = p
    return result


_SESSION_EXPAND = ["line_items.data.price"]


def retrieve_checkout_session(session_id: str) -> dict[str, Any]:
    """Current state of a Checkout Session, with line items (price + lookup_key) expanded."""
    return plain(_client().v1.checkout.sessions.retrieve(session_id, params={"expand": _SESSION_EXPAND}))


def find_checkout_session_for_payment_intent(payment_intent_id: str) -> dict[str, Any] | None:
    listing = _client().v1.checkout.sessions.list(
        params={"payment_intent": payment_intent_id, "limit": 1, "expand": ["data.line_items.data.price"]}
    )
    return plain(listing.data[0]) if listing.data else None


def retrieve_charge(charge_id: str) -> dict[str, Any]:
    return plain(_client().v1.charges.retrieve(charge_id))


# --------------------------------------------------------------------------- writes


def create_customer(*, email: str, user_id: str) -> dict[str, Any]:
    cust = _client().v1.customers.create(
        params={"email": email, "metadata": {"user_id": user_id, "app": "plan"}},
        options={"idempotency_key": f"customer:{user_id}"},
    )
    return plain(cust)


def create_checkout_session(params: dict[str, Any], *, idempotency_key: str) -> dict[str, Any]:
    session = _client().v1.checkout.sessions.create(
        params=params, options={"idempotency_key": idempotency_key}
    )
    return plain(session)


# --------------------------------------------------------------------------- webhooks


def construct_event(payload: bytes, sig_header: str, secret: str) -> dict[str, Any]:
    """Verify the Stripe-Signature header (HMAC-SHA256, 5-min tolerance). Offline; no API call."""
    event = stripe.Webhook.construct_event(payload, sig_header, secret)
    return plain(event)
