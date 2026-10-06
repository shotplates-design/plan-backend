"""Pluggable paywall: lifetime free tries + a paid entitlement (purchased credits / unlimited).

Gated requests never call Stripe; access comes from OUR DB (`usage_accounts`, `prop_unlocks`).
Order of consumption: unlimited (comp) → free tries → paid credits → 402 UPGRADE_REQUIRED.

To swap the billing model later (e.g. a subscription), set `usage_accounts.unlimited_until`
from the new source (webhook sync) — the gating code below doesn't change.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import Depends
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import DB, CurrentUser, OptionalUser
from app.billing.packs import packs_summary
from app.config import get_settings
from app.core.errors import AppError
from app.models import Prop, PropUnlock, UsageAccount, UsageLedger, User
from app.models.enums import LedgerKind, UsageBucket

ET = ZoneInfo("America/New_York")


def et_today(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(ET).date()


def et_next_midnight(now: datetime | None = None) -> datetime:
    return datetime.combine(et_today(now) + timedelta(days=1), time(0, 0), tzinfo=ET)


def unlocks_reset_at(now: datetime | None = None) -> datetime | None:
    """When re-opening starts costing a try again (None when scope=forever)."""
    return et_next_midnight(now) if get_settings().paywall_reopen_scope == "day" else None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- pure logic


@dataclass(frozen=True)
class UsageState:
    paywall_enabled: bool
    free_limit: int
    free_used: int = 0
    paid_balance: int = 0
    unlimited_until: datetime | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def free_remaining(self) -> int:
        return max(0, self.free_limit - self.free_used)

    @property
    def unlimited(self) -> bool:
        if not self.paywall_enabled:
            return True
        until = _aware(self.unlimited_until)
        return until is not None and until > self.now

    @property
    def tries_remaining(self) -> int | None:
        """None = unlimited."""
        return None if self.unlimited else self.free_remaining + self.paid_balance

    @property
    def has_paid_entitlement(self) -> bool:
        return self.unlimited or self.paid_balance > 0


def next_bucket(state: UsageState) -> UsageBucket | None:
    """Which bucket pays for the next unlock; None → paywall (402)."""
    if state.unlimited:
        return UsageBucket.UNLIMITED
    if state.free_remaining > 0:
        return UsageBucket.FREE
    if state.paid_balance > 0:
        return UsageBucket.PAID
    return None


def state_from_account(acct: UsageAccount | None, now: datetime | None = None) -> UsageState:
    s = get_settings()
    return UsageState(
        paywall_enabled=s.paywall_enabled,
        free_limit=s.free_tries,
        free_used=acct.free_used if acct else 0,
        paid_balance=acct.paid_balance if acct else 0,
        unlimited_until=acct.unlimited_until if acct else None,
        now=now or datetime.now(UTC),
    )


# --------------------------------------------------------------------------- DB-backed


def get_account(db: Session, user_id: uuid.UUID, *, lock: bool = False) -> UsageAccount:
    """Fetch (optionally FOR UPDATE) or create the user's usage account."""
    stmt = select(UsageAccount).where(UsageAccount.user_id == user_id)
    if lock:
        stmt = stmt.with_for_update()
    acct = db.scalars(stmt).first()
    if acct is not None:
        return acct
    try:
        with db.begin_nested():
            acct = UsageAccount(
                user_id=user_id, free_used=0, paid_balance=0, paid_purchased_total=0, paid_used_total=0
            )
            db.add(acct)
    except IntegrityError:  # created concurrently
        acct = db.scalars(stmt).one()
    return acct


@dataclass
class Access:
    """Per-request paywall view for one user (or anonymous)."""

    state: UsageState
    user_id: uuid.UUID | None = None
    unlocked_prop_ids: set[uuid.UUID] = field(default_factory=set)
    # Sportsbook skin for top-level line/price/hitRate (fanduel | draftkings | None → FD then DK).
    preferred_book: str | None = None

    @property
    def anonymous(self) -> bool:
        return self.user_id is None

    def is_unlocked(self, prop_id: uuid.UUID) -> bool:
        return self.state.unlimited or prop_id in self.unlocked_prop_ids


def _window(stmt, now: datetime | None):
    if get_settings().paywall_reopen_scope == "day":
        stmt = stmt.where(PropUnlock.unlock_date == et_today(now))
    return stmt


def unlocked_ids(
    db: Session, user_id: uuid.UUID, prop_ids: Iterable[uuid.UUID] | None = None, now: datetime | None = None
) -> set[uuid.UUID]:
    """Props this user may view without paying (opened within the re-open window)."""
    stmt = _window(select(PropUnlock.prop_id).where(PropUnlock.user_id == user_id), now)
    if prop_ids is not None:
        ids = list(prop_ids)
        if not ids:
            return set()
        stmt = stmt.where(PropUnlock.prop_id.in_(ids))
    return set(db.scalars(stmt))


def props_unlocked_count(db: Session, user_id: uuid.UUID, now: datetime | None = None) -> int:
    stmt = _window(
        select(func.count(func.distinct(PropUnlock.prop_id))).where(PropUnlock.user_id == user_id), now
    )
    return db.scalar(stmt) or 0


def resolve_access(db: Session, user: User | None) -> Access:
    if user is None:
        return Access(state=state_from_account(None), preferred_book=None)
    acct = db.get(UsageAccount, user.id)
    theme = user.theme.value if user.theme is not None else None
    return Access(
        state=state_from_account(acct),
        user_id=user.id,
        unlocked_prop_ids=unlocked_ids(db, user.id),
        preferred_book=theme,
    )


def get_access(user: CurrentUser, db: DB) -> Access:
    """Dependency for authenticated routes."""
    return resolve_access(db, user)


def get_optional_access(user: OptionalUser, db: DB) -> Access:
    """Dependency for public routes (anonymous → nothing unlocked unless the paywall is off)."""
    return resolve_access(db, user)


# --------------------------------------------------------------------------- 402 gating


def upgrade_required(state: UsageState, reason: str, message: str, **extra: Any) -> AppError:
    """402 UPGRADE_REQUIRED in the standard error envelope, with everything the buy sheet needs."""
    s = get_settings()
    details: dict[str, Any] = {
        "reason": reason,
        "free_limit": state.free_limit,
        "free_used": min(state.free_used, state.free_limit),
        "free_remaining": state.free_remaining,
        "paid_balance": state.paid_balance,
        "tries_remaining": state.tries_remaining,
        "purchase_available": s.billing_enabled,
        "packs": packs_summary(),
        **extra,
    }
    return AppError(402, "UPGRADE_REQUIRED", message, details)


def require_entitlement(
    check: Callable[[Access], bool], *, reason: str, message: str
) -> Callable[..., Access]:
    """Generic gate: `Depends(require_entitlement(lambda a: a.state.has_paid_entitlement, ...))`."""

    def dep(access: Access = Depends(get_access)) -> Access:
        if not check(access):
            raise upgrade_required(access.state, reason, message)
        return access

    dep.__name__ = f"require_{reason}"
    return dep


require_paid_entitlement = require_entitlement(
    lambda a: a.state.has_paid_entitlement,
    reason="paid_required",
    message="This feature requires purchased tries.",
)


def require_prop_unlocked(access: Access, prop: Prop) -> None:
    if not access.is_unlocked(prop.id):
        raise upgrade_required(
            access.state,
            "prop_locked",
            "Open this prop's detail to see full hit rates (uses 1 try).",
            prop_id=prop.external_id,
            unlock_cost=1,
        )


@dataclass
class UnlockResult:
    bucket: UsageBucket | None  # None when the paywall is disabled
    charged: bool
    already_unlocked: bool
    state: UsageState


def unlock_prop(db: Session, user: User, prop: Prop, now: datetime | None = None) -> UnlockResult:
    """Open a prop for a user, consuming one try unless already opened within the re-open window
    (same ET day by default). Idempotent. Commits.

    Raises 402 UPGRADE_REQUIRED (reason=out_of_tries) when no free tries or credits remain.
    """
    now = now or datetime.now(UTC)
    s = get_settings()
    if not s.paywall_enabled:
        return UnlockResult(None, False, False, state_from_account(db.get(UsageAccount, user.id), now))

    acct = get_account(db, user.id, lock=True)  # serialises concurrent unlocks for this user
    state = state_from_account(acct, now)
    if prop.id in unlocked_ids(db, user.id, [prop.id], now):
        db.commit()  # release lock
        return UnlockResult(None, False, True, state)

    bucket = next_bucket(state)
    if bucket is None:
        db.rollback()
        raise upgrade_required(
            state, "out_of_tries", "You're out of tries. Buy a pack to keep unlocking props.",
            prop_id=prop.external_id, unlock_cost=1,
        )  # fmt: skip

    if bucket == UsageBucket.FREE:
        acct.free_used += 1
        db.add(
            UsageLedger(user_id=user.id, kind=LedgerKind.FREE_USE, bucket=bucket, delta=1, prop_id=prop.id)
        )
    elif bucket == UsageBucket.PAID:
        acct.paid_balance -= 1
        acct.paid_used_total += 1
        db.add(
            UsageLedger(user_id=user.id, kind=LedgerKind.PAID_USE, bucket=bucket, delta=-1, prop_id=prop.id)
        )
    db.add(
        PropUnlock(
            user_id=user.id, prop_id=prop.id, unlock_date=et_today(now), bucket=bucket, unlocked_at=now
        )
    )
    db.commit()
    return UnlockResult(bucket, bucket != UsageBucket.UNLIMITED, False, state_from_account(acct, now))


def credit_tries(
    db: Session,
    user_id: uuid.UUID,
    tries: int,
    *,
    kind: LedgerKind,
    purchase_id: uuid.UUID | None = None,
    note: str | None = None,
) -> int:
    """Add (or with negative `tries`, remove, floored at 0) paid tries. Caller commits.

    Returns the delta actually applied (also recorded in the ledger).
    """
    acct = get_account(db, user_id, lock=True)
    delta = tries if tries >= 0 else -min(-tries, acct.paid_balance)
    acct.paid_balance += delta
    if kind in (LedgerKind.PURCHASE, LedgerKind.GRANT) and delta > 0:
        acct.paid_purchased_total += delta
    db.add(
        UsageLedger(
            user_id=user_id,
            kind=kind,
            bucket=UsageBucket.PAID,
            delta=delta,
            purchase_id=purchase_id,
            note=note,
        )
    )
    db.flush()
    return delta
