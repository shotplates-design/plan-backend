"""Odds / Plan Data market keys ↔ board labels ↔ stat columns."""

from __future__ import annotations

# Normalized Plan Data / Odds API market → board label (Prop.market)
ODDS_TO_BOARD: dict[str, str] = {
    "receptions": "Receptions",
    "receiving_yards": "Receiving Yards",
    "rushing_yards": "Rushing Yards",
    "rush_attempts": "Rushing Attempts",
    "passing_yards": "Passing Yards",
    "passing_tds": "Passing TDs",
    "completions": "Pass Completions",
    "anytime_td": "Anytime TD",
}

BOARD_TO_ODDS: dict[str, str] = {v: k for k, v in ODDS_TO_BOARD.items()}

# Board label → player_game_stats column (Anytime TD is composite — handled in loader)
BOARD_STAT_KEYS: dict[str, str | None] = {
    "Receptions": "receptions",
    "Receiving Yards": "receiving_yards",
    "Passing Yards": "passing_yards",
    "Rushing Yards": "rushing_yards",
    "Passing Attempts": "attempts",
    "Pass Completions": "completions",
    "Passing TDs": "passing_tds",
    "Rushing Attempts": "carries",
    "Targets": "targets",
    "Anytime TD": None,  # receiving_tds + rushing_tds + passing_tds
}

# Stable prop id suffix from normalized market
MARKET_ID_SUFFIX: dict[str, str] = {
    "receptions": "rec",
    "receiving_yards": "yds",
    "rushing_yards": "rush",
    "rush_attempts": "carries",
    "passing_yards": "pass",
    "passing_tds": "pass-td",
    "completions": "comp",
    "anytime_td": "atd",
}

BOOKS = ("fanduel", "draftkings")
