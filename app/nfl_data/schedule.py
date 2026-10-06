"""Real schedule import (schedule_games_raw.csv → games/markets) and retiring the sample slate.

- Games are matched by `nfl_game_id` (Plan Data / nflverse `game_id`, e.g. "2026_05_BUF_LA").
- Public ids stay readable: `{away}-{home}` with OUR team abbrs (LA → LAR, so "buf-lar"). A sample
  game with the same slug, season, week and teams is ADOPTED (same row/UUID, so bets, unlocks and
  props on it survive). If the slug is taken by an unrelated game, `-{season}-w{week:02d}` is added.
- Game lines (moneyline / spread / total + prices) come from the schedule file when present and are
  marked `source=nflverse_schedule`. Missing lines leave existing (sample) markets as they are.
- Sample games in that week that aren't real are ARCHIVED, not deleted: hidden from /games, status
  cancelled, markets settled void; pending bets on them are voided with a wallet refund through the
  normal settlement path (settle_game). Rows are kept so bet history still resolves.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Bet, BetLeg, Game, Market, Selection, Team
from app.models.enums import BetStatus, GameStatus, MarketType, SeasonType, SelectionSide
from app.nfl_data.files import DataSet, team, to_float, to_opt_int

if TYPE_CHECKING:
    from app.nfl_data.loader import LoadReport

ET = ZoneInfo("America/New_York")
LINES_SOURCE = "nflverse_schedule"
ARCHIVE_REASON = "sample_slate_retired"


def slug(away: str, home: str) -> str:
    return f"{away.lower()}-{home.lower()}"


def kickoff_utc(gameday: str, gametime: str) -> datetime:
    """Schedule times are America/New_York wall-clock → aware UTC."""
    local = datetime.strptime(f"{gameday} {gametime or '13:00'}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
    return local.astimezone(UTC)


def _odds(v: str | None) -> int | None:
    n = to_opt_int(v)
    return n if n is not None and (n >= 100 or n <= -100) else None


def _lines(r: dict[str, str]) -> dict[MarketType, list[tuple[SelectionSide, Decimal | None, int]]]:
    """nflverse: spread_line > 0 means the HOME team is favoured by that many points."""
    out: dict[MarketType, list[tuple[SelectionSide, Decimal | None, int]]] = {}
    aml, hml = _odds(r.get("away_moneyline")), _odds(r.get("home_moneyline"))
    if aml is not None and hml is not None:
        out[MarketType.MONEYLINE] = [(SelectionSide.AWAY, None, aml), (SelectionSide.HOME, None, hml)]
    sl = to_float(r.get("spread_line"))
    aso, hso = _odds(r.get("away_spread_odds")), _odds(r.get("home_spread_odds"))
    if sl is not None and aso is not None and hso is not None:
        d = Decimal(str(sl))
        out[MarketType.SPREAD] = [(SelectionSide.AWAY, d, aso), (SelectionSide.HOME, -d, hso)]
    tl = to_float(r.get("total_line"))
    oo, uo = _odds(r.get("over_odds")), _odds(r.get("under_odds"))
    if tl is not None and oo is not None and uo is not None:
        t = Decimal(str(tl))
        out[MarketType.TOTAL] = [(SelectionSide.OVER, t, oo), (SelectionSide.UNDER, t, uo)]
    return out


def _sel_name(side: SelectionSide, home: Team, away: Team) -> str:
    if side == SelectionSide.HOME:
        return f"{home.abbr} {home.name}"
    if side == SelectionSide.AWAY:
        return f"{away.abbr} {away.name}"
    return side.value.capitalize()


def _set(obj: Any, values: dict[str, Any]) -> bool:
    changed = False
    for k, v in values.items():
        if getattr(obj, k) != v:
            setattr(obj, k, v)
            changed = True
    return changed


def _ensure_teams(db: Session) -> dict[str, Team]:
    teams = {t.abbr: t for t in db.scalars(select(Team))}
    if len(teams) < 32:
        from app.odds_provider.mock import MockOddsProvider
        from app.services.odds_sync import sync_teams

        teams = sync_teams(db, MockOddsProvider())
    return teams


def import_schedule(db: Session, ds: DataSet, report: LoadReport) -> dict[str, Game]:
    """Upsert the real games for ds.schedule_week. Returns nfl_game_id → Game."""
    if not ds.schedule_rows or ds.schedule_week is None:
        return {}
    teams = _ensure_teams(db)
    season, week = ds.season, ds.schedule_week
    by_nfl = {g.nfl_game_id: g for g in db.scalars(select(Game).where(Game.nfl_game_id.is_not(None)))}
    by_slug = {g.external_id: g for g in db.scalars(select(Game))}
    out: dict[str, Game] = {}
    c = dict.fromkeys(
        ("created", "adopted", "updated", "markets_created", "markets_updated", "unknown_teams"), 0
    )
    code_map: dict[str, str] = {}
    for r in ds.schedule_rows:
        a_code, h_code = r["away_team"].strip().upper(), r["home_team"].strip().upper()
        away_abbr, home_abbr = team(a_code), team(h_code)
        for raw, ours in ((a_code, away_abbr), (h_code, home_abbr)):
            if raw != ours:
                code_map[raw] = str(ours)
        away, home = teams.get(away_abbr or ""), teams.get(home_abbr or "")
        if away is None or home is None:
            c["unknown_teams"] += 1
            report.warnings.append(f"schedule {r['game_id']}: unknown team {a_code}/{h_code}; skipped")
            continue
        nfl_id = r["game_id"].strip()
        game = by_nfl.get(nfl_id)
        is_new = adopted = False
        new_slug = slug(away.abbr, home.abbr)
        if game is None:
            twin = by_slug.get(new_slug)
            if (
                twin is not None
                and twin.nfl_game_id is None
                and (twin.season, twin.week, twin.home_team_id, twin.away_team_id)
                == (season, week, home.id, away.id)
            ):
                game = twin  # adopt the sample game: same UUID, slug, props, bets
                adopted = True
                c["adopted"] += 1
                report.gaps.append(
                    {"game": new_slug, "note": "sample game adopted as real", "nfl_game_id": nfl_id}
                )
            else:
                if twin is not None:
                    new_slug = f"{new_slug}-{season}-w{week:02d}"
                    report.warnings.append(f"slug {slug(away.abbr, home.abbr)} taken; using {new_slug}")
                game = Game(external_id=new_slug, season=season, week=week, status=GameStatus.SCHEDULED)
                db.add(game)
                by_slug[new_slug] = game
                is_new = True
                c["created"] += 1
        stype = SeasonType.REG if r["game_type"].strip().upper() == "REG" else SeasonType.POST
        values: dict[str, Any] = {
            "nfl_game_id": nfl_id,
            "season": season,
            "week": week,
            "season_type": stype,
            "home_team_id": home.id,
            "away_team_id": away.id,
            "kickoff_at": kickoff_utc(r["gameday"], r["gametime"]),
            "venue": r.get("stadium") or None,
            "source": "plan_data",
            "archived_at": None,
            "archive_reason": None,
        }
        hs, as_ = to_opt_int(r.get("home_score")), to_opt_int(r.get("away_score"))
        if hs is not None and as_ is not None:
            values.update(status=GameStatus.FINAL, home_score=hs, away_score=as_)
        # Compare aware datetimes in UTC (the DB returns them in the session zone).
        if game.kickoff_at is not None and game.kickoff_at.astimezone(UTC) == values["kickoff_at"]:
            values.pop("kickoff_at")
        if _set(game, values) and not (is_new or adopted):
            c["updated"] += 1
        db.flush()
        game.home_team, game.away_team = home, away
        out[nfl_id] = game

        # --- game lines
        existing = {
            (m.type, m.period): m for m in db.scalars(select(Market).where(Market.game_id == game.id))
        }
        lines = _lines(r)
        for mtype, sels in lines.items():
            m = existing.get((mtype, "full_game"))
            if m is None:
                m = Market(game_id=game.id, type=mtype, period="full_game", source=LINES_SOURCE)
                db.add(m)
                db.flush()
                c["markets_created"] += 1
                cur = {}
            else:
                cur = {s.side: s for s in db.scalars(select(Selection).where(Selection.market_id == m.id))}
            changed = _set(m, {"source": LINES_SOURCE})
            for side, line, odds in sels:
                sel = cur.get(side)
                if sel is None:
                    db.add(
                        Selection(
                            market_id=m.id,
                            side=side,
                            name=_sel_name(side, home, away),
                            line=line,
                            odds_american=odds,
                        )
                    )
                    changed = True
                else:
                    changed |= _set(
                        sel, {"name": _sel_name(side, home, away), "line": line, "odds_american": odds}
                    )
            if changed and (mtype, "full_game") in existing:
                c["markets_updated"] += 1
        sources = {m.source for m in db.scalars(select(Market).where(Market.game_id == game.id))}
        if not sources:
            lines_source = "none"
        elif sources == {LINES_SOURCE}:
            lines_source = LINES_SOURCE
        else:
            lines_source = "mixed" if LINES_SOURCE in sources else "sample"
        if game.lines_source != lines_source:
            game.lines_source = lines_source
        if lines_source != LINES_SOURCE:
            report.warnings.append(
                f"{game.external_id}: game lines are {lines_source or 'missing'} (schedule had none)"
            )
    db.flush()
    report.counts.update(
        schedule_week=week,
        schedule_games=len(out),
        schedule_games_created=c["created"],
        schedule_games_adopted=c["adopted"],
        schedule_games_updated=c["updated"],
        schedule_markets_created=c["markets_created"],
        schedule_markets_updated=c["markets_updated"],
        schedule_games_real_lines=sum(1 for g in out.values() if g.lines_source == LINES_SOURCE),
    )
    report.validation["schedule_unknown_teams"] = c["unknown_teams"]
    for raw, ours in sorted(code_map.items()):
        report.warnings.append(f"team code {raw} normalised to {ours}")
    return out


def retire_sample_games(db: Session, season: int, week: int, report: LoadReport) -> None:
    """Archive sample games in `season`/`week` that aren't real; void + refund pending bets on them."""
    from app.services.betting import settle_game

    games = db.scalars(
        select(Game)
        .where(Game.season == season, Game.week == week, Game.source == "sample", Game.archived_at.is_(None))
        .options(selectinload(Game.markets).selectinload(Market.selections), selectinload(Game.props))
    ).all()
    retired, bets_voided, refunded, props_closed = [], 0, 0, 0
    now = datetime.now(UTC)
    for g in games:
        sel_ids = [s.id for m in g.markets for s in m.selections]
        prop_ids = [p.id for p in g.props]
        pending = (
            db.scalars(
                select(Bet)
                .join(BetLeg)
                .where(Bet.status == BetStatus.PENDING)
                .where(BetLeg.selection_id.in_(sel_ids) | BetLeg.prop_id.in_(prop_ids))
            )
            .unique()
            .all()
        )
        stakes = {b.id: b.stake_cents for b in pending}
        props_closed += sum(1 for p in g.props if p.is_open)
        g.status = GameStatus.CANCELLED
        g.archived_at, g.archive_reason = now, ARCHIVE_REASON
        settle_game(db, g, commit=False)  # void selections/props, void legs, refund fully-void bets
        for b in pending:
            db.refresh(b)
            if b.status == BetStatus.VOID:
                bets_voided += 1
                refunded += stakes[b.id]
        retired.append(g.external_id)
        report.gaps.append(
            {"game": g.external_id, "note": "sample game archived (not on the real schedule)",
             "pending_bets_voided": len(pending), "props_left_closed": [p.external_id for p in g.props]}
        )  # fmt: skip
    db.flush()
    report.counts.update(
        sample_games_archived=len(retired),
        sample_bets_voided=bets_voided,
        sample_bets_refunded_cents=refunded,
        sample_props_closed_on_archive=props_closed,
    )
