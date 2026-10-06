from datetime import UTC, datetime
from decimal import Decimal

from app.models.enums import MarketType, SelectionSide
from app.nfl_data.schedule import _lines, kickoff_utc, slug


def test_kickoff_et_to_utc_handles_dst():
    assert kickoff_utc("2026-10-08", "20:15") == datetime(2026, 10, 9, 0, 15, tzinfo=UTC)  # EDT (UTC-4)
    assert kickoff_utc("2026-12-06", "13:00") == datetime(2026, 12, 6, 18, 0, tzinfo=UTC)  # EST (UTC-5)
    assert kickoff_utc("2026-10-11", "09:30").tzinfo is not None  # London game, still ET wall clock


def test_spread_sign_home_favoured_positive():
    lines = _lines({"spread_line": "8.5", "away_spread_odds": "-108", "home_spread_odds": "-112",
                    "away_moneyline": "350", "home_moneyline": "-455",
                    "total_line": "47.5", "over_odds": "-115", "under_odds": "-105"})  # fmt: skip
    assert lines[MarketType.SPREAD] == [
        (SelectionSide.AWAY, Decimal("8.5"), -108),
        (SelectionSide.HOME, Decimal("-8.5"), -112),
    ]
    assert lines[MarketType.MONEYLINE] == [(SelectionSide.AWAY, None, 350), (SelectionSide.HOME, None, -455)]
    assert lines[MarketType.TOTAL][0] == (SelectionSide.OVER, Decimal("47.5"), -115)
    away_fav = _lines({"spread_line": "-3", "away_spread_odds": "-102", "home_spread_odds": "-118"})
    assert away_fav[MarketType.SPREAD][0] == (SelectionSide.AWAY, Decimal("-3"), -102)


def test_missing_or_invalid_lines_skipped():
    assert _lines({}) == {}
    assert MarketType.MONEYLINE not in _lines(
        {"away_moneyline": "50", "home_moneyline": "-110"}
    )  # invalid odds


def test_slug_uses_our_abbrs():
    assert slug("BUF", "LAR") == "buf-lar"
