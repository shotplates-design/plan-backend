from decimal import Decimal
from fractions import Fraction

import pytest

from app.core.odds import (
    InvalidOddsError,
    american_to_decimal,
    decimal_to_american,
    implied_probability,
    market_hold,
    no_vig_probabilities,
    parlay_american,
    parlay_decimal,
    parlay_payout_cents,
    payout_cents,
    profit_cents,
    validate_american,
)


class TestValidate:
    @pytest.mark.parametrize("odds", [-100, 100, -110, 150, -10000, 25000])
    def test_valid(self, odds):
        assert validate_american(odds) == odds

    @pytest.mark.parametrize("odds", [0, 99, -99, 50, -1])
    def test_invalid_range(self, odds):
        with pytest.raises(InvalidOddsError):
            validate_american(odds)

    @pytest.mark.parametrize("odds", [1.5, "110", None, True])
    def test_invalid_type(self, odds):
        with pytest.raises(InvalidOddsError):
            validate_american(odds)


class TestConversions:
    @pytest.mark.parametrize(
        ("american", "decimal"),
        [
            (100, Fraction(2)),
            (-100, Fraction(2)),
            (150, Fraction(5, 2)),
            (-110, Fraction(21, 11)),
            (-200, Fraction(3, 2)),
            (280, Fraction(19, 5)),
            (-340, Fraction(22, 17)),
        ],
    )
    def test_american_to_decimal(self, american, decimal):
        assert american_to_decimal(american) == decimal

    @pytest.mark.parametrize("american", [100, 105, 150, 280, 1000, -105, -110, -200, -340, -1000])
    def test_round_trip(self, american):
        back = decimal_to_american(american_to_decimal(american))
        # -100 and +100 are the same price; everything else should round-trip exactly
        assert back == american

    def test_decimal_to_american_rounding(self):
        assert decimal_to_american(Decimal("2.645")) == 165  # 164.5 rounds half up
        assert decimal_to_american(Decimal("1.5")) == -200
        assert decimal_to_american(Fraction(2)) == 100

    def test_decimal_to_american_rejects_le_one(self):
        with pytest.raises(InvalidOddsError):
            decimal_to_american(Decimal("1.0"))


class TestImpliedProbability:
    def test_values(self):
        assert implied_probability(100) == Fraction(1, 2)
        assert implied_probability(-110) == Fraction(110, 210)
        assert implied_probability(150) == Fraction(2, 5)
        assert implied_probability(-300) == Fraction(3, 4)

    def test_hold_standard_market(self):
        hold = market_hold([-110, -110])
        assert float(hold) == pytest.approx(0.04545, abs=1e-4)

    def test_no_vig_sums_to_one(self):
        probs = no_vig_probabilities([-340, 280])
        assert sum(probs) == 1
        assert probs[0] > probs[1]


class TestPayouts:
    def test_single_payouts(self):
        assert payout_cents(10_000, 150) == 25_000  # $100 @ +150 returns $250
        assert payout_cents(11_000, -110) == 21_000  # $110 @ -110 returns $210
        assert payout_cents(10_000, -110) == 19_090  # 190.909... floored to the cent
        assert profit_cents(10_000, -110) == 9_090
        assert payout_cents(0, -110) == 0

    def test_negative_stake_rejected(self):
        with pytest.raises(ValueError):
            payout_cents(-1, 100)

    def test_parlay_two_legs_standard(self):
        # Classic -110/-110 two-teamer pays ~+264
        assert parlay_decimal([-110, -110]) == Fraction(441, 121)
        assert parlay_american([-110, -110]) == 264
        assert parlay_payout_cents(10_000, [-110, -110]) == 36_446

    def test_parlay_mixed(self):
        # +150 (2.5) * -200 (1.5) = 3.75 -> +275
        assert parlay_american([150, -200]) == 275
        assert parlay_payout_cents(1_000, [150, -200]) == 3_750

    def test_parlay_three_legs(self):
        dec = parlay_decimal([-110, -110, -110])
        assert float(dec) == pytest.approx(6.9579, abs=1e-4)
        assert parlay_american([-110, -110, -110]) == 596

    def test_parlay_single_leg_equals_single(self):
        assert parlay_payout_cents(5_000, [-140]) == payout_cents(5_000, -140)

    def test_parlay_requires_legs(self):
        with pytest.raises(InvalidOddsError):
            parlay_decimal([])
