"""Pure hit-rate math. No DB, no I/O, so it's easy to test.

Rules (Plan Data participation fix, 2026-10-06):
- Only PLAYED games count (`played` = true: >= 1 offensive snap or an offensive stat).
- Season denominator = `games_played` from the season file (NOT nflverse `games`, NOT the
  number of stat rows). Zero-stat played games are zero rows → misses for Over lines.
- last5 = the 5 most recent played games by (season, week), newest first.
- vsOpp = played games against the upcoming opponent; total may be 0 → pct null.
- Hit = strictly over (or under) the line; a push on a whole-number line is not a hit.
- vsOppLastSeason = prior REGULAR-season played games vs the same opponent, against the same line
  (zero rows are misses; total may be 0 → pct null). Prior seasons never feed season/last5.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import SelectionSide

LAST_N = 5

# Board market label → stat column. Unknown markets get no hit rates (data_status=unsupported_market).
MARKET_STAT_KEYS: dict[str, str] = {
    "Receptions": "receptions",
    "Receiving Yards": "receiving_yards",
    "Passing Yards": "passing_yards",
    "Rushing Yards": "rushing_yards",
    "Passing Attempts": "attempts",
    "Pass Completions": "completions",
    "Passing TDs": "passing_tds",
    "Rushing Attempts": "carries",
    "Targets": "targets",
    # Anytime TD is composite (handled specially); listed so unsupported_market isn't set.
    "Anytime TD": "anytime_td",
}


@dataclass(frozen=True)
class GameLog:
    season: int
    week: int
    opponent: str
    value: float
    played: bool = True
    season_type: str = "REG"


@dataclass(frozen=True)
class Bucket:
    hit: int
    total: int

    @property
    def pct(self) -> int | None:
        return pct(self.hit, self.total)


@dataclass(frozen=True)
class HitRates:
    season: Bucket
    last5: Bucket
    vs_opp: Bucket | None  # None when there is no opponent (player not in the game)
    vs_opp_label: str | None


def pct(hit: int, total: int) -> int | None:
    """Whole-percent, round half up (3/8 → 38, 2/3 → 67). None when total is 0."""
    if total <= 0:
        return None
    return (200 * hit + total) // (2 * total)


def is_hit(value: float, line: Decimal | float, side: SelectionSide) -> bool:
    v, ln = Decimal(str(value)), Decimal(str(line))
    return v < ln if side == SelectionSide.UNDER else v > ln


def season_label(opponent: str, season: int) -> str:
    """'vs DAL '25' for a prior-season window."""
    return f"vs {opponent} '{season % 100:02d}"


def compute_vs_opp_season(
    logs: Iterable[GameLog],
    *,
    season: int,
    opponent: str,
    line: Decimal | float,
    side: SelectionSide = SelectionSide.OVER,
) -> Bucket:
    """Hit rate vs `opponent` over `season`'s REGULAR-season played games (playoffs excluded)."""
    vs = [
        is_hit(g.value, line, side)
        for g in logs
        if g.played and g.season == season and g.season_type == "REG" and g.opponent == opponent
    ]
    return Bucket(sum(vs), len(vs))


def compute_hit_rates(
    logs: Iterable[GameLog],
    *,
    games_played: int,
    line: Decimal | float,
    side: SelectionSide = SelectionSide.OVER,
    opponent: str | None = None,
) -> HitRates:
    played = sorted((g for g in logs if g.played), key=lambda g: (g.season, g.week), reverse=True)
    hits = [is_hit(g.value, line, side) for g in played]
    # Denominator is games_played. If rows are missing, the missing games count as misses. More
    # rows than games_played would be a data error, so never report hit > total.
    season_total = max(games_played, len(played))
    season = Bucket(sum(hits), season_total)
    last = hits[:LAST_N]
    last5 = Bucket(sum(last), len(last))
    vs_opp = None
    label = None
    if opponent:
        vs = [h for g, h in zip(played, hits, strict=True) if g.opponent == opponent]
        vs_opp = Bucket(sum(vs), len(vs))
        label = f"vs {opponent}"
    return HitRates(season=season, last5=last5, vs_opp=vs_opp, vs_opp_label=label)
