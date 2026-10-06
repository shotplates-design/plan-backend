"""Billing + paywall integration tests (credit packs). The Stripe SDK boundary
(app.billing.stripe_client) is mocked; webhook signatures are real HMAC-SHA256 with a local test
secret. No network."""

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import stripe
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.billing import stripe_client
from app.billing.paywall import et_today
from app.config import Settings, get_settings
from app.models import BillingCustomer, CreditPurchase, PropUnlock, StripeEvent, UsageAccount, UsageLedger
from app.models.enums import LedgerKind, PurchaseStatus, StripeEventStatus
from tests.conftest import make_event, post_event, sign_payload, signup

# The paywall is OFF by default (until billing is live); these tests enforce it explicitly.
pytestmark = pytest.mark.usefixtures("paywall_on")

NOW = int(time.time())
LOOKUP = {"small": "plan_tries_30_v1", "large": "plan_tries_100_v1"}
AMOUNT = {"small": 300, "large": 500}


def session_obj(sid, user_id, *, pack="small", payment_status="paid", status="complete", customer="cus_test_1",
                pi="pi_test_1", mode="payment", livemode=False, lookup=None):  # fmt: skip
    lk = lookup or LOOKUP[pack]
    return {
        "id": sid,
        "object": "checkout.session",
        "mode": mode,
        "status": status,
        "payment_status": payment_status,
        "livemode": livemode,
        "customer": customer,
        "client_reference_id": str(user_id) if user_id else None,
        "payment_intent": pi,
        "amount_total": AMOUNT.get(pack, 300),
        "currency": "usd",
        "metadata": {"user_id": str(user_id), "pack": pack, "lookup_key": lk} if user_id else {},
        "line_items": {
            "data": [{"price": {"id": f"price_{lk}", "lookup_key": lk, "unit_amount": AMOUNT.get(pack)}}]
        },
    }


class FakeStripeBoundary:
    """In-memory stand-in for app.billing.stripe_client's public functions."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}
        self.charges: dict[str, dict] = {}
        self.calls: list[tuple[str, object]] = []
        self.retrieve_error: Exception | None = None
        self.prices_missing = False

    def get_prices_by_lookup_keys(self, keys):
        self.calls.append(("prices", keys))
        if self.prices_missing:
            return {}
        amounts = {"plan_tries_30_v1": 300, "plan_tries_100_v1": 500}
        return {k: {"id": f"price_for_{k}", "lookup_key": k, "unit_amount": amounts[k], "currency": "usd"}
                for k in keys if k in amounts}  # fmt: skip

    def create_customer(self, *, email, user_id):
        self.calls.append(("customer", user_id))
        return {"id": f"cus_test_{user_id[:8]}", "livemode": False}

    def create_checkout_session(self, params, *, idempotency_key):
        self.calls.append(("checkout", {"params": params, "idempotency_key": idempotency_key}))
        return {"id": "cs_test_123", "url": "https://checkout.stripe.com/c/pay/cs_test_123"}

    def retrieve_checkout_session(self, session_id):
        self.calls.append(("retrieve_session", session_id))
        if self.retrieve_error:
            raise self.retrieve_error
        if session_id not in self.sessions:
            raise stripe.InvalidRequestError("No such checkout session", "id")
        return self.sessions[session_id]

    def find_checkout_session_for_payment_intent(self, pi):
        self.calls.append(("find_session", pi))
        return next((s for s in self.sessions.values() if s.get("payment_intent") == pi), None)

    def retrieve_charge(self, charge_id):
        self.calls.append(("retrieve_charge", charge_id))
        if self.retrieve_error:
            raise self.retrieve_error
        return self.charges[charge_id]

    def count(self, kind):
        return sum(1 for c in self.calls if c[0] == kind)


@pytest.fixture
def fake_stripe(monkeypatch):
    fake = FakeStripeBoundary()
    for name in ("get_prices_by_lookup_keys", "create_customer", "create_checkout_session",
                 "retrieve_checkout_session", "find_checkout_session_for_payment_intent",
                 "retrieve_charge"):  # fmt: skip
        monkeypatch.setattr(stripe_client, name, getattr(fake, name))
    return fake


def auth(body):
    return {"Authorization": f"Bearer {body['access_token']}"}


def new_user(client):
    body = signup(client, f"u{uuid.uuid4().hex[:8]}@example.com")
    client.cookies.clear()
    return body, uuid.UUID(body["user"]["id"])


def usage(client, body):
    r = client.get("/api/v1/auth/me", headers=auth(body))
    assert r.status_code == 200, r.text
    return r.json()["usage"]


def buy(client, fake, uid, sid="cs_buy", pack="small", pi=None):
    fake.sessions[sid] = session_obj(sid, uid, pack=pack, pi=pi or f"pi_{sid}")
    r = post_event(
        client, make_event("checkout.session.completed", {"id": sid, "mode": "payment"}, created=NOW)
    )
    assert r.status_code == 200, r.text
    return r


def account(db, uid):
    db.expire_all()
    return db.get(UsageAccount, uid)


# --------------------------------------------------------------------------- plans


def test_plans_without_stripe_keys(client):
    r = client.get("/api/v1/billing/plans")
    assert r.status_code == 200
    d = r.json()
    assert d["model"] == "credit_packs" and d["try_unit"] == "prop_unlock" and d["free_tries"] == 10
    assert [(p["key"], p["tries"], p["unit_amount"], p["source"]) for p in d["packs"]] == [
        ("small", 30, 300, "config"),
        ("large", 100, 500, "config"),
    ]
    assert [p["price_per_try_cents"] for p in d["packs"]] == [10.0, 5.0]
    assert [p["savings_pct"] for p in d["packs"]] == [None, 50]
    assert d["usage"] is None
    assert d["billing"]["enabled"] is False and d["billing"]["stripe_ready"] is False
    assert d["billing"]["publishable_key"] is None and d["billing"]["test_mode_only"] is True


def test_plans_with_stripe_prices_and_usage(client, billing_on, fake_stripe, monkeypatch):
    monkeypatch.setattr(billing_on, "stripe_publishable_key", "pk_test_abc")
    body, _ = new_user(client)
    d = client.get("/api/v1/billing/plans", headers=auth(body)).json()
    assert d["billing"]["stripe_ready"] is True and d["billing"]["publishable_key"] == "pk_test_abc"
    assert all(p["source"] == "stripe" for p in d["packs"])
    assert d["usage"]["free_remaining"] == 10 and d["usage"]["tries_remaining"] == 10
    monkeypatch.setattr(billing_on, "stripe_publishable_key", "pk_live_abc")
    assert client.get("/api/v1/billing/plans").json()["billing"]["publishable_key"] is None


# --------------------------------------------------------------------------- checkout


def test_checkout_disabled_and_unconfigured(client, monkeypatch, fake_stripe):
    body, _ = new_user(client)
    payload = {"pack": "small"}
    r = client.post("/api/v1/billing/checkout-session", json=payload, headers=auth(body))
    assert (r.status_code, r.json()["error"]["code"]) == (503, "BILLING_DISABLED")
    s = get_settings()
    monkeypatch.setattr(s, "billing_enabled", True)
    r = client.post("/api/v1/billing/checkout-session", json=payload, headers=auth(body))
    assert (r.status_code, r.json()["error"]["code"]) == (503, "STRIPE_NOT_CONFIGURED")
    for key, code in (("sk_live_nope", "STRIPE_LIVE_KEY_REFUSED"), ("rk_live_nope", "STRIPE_LIVE_KEY_REFUSED"),
                      ("pk_test_wrong_kind", "STRIPE_KEY_INVALID")):  # fmt: skip
        monkeypatch.setattr(s, "stripe_secret_key", SecretStr(key))
        r = client.post("/api/v1/billing/checkout-session", json=payload, headers=auth(body))
        assert (r.status_code, r.json()["error"]["code"]) == (503, code)
    assert fake_stripe.calls == []


def test_checkout_validation_and_auth(client, billing_on, fake_stripe):
    assert client.post("/api/v1/billing/checkout-session", json={"pack": "small"}).status_code == 401
    body, _ = new_user(client)
    r = client.post("/api/v1/billing/checkout-session", json={"pack": "Pro Monthly"}, headers=auth(body))
    assert r.status_code == 422
    r = client.post("/api/v1/billing/checkout-session", json={"pack": "mega"}, headers=auth(body))
    assert (r.status_code, r.json()["error"]["code"]) == (422, "UNKNOWN_PACK")
    assert r.json()["error"]["details"] == {"packs": ["small", "large"]}
    # Client can't choose the price: extra fields are ignored, server maps pack → lookup key.
    r = client.post("/api/v1/billing/checkout-session", json={"pack": "large", "price": "price_hack"},
                    headers=auth(body))  # fmt: skip
    assert r.status_code == 200
    assert fake_stripe.calls[-1][1]["params"]["line_items"] == [
        {"price": "price_for_plan_tries_100_v1", "quantity": 1}
    ]


def test_checkout_creates_one_time_payment_session(client, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    r = client.post("/api/v1/billing/checkout-session", json={"pack": "small"}, headers=auth(body))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out == {"checkout_url": "https://checkout.stripe.com/c/pay/cs_test_123", "session_id": "cs_test_123",
                   "pack": "small", "tries": 30, "unit_amount": 300}  # fmt: skip
    call = next(c for c in fake_stripe.calls if c[0] == "checkout")[1]
    p = call["params"]
    assert p["mode"] == "payment"
    assert "subscription_data" not in p and "allow_promotion_codes" not in p
    assert p["client_reference_id"] == str(uid) and p["customer"].startswith("cus_test_")
    assert p["line_items"] == [{"price": "price_for_plan_tries_30_v1", "quantity": 1}]
    meta = {"user_id": str(uid), "pack": "small", "tries": "30", "lookup_key": "plan_tries_30_v1"}
    assert p["metadata"] == meta and p["payment_intent_data"]["metadata"] == meta
    assert "{CHECKOUT_SESSION_ID}" in p["success_url"]
    assert call["idempotency_key"].startswith(f"checkout:{uid}:small:")
    bc = db.scalars(select(BillingCustomer).where(BillingCustomer.user_id == uid)).one()
    assert bc.stripe_customer_id == p["customer"]
    row = db.scalars(select(CreditPurchase).where(CreditPurchase.user_id == uid)).one()
    assert (row.status, row.tries, row.amount_total, row.pack_key) == (
        PurchaseStatus.PENDING,
        30,
        300,
        "small",
    )
    # No tries are credited by creating a session
    assert usage(client, body)["paid_balance"] == 0
    # Replay (same idempotent session id) → no duplicate purchase row, same customer
    client.post("/api/v1/billing/checkout-session", json={"pack": "small"}, headers=auth(body))
    assert fake_stripe.count("customer") == 1
    assert len(db.scalars(select(CreditPurchase).where(CreditPurchase.user_id == uid)).all()) == 1


def test_checkout_price_not_configured_and_stripe_error(client, billing_on, fake_stripe, monkeypatch):
    body, _ = new_user(client)
    fake_stripe.prices_missing = True
    r = client.post("/api/v1/billing/checkout-session", json={"pack": "small"}, headers=auth(body))
    assert (r.status_code, r.json()["error"]["code"]) == (503, "PRICE_NOT_CONFIGURED")
    fake_stripe.prices_missing = False

    def boom(*a, **k):
        raise stripe.APIConnectionError("network down")

    monkeypatch.setattr(stripe_client, "create_checkout_session", boom)
    r = client.post("/api/v1/billing/checkout-session", json={"pack": "small"}, headers=auth(body))
    assert (r.status_code, r.json()["error"]["code"]) == (502, "STRIPE_ERROR")


def test_portal_and_subscription_endpoints_removed(client):
    assert client.post("/api/v1/billing/portal-session").status_code in (404, 405)
    assert client.get("/api/v1/billing/subscription").status_code in (404, 405)


# --------------------------------------------------------------------------- webhooks: verification


def test_webhook_signature_required(client, billing_on, fake_stripe):
    event = make_event("checkout.session.completed", {"id": "cs_x", "mode": "payment"}, created=NOW)
    r = post_event(client, event, secret="whsec_wrong")
    assert (r.status_code, r.json()["error"]["code"]) == (400, "INVALID_SIGNATURE")
    r = client.post("/api/v1/webhooks/stripe", content=b"{}")
    assert (r.status_code, r.json()["error"]["code"]) == (400, "INVALID_SIGNATURE")
    good = json.dumps(event).encode()
    r = client.post("/api/v1/webhooks/stripe", content=good.replace(b"cs_x", b"cs_y"),
                    headers={"Stripe-Signature": sign_payload(good)})  # fmt: skip
    assert r.status_code == 400  # tampered body
    r = client.post("/api/v1/webhooks/stripe", content=good,
                    headers={"Stripe-Signature": sign_payload(good, ts=NOW - 3600)})  # fmt: skip
    assert r.status_code == 400  # outside 5-min tolerance
    assert fake_stripe.calls == []


def test_webhook_not_configured(client, fake_stripe):
    r = post_event(client, make_event("checkout.session.completed", {"id": "cs_x"}, created=NOW))
    assert (r.status_code, r.json()["error"]["code"]) == (503, "STRIPE_NOT_CONFIGURED")


def test_webhook_live_secret_key_refused(client, billing_on, fake_stripe, monkeypatch):
    monkeypatch.setattr(billing_on, "stripe_secret_key", SecretStr("sk_live_nope"))
    r = post_event(client, make_event("checkout.session.completed", {"id": "cs_x"}, created=NOW))
    assert (r.status_code, r.json()["error"]["code"]) == (503, "STRIPE_LIVE_KEY_REFUSED")
    assert fake_stripe.calls == []


# --------------------------------------------------------------------------- webhooks: fulfilment


def test_checkout_completed_credits_pack(client, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    fake_stripe.sessions["cs_1"] = session_obj("cs_1", uid, pack="large", customer="cus_new")
    # The event snapshot is never trusted (says unpaid, small) — the re-fetch is authoritative.
    snapshot = session_obj("cs_1", uid, pack="small", payment_status="unpaid")
    event = make_event("checkout.session.completed", snapshot, created=NOW)
    r = post_event(client, event)
    assert r.status_code == 200 and r.json() == {"received": True, "duplicate": None, "ignored": None}
    u = usage(client, body)
    assert u["paid_balance"] == 100 and u["tries_remaining"] == 110
    row = db.scalars(select(CreditPurchase).where(CreditPurchase.stripe_checkout_session_id == "cs_1")).one()
    assert (row.status, row.tries, row.pack_key, row.stripe_payment_intent_id) == (
        PurchaseStatus.PAID, 100, "large", "pi_test_1",
    )  # fmt: skip
    assert (
        db.scalars(select(BillingCustomer).where(BillingCustomer.user_id == uid)).one().stripe_customer_id
        == "cus_new"
    )
    ledger = db.scalars(select(UsageLedger).where(UsageLedger.user_id == uid)).all()
    assert [(entry.kind, entry.delta) for entry in ledger] == [(LedgerKind.PURCHASE, 100)]
    ev = db.get(StripeEvent, event["id"])
    assert ev.status == StripeEventStatus.PROCESSED and ev.payload["type"] == event["type"]


def test_webhook_idempotent_same_event_twice(client, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    fake_stripe.sessions["cs_2"] = session_obj("cs_2", uid)
    event = make_event("checkout.session.completed", {"id": "cs_2", "mode": "payment"}, created=NOW)
    assert post_event(client, event).json()["received"] is True
    second = post_event(client, event)
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert fake_stripe.count("retrieve_session") == 1
    assert usage(client, body)["paid_balance"] == 30


def test_multiple_paths_credit_exactly_once(client, billing_on, fake_stripe, db):
    """completed + async_payment_succeeded + success-page sync for one session → one credit."""
    body, uid = new_user(client)
    fake_stripe.sessions["cs_3"] = session_obj("cs_3", uid)
    r = client.get("/api/v1/billing/usage?session_id=cs_3", headers=auth(body))
    assert r.status_code == 200 and r.json()["synced_purchase"]["status"] == "paid"
    for etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        assert post_event(client, make_event(etype, {"id": "cs_3"}, created=NOW)).status_code == 200
    assert client.get("/api/v1/billing/usage?session_id=cs_3", headers=auth(body)).status_code == 200
    assert usage(client, body)["paid_balance"] == 30
    assert len(db.scalars(select(UsageLedger).where(UsageLedger.kind == LedgerKind.PURCHASE)).all()) == 1


def test_out_of_order_async_payment(client, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    # 1) completed while the (e.g. ACH) payment is still processing → pending, no credit
    fake_stripe.sessions["cs_4"] = session_obj("cs_4", uid, payment_status="unpaid")
    post_event(client, make_event("checkout.session.completed", {"id": "cs_4"}, created=NOW))
    assert usage(client, body)["paid_balance"] == 0
    row = db.scalars(select(CreditPurchase).where(CreditPurchase.stripe_checkout_session_id == "cs_4")).one()
    assert row.status == PurchaseStatus.PENDING
    # 2) payment clears; Stripe delivers async_payment_succeeded, then a late duplicate-ish
    #    'completed' (older created) arrives. Both re-fetch → paid → still one credit.
    fake_stripe.sessions["cs_4"] = session_obj("cs_4", uid, payment_status="paid")
    post_event(
        client, make_event("checkout.session.async_payment_succeeded", {"id": "cs_4"}, created=NOW + 60)
    )
    post_event(client, make_event("checkout.session.completed", {"id": "cs_4"}, created=NOW - 60))
    assert usage(client, body)["paid_balance"] == 30


def test_async_payment_failed_and_expired(client, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    fake_stripe.sessions["cs_f"] = session_obj("cs_f", uid, payment_status="unpaid", pi="pi_f")
    post_event(client, make_event("checkout.session.async_payment_failed", {"id": "cs_f"}, created=NOW))
    fake_stripe.sessions["cs_e"] = session_obj(
        "cs_e", uid, payment_status="unpaid", status="expired", pi=None
    )
    post_event(client, make_event("checkout.session.expired", {"id": "cs_e"}, created=NOW))
    rows = {r.stripe_checkout_session_id: r.status for r in db.scalars(select(CreditPurchase))}
    assert rows == {"cs_f": PurchaseStatus.FAILED, "cs_e": PurchaseStatus.EXPIRED}
    assert usage(client, body)["paid_balance"] == 0


def test_refund_claws_back_unused_tries(client, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    buy(client, fake_stripe, uid, sid="cs_r", pi="pi_r")
    acct = account(db, uid)
    acct.paid_balance = 25  # 5 tries already used
    db.commit()
    fake_stripe.charges["ch_r"] = {
        "id": "ch_r",
        "payment_intent": "pi_r",
        "refunded": True,
        "livemode": False,
    }
    r = post_event(client, make_event("charge.refunded", {"id": "ch_r"}, created=NOW + 10))
    assert r.status_code == 200
    row = db.scalars(select(CreditPurchase).where(CreditPurchase.stripe_checkout_session_id == "cs_r")).one()
    db.refresh(row)
    assert (row.status, row.tries_revoked) == (PurchaseStatus.REFUNDED, 25)
    assert usage(client, body)["paid_balance"] == 0  # never negative
    # Late redelivery of 'completed' after the refund does not re-credit
    post_event(client, make_event("checkout.session.completed", {"id": "cs_r"}, created=NOW))
    assert usage(client, body)["paid_balance"] == 0


def test_refund_arriving_before_fulfilment(client, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    fake_stripe.sessions["cs_o"] = session_obj("cs_o", uid, pi="pi_o")
    fake_stripe.charges["ch_o"] = {
        "id": "ch_o",
        "payment_intent": "pi_o",
        "refunded": True,
        "livemode": False,
    }
    assert post_event(client, make_event("charge.refunded", {"id": "ch_o"}, created=NOW)).status_code == 200
    post_event(client, make_event("checkout.session.completed", {"id": "cs_o"}, created=NOW - 100))
    assert usage(client, body)["paid_balance"] == 0
    kinds = [(e.kind, e.delta) for e in db.scalars(select(UsageLedger).order_by(UsageLedger.created_at))]
    assert kinds == [(LedgerKind.PURCHASE, 30), (LedgerKind.REFUND, -30)]


def test_partial_refund_ignored(client, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    buy(client, fake_stripe, uid, sid="cs_p", pi="pi_p")
    fake_stripe.charges["ch_p"] = {
        "id": "ch_p",
        "payment_intent": "pi_p",
        "refunded": False,
        "livemode": False,
    }
    r = post_event(client, make_event("charge.refunded", {"id": "ch_p"}, created=NOW))
    assert r.json()["ignored"] is True
    assert usage(client, body)["paid_balance"] == 30


def test_processing_failure_returns_500_then_retry_succeeds(client, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    fake_stripe.sessions["cs_5"] = session_obj("cs_5", uid)
    fake_stripe.retrieve_error = stripe.APIConnectionError("down")
    event = make_event("checkout.session.completed", {"id": "cs_5"}, created=NOW)
    r = post_event(client, event)
    assert (r.status_code, r.json()["error"]["code"]) == (500, "WEBHOOK_FAILED")
    ev = db.get(StripeEvent, event["id"])
    assert ev.status == StripeEventStatus.FAILED and "down" in ev.error
    fake_stripe.retrieve_error = None
    assert post_event(client, event).status_code == 200  # Stripe redelivery
    db.refresh(ev)
    assert ev.status == StripeEventStatus.PROCESSED and ev.attempts == 2
    assert usage(client, body)["paid_balance"] == 30


def test_non_actionable_events_ignored(client, billing_on, fake_stripe, db):
    _, uid = new_user(client)
    cases = {
        "cs_unknown_user": session_obj("cs_unknown_user", uuid.uuid4()),
        "cs_no_user": session_obj("cs_no_user", None),
        "cs_sub": session_obj("cs_sub", uid, mode="subscription"),
        "cs_other_price": session_obj("cs_other_price", uid, pack="other", lookup="someone_elses_price"),
        "cs_live": session_obj("cs_live", uid, livemode=True),
    }
    cases["cs_other_price"]["metadata"] = {}
    fake_stripe.sessions.update(cases)
    for sid in cases:
        r = post_event(client, make_event("checkout.session.completed", {"id": sid}, created=NOW))
        assert r.status_code == 200 and r.json()["ignored"] is True, sid
    live = make_event("checkout.session.completed", {"id": "cs_live"}, created=NOW, livemode=True)
    r = post_event(client, live)
    assert r.json()["ignored"] is True
    assert db.get(StripeEvent, live["id"]).status == StripeEventStatus.IGNORED
    r = post_event(client, make_event("customer.subscription.updated", {"id": "sub_1"}, created=NOW))
    assert r.json()["ignored"] is True
    assert db.scalars(select(UsageLedger)).all() == []


def test_success_page_sync_checks_ownership(client, billing_on, fake_stripe):
    body, uid = new_user(client)
    other, _ = new_user(client)
    fake_stripe.sessions["cs_9"] = session_obj("cs_9", uid, customer="cus_9")
    r = client.get("/api/v1/billing/usage?session_id=cs_9", headers=auth(other))
    assert (r.status_code, r.json()["error"]["code"]) == (404, "CHECKOUT_SESSION_NOT_FOUND")
    r = client.get("/api/v1/billing/usage?session_id=cs_missing", headers=auth(body))
    assert (r.status_code, r.json()["error"]["code"]) == (404, "CHECKOUT_SESSION_NOT_FOUND")
    r = client.get("/api/v1/billing/usage?session_id=cs_9", headers=auth(body))
    assert r.status_code == 200
    d = r.json()
    assert d["usage"]["paid_balance"] == 30 and d["purchases"][0]["status"] == "paid"
    assert usage(client, other)["paid_balance"] == 0


def test_usage_endpoint_without_billing(client):
    body, _ = new_user(client)
    d = client.get("/api/v1/billing/usage", headers=auth(body)).json()
    assert d["purchases"] == [] and d["usage"]["free_remaining"] == 10
    r = client.get("/api/v1/billing/usage?session_id=cs_x", headers=auth(body))
    assert (r.status_code, r.json()["error"]["code"]) == (503, "BILLING_DISABLED")


def test_balance_cannot_go_negative(db, client):
    _, uid = new_user(client)
    db.add(UsageAccount(user_id=uid, free_used=0, paid_balance=-1, paid_purchased_total=0, paid_used_total=0))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --------------------------------------------------------------------------- paywall gating (402)

PROPS_TB = ["godwin-rec", "lamb-rec", "lamb-yds", "evans-rec", "evans-yds", "prescott-pass",
            "mayfield-pass", "dowdle-rush"]  # fmt: skip
PROPS_KC = ["cook-rush", "cook-rec2", "kelce-rec", "mahomes-pass", "allen-pass", "allen-rush"]
TEN = [("tb-dal", p) for p in PROPS_TB] + [("kc-buf", p) for p in PROPS_KC[:2]]


def open_prop(client, h, game, prop):
    return client.get(f"/api/v1/games/{game}/props/{prop}", headers=h)


def test_ten_free_tries_then_402(client, seeded, db):
    body, uid = new_user(client)
    h = auth(body)
    for i, (g, p) in enumerate(TEN, start=1):
        r = open_prop(client, h, g, p)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["charged"] is True and d["bucket"] == "free"
        assert d["usage"]["free_used"] == i and d["usage"]["free_remaining"] == 10 - i
        assert d["unlocked"] is True and d["locked"] == [] and d["hitRate"]["season"] is not None
    again = open_prop(client, h, "tb-dal", "godwin-rec").json()  # same-day re-open: free
    assert again["charged"] is False and again["usage"]["free_used"] == 10
    r = open_prop(client, h, "kc-buf", "kelce-rec")
    assert r.status_code == 402
    err = r.json()["error"]
    assert err["code"] == "UPGRADE_REQUIRED" and err["details"]["reason"] == "out_of_tries"
    d = err["details"]
    assert (d["free_limit"], d["free_used"], d["free_remaining"], d["paid_balance"], d["tries_remaining"]) == (
        10, 10, 0, 0, 0,
    )  # fmt: skip
    assert d["purchase_available"] is False  # BILLING_ENABLED=false
    assert [(p["key"], p["tries"], p["unit_amount"]) for p in d["packs"]] == [
        ("small", 30, 300),
        ("large", 100, 500),
    ]
    assert d["prop_id"] == "kelce-rec"
    u = usage(client, body)
    assert u["props_unlocked"] == 10 and u["reopen_scope"] == "day" and u["unlocks_reset_at"] is not None
    assert len(db.scalars(select(PropUnlock).where(PropUnlock.user_id == uid)).all()) == 10


def test_purchase_unlocks_more_tries(client, seeded, billing_on, fake_stripe, db):
    body, uid = new_user(client)
    h = auth(body)
    for g, p in TEN:
        open_prop(client, h, g, p)
    assert open_prop(client, h, "kc-buf", "kelce-rec").status_code == 402
    buy(client, fake_stripe, uid, sid="cs_g")
    r = open_prop(client, h, "kc-buf", "kelce-rec")
    assert r.status_code == 200
    d = r.json()
    assert d["bucket"] == "paid" and d["usage"]["paid_balance"] == 29 and d["usage"]["tries_remaining"] == 29
    acct = account(db, uid)
    assert (acct.free_used, acct.paid_balance, acct.paid_purchased_total, acct.paid_used_total) == (
        10,
        29,
        30,
        1,
    )


def test_reopen_scope_day_vs_forever(client, seeded, db, monkeypatch):
    body, uid = new_user(client)
    h = auth(body)
    assert open_prop(client, h, "tb-dal", "godwin-rec").json()["charged"] is True
    row = db.scalars(select(PropUnlock).where(PropUnlock.user_id == uid)).one()
    row.unlock_date = et_today() - timedelta(days=1)  # pretend it was opened yesterday (ET)
    db.commit()
    monkeypatch.setattr(get_settings(), "paywall_reopen_scope", "forever")
    assert open_prop(client, h, "tb-dal", "godwin-rec").json()["charged"] is False
    monkeypatch.setattr(get_settings(), "paywall_reopen_scope", "day")
    d = open_prop(client, h, "tb-dal", "godwin-rec").json()
    assert d["charged"] is True and d["usage"]["free_used"] == 2
    assert open_prop(client, h, "tb-dal", "godwin-rec").json()["charged"] is False


def test_sub_endpoints_require_opened_prop(client, seeded):
    body, _ = new_user(client)
    h = auth(body)
    base = "/api/v1/games/tb-dal/props/godwin-rec"
    r = client.get(f"{base}/splits/vs-opponent", headers=h)
    assert r.status_code == 402
    d = r.json()["error"]["details"]
    assert r.json()["error"]["code"] == "UPGRADE_REQUIRED" and d["reason"] == "prop_locked"
    assert d["unlock_cost"] == 1 and d["tries_remaining"] == 10
    assert client.get(f"{base}/hit-rates?window=season", headers=h).status_code == 402
    r = client.get(f"{base}/hit-rates?window=L5", headers=h)  # board teaser stays free
    assert r.status_code == 200 and r.json()["hitRate"]["pct"] == 100
    assert usage(client, body)["free_used"] == 0  # sub-endpoints never consume
    open_prop(client, h, "tb-dal", "godwin-rec")
    assert client.get(f"{base}/splits/vs-opponent", headers=h).json()["label"] == "vs DAL"
    assert client.get(f"{base}/hit-rates?window=season", headers=h).status_code == 200
    assert usage(client, body)["free_used"] == 1


def test_board_lists_and_bets_never_consume(client, seeded, auth_headers):
    for path in ("/api/v1/games?season=2026&week=5", "/api/v1/games/tb-dal", "/api/v1/games/tb-dal/props"):
        assert client.get(path, headers=auth_headers).status_code == 200
    r = client.post(
        "/api/v1/bets",
        headers=auth_headers,
        json={"type": "single", "stake_cents": 500, "accept_odds_changes": True,
              "legs": [{"selection_key": "tb-dal|prop|godwin-rec", "odds_american": -125, "line": "Over 4.5"}]},
    )  # fmt: skip
    assert r.status_code in (200, 201), r.text
    u = client.get("/api/v1/auth/me", headers=auth_headers).json()["usage"]
    assert u["free_used"] == 0 and u["props_unlocked"] == 0


def test_prop_detail_requires_auth(client, seeded):
    assert client.get("/api/v1/games/tb-dal/props/godwin-rec").status_code == 401
    assert client.get("/api/v1/games/tb-dal/props/nope", headers={}).status_code == 401


def test_unknown_prop_404_without_charge(client, seeded):
    body, _ = new_user(client)
    r = open_prop(client, auth(body), "tb-dal", "nope")
    assert (r.status_code, r.json()["error"]["code"]) == (404, "PROP_NOT_FOUND")
    assert usage(client, body)["free_used"] == 0


def test_paywall_disabled_opens_everything(client, seeded, monkeypatch):
    monkeypatch.setattr(get_settings(), "paywall_enabled", False)
    g = client.get("/api/v1/games/tb-dal").json()
    prop = next(p for p in g["props"] if p["id"] == "godwin-rec")
    assert prop["unlocked"] is True and prop["hitRate"]["season"] is not None and prop["locked"] == []
    body, _ = new_user(client)
    d = open_prop(client, auth(body), "tb-dal", "godwin-rec").json()
    assert d["charged"] is False and d["usage"]["unlimited"] is True and d["usage"]["tries_remaining"] is None


def test_unlimited_comp_and_teaser_setting(client, seeded, db, monkeypatch):
    body, uid = new_user(client)
    db.add(UsageAccount(user_id=uid, free_used=10, paid_balance=0, paid_purchased_total=0, paid_used_total=0,
                        unlimited_until=datetime.now(UTC) + timedelta(days=3)))  # fmt: skip
    db.commit()
    d = open_prop(client, auth(body), "tb-dal", "godwin-rec").json()
    assert d["bucket"] == "unlimited" and d["charged"] is False
    monkeypatch.setattr(get_settings(), "paywall_board_teaser", "none")
    prop = next(p for p in client.get("/api/v1/games/tb-dal").json()["props"] if p["id"] == "lamb-rec")
    assert prop["hitRate"]["last5"] is None and prop["locked"] == ["season", "last5", "vs_opponent"]


def test_free_tries_configurable(client, seeded, monkeypatch):
    monkeypatch.setattr(get_settings(), "free_tries", 2)
    body, _ = new_user(client)
    h = auth(body)
    assert open_prop(client, h, "tb-dal", "godwin-rec").status_code == 200
    assert open_prop(client, h, "tb-dal", "lamb-rec").status_code == 200
    assert open_prop(client, h, "tb-dal", "lamb-yds").status_code == 402


def test_default_paywall_off_everything_unlocked(client, seeded, monkeypatch):
    default = Settings.model_fields["paywall_enabled"].default
    assert default is False
    monkeypatch.setattr(get_settings(), "paywall_enabled", default)  # undo module-level paywall_on
    body, _ = new_user(client)
    h = auth(body)
    g = client.get("/api/v1/games/tb-dal", headers=h).json()
    assert all(p["unlocked"] and p["locked"] == [] for p in g["props"])
    prop = next(p for p in g["props"] if p["id"] == "godwin-rec")
    assert prop["hitRate"]["season"] is not None and prop["hitRate"]["vsOpp"] is not None
    for gid, pid in [*TEN, ("kc-buf", "kelce-rec"), ("kc-buf", "mahomes-pass")]:  # > 10, never 402
        d = open_prop(client, h, gid, pid).json()
        assert d["charged"] is False and d["bucket"] is None
    assert client.get("/api/v1/games/tb-dal/props/lamb-rec/splits/vs-opponent", headers=h).status_code == 200
    u = usage(client, body)
    assert u["paywall_enabled"] is False and u["unlimited"] is True and u["tries_remaining"] is None
    assert u["free_used"] == 0 and u["props_unlocked"] == 0
    assert client.get("/api/v1/billing/plans").json()["usage"] is None


def test_me_shape(client):
    body, _ = new_user(client)
    m = client.get("/api/v1/auth/me", headers=auth(body)).json()
    assert {"id", "email", "display_name", "theme", "created_at", "usage"} <= set(m)
    assert "plan" not in m and "entitlements" not in m
    assert m["usage"] == {**m["usage"], "paywall_enabled": True, "free_limit": 10, "free_used": 0,
                          "free_remaining": 10, "paid_balance": 0, "tries_remaining": 10, "unlimited": False,
                          "props_unlocked": 0, "reopen_scope": "day"}  # fmt: skip
