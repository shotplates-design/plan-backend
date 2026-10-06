"""Application settings loaded from environment / .env (pydantic-settings)."""

from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class CreditPack(BaseModel):
    """A purchasable bundle of tries (one-time Stripe Price)."""

    key: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    name: str
    tries: int = Field(gt=0)
    unit_amount: int = Field(gt=0, description="Cents")
    currency: str = Field(default="usd", pattern=r"^[a-z]{3}$")
    lookup_key: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Plan API"
    environment: str = Field(default="development", description="development | test | production")

    # Postgres is the primary target. SQLite (sqlite:///./plan.db) works for quick local hacking.
    database_url: str = "postgresql+psycopg://plan:plan@localhost:5432/plan"

    # Auth
    jwt_secret: str = "change-me-in-dev-only-not-for-production-use"
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 60 * 24 * 7  # 7 days
    auth_cookie_name: str = "plan_session"
    auth_cookie_secure: bool = False  # set True behind HTTPS
    auth_cookie_samesite: str = "lax"

    # CORS (frontend dev servers)
    cors_origins: list[str] = ["http://localhost:3000", "http://localhost:5173"]

    # Play-money wallet
    signup_bonus_cents: int = 100_000  # 1,000.00 play credits
    min_stake_cents: int = 100  # 1.00
    max_stake_cents: int = 1_000_000  # 10,000.00
    max_parlay_legs: int = 12
    max_payout_cents: int = 100_000_000  # 1,000,000.00 cap

    # Odds provider: "mock" for now; real providers plug in via app.odds_provider
    odds_provider: str = "mock"
    # The Odds API (optional). Empty → load-nfl-data uses Plan Data's odds JSON snapshot;
    # the HTTP client is available for future re-pulls but is never required to boot.
    odds_api_key: SecretStr = SecretStr("")

    # ------------------------------------------------------------------ paywall
    # Pay-per-use model: FREE_TRIES lifetime free tries per user, then purchased credit packs.
    # One "try" = opening one prop's hit-rate detail (GET /games/{g}/props/{p}). Board/list views
    # and play-money bets never consume tries.
    # OFF by default (owner decision 2026-10-06): no try limit until billing is live. When false,
    # every prop is unlocked with full hit rates and no tries are consumed; /auth/me still returns
    # `usage` with paywall_enabled=false. Turn on together with BILLING_ENABLED=true.
    paywall_enabled: bool = False
    free_tries: int = Field(default=10, ge=0)
    # Re-opening an already-opened prop is free within this scope: "day" = same ET calendar day
    # (default), "forever" = once per user+prop.
    paywall_reopen_scope: Literal["day", "forever"] = "day"
    # What locked props show on board/list endpoints: "last5" teaser (default) or nothing.
    paywall_board_teaser: Literal["last5", "none"] = "last5"

    # ------------------------------------------------------------------ billing (Stripe)
    # TEST MODE ONLY. Live keys (sk_live_/rk_live_) are refused by app.billing.guard.
    # Purchases on/off (stands in for Stripe restricted-business approval). When false,
    # checkout returns 503 BILLING_DISABLED; free tries + already-purchased credits still work.
    billing_enabled: bool = False
    # Informational: Stripe restricted-business review state. Not a security control.
    stripe_approval_status: Literal["pending", "approved", "declined"] = "pending"
    # Offseason handling — STUB ONLY, surfaced in /billing/plans; no behaviour yet.
    billing_offseason_mode: Literal["none", "pause", "season_pass"] = "none"

    stripe_secret_key: SecretStr = SecretStr("")  # sk_test_... or rk_test_... only
    stripe_webhook_secret: SecretStr = SecretStr("")  # whsec_... (from `stripe listen`)
    stripe_publishable_key: str = ""  # pk_test_... (exposed via /billing/plans)
    stripe_api_version: str = "2025-10-29.clover"  # pin; match the webhook endpoint version

    # Credit packs (one-time Prices, looked up by lookup_key). JSON env override, e.g.
    # CREDIT_PACKS='[{"key":"small","name":"30 tries","tries":30,"unit_amount":300,
    #                 "lookup_key":"plan_tries_30_v1"}]'
    credit_packs: list[CreditPack] = Field(
        default_factory=lambda: [
            CreditPack(
                key="small", name="30 tries", tries=30, unit_amount=300, lookup_key="plan_tries_30_v1"
            ),
            CreditPack(
                key="large", name="100 tries", tries=100, unit_amount=500, lookup_key="plan_tries_100_v1"
            ),
        ]
    )

    checkout_success_url: str = "http://localhost:3000/billing/success?session_id={CHECKOUT_SESSION_ID}"
    checkout_cancel_url: str = "http://localhost:3000/pricing?canceled=1"

    @field_validator("credit_packs")
    @classmethod
    def _unique_packs(cls, v: list[CreditPack]) -> list[CreditPack]:
        if len({p.key for p in v}) != len(v) or len({p.lookup_key for p in v}) != len(v):
            raise ValueError("credit pack keys and lookup_keys must be unique")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
