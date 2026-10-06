"""load-nfl-data: synthetic Plan Data files → DB → props/board (plus a real-data sanity check)."""

import csv
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli import main as cli_main
from app.models import NflDataLoad, Player, PlayerGameStat, PlayerMatchup, PlayerSeasonStat, Prop
from app.models.nfl_data import STAT_COLUMNS
from app.nfl_data.files import DataError, read_dataset
from app.nfl_data.loader import load_dataset
from app.odds_provider.mock import SAMPLE_SEASON, SAMPLE_WEEK, MockOddsProvider
from app.services.odds_sync import sync_week
from tests.conftest import signup

REAL_DIR = Path("/workspace/plan-nfl-data")
GAME_COLS = ["player_id", "full_name", "position", "position_group", "season", "week", "season_type", "game_type",
             "game_id",
             "team", "opponent_team", "played", "played_any_snaps", "stats_row_imputed", "snap_data_found",
             "offense_snaps", "offense_snap_pct", *STAT_COLUMNS, "fantasy_points_ppr"]  # fmt: skip
SEASON_COLS = ["player_id", "full_name", "position", "position_group", "season", "season_type", "team", "games",
               "games_played", "games_played_any_snaps", "team_games", "offense_snaps_total", *STAT_COLUMNS,
               "fantasy_points_ppr"]  # fmt: skip
VS_COLS = ["player_id", "full_name", "position", "season", "opponent_team", "games_played", "games_played_reg",
           "games_played_post", "receptions", "targets", "receiving_yards", "carries", "rushing_yards", "completions",
           "attempts", "passing_yards", "passing_tds", "rushing_tds", "receiving_tds"]  # fmt: skip
MATCHUP_COLS = ["season", "week", "player_id", "full_name", "position", "team", "status", "opponent_team", "is_bye",
                "game_id", "home_away", "kickoff_et", "has_2025_vs_opp_games"]  # fmt: skip
PLAYER_COLS = ["player_id", "sleeper_id", "full_name", "first_name", "last_name", "team", "position",
               "depth_chart_position", "jersey_number", "status", "status_code", "headshot_url"]  # fmt: skip

# (gsis, name, team, pos, in_active_roster, [(week, opp, {stat: value}, imputed)])
PLAYERS = [
    ("00-0000001", "Chris Godwin Jr.", "TB", "WR", True,
     [(1, "CIN", {"receptions": 6, "targets": 8}, False), (2, "CLE", {}, True),  # week 2: zero-filled played game
      (3, "MIN", {"receptions": 5, "targets": 6}, False), (4, "GB", {"receptions": 2, "targets": 3}, False)]),
    ("00-0000002", "CeeDee Lamb", "DAL", "WR", True,
     [(1, "TB", {"receptions": 7}, False), (2, "NYG", {"receptions": 4}, False),
      (3, "LA", {"receptions": 8}, False), (4, "HOU", {"receptions": 6}, False)]),
    ("00-0000003", "Mike Evans", "SF", "WR", True, [(1, "SEA", {"receptions": 5}, False)]),  # traded: not TB
    ("00-0000004", "Travis Kelce", "KC", "TE", False,  # KC missing from players_active (like the real file)
     [(1, "LAC", {"receptions": 6}, False), (2, "PHI", {"receptions": 3}, False)]),
]  # fmt: skip

# 2025 logs: (gsis, name, team, pos, [(week, season_type, opp, {stat: value}, imputed)])
PREV = [
    ("00-0000001", "Chris Godwin Jr.", "TB", "WR",
     [(3, "REG", "DAL", {"receptions": 6}, False),     # hit vs DAL
      (10, "REG", "DAL", {}, True),                    # zero row vs DAL -> miss
      (12, "REG", "NO", {"receptions": 9}, False),     # other opponent
      (19, "POST", "DAL", {"receptions": 9}, False)]),  # playoffs: excluded from vsOppLastSeason
    ("00-0000004", "Travis Kelce", "KC", "TE", [(9, "REG", "BUF", {"receptions": 7}, False)]),
    ("00-0000003", "Mike Evans", "TB", "WR", [(5, "REG", "SEA", {"receptions": 3}, False)]),
    ("00-0000099", "Old Timer", "LA", "WR", [(1, "REG", "SF", {"receptions": 2}, False)]),  # retired: history only
]  # fmt: skip

# Week-5 matchups: gsis -> (team, opponent | "BYE", real game id)
MATCHUPS = {
    "00-0000001": ("TB", "DAL", "2026_05_TB_DAL"),
    "00-0000002": ("DAL", "TB", "2026_05_TB_DAL"),
    "00-0000003": ("SF", "SEA", "2026_05_SF_SEA"),  # traded -> hidden, but a real game exists
    "00-0000004": ("KC", "BYE", ""),  # bye -> vsOpp falls back to our sample game (BUF)
}


def write_prev(d: Path, prev=PREV, *, vs_overrides: dict | None = None) -> None:
    rows, pairs = [], {}
    for gsis, name, tm, pos, logs in prev:
        for week, stype, opp, stats, imputed in logs:
            row = dict.fromkeys(STAT_COLUMNS, 0) | stats
            rows.append({**row, "player_id": gsis, "full_name": name, "position": pos, "position_group": pos,
                         "season": 2025, "week": week, "season_type": stype,
                         "game_type": "REG" if stype == "REG" else "WC", "game_id": f"2025_{week:02d}_{tm}_{opp}",
                         "team": tm, "opponent_team": opp, "played": "True", "played_any_snaps": "True",
                         "stats_row_imputed": str(imputed), "snap_data_found": "True", "offense_snaps": "30.0",
                         "offense_snap_pct": "0.5", "fantasy_points_ppr": "0.0"})  # fmt: skip
            v = pairs.setdefault((gsis, opp), {"player_id": gsis, "full_name": name, "position": pos, "season": 2025,
                                               "opponent_team": opp, "games_played": 0, "games_played_reg": 0,
                                               "games_played_post": 0} | dict.fromkeys(VS_COLS[8:], 0))  # fmt: skip
            v["games_played"] += 1
            v["games_played_reg" if stype == "REG" else "games_played_post"] += 1
            for c in VS_COLS[8:]:
                v[c] += row.get(c, 0)
    for key, changes in (vs_overrides or {}).items():
        pairs[key].update(changes)
    for name, cols, out in (("player_game_stats_2025_offense_props.csv", GAME_COLS, rows),
                            ("player_vs_opponent_2025.csv", VS_COLS, pairs.values())):  # fmt: skip
        with (d / name).open("w", newline="") as fh:
            w = csv.DictWriter(fh, cols)
            w.writeheader()
            w.writerows(out)


def write_matchups(d: Path, matchups=MATCHUPS) -> None:
    with (d / "week5_2026_matchups.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, MATCHUP_COLS)
        w.writeheader()
        for gsis, (tm, opp, gid) in matchups.items():
            w.writerow({"season": 2026, "week": 5, "player_id": gsis, "team": tm, "opponent_team": opp,
                        "is_bye": str(opp == "BYE"), "game_id": gid, "status": "Active"})  # fmt: skip
        w.writerow({"season": 2026, "week": 5, "player_id": "", "full_name": "No Id", "team": "NYJ",
                    "opponent_team": "CLE", "is_bye": "False"})  # fmt: skip


def write_dataset(
    d: Path, players=PLAYERS, *, extra_week: int | None = None, drop: tuple | None = None, prev=True,
    matchups=MATCHUPS, vs_overrides: dict | None = None,
) -> Path:  # fmt: skip
    d.mkdir(parents=True, exist_ok=True)
    (d / "_meta.json").write_text(json.dumps({"season": 2026, "latest_completed_week": extra_week or 4}))
    # A decoy _prev/ dir with garbage must be ignored.
    (d / "_prev").mkdir(exist_ok=True)
    (d / "_prev" / "player_game_stats_2026_offense_props.csv").write_text("garbage\n")
    with (d / "players_active.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, PLAYER_COLS)
        w.writeheader()
        for gsis, name, tm, pos, active, _ in players:
            if active:
                w.writerow({"player_id": gsis, "full_name": name, "team": tm, "position": pos, "status": "Active", "status_code": "ACT",
                            "first_name": name.split()[0], "last_name": name.split()[-1], "jersey_number": "13.0"})  # fmt: skip
    games, seasons = [], []
    for gsis, name, tm, pos, _, logs in players:
        logs = list(logs)
        if extra_week:
            logs.append((extra_week, "XXX", {"receptions": 9}, False))
        if drop and drop[0] == gsis:
            logs = [g for g in logs if g[0] != drop[1]]
        totals = dict.fromkeys(STAT_COLUMNS, 0)
        for week, opp, stats, imputed in logs:
            row = dict.fromkeys(STAT_COLUMNS, 0) | stats
            for c in STAT_COLUMNS:
                totals[c] += row[c]
            games.append({**row, "player_id": gsis, "full_name": name, "position": pos, "position_group": pos,
                          "season": 2026, "week": week, "season_type": "REG",
                          "game_id": f"2026_{week:02d}_{tm}_{opp}", "team": tm, "opponent_team": opp,
                          "played": "True", "played_any_snaps": "True", "stats_row_imputed": str(imputed),
                          "snap_data_found": "True", "offense_snaps": "40.0", "offense_snap_pct": "0.6",
                          "fantasy_points_ppr": "0.0"})  # fmt: skip
        seasons.append({**totals, "player_id": gsis, "full_name": name, "position": pos, "position_group": pos,
                        "season": 2026, "season_type": "REG", "team": tm, "games": len(logs) - 1,
                        "games_played": len(logs), "games_played_any_snaps": len(logs), "team_games": 4,
                        "offense_snaps_total": 40 * len(logs), "fantasy_points_ppr": "0.0"})  # fmt: skip
    for name, cols, rows in (("player_game_stats_2026_offense_props.csv", GAME_COLS, games),
                             ("player_season_stats_2026_offense_props.csv", SEASON_COLS, seasons)):  # fmt: skip
        with (d / name).open("w", newline="") as fh:
            w = csv.DictWriter(fh, cols)
            w.writeheader()
            w.writerows(rows)
    if prev:
        write_prev(d, vs_overrides=vs_overrides)
    if matchups is not None:
        write_matchups(d, matchups)
    return d


def prop(db, ext_id) -> Prop:
    db.expire_all()
    return db.scalars(select(Prop).where(Prop.external_id == ext_id)).one()


@pytest.fixture
def loaded(tmp_path, db, seeded):
    ds = read_dataset(write_dataset(tmp_path / "plan-nfl-data"))
    return load_dataset(db, ds)


def test_validation_is_clean_and_prev_is_ignored(tmp_path):
    ds = read_dataset(write_dataset(tmp_path / "d"))
    assert ds.season == 2026 and ds.through_week == 4
    assert len(ds.validation) == 10 and not any(ds.validation.values()), ds.validation
    assert len(ds.game_rows) == 11
    assert ds.prev_season == 2025 and len(ds.prev_game_rows) == 7 and len(ds.prev_vs_opp_rows) == 5
    assert ds.matchup_week == 5 and len(ds.matchups) == 5


def test_validation_flags_mismatch_and_missing_columns(tmp_path):
    d = write_dataset(tmp_path / "d")
    rows = list(csv.DictReader((d / "player_season_stats_2026_offense_props.csv").open()))
    rows[0]["receptions"] = "99"
    rows[1]["games_played"] = "9"
    with (d / "player_season_stats_2026_offense_props.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, SEASON_COLS)
        w.writeheader()
        w.writerows(rows)
    ds = read_dataset(d)
    assert ds.validation["season_total_mismatches"] == 1 and ds.validation["games_played_mismatches"] == 1
    (d / "players_active.csv").write_text("player_id,full_name\n")
    with pytest.raises(DataError, match="missing columns"):
        read_dataset(d)


def test_prev_season_cross_check_flags_mismatch(tmp_path, db, seeded):
    d = write_dataset(tmp_path / "d", vs_overrides={("00-0000001", "DAL"): {"games_played_reg": 3}})
    ds = read_dataset(d)
    assert ds.validation["prev_season_vs_opp_games_reg_mismatches"] == 1
    assert any("games_played_reg 3 != 2 REG logs" in w for w in ds.warnings)
    with pytest.raises(DataError, match="validation failed"):
        load_dataset(db, ds, strict=True)
    assert db.scalars(select(Player)).first() is None  # strict wrote nothing
    report = load_dataset(db, read_dataset(d))  # non-strict: loads; the prop-level cross-check reports it too
    assert report.validation["prev_season_prop_vs_opp_cross_check_mismatches"] == 1


def test_optional_files_absent_still_loads(tmp_path, db, seeded):
    ds = read_dataset(write_dataset(tmp_path / "d", prev=False, matchups=None))
    assert ds.prev_season is None and ds.matchup_week is None
    load_dataset(db, ds)
    p = prop(db, "godwin-rec")
    assert p.hr_vs_opp_last_total is None and p.hr_opponent_source == "sample_game"
    assert p.hr_vs_opp_opponent == "DAL" and p.hr_vs_opp_season == 2026


def test_hit_rates_use_games_played_and_zero_rows(loaded, db):
    p = prop(db, "godwin-rec")  # Receptions Over 4.5: 6, 0 (zero-filled), 5, 2
    assert (p.hr_season_hit, p.hr_season_total, p.hr_season_pct) == (2, 4, 50)
    assert (p.hr_last5_hit, p.hr_last5_total) == (2, 4)
    assert p.hr_vs_opp_label == "vs DAL" and (p.hr_vs_opp_hit, p.hr_vs_opp_total, p.hr_vs_opp_pct) == (
        0,
        0,
        None,
    )
    assert (p.hit_rate_source, p.line_source, p.data_status, p.player_gsis_id) == (
        "plan_data", "sample", "ok", "00-0000001",
    )  # fmt: skip
    assert p.hit_rate_through_week == 4 and p.stat_key == "receptions"


def test_vs_opp_and_team_code_mapping(loaded, db):
    lamb = prop(db, "lamb-rec")  # Over 5.5: 7 vs TB, 4, 8 vs LA(→LAR), 6
    assert (lamb.hr_season_hit, lamb.hr_season_total) == (3, 4)
    assert (lamb.hr_vs_opp_hit, lamb.hr_vs_opp_total, lamb.hr_vs_opp_pct, lamb.hr_vs_opp_label) == (
        1,
        1,
        100,
        "vs TB",
    )
    assert db.scalars(select(PlayerGameStat).where(PlayerGameStat.opponent_abbr == "LAR")).first() is not None


def test_players_loaded_with_season_stats_fallback(loaded, db):
    kelce = db.scalars(select(Player).where(Player.gsis_id == "00-0000004")).one()
    assert (kelce.team_abbr, kelce.roster_source) == ("KC", "season_stats")
    assert prop(db, "kelce-rec").hr_season_total == 2
    assert any("KC" in w for w in loaded.warnings)
    gp = db.scalars(select(PlayerSeasonStat).where(PlayerSeasonStat.player_id == kelce.id)).one().games_played
    assert gp == 2


def test_unmatched_and_traded_players_are_gaps(loaded, db, client):
    gaps = {g["prop"]: g["gap"] for g in loaded.gaps}
    assert gaps["tb-dal/evans-rec"] == "player_not_in_game"  # Evans is on SF in real data
    evans_gap = next(g for g in loaded.gaps if g["prop"] == "tb-dal/evans-rec")
    assert (
        evans_gap["restored_by_real_schedule_import"] is True
        and evans_gap["real_week_game"] == "2026_05_SF_SEA"
    )
    assert evans_gap["sample_slate_game_for_team"] == "sf-sea" and evans_gap["real_opponent"] == "SEA"
    assert loaded.counts["props_hidden_restorable_by_real_schedule"] == 2
    assert gaps["tb-dal/prescott-pass"] == "player_not_found"  # not in the synthetic files
    evans = prop(db, "evans-rec")
    assert evans.is_open is False and evans.team_abbr == "SF"
    pres = prop(db, "prescott-pass")
    assert pres.hit_rate_source == "none" and pres.hr_season_total is None
    board = client.get("/api/v1/games/tb-dal").json()
    ids = {p["id"] for p in board["props"]}
    assert "evans-rec" not in ids and "godwin-rec" in ids  # hidden from listings
    body = signup(client, "gap@example.com")
    r = client.get(
        "/api/v1/games/tb-dal/props/evans-rec", headers={"Authorization": f"Bearer {body['access_token']}"}
    )
    assert r.status_code == 404
    pres_out = next(p for p in board["props"] if p["id"] == "prescott-pass")
    assert pres_out["hitRate"] is None and pres_out["hitRateSource"] == "none"


def test_board_shape_with_real_data(loaded, client):
    board = client.get("/api/v1/games/tb-dal").json()
    p = next(x for x in board["props"] if x["id"] == "godwin-rec")
    assert p["isSample"] is True and p["lineSource"] == "sample" and p["hitRateSource"] == "plan_data"
    assert p["playerId"] == "00-0000001" and p["hitRateThroughWeek"] == 4
    assert p["hitRate"] == {
        "season": {"pct": 50, "hit": 2, "total": 4},
        "last5": {"pct": 50, "hit": 2, "total": 4},
        "vsOpp": {"pct": None, "hit": 0, "total": 0, "label": "vs DAL", "season": 2026, "opponent": "DAL"},
        "vsOppLastSeason": {"pct": 50, "hit": 1, "total": 2, "label": "vs DAL '25", "season": 2025,
                            "opponent": "DAL"},
    }  # fmt: skip
    assert p["line"] == "Over 4.5" and isinstance(p["price"], int)  # sample line/price kept


def test_paywall_locking_still_applies(loaded, client, paywall_on):
    p = next(x for x in client.get("/api/v1/games/tb-dal").json()["props"] if x["id"] == "lamb-rec")
    assert p["unlocked"] is False and p["locked"] == ["season", "vs_opponent", "vs_opponent_last_season"]
    assert p["hitRate"]["vsOppLastSeason"] is None
    assert (
        p["hitRate"]["season"] is None
        and p["hitRate"]["vsOpp"] is None
        and p["hitRate"]["last5"]["total"] == 4
    )


def test_idempotent_rerun(tmp_path, db, seeded, loaded):
    before = {
        p.external_id: (p.hr_season_hit, p.hr_season_total, p.hr_last5_hit) for p in db.scalars(select(Prop))
    }
    report = load_dataset(db, read_dataset(tmp_path / "plan-nfl-data"))
    for k in ("players_created", "players_updated", "season_created", "season_updated", "season_deleted",
              "game_created", "game_updated", "game_deleted", "prev_game_created", "prev_game_updated",
              "prev_game_deleted", "matchup_created", "matchup_updated", "matchup_deleted"):  # fmt: skip
        assert report.counts[k] == 0, k
    db.expire_all()
    after = {
        p.external_id: (p.hr_season_hit, p.hr_season_total, p.hr_last5_hit) for p in db.scalars(select(Prop))
    }
    assert before == after
    assert len(db.scalars(select(NflDataLoad)).all()) == 2


def test_week5_rerun_adds_rows_without_leaking_into_week5_props(tmp_path, db, seeded, loaded):
    report = load_dataset(db, read_dataset(write_dataset(tmp_path / "plan-nfl-data", extra_week=5)))
    assert report.counts["game_created"] == 4 and report.through_week == 5
    p = prop(db, "godwin-rec")  # props are on week-5 games → only weeks 1-4 count
    assert (p.hr_season_hit, p.hr_season_total, p.hit_rate_through_week) == (2, 4, 4)


def test_rows_removed_from_files_are_deleted(tmp_path, db, seeded, loaded):
    report = load_dataset(db, read_dataset(write_dataset(tmp_path / "plan-nfl-data", drop=("00-0000002", 4))))
    assert report.counts["game_deleted"] == 1
    lamb = prop(db, "lamb-rec")
    assert (lamb.hr_season_hit, lamb.hr_season_total) == (2, 3)


def test_reseeding_does_not_clobber_real_hit_rates(loaded, db):
    sync_week(db, MockOddsProvider(), SAMPLE_SEASON, SAMPLE_WEEK)
    p = prop(db, "godwin-rec")
    assert p.hit_rate_source == "plan_data" and (p.hr_season_hit, p.hr_season_total) == (2, 4)


def test_cli_dry_run_rolls_back(tmp_path, db, seeded, monkeypatch, capsys):
    d = write_dataset(tmp_path / "cli")
    monkeypatch.setattr("sys.argv", ["app.cli", "load-nfl-data", "--dir", str(d), "--dry-run"])
    cli_main()
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "WR/TE 1+ catch" in out
    assert db.scalars(select(Player)).first() is None
    assert prop(db, "godwin-rec").hit_rate_source == "sample"


@pytest.mark.skipif(not (REAL_DIR / "_meta.json").is_file(), reason="Plan Data directory not present")
def test_real_plan_data_reconciles_and_sanity_number():
    ds = read_dataset(REAL_DIR)
    assert not any(ds.validation.values()), ds.validation
    assert len({r["team"] for r in ds.players}) == 32  # KC + CAR fixed
    assert sum(r["team"] in ("KC", "CAR") for r in ds.players) == 158
    assert ds.prev_season == 2025 and len(ds.prev_game_rows) == 6938
    assert ds.matchup_week == 5
    from app.nfl_data.loader import sanity_metrics

    s = sanity_metrics(ds)["wr_te_1plus_catch"]
    assert abs(s["rate"] - 0.662) < 0.001, s  # was 0.734 before Plan Data's zero-row fix


# --------------------------------------------------------------------------- vsOppLastSeason / matchups


def test_vs_opp_last_season_regular_season_only_zero_rows_miss(loaded, db):
    p = prop(db, "godwin-rec")  # 2025 vs DAL: REG 6 (hit), REG zero row (miss); POST 9 excluded
    assert (p.hr_vs_opp_last_hit, p.hr_vs_opp_last_total, p.hr_vs_opp_last_pct) == (1, 2, 50)
    assert (p.hr_vs_opp_last_label, p.hr_vs_opp_last_season) == ("vs DAL '25", 2025)
    # Prior season never leaks into the 2026 windows.
    assert (p.hr_season_hit, p.hr_season_total, p.hr_last5_total) == (2, 4, 4)


def test_vs_opp_last_season_empty_is_honest(loaded, client):
    board = client.get("/api/v1/games/tb-dal").json()
    lamb = next(x for x in board["props"] if x["id"] == "lamb-rec")  # no 2025 games vs TB
    assert lamb["hitRate"]["vsOppLastSeason"] == {"pct": None, "hit": 0, "total": 0, "label": "vs TB '25",
                                                  "season": 2025, "opponent": "TB"}  # fmt: skip
    assert lamb["hitRate"]["vsOpp"]["season"] == 2026 and lamb["hitRate"]["vsOpp"]["opponent"] == "TB"


def test_matchup_bye_falls_back_to_sample_game_opponent(loaded, db):
    k = prop(db, "kelce-rec")
    assert (k.hr_vs_opp_opponent, k.hr_opponent_source) == ("BUF", "sample_game")
    assert (k.hr_vs_opp_last_hit, k.hr_vs_opp_last_total) == (1, 1)
    assert any(g["gap"] == "bye_in_real_schedule" and g["prop"] == "kc-buf/kelce-rec" for g in loaded.gaps)


def test_matchup_opponent_overrides_sample_game(tmp_path, db, seeded):
    m = dict(MATCHUPS) | {"00-0000001": ("TB", "NO", "2026_05_TB_NO")}
    report = load_dataset(db, read_dataset(write_dataset(tmp_path / "d", matchups=m)))
    p = prop(db, "godwin-rec")
    assert (p.hr_vs_opp_opponent, p.hr_opponent_source, p.hr_vs_opp_label) == ("NO", "matchups", "vs NO")
    assert (p.hr_vs_opp_last_hit, p.hr_vs_opp_last_total, p.hr_vs_opp_last_label) == (1, 1, "vs NO '25")
    assert p.data_status == "ok"  # still on TB -> stays on the tb-dal card
    gap = next(g for g in report.gaps if g["prop"] == "tb-dal/godwin-rec")
    assert gap["gap"] == "real_opponent_differs" and gap["sample_opponent"] == "DAL"


def test_matchup_team_decides_hidden(tmp_path, db, seeded):
    # Matchups say Evans is back on TB this week -> the prop is restored on tb-dal.
    m = dict(MATCHUPS) | {"00-0000003": ("TB", "DAL", "2026_05_TB_DAL")}
    load_dataset(db, read_dataset(write_dataset(tmp_path / "d")))
    assert prop(db, "evans-rec").is_open is False
    load_dataset(db, read_dataset(write_dataset(tmp_path / "d", matchups=m)))
    e = prop(db, "evans-rec")
    assert (e.data_status, e.is_open, e.team_abbr, e.hr_vs_opp_opponent) == ("ok", True, "TB", "DAL")


def test_prev_season_rows_matchups_and_history_players_stored(loaded, db):
    rows = db.scalars(select(PlayerGameStat).where(PlayerGameStat.season == 2025)).all()
    assert len(rows) == 7 and {r.season_type for r in rows} == {"REG", "POST"}
    assert {r.game_type for r in rows if r.season_type == "POST"} == {"WC"}
    old = db.scalars(select(Player).where(Player.gsis_id == "00-0000099")).one()
    assert (old.roster_source, old.team_abbr) == ("history", None)
    assert (
        db.scalars(select(PlayerGameStat).where(PlayerGameStat.player_id == old.id)).one().opponent_abbr
        == "SF"
    )
    mus = db.scalars(select(PlayerMatchup)).all()
    assert len(mus) == 4 and sum(m.is_bye for m in mus) == 1
    assert loaded.counts["matchup_skipped_no_player"] == 1
    load = db.scalars(select(NflDataLoad)).one()
    assert "player_vs_opponent_2025.csv" in load.file_hashes and "week5_2026_matchups.csv" in load.file_hashes
    assert load.counts["prev_game_rows"] == 7


def test_sample_props_expose_vs_opp_season_and_opponent(client, seeded):
    p = next(x for x in client.get("/api/v1/games/tb-dal").json()["props"] if x["id"] == "godwin-rec")
    assert p["hitRateSource"] == "sample"
    assert p["hitRate"]["vsOpp"]["opponent"] == "DAL" and p["hitRate"]["vsOpp"]["season"] == 2026
    assert p["hitRate"]["vsOppLastSeason"] is None
