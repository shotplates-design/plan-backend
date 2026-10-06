"""Domain enums. Stored as strings (non-native) for portability across Postgres/SQLite."""

from enum import StrEnum


class Theme(StrEnum):
    """Sportsbook look-and-feel. Frontend maps missing/null to the skin picker."""

    FANDUEL = "fanduel"
    DRAFTKINGS = "draftkings"


class SeasonType(StrEnum):
    PRE = "PRE"
    REG = "REG"
    POST = "POST"


class GameStatus(StrEnum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class MarketType(StrEnum):
    MONEYLINE = "moneyline"
    SPREAD = "spread"
    TOTAL = "total"
    PROP = "prop"


class MarketStatus(StrEnum):
    OPEN = "open"
    SUSPENDED = "suspended"
    CLOSED = "closed"
    SETTLED = "settled"


class SelectionSide(StrEnum):
    HOME = "home"
    AWAY = "away"
    OVER = "over"
    UNDER = "under"


class SelectionResult(StrEnum):
    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    PUSH = "push"
    VOID = "void"


class BetType(StrEnum):
    SINGLE = "single"
    PARLAY = "parlay"


class BetStatus(StrEnum):
    PENDING = "pending"
    WON = "won"
    LOST = "lost"
    PUSH = "push"
    VOID = "void"


class WalletTxType(StrEnum):
    SIGNUP_BONUS = "signup_bonus"
    BET_STAKE = "bet_stake"
    BET_PAYOUT = "bet_payout"
    BET_REFUND = "bet_refund"
    ADJUSTMENT = "adjustment"


class PropMarketKind(StrEnum):
    """Player-prop market labels matching the frontend mock."""

    RECEPTIONS = "Receptions"
    RECEIVING_YARDS = "Receiving Yards"
    PASSING_YARDS = "Passing Yards"
    RUSHING_YARDS = "Rushing Yards"


# --------------------------------------------------------------------------- billing / paywall


class UsageBucket(StrEnum):
    """Where a prop unlock was paid from."""

    FREE = "free"  # one of the FREE_TRIES lifetime free tries
    PAID = "paid"  # one purchased try (credit pack)
    UNLIMITED = "unlimited"  # comp / future unlimited entitlement; costs nothing


class LedgerKind(StrEnum):
    FREE_USE = "free_use"
    PAID_USE = "paid_use"
    PURCHASE = "purchase"
    REFUND = "refund"
    GRANT = "grant"
    ADJUST = "adjust"


class PurchaseStatus(StrEnum):
    PENDING = "pending"  # Checkout Session created / async payment processing
    PAID = "paid"  # tries credited
    REFUNDED = "refunded"  # unused tries clawed back
    FAILED = "failed"  # async payment failed
    EXPIRED = "expired"  # session expired unpaid


class StripeEventStatus(StrEnum):
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
    IGNORED = "ignored"
