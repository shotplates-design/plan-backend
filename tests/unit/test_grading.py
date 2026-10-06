from decimal import Decimal

import pytest

from app.models.enums import MarketType as M
from app.models.enums import SelectionResult as R
from app.models.enums import SelectionSide as S
from app.services.betting import grade


@pytest.mark.parametrize(
    ("mtype", "side", "line", "home", "away", "expected"),
    [
        (M.MONEYLINE, S.HOME, None, 24, 17, R.WON),
        (M.MONEYLINE, S.AWAY, None, 24, 17, R.LOST),
        (M.MONEYLINE, S.HOME, None, 20, 20, R.PUSH),  # NFL tie
        (M.SPREAD, S.HOME, Decimal("-7.5"), 24, 17, R.LOST),  # won by 7, needed 8
        (M.SPREAD, S.AWAY, Decimal("7.5"), 24, 17, R.WON),
        (M.SPREAD, S.HOME, Decimal("-7.0"), 24, 17, R.PUSH),
        (M.SPREAD, S.AWAY, Decimal("7.0"), 24, 17, R.PUSH),
        (M.SPREAD, S.AWAY, Decimal("-3.0"), 17, 24, R.WON),  # road favourite covers
        (M.TOTAL, S.OVER, Decimal("40.5"), 24, 17, R.WON),
        (M.TOTAL, S.UNDER, Decimal("40.5"), 24, 17, R.LOST),
        (M.TOTAL, S.OVER, Decimal("41.0"), 24, 17, R.PUSH),
        (M.TOTAL, S.UNDER, Decimal("47.5"), 24, 17, R.WON),
    ],
)
def test_grade(mtype, side, line, home, away, expected):
    assert grade(mtype, side, line, home, away) == expected
