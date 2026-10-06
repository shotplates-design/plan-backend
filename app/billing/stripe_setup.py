"""Idempotent Stripe TEST-MODE catalog setup for credit packs.

For each configured pack (CREDIT_PACKS; default 30 tries/$3 and 100 tries/$5) ensures one
Product (metadata app=plan, pack=<key>) and one ONE-TIME Price with the pack's lookup_key.
Looks everything up before creating, so it is safe to re-run. Prices are immutable: a mismatch
is reported, never "fixed" (bump the lookup key, e.g. _v2, instead). `--dry-run` makes NO
Stripe calls.

Live keys are refused (even for --dry-run). There is no --live override.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import stripe

from app.billing.guard import LIVE_SECRET_PREFIXES, StripeKeyError, check_secret_key
from app.billing.packs import packs
from app.config import get_settings

APP_TAG = "plan"


@dataclass
class SetupReport:
    dry_run: bool
    actions: list[str] = field(default_factory=list)
    product_ids: dict[str, str] = field(default_factory=dict)
    price_ids: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    created: int = 0

    def log(self, msg: str, out: Callable[[str], None]) -> None:
        self.actions.append(msg)
        out(msg)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    try:
        return obj[key]
    except (KeyError, TypeError):
        return getattr(obj, key, default)


def _product_name(pack: Any) -> str:
    return f"Plan: {pack.name}"


def run_setup(
    *,
    dry_run: bool,
    client: Any | None = None,
    out: Callable[[str], None] = print,
) -> SetupReport:
    s = get_settings()
    raw_key = s.stripe_secret_key.get_secret_value().strip()
    if raw_key.startswith(LIVE_SECRET_PREFIXES):
        raise StripeKeyError(
            "STRIPE_LIVE_KEY_REFUSED", "Refusing to run: live Stripe key detected (TEST MODE ONLY)"
        )
    report = SetupReport(dry_run=dry_run)
    catalog = packs()

    if dry_run:
        out("DRY RUN: no Stripe API calls will be made.")
        out(f"Stripe API version: {s.stripe_api_version}; key status: "
            f"{'set (test)' if raw_key else 'not set'}")  # fmt: skip
        out(f"Paywall: {s.free_tries} free tries per user, then credit packs (one-time payments).")
        for pack in catalog:
            report.log(
                f"[would ensure] Product '{_product_name(pack)}' "
                f"metadata={{app: {APP_TAG}, pack: {pack.key}}}",
                out,
            )
            report.log(
                f"[would ensure]   Price lookup_key={pack.lookup_key} {pack.unit_amount / 100:.2f} "
                f"{pack.currency.upper()} one-time ({pack.tries} tries) "
                f"metadata={{pack: {pack.key}, tries: {pack.tries}}}",
                out,
            )
        return report

    key = check_secret_key(raw_key)  # missing / live / invalid → StripeKeyError
    client = client or stripe.StripeClient(key, stripe_version=s.stripe_api_version)
    v1 = client.v1

    existing_products = list(v1.products.list(params={"limit": 100}).data)
    for pack in catalog:
        product = next(
            (
                p
                for p in existing_products
                if (_get(p, "metadata") or {}).get("app") == APP_TAG
                and (_get(p, "metadata") or {}).get("pack") == pack.key
            ),
            None,
        )
        if product is None:
            product = v1.products.create(
                params={
                    "name": _product_name(pack),
                    "description": f"{pack.tries} prop unlocks for Plan NFL prop analytics "
                    "(information service; no wagering). Tries do not expire.",
                    "metadata": {"app": APP_TAG, "pack": pack.key},
                },
                options={"idempotency_key": f"plan-setup:product:pack:{pack.key}:v1"},
            )
            report.created += 1
            report.log(f"[created] Product {_get(product, 'id')} ({_product_name(pack)})", out)
        else:
            if not _get(product, "active", True):
                report.warnings.append(f"Product {_get(product, 'id')} is archived")
            report.log(f"[exists]  Product {_get(product, 'id')} ({_product_name(pack)})", out)
        product_id = _get(product, "id")
        report.product_ids[pack.key] = product_id

        found = list(v1.prices.list(params={"lookup_keys": [pack.lookup_key], "limit": 10}).data)
        price = found[0] if found else None
        if price is None:
            price = v1.prices.create(
                params={
                    "product": product_id,
                    "unit_amount": pack.unit_amount,
                    "currency": pack.currency,
                    "lookup_key": pack.lookup_key,
                    "nickname": pack.name,
                    "metadata": {"app": APP_TAG, "pack": pack.key, "tries": str(pack.tries)},
                },
                options={"idempotency_key": f"plan-setup:price:{pack.lookup_key}"},
            )
            report.created += 1
            report.log(f"[created]   Price {_get(price, 'id')} ({pack.lookup_key})", out)
        else:
            problems = []
            if _get(price, "unit_amount") != pack.unit_amount:
                problems.append(f"amount {_get(price, 'unit_amount')} != {pack.unit_amount}")
            if _get(price, "currency") not in (None, pack.currency):
                problems.append(f"currency {_get(price, 'currency')} != {pack.currency}")
            if _get(price, "recurring"):
                problems.append("price is recurring (expected one-time)")
            prod = _get(price, "product")
            prod = _get(prod, "id") if not isinstance(prod, str) else prod
            if prod != product_id:
                problems.append(f"product {prod} != {product_id}")
            if not _get(price, "active", True):
                problems.append("price is inactive")
            if problems:
                msg = (
                    f"Price {pack.lookup_key}: "
                    + "; ".join(problems)
                    + " (prices are immutable; use a new lookup key, e.g. _v2)"
                )
                report.warnings.append(msg)
                report.log(f"[WARN]    {msg}", out)
            else:
                report.log(f"[exists]    Price {_get(price, 'id')} ({pack.lookup_key})", out)
        report.price_ids[pack.lookup_key] = _get(price, "id")
    return report
