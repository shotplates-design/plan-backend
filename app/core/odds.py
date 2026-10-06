"""American odds math.

All internal math uses `fractions.Fraction` so results are exact; money is integer cents and
payouts are rounded *down* to the cent (house-favourable, standard sportsbook practice).

American odds conventions:
  +150  -> win 150 on a 100 stake (decimal 2.50)
  -110  -> stake 110 to win 100   (decimal ~1.9091)
Valid American odds are <= -100 or >= +100. (+100 and -100 are both "even money".)
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction
from math import floor


class InvalidOddsError(ValueError):
    pass


def validate_american(odds: int) -> int:
    if isinstance(odds, bool) or not isinstance(odds, int):
        raise InvalidOddsError(f"American odds must be an integer, got {odds!r}")
    if -100 < odds < 100:
        raise InvalidOddsError(f"American odds must be <= -100 or >= +100, got {odds}")
    return odds


def american_to_decimal(odds: int) -> Fraction:
    """Decimal (European) odds = total return per 1 unit staked."""
    validate_american(odds)
    if odds > 0:
        return 1 + Fraction(odds, 100)
    return 1 + Fraction(100, -odds)


def decimal_to_american(decimal_odds: Fraction | Decimal | float) -> int:
    """Convert decimal odds (> 1.0) to American, rounded half-up to the nearest integer."""
    d = Fraction(decimal_odds) if not isinstance(decimal_odds, Fraction) else decimal_odds
    if d <= 1:
        raise InvalidOddsError(f"Decimal odds must be > 1.0, got {float(d)}")
    if d >= 2:
        value = (d - 1) * 100
        return int(_round_half_up(value))
    value = Fraction(-100) / (d - 1)
    return -int(_round_half_up(-value))


def implied_probability(odds: int) -> Fraction:
    """Break-even win probability implied by the price (includes the book's vig)."""
    validate_american(odds)
    if odds > 0:
        return Fraction(100, odds + 100)
    return Fraction(-odds, -odds + 100)


def profit_cents(stake_cents: int, odds: int) -> int:
    """Net winnings (excluding stake) for a winning single bet."""
    return payout_cents(stake_cents, odds) - stake_cents


def payout_cents(stake_cents: int, odds: int) -> int:
    """Total return (stake + winnings) for a winning single bet, floored to the cent."""
    _validate_stake(stake_cents)
    return floor(stake_cents * american_to_decimal(odds))


def parlay_decimal(odds_list: Iterable[int]) -> Fraction:
    """Combined decimal odds of a parlay = product of each leg's decimal odds."""
    legs = list(odds_list)
    if not legs:
        raise InvalidOddsError("A parlay needs at least one leg")
    result = Fraction(1)
    for o in legs:
        result *= american_to_decimal(o)
    return result


def parlay_american(odds_list: Iterable[int]) -> int:
    return decimal_to_american(parlay_decimal(odds_list))


def parlay_payout_cents(stake_cents: int, odds_list: Iterable[int]) -> int:
    """Total return for a winning parlay, floored to the cent.

    Uses exact leg-by-leg decimal product (not the rounded American price) so the user is
    paid the true combined price.
    """
    _validate_stake(stake_cents)
    return floor(stake_cents * parlay_decimal(odds_list))


def market_hold(odds_list: Iterable[int]) -> Fraction:
    """Bookmaker margin (overround) for a complete market, e.g. -110/-110 -> ~4.76%."""
    total = sum((implied_probability(o) for o in odds_list), Fraction(0))
    return 1 - 1 / total


def no_vig_probabilities(odds_list: Iterable[int]) -> list[Fraction]:
    """Fair (vig-removed) probabilities via simple normalisation."""
    probs = [implied_probability(o) for o in odds_list]
    total = sum(probs, Fraction(0))
    return [p / total for p in probs]


def to_decimal(value: Fraction, places: int = 6) -> Decimal:
    q = Decimal(1).scaleb(-places)
    return (Decimal(value.numerator) / Decimal(value.denominator)).quantize(q, ROUND_HALF_UP)


def _round_half_up(value: Fraction) -> int:
    return floor(value + Fraction(1, 2))


def _validate_stake(stake_cents: int) -> None:
    if isinstance(stake_cents, bool) or not isinstance(stake_cents, int) or stake_cents < 0:
        raise ValueError(f"stake_cents must be a non-negative integer, got {stake_cents!r}")
