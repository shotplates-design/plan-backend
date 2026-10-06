"""Plan Data odds JSON → live dual-book props on the week-5 board."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Prop, PropBook
from app.nfl_data.files import read_dataset
from app.nfl_data.loader import load_dataset
from app.nfl_data.prop_lines import read_prop_lines
from tests.conftest import signup
from tests.integration.test_nfl_schedule import dataset as schedule_dataset

ODDS = Path("/workspace/plan-nfl-data/odds/prop_lines_week5_2026.json")


def _write_mini_odds(d: Path) -> Path:
    """Tiny odds file covering Godwin (both books) + a DK-only quote + unmatched DST."""
    odds_dir = d / "odds"
    odds_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "player_id": "00-0000001",
            "player_name_odds": "Chris Godwin Jr.",
            "team": "TB",
            "opponent": "DAL",
            "game_id": "2026_05_TB_DAL",
            "market": "receptions",
            "books": {
                "fanduel": {"line": 3.5, "price": 112, "side": "over"},
                "draftkings": {"line": 3.5, "price": -106, "side": "over"},
            },
            "fetched_at": "2026-10-06T23:15:23Z",
            "source": "the-odds-api",
        },
        {
            "player_id": "00-0000002",
            "player_name_odds": "CeeDee Lamb",
            "team": "DAL",
            "opponent": "TB",
            "game_id": "2026_05_TB_DAL",
            "market": "receptions",
            "books": {
                "fanduel": {"line": 5.5, "price": -166, "side": "over"},
                "draftkings": {"line": 6.5, "price": 115, "side": "over"},
            },
            "fetched_at": "2026-10-06T23:15:23Z",
            "source": "the-odds-api",
        },
        {
            "player_id": "00-0000002",
            "player_name_odds": "CeeDee Lamb",
            "team": "DAL",
            "opponent": "TB",
            "game_id": "2026_05_TB_DAL",
            "market": "receiving_yards",
            "books": {"fanduel": None, "draftkings": {"line": 80.5, "price": -110, "side": "over"}},
            "fetched_at": "2026-10-06T23:15:23Z",
            "source": "the-odds-api",
        },
        {
            "player_id": None,
            "player_name_odds": "TB Defense",
            "game_id": "2026_05_TB_DAL",
            "market": "anytime_td",
            "books": {"fanduel": {"line": None, "price": 650, "side": "yes"}, "draftkings": None},
            "source": "the-odds-api",
        },
    ]
    path = odds_dir / "prop_lines_week5_2026.json"
    path.write_text(json.dumps(rows))
    return path


@pytest.fixture
def loaded_odds(tmp_path, db, seeded):
    d = schedule_dataset(tmp_path)
    _write_mini_odds(d)
    return load_dataset(db, read_dataset(d))


def test_live_books_on_board(loaded_odds, client):
    body = signup(client, "odds@example.com")
    # force fanduel theme via DB would need prefs; signup default null → FD first
    from app.db import SessionLocal
    from app.models import User
    from app.models.enums import Theme

    with SessionLocal() as s:
        u = s.scalars(select(User).where(User.email == "odds@example.com")).one()
        u.theme = Theme.FANDUEL
        s.commit()
    h = {"Authorization": f"Bearer {body['access_token']}"}
    # re-login to refresh token claims? theme is read from DB on each request, not JWT
    board = client.get("/api/v1/games/tb-dal", headers=h).json()
    godwin = next(p for p in board["props"] if p["id"] == "godwin-rec")
    assert godwin["isSample"] is False and godwin["lineSource"] == "plan_data_odds"
    assert godwin["book"] == "fanduel" and godwin["price"] == 112
    assert godwin["books"]["fanduel"]["line"] == 3.5
    assert godwin["books"]["draftkings"]["line"] == 3.5
    assert godwin["books"]["fanduel"]["hitRate"]["season"]["total"] == 4
    lamb = next(p for p in board["props"] if p["id"] == "lamb-rec")
    assert lamb["books"]["fanduel"]["line"] == 5.5 and lamb["books"]["draftkings"]["line"] == 6.5
    # DK-only receiving yards — FD must stay null
    yds = next(p for p in board["props"] if p["id"] == "lamb-yds")
    assert yds["books"]["fanduel"] is None and yds["books"]["draftkings"]["line"] == 80.5
    assert yds["book"] == "draftkings" and yds["isSample"] is False
    assert loaded_odds.counts["odds_props_live"] >= 3
    # DST skipped
    assert not any(p["player"] == "TB Defense" for p in board["props"])


def test_theme_fallback_and_null_never_copied(loaded_odds, client, db):
    body = signup(client, "odds2@example.com")
    from app.models import User
    from app.models.enums import Theme

    u = db.scalars(select(User).where(User.email == "odds2@example.com")).one()
    u.theme = Theme.DRAFTKINGS
    db.commit()
    h = {"Authorization": f"Bearer {body['access_token']}"}
    lamb = next(
        p for p in client.get("/api/v1/games/tb-dal", headers=h).json()["props"] if p["id"] == "lamb-rec"
    )
    assert lamb["book"] == "draftkings" and lamb["price"] == 115 and "6.5" in lamb["line"]
    yds = next(
        p for p in client.get("/api/v1/games/tb-dal", headers=h).json()["props"] if p["id"] == "lamb-yds"
    )
    assert yds["books"]["fanduel"] is None  # never copied from DK


def test_idempotent_odds_reload(tmp_path, db, seeded, loaded_odds):
    before = db.scalars(select(PropBook)).all()
    n_books, n_props = len(before), len(db.scalars(select(Prop)).all())
    again = load_dataset(db, read_dataset(tmp_path / "plan-nfl-data"))
    assert again.counts["odds_props_created"] == 0
    assert len(db.scalars(select(PropBook)).all()) == n_books
    assert len(db.scalars(select(Prop)).all()) == n_props


@pytest.mark.skipif(not ODDS.is_file(), reason="Plan Data odds file not present")
def test_real_odds_file_loads_live_props():
    rows = read_prop_lines(ODDS)
    live = [
        r for r in rows if r.get("player_id") and (r["books"].get("fanduel") or r["books"].get("draftkings"))
    ]
    assert len(live) >= 300
    assert any(r["books"].get("fanduel") and not r["books"].get("draftkings") for r in live)
    assert any(r["books"].get("draftkings") and not r["books"].get("fanduel") for r in live)
