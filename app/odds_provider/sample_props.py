"""SAMPLE player props + hit rates mirrored from plan-frontend/mock/data.js.

!!! SAMPLE DATA — NOT REAL ODDS OR HIT RATES !!!
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class SampleHitRate:
    pct: int
    hit: int
    total: int
    label: str | None = None


@dataclass(frozen=True)
class SampleProp:
    id: str
    player: str
    team: str
    market: str
    line_label: str  # "Over 4.5"
    line: Decimal
    price: int
    season: SampleHitRate | None
    last5: SampleHitRate | None
    vs_opp: SampleHitRate | None


def _hr(pct: int, hit: int, total: int, label: str | None = None) -> SampleHitRate:
    return SampleHitRate(pct, hit, total, label)


def _p(
    pid: str,
    player: str,
    team: str,
    market: str,
    line_label: str,
    price: int,
    season: tuple[int, int, int] | None,
    last5: tuple[int, int, int] | None,
    vs_opp: tuple[int, int, int, str] | None,
) -> SampleProp:
    line = Decimal(line_label.split()[-1])
    return SampleProp(
        id=pid,
        player=player,
        team=team,
        market=market,
        line_label=line_label,
        line=line,
        price=price,
        season=_hr(*season) if season else None,
        last5=_hr(*last5) if last5 else None,
        vs_opp=_hr(vs_opp[0], vs_opp[1], vs_opp[2], vs_opp[3]) if vs_opp else None,
    )


# Keyed by short game slug (away-home), matching frontend mock ids.
SAMPLE_PROPS: dict[str, list[SampleProp]] = {
    "tb-dal": [
        _p(
            "godwin-rec",
            "Chris Godwin",
            "TB",
            "Receptions",
            "Over 4.5",
            -125,
            (100, 4, 4),
            (100, 4, 4),
            (75, 3, 4, "vs DAL"),
        ),
        _p(
            "lamb-rec",
            "CeeDee Lamb",
            "DAL",
            "Receptions",
            "Over 5.5",
            -115,
            (75, 3, 4),
            (80, 4, 5),
            (67, 2, 3, "vs TB"),
        ),
        _p(
            "lamb-yds",
            "CeeDee Lamb",
            "DAL",
            "Receiving Yards",
            "Over 74.5",
            -110,
            (50, 2, 4),
            (60, 3, 5),
            (33, 1, 3, "vs TB"),
        ),
        _p(
            "evans-rec",
            "Mike Evans",
            "TB",
            "Receptions",
            "Over 4.5",
            105,
            (50, 2, 4),
            (40, 2, 5),
            (50, 2, 4, "vs DAL"),
        ),
        _p(
            "evans-yds",
            "Mike Evans",
            "TB",
            "Receiving Yards",
            "Over 64.5",
            -105,
            (75, 3, 4),
            (60, 3, 5),
            (75, 3, 4, "vs DAL"),
        ),
        _p(
            "prescott-pass",
            "Dak Prescott",
            "DAL",
            "Passing Yards",
            "Over 249.5",
            -115,
            (50, 2, 4),
            (40, 2, 5),
            (67, 2, 3, "vs TB"),
        ),
        _p(
            "mayfield-pass",
            "Baker Mayfield",
            "TB",
            "Passing Yards",
            "Over 234.5",
            -110,
            (75, 3, 4),
            (80, 4, 5),
            (50, 2, 4, "vs DAL"),
        ),
        _p(
            "dowdle-rush",
            "Rico Dowdle",
            "DAL",
            "Rushing Yards",
            "Over 62.5",
            -120,
            (50, 2, 4),
            (60, 3, 5),
            None,
        ),
    ],
    "kc-buf": [
        _p(
            "cook-rush",
            "James Cook",
            "BUF",
            "Rushing Yards",
            "Over 68.5",
            -115,
            (75, 3, 4),
            (80, 4, 5),
            (100, 2, 2, "vs KC"),
        ),
        _p(
            "cook-rec2",
            "James Cook",
            "BUF",
            "Receptions",
            "Over 1.5",
            -130,
            (100, 4, 4),
            (100, 4, 4),
            (100, 2, 2, "vs KC"),
        ),
        _p(
            "kelce-rec",
            "Travis Kelce",
            "KC",
            "Receptions",
            "Over 5.5",
            -105,
            (50, 2, 4),
            (60, 3, 5),
            (50, 1, 2, "vs BUF"),
        ),
        _p(
            "mahomes-pass",
            "Patrick Mahomes",
            "KC",
            "Passing Yards",
            "Over 274.5",
            -110,
            (50, 2, 4),
            (40, 2, 5),
            (50, 1, 2, "vs BUF"),
        ),
        _p(
            "allen-pass",
            "Josh Allen",
            "BUF",
            "Passing Yards",
            "Over 254.5",
            -115,
            (75, 3, 4),
            (60, 3, 5),
            (100, 2, 2, "vs KC"),
        ),
        _p(
            "allen-rush",
            "Josh Allen",
            "BUF",
            "Rushing Yards",
            "Over 34.5",
            -110,
            (100, 4, 4),
            (80, 4, 5),
            (100, 2, 2, "vs KC"),
        ),
    ],
    "det-gb": [
        _p(
            "gibbs-rush",
            "Jahmyr Gibbs",
            "DET",
            "Rushing Yards",
            "Over 72.5",
            -115,
            (75, 3, 4),
            (80, 4, 5),
            (67, 2, 3, "vs GB"),
        ),
        _p(
            "stbrown-rec",
            "Amon-Ra St. Brown",
            "DET",
            "Receptions",
            "Over 6.5",
            -120,
            (100, 4, 4),
            (100, 5, 5),
            (100, 3, 3, "vs GB"),
        ),
        _p(
            "love-pass",
            "Jordan Love",
            "GB",
            "Passing Yards",
            "Over 239.5",
            -110,
            (50, 2, 4),
            (40, 2, 5),
            (33, 1, 3, "vs DET"),
        ),
        _p(
            "doubs-yds",
            "Romeo Doubs",
            "GB",
            "Receiving Yards",
            "Over 48.5",
            -105,
            (50, 2, 4),
            (60, 3, 5),
            (67, 2, 3, "vs DET"),
        ),
    ],
    "sf-sea": [
        _p(
            "cmc-rush",
            "Christian McCaffrey",
            "SF",
            "Rushing Yards",
            "Over 78.5",
            -115,
            (75, 3, 4),
            (80, 4, 5),
            (50, 1, 2, "vs SEA"),
        ),
        _p(
            "cmc-rec",
            "Christian McCaffrey",
            "SF",
            "Receptions",
            "Over 4.5",
            -110,
            (75, 3, 4),
            (60, 3, 5),
            (100, 2, 2, "vs SEA"),
        ),
        _p(
            "metcalf-yds",
            "DK Metcalf",
            "SEA",
            "Receiving Yards",
            "Over 58.5",
            -105,
            (50, 2, 4),
            (40, 2, 5),
            (50, 1, 2, "vs SF"),
        ),
        _p(
            "purdy-pass",
            "Brock Purdy",
            "SF",
            "Passing Yards",
            "Over 224.5",
            -110,
            (50, 2, 4),
            (60, 3, 5),
            (50, 1, 2, "vs SEA"),
        ),
    ],
    "phi-nyg": [
        _p(
            "saquon-rush",
            "Saquon Barkley",
            "PHI",
            "Rushing Yards",
            "Over 88.5",
            -115,
            (100, 4, 4),
            (80, 4, 5),
            (100, 2, 2, "vs NYG"),
        ),
        _p(
            "brown-rec",
            "A.J. Brown",
            "PHI",
            "Receptions",
            "Over 4.5",
            -120,
            (75, 3, 4),
            (60, 3, 5),
            (100, 2, 2, "vs NYG"),
        ),
        _p(
            "nabers-yds",
            "Malik Nabers",
            "NYG",
            "Receiving Yards",
            "Over 68.5",
            -110,
            (75, 3, 4),
            (80, 4, 5),
            (50, 1, 2, "vs PHI"),
        ),
        _p(
            "hurts-pass",
            "Jalen Hurts",
            "PHI",
            "Passing Yards",
            "Over 214.5",
            -105,
            (50, 2, 4),
            (40, 2, 5),
            (50, 1, 2, "vs NYG"),
        ),
    ],
    "bal-cin": [
        _p(
            "henry-rush",
            "Derrick Henry",
            "BAL",
            "Rushing Yards",
            "Over 84.5",
            -120,
            (75, 3, 4),
            (80, 4, 5),
            (100, 2, 2, "vs CIN"),
        ),
        _p(
            "chase-rec",
            "Ja'Marr Chase",
            "CIN",
            "Receptions",
            "Over 6.5",
            -115,
            (100, 4, 4),
            (100, 5, 5),
            (100, 2, 2, "vs BAL"),
        ),
        _p(
            "chase-yds",
            "Ja'Marr Chase",
            "CIN",
            "Receiving Yards",
            "Over 89.5",
            -110,
            (75, 3, 4),
            (60, 3, 5),
            (50, 1, 2, "vs BAL"),
        ),
        _p(
            "jackson-pass",
            "Lamar Jackson",
            "BAL",
            "Passing Yards",
            "Over 229.5",
            -105,
            (50, 2, 4),
            (40, 2, 5),
            (50, 1, 2, "vs CIN"),
        ),
    ],
}
