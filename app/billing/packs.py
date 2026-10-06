"""Credit-pack catalog (configured via CREDIT_PACKS; defaults: 30 tries/$3, 100 tries/$5)."""

from __future__ import annotations

from app.config import CreditPack, get_settings


def packs() -> list[CreditPack]:
    return list(get_settings().credit_packs)


def pack_by_key(key: str) -> CreditPack | None:
    return next((p for p in packs() if p.key == key), None)


def pack_by_lookup_key(lookup_key: str | None) -> CreditPack | None:
    return next((p for p in packs() if p.lookup_key == lookup_key), None) if lookup_key else None


def packs_summary() -> list[dict[str, object]]:
    """Compact pack list for 402 bodies (so the client can render a buy sheet directly)."""
    return [
        {"key": p.key, "name": p.name, "tries": p.tries, "unit_amount": p.unit_amount, "currency": p.currency}
        for p in packs()
    ]
