"""Read + validate Plan Data's product CSVs (top-level files only; `_prev/` is ignored)."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.models.nfl_data import STAT_COLUMNS

# nflverse/Plan Data team codes → our teams.abbr
TEAM_MAP = {"LA": "LAR", "STL": "LAR", "SD": "LAC", "OAK": "LV", "WSH": "WAS", "JAC": "JAX"}

PLAYERS_FILE = "players_active.csv"
SCHEDULE_FILE = "schedule_games_raw.csv"
REQUIRED_SCHEDULE_COLS = {
    "game_id",
    "season",
    "game_type",
    "week",
    "gameday",
    "gametime",
    "away_team",
    "home_team",
}


def season_file(season: int) -> str:
    return f"player_season_stats_{season}_offense_props.csv"


def game_file(season: int) -> str:
    return f"player_game_stats_{season}_offense_props.csv"


def vs_opp_file(season: int) -> str:
    return f"player_vs_opponent_{season}.csv"


def matchup_files(d: Path, season: int) -> list[tuple[int, Path]]:
    """weekN_{season}_matchups.csv files, sorted by week."""
    out = []
    for f in d.glob(f"week*_{season}_matchups.csv"):
        m = re.fullmatch(rf"week(\d+)_{season}_matchups\.csv", f.name)
        if m:
            out.append((int(m.group(1)), f))
    return sorted(out)


REQUIRED_VS_OPP_COLS = {"player_id", "season", "opponent_team", "games_played", "games_played_reg"}
REQUIRED_MATCHUP_COLS = {"season", "week", "player_id", "team", "opponent_team", "is_bye"}
# vs-opponent summary columns cross-checked against summed game logs (REG + POST, like the file).
VS_OPP_SUM_COLS = (
    "receptions", "targets", "receiving_yards", "carries", "rushing_yards", "completions", "attempts",
    "passing_yards", "passing_tds", "rushing_tds", "receiving_tds",
)  # fmt: skip

REQUIRED_PLAYER_COLS = {"player_id", "full_name", "team", "position"}
REQUIRED_SEASON_COLS = {
    "player_id",
    "full_name",
    "season",
    "season_type",
    "team",
    "games_played",
    *STAT_COLUMNS,
}
REQUIRED_GAME_COLS = {
    "player_id", "full_name", "season", "week", "season_type", "game_id", "team", "opponent_team", "played",
    *STAT_COLUMNS,
}  # fmt: skip

_EXTRA_NUMERIC = (
    "sacks_suffered", "sack_yards_lost", "passing_2pt_conversions", "rushing_fumbles", "rushing_fumbles_lost",
    "rushing_first_downs", "rushing_2pt_conversions", "receiving_fumbles", "receiving_fumbles_lost",
    "receiving_first_downs", "receiving_2pt_conversions", "target_share", "air_yards_share", "fantasy_points",
)  # fmt: skip


class DataError(ValueError):
    """The data directory is unusable (missing files/columns)."""


def team(code: str | None) -> str | None:
    code = (code or "").strip().upper()
    return TEAM_MAP.get(code, code) or None


def name_key(name: str) -> str:
    n = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    n = re.sub(r"[.',-]", " ", n)
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", " ", n)
    return " ".join(n.split())


def to_int(v: Any) -> int:
    if v in (None, ""):
        return 0
    return round(float(v))


def to_opt_int(v: Any) -> int | None:
    return None if v in (None, "") else round(float(v))


def to_float(v: Any) -> float | None:
    return None if v in (None, "") else float(v)


def to_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "t", "yes", "y")


def extra(row: dict[str, str]) -> dict[str, float]:
    return {k: float(row[k]) for k in _EXTRA_NUMERIC if row.get(k) not in (None, "")}


@dataclass
class DataSet:
    directory: Path
    season: int
    through_week: int
    players: list[dict[str, str]]
    season_rows: list[dict[str, str]]
    game_rows: list[dict[str, str]]  # played == True only
    file_hashes: dict[str, str]
    warnings: list[str] = field(default_factory=list)
    dropped_not_played: int = 0
    validation: dict[str, int] = field(default_factory=dict)
    # Prior-season context (optional files): game logs + per-opponent summary for cross-checks.
    prev_season: int | None = None
    prev_game_rows: list[dict[str, str]] = field(default_factory=list)
    prev_vs_opp_rows: list[dict[str, str]] = field(default_factory=list)
    # Current-season upcoming-week matchups (optional): player's current team + opponent / BYE.
    matchup_week: int | None = None
    matchups: list[dict[str, str]] = field(default_factory=list)
    # Real schedule for one week (optional schedule_games_raw.csv), imported as our games.
    schedule_week: int | None = None
    schedule_rows: list[dict[str, str]] = field(default_factory=list)


def _read(path: Path, required: set[str]) -> list[dict[str, str]]:
    if not path.is_file():
        raise DataError(f"missing file: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise DataError(f"{path.name}: missing columns {sorted(missing)}")
        return list(reader)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_dataset(
    directory: str | Path, season: int | None = None, schedule_week: int | None = None
) -> DataSet:
    d = Path(directory)
    if not d.is_dir():
        raise DataError(f"not a directory: {d}")
    meta: dict[str, Any] = {}
    if (d / "_meta.json").is_file():
        meta = json.loads((d / "_meta.json").read_text())
    season = season or meta.get("season")
    if not season:
        raise DataError("season unknown: pass --season or provide _meta.json")
    season = int(season)

    players = _read(d / PLAYERS_FILE, REQUIRED_PLAYER_COLS)
    season_rows = _read(d / season_file(season), REQUIRED_SEASON_COLS)
    all_games = _read(d / game_file(season), REQUIRED_GAME_COLS)
    warnings: list[str] = []

    game_rows = [r for r in all_games if to_bool(r["played"]) and to_int(r["season"]) == season]
    dropped = len(all_games) - len(game_rows)
    if dropped:
        warnings.append(f"{dropped} game row(s) with played=false or another season were skipped")
    season_rows = [r for r in season_rows if to_int(r["season"]) == season]

    # --- validation: one row per player-week; season totals == game sums; games_played == rows
    dup = [
        k
        for k, n in Counter((r["player_id"], r["season_type"], r["week"]) for r in game_rows).items()
        if n > 1
    ]
    if dup:
        raise DataError(f"duplicate player-week rows, e.g. {dup[:3]}")
    by_player: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for r in game_rows:
        by_player[(r["player_id"], r["season_type"])].append(r)
    season_keys = {(r["player_id"], r["season_type"]) for r in season_rows}
    total_mismatch = gp_mismatch = 0
    for r in season_rows:
        rows = by_player.get((r["player_id"], r["season_type"]), [])
        for col in STAT_COLUMNS:
            if sum(to_int(g[col]) for g in rows) != to_int(r[col]):
                total_mismatch += 1
                if total_mismatch <= 10:
                    warnings.append(f"season total != game sum: {r['full_name']} {col}")
        if to_int(r["games_played"]) != len(rows):
            gp_mismatch += 1
            if gp_mismatch <= 10:
                warnings.append(
                    f"games_played {r['games_played']} != {len(rows)} played rows: {r['full_name']}"
                )
    orphan_games = sum(1 for k in by_player if k not in season_keys)
    if orphan_games:
        warnings.append(f"{orphan_games} player(s) have game rows but no season row")

    # --- prior season (optional): game logs, vs-opponent summary, cross-checks
    prev = season - 1
    prev_rows: list[dict[str, str]] = []
    vs_rows: list[dict[str, str]] = []
    prev_validation = {
        "prev_season_vs_opp_games_reg_mismatches": 0,
        "prev_season_vs_opp_games_mismatches": 0,
        "prev_season_vs_opp_sum_mismatches": 0,
        "prev_season_vs_opp_pairs_missing": 0,
    }
    if (d / game_file(prev)).is_file():
        raw = _read(d / game_file(prev), REQUIRED_GAME_COLS)
        prev_rows = [r for r in raw if to_bool(r["played"]) and to_int(r["season"]) == prev]
        if len(raw) != len(prev_rows):
            warnings.append(f"{len(raw) - len(prev_rows)} {prev} game row(s) with played=false skipped")
        dup = [
            k
            for k, n in Counter((r["player_id"], r["season_type"], r["week"]) for r in prev_rows).items()
            if n > 1
        ]
        if dup:
            raise DataError(f"duplicate {prev} player-week rows, e.g. {dup[:3]}")
    else:
        warnings.append(f"no {game_file(prev)}: vsOppLastSeason will be null")
    if prev_rows and (d / vs_opp_file(prev)).is_file():
        vs_rows = [
            r for r in _read(d / vs_opp_file(prev), REQUIRED_VS_OPP_COLS) if to_int(r["season"]) == prev
        ]
        by_pair: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
        for r in prev_rows:
            by_pair[(r["player_id"], team(r["opponent_team"]) or "")].append(r)
        seen_pairs = set()
        for v in vs_rows:
            key = (v["player_id"], team(v["opponent_team"]) or "")
            seen_pairs.add(key)
            rows = by_pair.get(key, [])
            reg = sum(1 for r in rows if r["season_type"] == "REG")
            who = f"{v.get('full_name', v['player_id'])} vs {key[1]}"
            if reg != to_int(v["games_played_reg"]):
                prev_validation["prev_season_vs_opp_games_reg_mismatches"] += 1
                warnings.append(
                    f"{prev} vs-opp games_played_reg {v['games_played_reg']} != {reg} REG logs: {who}"
                )
            if len(rows) != to_int(v["games_played"]):
                prev_validation["prev_season_vs_opp_games_mismatches"] += 1
                warnings.append(f"{prev} vs-opp games_played {v['games_played']} != {len(rows)} logs: {who}")
            bad = [c for c in VS_OPP_SUM_COLS if c in v and sum(to_int(r[c]) for r in rows) != to_int(v[c])]
            if bad:
                prev_validation["prev_season_vs_opp_sum_mismatches"] += 1
                warnings.append(f"{prev} vs-opp sums != game logs ({', '.join(bad)}): {who}")
        missing_pairs = [k for k in by_pair if k not in seen_pairs]
        prev_validation["prev_season_vs_opp_pairs_missing"] = len(missing_pairs)
        if missing_pairs:
            warnings.append(
                f"{len(missing_pairs)} {prev} player/opponent pair(s) in game logs lack a vs-opp row"
            )
    elif prev_rows:
        warnings.append(f"no {vs_opp_file(prev)}: {prev} game counts not cross-checked")

    # --- upcoming-week matchups (optional; latest weekN file for this season)
    matchup_week = None
    matchups: list[dict[str, str]] = []
    files = matchup_files(d, season)
    if files:
        matchup_week, mf = files[-1]
        if len(files) > 1:
            warnings.append(f"several matchup files; using {mf.name}")
        matchups = [
            r
            for r in _read(mf, REQUIRED_MATCHUP_COLS)
            if to_int(r["season"]) == season and to_int(r["week"]) == matchup_week
        ]
        no_id = sum(1 for r in matchups if not r["player_id"].strip())
        if no_id:
            warnings.append(f"{no_id} matchup row(s) without player_id skipped")
    else:
        warnings.append("no week*_matchups file: vsOpp opponent falls back to the other team on our game")

    through_week = int(
        meta.get("latest_completed_week") or max((to_int(r["week"]) for r in game_rows), default=0)
    )

    # --- real schedule (optional): the upcoming week = matchup week, else through_week + 1
    sched_week = None
    sched_rows: list[dict[str, str]] = []
    sched_validation = {
        "schedule_duplicate_teams": 0,
        "schedule_matchup_mismatches": 0,
        "schedule_bye_mismatches": 0,
    }
    if (d / SCHEDULE_FILE).is_file():
        sched_week = schedule_week or matchup_week or through_week + 1
        sched_rows = [
            r
            for r in _read(d / SCHEDULE_FILE, REQUIRED_SCHEDULE_COLS)
            if to_int(r["season"]) == season and to_int(r["week"]) == sched_week
        ]
        if not sched_rows:
            warnings.append(
                f"{SCHEDULE_FILE}: no games for {season} week {sched_week}; schedule not imported"
            )
            sched_week = None
    else:
        warnings.append(f"no {SCHEDULE_FILE}: board stays on the existing (sample) slate")
    if sched_rows:
        teams_n = Counter(t for r in sched_rows for t in (team(r["away_team"]), team(r["home_team"])))
        dups = [t for t, n in teams_n.items() if n > 1]
        sched_validation["schedule_duplicate_teams"] = len(dups)
        if dups:
            warnings.append(
                f"teams scheduled twice in week {sched_week}: {', '.join(sorted(map(str, dups)))}"
            )
        by_gid = {r["game_id"]: r for r in sched_rows}
        if matchup_week == sched_week:
            for m in matchups:
                if not m["player_id"].strip():
                    continue
                t = team(m["team"])
                if to_bool(m["is_bye"]) or m["opponent_team"].strip().upper() == "BYE":
                    if t in teams_n:
                        sched_validation["schedule_bye_mismatches"] += 1
                        warnings.append(
                            f"matchups say {t} is on bye but it is scheduled in week {sched_week}"
                        )
                    continue
                g = by_gid.get(m.get("game_id", ""))
                teams = {team(g["away_team"]), team(g["home_team"])} if g else set()
                if g is None or teams != {t, team(m["opponent_team"])}:
                    sched_validation["schedule_matchup_mismatches"] += 1
                    if sched_validation["schedule_matchup_mismatches"] <= 10:
                        warnings.append(
                            f"matchup {m.get('full_name')} {t} vs {m['opponent_team']} ({m.get('game_id')}) "
                            "not in schedule"
                        )
    hashes = {
        name: _sha256(d / name)
        for name in (
            PLAYERS_FILE, season_file(season), game_file(season), "_meta.json",
            game_file(prev), vs_opp_file(prev), SCHEDULE_FILE,
            *(f.name for _, f in files[-1:]),
        )
        if (d / name).is_file()
    }  # fmt: skip
    return DataSet(
        directory=d,
        season=season,
        through_week=through_week,
        players=players,
        season_rows=season_rows,
        game_rows=game_rows,
        file_hashes=hashes,
        warnings=warnings,
        dropped_not_played=dropped,
        validation={
            "season_total_mismatches": total_mismatch,
            "games_played_mismatches": gp_mismatch,
            "players_with_games_no_season_row": orphan_games,
            **prev_validation,
            **sched_validation,
        },
        prev_season=prev if prev_rows else None,
        prev_game_rows=prev_rows,
        prev_vs_opp_rows=vs_rows,
        matchup_week=matchup_week,
        matchups=matchups,
        schedule_week=sched_week,
        schedule_rows=sched_rows,
    )
