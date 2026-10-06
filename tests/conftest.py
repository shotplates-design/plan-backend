"""Test setup.

Tests run against TEST_DATABASE_URL (default: local Postgres `plan_test`). All tables are dropped
and recreated at session start and truncated between tests. Set TEST_DATABASE_URL=sqlite:///./test.db
to run without Postgres.
"""

import os
from collections.abc import Iterator

from tests.dotenv_shim import load_env_file

load_env_file(".env")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://plan:plan@localhost:5432/plan_test"
)
os.environ["ENVIRONMENT"] = "test"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app import models  # noqa: E402, F401
from app.cli import _upcoming_shift  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.odds_provider.mock import SAMPLE_SEASON, SAMPLE_WEEK, MockOddsProvider  # noqa: E402
from app.services.odds_sync import sync_week  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _schema() -> Iterator[None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clean_tables() -> Iterator[None]:
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture
def db() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def seeded(db: Session) -> None:
    """Sample week with kickoffs shifted into the future so it is bettable."""
    sync_week(db, MockOddsProvider(shift=_upcoming_shift()), SAMPLE_SEASON, SAMPLE_WEEK)


def signup(client: TestClient, email: str = "fan@example.com", password: str = "hunter2-long"):
    r = client.post("/api/v1/auth/signup", json={"email": email, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    body = signup(client)
    client.cookies.clear()  # force header auth so cookie doesn't mask bugs
    return {"Authorization": f"Bearer {body['access_token']}"}


# --------------------------------------------------------------------------- billing helpers

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402
import uuid as _uuid  # noqa: E402

from pydantic import SecretStr  # noqa: E402

from app.billing import stripe_client  # noqa: E402
from app.config import get_settings  # noqa: E402

ORIGINAL_STRIPE_CLIENT = stripe_client._client
TEST_WEBHOOK_SECRET = "whsec_test_local_only_secret"
TEST_SECRET_KEY = "sk_test_dummy_not_a_real_key"


@pytest.fixture(autouse=True)
def _billing_isolation(monkeypatch):
    """Known billing settings per test + hard block on any real Stripe network call."""
    s = get_settings()
    monkeypatch.setattr(s, "paywall_enabled", False)  # matches the default; use `paywall_on`
    monkeypatch.setattr(s, "free_tries", 10)
    monkeypatch.setattr(s, "paywall_board_teaser", "last5")
    monkeypatch.setattr(s, "billing_enabled", False)
    monkeypatch.setattr(s, "stripe_secret_key", SecretStr(""))
    monkeypatch.setattr(s, "stripe_webhook_secret", SecretStr(""))
    monkeypatch.setattr(s, "stripe_publishable_key", "")
    monkeypatch.setattr(s, "billing_offseason_mode", "none")
    monkeypatch.setattr(s, "credit_packs", type(s)().credit_packs)

    def _no_network():
        raise AssertionError("Real Stripe client requested in tests; mock app.billing.stripe_client")

    monkeypatch.setattr(stripe_client, "_client", _no_network)
    stripe_client.clear_price_cache()
    yield


@pytest.fixture
def paywall_on(monkeypatch):
    """Enforce the try limit (off by default until billing is live)."""
    monkeypatch.setattr(get_settings(), "paywall_enabled", True)
    return get_settings()


@pytest.fixture
def billing_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "billing_enabled", True)
    monkeypatch.setattr(s, "stripe_secret_key", SecretStr(TEST_SECRET_KEY))
    monkeypatch.setattr(s, "stripe_webhook_secret", SecretStr(TEST_WEBHOOK_SECRET))
    return s


def sign_payload(payload: bytes, secret: str = TEST_WEBHOOK_SECRET, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def make_event(etype: str, obj: dict, *, created: int, evt_id: str | None = None,
               livemode: bool = False) -> dict:  # fmt: skip
    return {
        "id": evt_id or f"evt_test_{_uuid.uuid4().hex[:20]}",
        "object": "event",
        "type": etype,
        "created": created,
        "livemode": livemode,
        "api_version": "2025-10-29.clover",
        "data": {"object": obj},
    }


def post_event(client, event: dict, secret: str = TEST_WEBHOOK_SECRET):
    payload = json.dumps(event).encode()
    return client.post(
        "/api/v1/webhooks/stripe",
        content=payload,
        headers={"Stripe-Signature": sign_payload(payload, secret), "Content-Type": "application/json"},
    )
