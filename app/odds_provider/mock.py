"""Mock odds provider with a hand-built SAMPLE NFL week.

!!! SAMPLE DATA — NOT REAL ODDS AND NOT AN OFFICIAL SCHEDULE !!!
Matchups/prices are illustrative (the first six games mirror the frontend mock in
plan-frontend/mock/data.js so UI and API line up). Do not use for real wagering decisions.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.models.enums import MarketType, SeasonType, SelectionSide
from app.odds_provider.base import (
    ProviderGame,
    ProviderMarket,
    ProviderSelection,
    ProviderTeam,
)
from app.odds_provider.teams import NFL_TEAMS

ET = ZoneInfo("America/New_York")
SAMPLE_SEASON = 2026
SAMPLE_WEEK = 5

# (away, home, kickoff ET, venue, home_spread, spread_odds(away, home), ml(away, home),
#  total, total_odds(over, under))
_SAMPLE_WEEK_ROWS: list[tuple] = [
    ("TB", "DAL", "2026-10-08 20:15", "AT&T Stadium", -7.5, (-110, -110), (280, -340), 47.5, (-108, -112)),
    ("DET", "GB", "2026-10-11 13:00", "Lambeau Field", 3.5, (-110, -110), (-175, 150), 48.5, (-108, -112)),
    ("BAL", "CIN", "2026-10-11 13:00", "Paycor Stadium", 3.0, (-115, -105), (-155, 135), 51.5, (-110, -110)),
    ("NYJ", "MIA", "2026-10-11 13:00", "Hard Rock Stadium", -2.5, (-110, -110), (115, -135), 43.5, (-110, -110)),
    ("CLE", "PIT", "2026-10-11 13:00", "Acrisure Stadium", -4.5, (-108, -112), (175, -210), 39.5, (-105, -115)),
    ("IND", "JAX", "2026-10-11 13:00", "EverBank Stadium", -1.5, (-110, -110), (100, -120), 45.5, (-110, -110)),
    ("CAR", "ATL", "2026-10-11 13:00", "Mercedes-Benz Stadium", -5.5, (-110, -110), (190, -230), 44.5, (-112, -108)),
    ("TEN", "HOU", "2026-10-11 13:00", "NRG Stadium", -6.5, (-105, -115), (235, -290), 41.5, (-110, -110)),
    ("SF", "SEA", "2026-10-11 16:05", "Lumen Field", 1.5, (-105, -115), (-120, 100), 44.5, (-110, -110)),
    ("LV", "DEN", "2026-10-11 16:05", "Empower Field at Mile High", -7.0, (-110, -110), (280, -350), 40.5, (-110, -110)),
    ("KC", "BUF", "2026-10-11 16:25", "Highmark Stadium", -2.5, (-108, -112), (120, -140), 49.5, (-110, -110)),
    ("ARI", "LAR", "2026-10-11 16:25", "SoFi Stadium", -3.0, (-110, -110), (135, -160), 46.5, (-110, -110)),
    ("CHI", "MIN", "2026-10-11 20:20", "U.S. Bank Stadium", -3.5, (-105, -115), (150, -175), 45.5, (-110, -110)),
    ("PHI", "NYG", "2026-10-12 20:20", "MetLife Stadium", 6.5, (-110, -110), (-280, 230), 42.5, (-108, -112)),
]  # fmt: skip
# Byes in this sample week: NE, NO, LAC, WAS


def _d(x: float) -> Decimal:
    return Decimal(str(x))


def _build_game(row: tuple, season: int, week: int) -> ProviderGame:
    away, home, ko, venue, home_spread, (sp_a, sp_h), (ml_a, ml_h), total, (ov, un) = row
    kickoff = datetime.strptime(ko, "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    markets = [
        ProviderMarket(
            MarketType.MONEYLINE,
            [
                ProviderSelection(SelectionSide.AWAY, ml_a),
                ProviderSelection(SelectionSide.HOME, ml_h),
            ],
        ),
        ProviderMarket(
            MarketType.SPREAD,
            [
                ProviderSelection(SelectionSide.AWAY, sp_a, _d(-home_spread)),
                ProviderSelection(SelectionSide.HOME, sp_h, _d(home_spread)),
            ],
        ),
        ProviderMarket(
            MarketType.TOTAL,
            [
                ProviderSelection(SelectionSide.OVER, ov, _d(total), "Over"),
                ProviderSelection(SelectionSide.UNDER, un, _d(total), "Under"),
            ],
        ),
    ]
    return ProviderGame(
        external_id=f"{away.lower()}-{home.lower()}",  # matches frontend mock game ids
        season=season,
        week=week,
        home_abbr=home,
        away_abbr=away,
        kickoff_at=kickoff,
        season_type=SeasonType.REG,
        venue=venue,
        markets=markets,
    )


class MockOddsProvider:
    """Serves one static SAMPLE week (2026 REG week 5).

    `shift` lets dev/demo environments move all kickoffs forward so the slate is always bettable.
    """

    name = "mock"
    is_sample_data = True

    def __init__(self, shift: timedelta = timedelta(0)) -> None:
        self.shift = shift

    def get_teams(self) -> list[ProviderTeam]:
        return list(NFL_TEAMS)

    def get_week(
        self, season: int, week: int, season_type: SeasonType = SeasonType.REG
    ) -> list[ProviderGame]:
        if (season, week, season_type) != (SAMPLE_SEASON, SAMPLE_WEEK, SeasonType.REG):
            return []
        games = [_build_game(r, season, week) for r in _SAMPLE_WEEK_ROWS]
        if self.shift:
            games = [replace(g, kickoff_at=g.kickoff_at + self.shift) for g in games]
        return games

    @staticmethod
    def first_kickoff() -> datetime:
        return min(_build_game(r, SAMPLE_SEASON, SAMPLE_WEEK).kickoff_at for r in _SAMPLE_WEEK_ROWS)
