"""Pure paywall logic: free tries → paid credits → 402; unlimited hook; pack catalog config."""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.billing.packs import pack_by_key, pack_by_lookup_key, packs, packs_summary
from app.billing.paywall import UsageState, next_bucket, upgrade_required
from app.config import CreditPack, Settings, get_settings
from app.models.enums import UsageBucket

NOW = datetime(2026, 10, 6, 21, 0, tzinfo=UTC)


def st(**kw):
    base = {"paywall_enabled": True, "free_limit": 10, "now": NOW}
    return UsageState(**{**base, **kw})


def test_approved_defaults():
    s = Settings()
    assert s.free_tries == 10 and s.billing_enabled is False
    assert Settings.model_fields["paywall_enabled"].default is False  # off until billing is live
    assert [(p.key, p.tries, p.unit_amount, p.lookup_key) for p in s.credit_packs] == [
        ("small", 30, 300, "plan_tries_30_v1"),
        ("large", 100, 500, "plan_tries_100_v1"),
    ]


def test_pack_lookup_helpers():
    assert pack_by_key("small").tries == 30
    assert pack_by_lookup_key("plan_tries_100_v1").key == "large"
    assert pack_by_key("pro") is None and pack_by_lookup_key(None) is None
    assert packs_summary()[0] == {
        "key": "small",
        "name": "30 tries",
        "tries": 30,
        "unit_amount": 300,
        "currency": "usd",
    }


def test_pack_catalog_validation():
    with pytest.raises(ValidationError):
        Settings(
            credit_packs=[{"key": "a", "name": "x", "tries": 1, "unit_amount": 1, "lookup_key": "k"}] * 2
        )
    with pytest.raises(ValidationError):
        CreditPack(key="Bad Key", name="x", tries=1, unit_amount=1, lookup_key="k")
    with pytest.raises(ValidationError):
        CreditPack(key="ok", name="x", tries=0, unit_amount=1, lookup_key="k")


def test_pack_catalog_env_override(monkeypatch):
    monkeypatch.setenv(
        "CREDIT_PACKS", '[{"key":"solo","name":"1 try","tries":1,"unit_amount":50,"lookup_key":"plan_try_1"}]'
    )
    assert [p.key for p in Settings().credit_packs] == ["solo"]


@pytest.mark.parametrize(
    ("kw", "bucket", "remaining"),
    [
        ({}, UsageBucket.FREE, 10),
        ({"free_used": 9}, UsageBucket.FREE, 1),
        ({"free_used": 10}, None, 0),
        ({"free_used": 10, "paid_balance": 30}, UsageBucket.PAID, 30),
        ({"free_used": 3, "paid_balance": 30}, UsageBucket.FREE, 37),  # free tries go first
        ({"free_used": 12}, None, 0),  # FREE_TRIES lowered below usage: no negatives
        ({"free_used": 10, "unlimited_until": NOW + timedelta(days=1)}, UsageBucket.UNLIMITED, None),
        ({"free_used": 10, "unlimited_until": NOW - timedelta(seconds=1)}, None, 0),
        ({"free_used": 10, "unlimited_until": NOW}, None, 0),  # boundary: expired
        ({"free_used": 10, "paywall_enabled": False}, UsageBucket.UNLIMITED, None),
    ],
)
def test_next_bucket(kw, bucket, remaining):
    state = st(**kw)
    assert next_bucket(state) == bucket
    assert state.tries_remaining == remaining


def test_free_limit_zero_means_pay_from_first_try():
    assert next_bucket(st(free_limit=0)) is None
    assert next_bucket(st(free_limit=0, paid_balance=1)) == UsageBucket.PAID


def test_paid_entitlement_flag():
    assert not st().has_paid_entitlement
    assert st(paid_balance=1).has_paid_entitlement
    assert st(unlimited_until=NOW + timedelta(hours=1)).has_paid_entitlement


def test_naive_unlimited_until_treated_as_utc():
    assert st(unlimited_until=(NOW + timedelta(hours=1)).replace(tzinfo=None)).unlimited


def test_upgrade_required_shape(monkeypatch):
    monkeypatch.setattr(get_settings(), "billing_enabled", True)
    err = upgrade_required(st(free_used=10), "out_of_tries", "msg", prop_id="godwin-rec")
    assert (err.status_code, err.code) == (402, "UPGRADE_REQUIRED")
    d = err.details
    assert d["reason"] == "out_of_tries" and d["prop_id"] == "godwin-rec"
    assert (d["free_limit"], d["free_used"], d["free_remaining"], d["paid_balance"], d["tries_remaining"]) == (
        10, 10, 0, 0, 0,
    )  # fmt: skip
    assert d["purchase_available"] is True
    assert [p["key"] for p in d["packs"]] == [p.key for p in packs()]
