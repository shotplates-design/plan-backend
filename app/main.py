"""FastAPI application factory."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import health
from app.api.v1.router import api_router
from app.billing.guard import secret_key_status
from app.config import get_settings
from app.core.errors import register_error_handlers

DESCRIPTION = """
Backend for **Plan**, an NFL-first sports betting web app (**play money only**).

* Auth: email + password (argon2id). Returns a JWT (`Authorization: Bearer ...`) and also sets an
  httpOnly cookie; either works.
* Money: all amounts are **integer cents** of play currency (`PLAY`).
* Odds: American odds (e.g. `-110`, `+150`). Sample odds are **not real**.
* Errors: `{"error": {"code", "message", "details"}}`.
* Paywall: 10 free tries per user (one try = unlocking one prop), then credit packs bought via
  Stripe Checkout. `GET /auth/me` returns `usage`. Gated endpoints return **402**
  `UPGRADE_REQUIRED` (`details.reason`: `prop_locked` | `out_of_tries`). Stripe is **test mode only**.
"""

TAGS = [
    {"name": "health", "description": "Liveness / DB check"},
    {"name": "auth", "description": "Signup, login, logout, current user"},
    {"name": "users", "description": "User preferences (sportsbook theme)"},
    {"name": "games", "description": "NFL teams, schedule and games with markets"},
    {"name": "markets", "description": "Odds feed (moneyline, spread, total)"},
    {"name": "bets", "description": "Quote, place and list bets (single + parlay)"},
    {"name": "wallet", "description": "Play-money balance and ledger"},
    {"name": "billing", "description": "Credit packs, Stripe Checkout, usage (TEST MODE ONLY)"},
    {"name": "webhooks", "description": "Stripe webhook receiver (signature-verified)"},
]

log = logging.getLogger("plan")


def _log_billing_status() -> None:
    s = get_settings()
    key = secret_key_status()
    if key == "live_refused":
        log.error("STRIPE_SECRET_KEY is a LIVE key: refused. Billing calls will return 503.")
    log.info(
        "Billing: paywall=%s free_tries=%s purchases=%s key=%s webhook_secret=%s approval=%s offseason=%s",
        s.paywall_enabled,
        s.free_tries,
        s.billing_enabled,
        key,
        "set" if s.stripe_webhook_secret.get_secret_value() else "missing",
        s.stripe_approval_status,
        s.billing_offseason_mode,
    )


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title=s.app_name,
        version="0.1.0",
        description=DESCRIPTION,
        openapi_tags=TAGS,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=s.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_error_handlers(app)
    _log_billing_status()
    app.include_router(health.router, include_in_schema=False)  # bare /health for load balancers
    app.include_router(api_router)
    return app


app = create_app()
