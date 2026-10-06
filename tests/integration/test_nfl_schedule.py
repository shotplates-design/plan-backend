"""Real week-5 schedule import: games + real lines, retired sample slate, props moved to real games."""

import csv
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Bet, Game, Prop, WalletTransaction
from app.models.enums import BetStatus, GameStatus, WalletTxType
from app.nfl_data.files import DataError, read_dataset
from app.nfl_data.loader import load_dataset
from app.odds_provider.mock import SAMPLE_SEASON, SAMPLE_WEEK, MockOddsProvider
from app.services.odds_sync import sync_week
from tests.integration.test_nfl_data_loader import MATCHUPS, REAL_DIR, prop, write_dataset

ET = ZoneInfo("America/New_York")
SCHED_COLS = ["game_id", "season", "game_type", "week", "gameday", "weekday", "gametime", "away_team", "away_score",
              "home_team", "home_score", "away_moneyline", "home_moneyline", "spread_line", "away_spread_odds",
              "home_spread_odds", "total_line", "under_odds", "over_odds", "stadium"]  # fmt: skip
GAMEDAY = (datetime.now(ET) + timedelta(days=3)).date().isoformat()


def _row(gid, away, home, gametime="13:00", lines=None, week=5, season=2026, **kw):
    r = {"game_id": gid, "season": season, "game_type": "REG", "week": week, "gameday": GAMEDAY,
         "gametime": gametime, "away_team": away, "home_team": home, "stadium": f"{home} Stadium"}  # fmt: skip
    if lines:
        aml, hml, spread, aso, hso, total, oo, uo = lines
        r |= {"away_moneyline": aml, "home_moneyline": hml, "spread_line": spread, "away_spread_odds": aso,
              "home_spread_odds": hso, "total_line": total, "over_odds": oo, "under_odds": uo}  # fmt: skip
    return r | kw


SCHEDULE = [
    _row("2026_05_TB_DAL", "TB", "DAL", "20:15", (350, -455, 8.5, -108, -112, 47.5, -115, -105)),
    _row("2026_05_SF_SEA", "SF", "SEA", "16:25", (130, -155, 2.5, 100, -120, 46.5, -115, -105)),
    _row("2026_05_BUF_LA", "BUF", "LA", "20:15", (130, -155, 2.5, 100, -120, 54.5, -115, -105)),
    _row("2026_05_LV_NE", "LV", "NE"),  # no lines in the file
    _row("2026_04_TB_GB", "TB", "GB", week=4),  # other week: ignored
    _row("2025_05_TB_DAL", "TB", "DAL", season=2025),  # other season: ignored
]


def write_schedule(d: Path, rows=SCHEDULE) -> None:
    with (d / "schedule_games_raw.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, SCHED_COLS)
        w.writeheader()
        w.writerows(rows)


def dataset(tmp_path, *, matchups=MATCHUPS, players_patch=None, rows=SCHEDULE) -> Path:
    d = write_dataset(tmp_path / "plan-nfl-data", matchups=matchups)
    write_schedule(d, rows)
    if players_patch:
        path = d / "players_active.csv"
        data = list(csv.DictReader(path.open()))
        cols = list(data[0].keys())
        for r in data:
            r.update(players_patch.get(r["player_id"], {}))
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, cols)
            w.writeheader()
            w.writerows(data)
    return d


def board(client):
    return {g["id"]: g for g in client.get("/api/v1/games").json()}


def place(client, headers, keys, stake=1000, idem=None):
    games = board(client)
    legs = []
    for key in keys:
        gid, kind, side = key.split("|")
        g = games[gid]
        price = g["money"][side] if kind == "money" else g["spread"][side]["price"]
        legs.append({"selection_key": key, "odds_american": price})
    r = client.post(
        "/api/v1/bets",
        headers=headers,
        json={"type": "single" if len(legs) == 1 else "parlay", "stake_cents": stake, "legs": legs,
              "idempotency_key": idem},
    )  # fmt: skip
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def loaded(tmp_path, db, seeded):
    return load_dataset(db, read_dataset(dataset(tmp_path)))


def test_real_games_replace_sample_board(loaded, client, db):
    games = board(client)
    assert set(games) == {"tb-dal", "sf-sea", "buf-lar", "lv-ne"}  # sample-only games gone
    assert loaded.counts["schedule_games_created"] == 2 and loaded.counts["schedule_games_adopted"] == 2
    assert loaded.counts["sample_games_archived"] == 12
    assert any("team code LA normalised to LAR" in w for w in loaded.warnings)

    bl = games["buf-lar"]
    assert bl["nflGameId"] == "2026_05_BUF_LA" and bl["home"]["abbr"] == "LAR" and bl["source"] == "plan_data"
    expected = datetime.fromisoformat(f"{GAMEDAY} 20:15").replace(tzinfo=ET)
    assert datetime.fromisoformat(bl["kickoff"]) == expected  # ET wall clock → stored aware, ISO out
    g = db.scalars(select(Game).where(Game.external_id == "buf-lar")).one()
    assert g.kickoff_at.astimezone(UTC) == expected.astimezone(UTC)


def test_real_game_lines_and_source_flags(loaded, client):
    tb = board(client)["tb-dal"]
    assert tb["linesSource"] == "nflverse_schedule" and tb["isSampleLines"] is False
    assert tb["spread"] == {"away": {"line": "+8.5", "price": -108}, "home": {"line": "-8.5", "price": -112}}
    assert tb["money"] == {"away": 350, "home": -455}
    assert tb["total"] == {"line": 47.5, "over": -115, "under": -105}
    ne = board(client)["lv-ne"]  # no lines in the file → no markets, flagged
    assert ne["spread"] is None and ne["money"] is None and ne["total"] is None
    assert ne["linesSource"] == "none" and ne["isSampleLines"] is True


def test_adopted_game_keeps_uuid_and_slug(tmp_path, db, seeded):
    before = db.scalars(select(Game.id).where(Game.external_id == "tb-dal")).one()
    load_dataset(db, read_dataset(dataset(tmp_path)))
    g = db.scalars(select(Game).where(Game.external_id == "tb-dal")).one()
    assert g.id == before and g.nfl_game_id == "2026_05_TB_DAL" and g.source == "plan_data"


def test_sample_games_archived_hidden_and_closed(loaded, client, db, auth_headers):
    assert client.get("/api/v1/games/kc-buf").status_code == 404
    r = client.post(
        "/api/v1/bets/quote",
        headers=auth_headers,
        json={"type": "single", "stake_cents": 1000, "legs": [{"selection_key": "kc-buf|money|home", "odds_american": -140}]},
    )  # fmt: skip
    assert r.status_code == 404
    kc = db.scalars(select(Game).where(Game.external_id == "kc-buf")).one()
    assert kc.archived_at is not None and kc.status == GameStatus.CANCELLED
    assert kc.archive_reason == "sample_slate_retired"
    assert all(not s.is_open for m in kc.markets for s in m.selections)


def test_pending_bets_on_retired_games_are_voided_and_refunded(tmp_path, db, seeded, client, auth_headers):
    on_sample = place(client, auth_headers, ["kc-buf|money|home"], stake=1000)
    on_real = place(client, auth_headers, ["tb-dal|money|home"], stake=2000)
    parlay = place(client, auth_headers, ["kc-buf|spread|away", "tb-dal|spread|away"], stake=500)
    assert client.get("/api/v1/wallet", headers=auth_headers).json()["balance_cents"] == 100_000 - 3500

    report = load_dataset(db, read_dataset(dataset(tmp_path)))
    assert report.counts["sample_bets_voided"] == 1 and report.counts["sample_bets_refunded_cents"] == 1000
    db.expire_all()
    status = {b.id: b for b in db.scalars(select(Bet))}
    s, r, p = (status[uuid.UUID(x)] for x in (on_sample, on_real, parlay))
    assert (s.status, s.payout_cents) == (BetStatus.VOID, 1000)
    assert r.status == BetStatus.PENDING  # tb-dal is real: the bet stands at the odds it was placed at
    assert p.status == BetStatus.PENDING  # kc-buf leg void, tb-dal leg still pending
    assert sorted(leg.result.value for leg in p.legs) == ["pending", "void"]
    refunds = db.scalars(
        select(WalletTransaction).where(WalletTransaction.type == WalletTxType.BET_REFUND)
    ).all()
    assert [t.amount_cents for t in refunds] == [1000]
    assert client.get("/api/v1/wallet", headers=auth_headers).json()["balance_cents"] == 100_000 - 2500


def test_props_move_to_their_players_real_game(loaded, client, db, auth_headers):
    moves = {m["prop"]: (m["from"], m["to"]) for m in loaded.moves}
    assert moves["evans-rec"] == ("tb-dal", "sf-sea") and moves["evans-yds"] == ("tb-dal", "sf-sea")
    sea = board(client)["sf-sea"]
    evans = next(p for p in sea["props"] if p["id"] == "evans-rec")
    assert evans["team"] == "SF" and evans["selectionKey"] == "sf-sea|prop|evans-rec"
    assert evans["hitRate"]["vsOpp"]["opponent"] == "SEA" and evans["hitRate"]["vsOpp"]["season"] == 2026
    assert evans["hitRate"]["vsOppLastSeason"] == {"pct": 0, "hit": 0, "total": 1, "label": "vs SEA '25",
                                                   "season": 2025, "opponent": "SEA"}  # fmt: skip
    assert client.get("/api/v1/games/sf-sea/props/evans-rec", headers=auth_headers).status_code == 200
    # Old key on the old card no longer resolves (documented id change).
    assert client.get("/api/v1/games/tb-dal/props/evans-rec", headers=auth_headers).status_code == 404
    e = prop(db, "evans-rec")
    assert (e.data_status, e.is_open) == ("ok", True)


def test_card_opponent_always_matches_vs_opp(loaded, client):
    assert loaded.validation["card_opponent_mismatches"] == 0
    for g in board(client).values():
        for p in g["props"]:
            if p["hitRateSource"] != "plan_data":
                continue
            other = g["home"]["abbr"] if p["team"] == g["away"]["abbr"] else g["away"]["abbr"]
            assert p["team"] in (g["home"]["abbr"], g["away"]["abbr"])
            assert p["hitRate"]["vsOpp"]["opponent"] == other == p["hitRate"]["vsOppLastSeason"]["opponent"]


def test_bye_player_props_hidden_not_on_fake_game(loaded, client, db):
    k = prop(db, "kelce-rec")
    assert (k.data_status, k.is_open) == ("player_on_bye", False)
    assert k.hr_vs_opp_opponent is None and k.hr_vs_opp_last_total is None
    assert all(p["id"] != "kelce-rec" for g in board(client).values() for p in g["props"])
    assert any(g["gap"] == "player_on_bye" for g in loaded.gaps if "gap" in g)


def test_inactive_roster_status_hides_prop_and_reopens(tmp_path, db, seeded, client):
    ir = {"00-0000002": {"status": "Reserve/IR", "status_code": "RES"}}
    report = load_dataset(db, read_dataset(dataset(tmp_path, players_patch=ir)))
    lamb = prop(db, "lamb-rec")
    assert (lamb.data_status, lamb.is_open) == ("player_inactive", False)
    assert "lamb-rec" not in {p["id"] for p in board(client)["tb-dal"]["props"]}
    assert any(g.get("gap") == "player_inactive" and g.get("status") == "Reserve/IR" for g in report.gaps)
    load_dataset(db, read_dataset(dataset(tmp_path)))  # activated again
    lamb = prop(db, "lamb-rec")
    assert (lamb.data_status, lamb.is_open) == ("ok", True)


def test_rerun_and_reseed_are_noops(tmp_path, db, seeded, loaded):
    again = load_dataset(db, read_dataset(tmp_path / "plan-nfl-data"))
    for k in ("schedule_games_created", "schedule_games_adopted", "schedule_games_updated",
              "schedule_markets_created", "schedule_markets_updated", "sample_games_archived"):  # fmt: skip
        assert again.counts[k] == 0, k
    assert again.moves == []
    res = sync_week(db, MockOddsProvider(), SAMPLE_SEASON, SAMPLE_WEEK)
    assert res.skipped_games == 14  # 12 archived + 2 real: never resurrected / overwritten
    db.expire_all()
    assert db.scalars(select(Game).where(Game.archived_at.is_(None))).all().__len__() == 4
    assert len(db.scalars(select(Prop).where(Prop.external_id == "evans-rec")).all()) == 1
    tb = db.scalars(select(Game).where(Game.external_id == "tb-dal")).one()
    assert tb.lines_source == "nflverse_schedule"
    third = load_dataset(db, read_dataset(tmp_path / "plan-nfl-data"))
    assert third.moves == [] and third.counts["schedule_markets_updated"] == 0


def test_dry_run_rolls_back_schedule(tmp_path, db, seeded):
    load_dataset(db, read_dataset(dataset(tmp_path)), dry_run=True)
    db.expire_all()
    assert db.scalars(select(Game).where(Game.archived_at.is_not(None))).first() is None
    assert db.scalars(select(Game).where(Game.nfl_game_id.is_not(None))).first() is None
    assert prop(db, "evans-rec").game.external_id == "tb-dal"


def test_matchup_not_in_schedule_fails_strict(tmp_path, db, seeded):
    m = dict(MATCHUPS) | {"00-0000001": ("TB", "NO", "2026_05_TB_NO")}
    ds = read_dataset(dataset(tmp_path, matchups=m))
    assert ds.validation["schedule_matchup_mismatches"] == 1
    with pytest.raises(DataError, match="validation failed"):
        load_dataset(db, ds, strict=True)
    assert db.scalars(select(Game).where(Game.nfl_game_id.is_not(None))).first() is None


def test_bye_team_scheduled_is_a_mismatch(tmp_path):
    rows = [*SCHEDULE, _row("2026_05_KC_JAX", "KC", "JAX")]
    ds = read_dataset(dataset(tmp_path, rows=rows))
    assert ds.validation["schedule_bye_mismatches"] == 1


@pytest.mark.skipif(
    not (REAL_DIR / "schedule_games_raw.csv").is_file(), reason="Plan Data directory not present"
)
def test_real_week5_schedule_reconciles():
    ds = read_dataset(REAL_DIR)
    assert ds.schedule_week == 5 and len(ds.schedule_rows) == 15
    teams = {r[k] for r in ds.schedule_rows for k in ("away_team", "home_team")}
    assert len(teams) == 30 and not teams & {"KC", "CAR"}
    assert ds.validation["schedule_matchup_mismatches"] == 0 and ds.validation["schedule_bye_mismatches"] == 0
