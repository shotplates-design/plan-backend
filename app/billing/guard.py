"""Hard test-mode guard. Every Stripe call path (API wrapper + setup script) goes through here.

Only `sk_test_` / `rk_test_` secret keys are accepted. Live keys are refused unconditionally —
there is deliberately no `--live` override in this codebase.
"""

from __future__ import annotations

from app.config import get_settings
from app.core.errors import AppError

ALLOWED_SECRET_PREFIXES = ("sk_test_", "rk_test_")
LIVE_SECRET_PREFIXES = ("sk_live_", "rk_live_")


class StripeKeyError(RuntimeError):
    """Raised by non-HTTP callers (CLI) when the key is missing or not test-mode."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def check_secret_key(key: str) -> str:
    """Return the key if it is a test-mode key; raise StripeKeyError otherwise."""
    key = (key or "").strip()
    if not key:
        raise StripeKeyError("STRIPE_NOT_CONFIGURED", "STRIPE_SECRET_KEY is not set")
    if key.startswith(LIVE_SECRET_PREFIXES):
        raise StripeKeyError(
            "STRIPE_LIVE_KEY_REFUSED",
            "Live Stripe keys are refused: this build is TEST MODE ONLY (use sk_test_/rk_test_)",
        )
    if not key.startswith(ALLOWED_SECRET_PREFIXES):
        raise StripeKeyError("STRIPE_KEY_INVALID", "STRIPE_SECRET_KEY must start with sk_test_ or rk_test_")
    return key


def configured_secret_key() -> str:
    """Validated test-mode secret key from settings (raises StripeKeyError)."""
    return check_secret_key(get_settings().stripe_secret_key.get_secret_value())


def secret_key_status() -> str:
    """'ok' | 'missing' | 'live_refused' | 'invalid' — for logs/health, never the key itself."""
    try:
        configured_secret_key()
        return "ok"
    except StripeKeyError as exc:
        return {
            "STRIPE_NOT_CONFIGURED": "missing",
            "STRIPE_LIVE_KEY_REFUSED": "live_refused",
        }.get(exc.code, "invalid")


def safe_publishable_key() -> str | None:
    """Only ever expose a pk_test_ key to clients."""
    pk = (get_settings().stripe_publishable_key or "").strip()
    return pk if pk.startswith("pk_test_") else None


def require_billing_enabled() -> None:
    if not get_settings().billing_enabled:
        raise AppError(
            503,
            "BILLING_DISABLED",
            "Billing is currently disabled",
            {"stripe_approval_status": get_settings().stripe_approval_status},
        )


def require_stripe_ready() -> None:
    """HTTP-facing check: billing on + valid test key. Raises a 503 AppError otherwise."""
    require_billing_enabled()
    try:
        configured_secret_key()
    except StripeKeyError as exc:
        raise AppError(503, exc.code, exc.message) from exc
