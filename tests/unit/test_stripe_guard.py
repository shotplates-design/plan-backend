from types import SimpleNamespace

import pytest
import stripe
from pydantic import SecretStr

from app.billing.guard import (
    StripeKeyError,
    check_secret_key,
    require_stripe_ready,
    safe_publishable_key,
    secret_key_status,
)
from app.billing.stripe_setup import run_setup
from app.config import get_settings
from app.core.errors import AppError
from tests.conftest import ORIGINAL_STRIPE_CLIENT


class TestLiveKeyGuard:
    @pytest.mark.parametrize("key", ["sk_live_abc", "rk_live_abc", "  sk_live_abc  "])
    def test_live_keys_refused(self, key):
        with pytest.raises(StripeKeyError) as e:
            check_secret_key(key)
        assert e.value.code == "STRIPE_LIVE_KEY_REFUSED"

    @pytest.mark.parametrize("key", ["sk_test_abc", "rk_test_abc"])
    def test_test_keys_allowed(self, key):
        assert check_secret_key(key) == key

    @pytest.mark.parametrize(("key", "code"), [("", "STRIPE_NOT_CONFIGURED"),
                                                ("pk_test_abc", "STRIPE_KEY_INVALID"),
                                                ("whsec_abc", "STRIPE_KEY_INVALID"),
                                                ("sk_abc", "STRIPE_KEY_INVALID")])  # fmt: skip
    def test_missing_or_invalid(self, key, code):
        with pytest.raises(StripeKeyError) as e:
            check_secret_key(key)
        assert e.value.code == code

    def test_status_never_returns_key(self, monkeypatch):
        s = get_settings()
        monkeypatch.setattr(s, "stripe_secret_key", SecretStr("sk_live_secret"))
        assert secret_key_status() == "live_refused"
        monkeypatch.setattr(s, "stripe_secret_key", SecretStr("sk_test_x"))
        assert secret_key_status() == "ok"

    def test_client_builder_refuses_live_key(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("rk_live_x"))
        with pytest.raises(StripeKeyError):
            ORIGINAL_STRIPE_CLIENT()

    def test_client_builder_accepts_test_key_offline(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("sk_test_x"))
        assert isinstance(ORIGINAL_STRIPE_CLIENT(), stripe.StripeClient)  # no network on init

    def test_publishable_key_only_test(self, monkeypatch):
        s = get_settings()
        monkeypatch.setattr(s, "stripe_publishable_key", "pk_live_x")
        assert safe_publishable_key() is None
        monkeypatch.setattr(s, "stripe_publishable_key", "pk_test_x")
        assert safe_publishable_key() == "pk_test_x"

    def test_require_stripe_ready_codes(self, monkeypatch):
        s = get_settings()
        with pytest.raises(AppError) as e:
            require_stripe_ready()
        assert (e.value.status_code, e.value.code) == (503, "BILLING_DISABLED")
        monkeypatch.setattr(s, "billing_enabled", True)
        with pytest.raises(AppError) as e:
            require_stripe_ready()
        assert e.value.code == "STRIPE_NOT_CONFIGURED"
        monkeypatch.setattr(s, "stripe_secret_key", SecretStr("sk_live_x"))
        with pytest.raises(AppError) as e:
            require_stripe_ready()
        assert e.value.code == "STRIPE_LIVE_KEY_REFUSED"


# --------------------------------------------------------------------------- setup script


class _Svc:
    def __init__(self, store, kind, calls):
        self.store, self.kind, self.calls = store, kind, calls

    def list(self, params=None, options=None):
        self.calls.append((self.kind, "list"))
        items = self.store[self.kind]
        if params and "lookup_keys" in params:
            items = [i for i in items if i.get("lookup_key") in params["lookup_keys"]]
        return SimpleNamespace(data=list(items))

    def create(self, params=None, options=None):
        self.calls.append((self.kind, "create"))
        obj = {"id": f"{self.kind}_{len(self.store[self.kind]) + 1}", "active": True, **params}
        self.store[self.kind].append(obj)
        return obj

    def update(self, obj_id, params=None, options=None):
        self.calls.append((self.kind, "update"))
        obj = next(o for o in self.store[self.kind] if o["id"] == obj_id)
        obj.update(params)
        return obj


class FakeStripe:
    def __init__(self):
        self.store = {"prod": [], "price": []}
        self.calls: list = []
        self.v1 = SimpleNamespace(
            products=_Svc(self.store, "prod", self.calls),
            prices=_Svc(self.store, "price", self.calls),
        )


class TestSetupScript:
    def test_dry_run_makes_no_calls_and_needs_no_key(self):
        fake = FakeStripe()
        report = run_setup(dry_run=True, client=fake, out=lambda _: None)
        assert fake.calls == []
        assert report.created == 0
        assert any("plan_tries_30_v1" in a and "3.00 USD one-time (30 tries)" in a for a in report.actions)
        assert any("plan_tries_100_v1" in a and "5.00 USD one-time (100 tries)" in a for a in report.actions)
        assert not any("Portal" in a or "month" in a for a in report.actions)

    @pytest.mark.parametrize("key", ["sk_live_x", "rk_live_x"])
    def test_live_key_refused_even_for_dry_run(self, monkeypatch, key):
        monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr(key))
        fake = FakeStripe()
        with pytest.raises(StripeKeyError):
            run_setup(dry_run=True, client=fake, out=lambda _: None)
        with pytest.raises(StripeKeyError):
            run_setup(dry_run=False, client=fake, out=lambda _: None)
        assert fake.calls == []

    def test_real_run_requires_key(self):
        with pytest.raises(StripeKeyError):
            run_setup(dry_run=False, client=FakeStripe(), out=lambda _: None)

    def test_idempotent(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("sk_test_x"))
        fake = FakeStripe()
        first = run_setup(dry_run=False, client=fake, out=lambda _: None)
        assert first.created == 2 + 2
        prices = {p["lookup_key"]: p for p in fake.store["price"]}
        assert prices["plan_tries_30_v1"]["unit_amount"] == 300
        assert prices["plan_tries_100_v1"]["unit_amount"] == 500
        assert all("recurring" not in p for p in prices.values())  # one-time prices
        assert prices["plan_tries_100_v1"]["metadata"] == {"app": "plan", "pack": "large", "tries": "100"}
        assert {p["metadata"]["pack"] for p in fake.store["prod"]} == {"small", "large"}

        second = run_setup(dry_run=False, client=fake, out=lambda _: None)
        assert second.created == 0
        assert len(fake.store["prod"]) == 2 and len(fake.store["price"]) == 2

    def test_price_mismatch_is_warned_not_changed(self, monkeypatch):
        monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("sk_test_x"))
        fake = FakeStripe()
        run_setup(dry_run=False, client=fake, out=lambda _: None)
        fake.store["price"][0]["unit_amount"] = 999
        report = run_setup(dry_run=False, client=fake, out=lambda _: None)
        assert report.created == 0
        assert any("amount 999 != 300" in w for w in report.warnings)
        assert fake.store["price"][0]["unit_amount"] == 999

    def test_custom_pack_catalog(self, monkeypatch):
        from app.config import CreditPack

        monkeypatch.setattr(
            get_settings(),
            "credit_packs",
            [
                CreditPack(
                    key="mega", name="500 tries", tries=500, unit_amount=1500, lookup_key="plan_tries_500_v1"
                )
            ],
        )
        report = run_setup(dry_run=True, client=FakeStripe(), out=lambda _: None)
        assert any("plan_tries_500_v1" in a and "15.00" in a for a in report.actions)
