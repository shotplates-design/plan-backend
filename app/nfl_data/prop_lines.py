"""Load Plan Data's Odds API prop-lines snapshot into props + prop_books.

Primary source (week 5): `/workspace/plan-nfl-data/odds/prop_lines_week5_2026.json`
Spec: plan-nfl-data/odds/README.md

isSample = false only when player_id is non-null AND at least one non-sample book quote is present.
Missing books stay null — never copied from the other book.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Game, Player, PlayerGameStat, PlayerSeasonStat, Prop, PropBook
from app.models.enums import SelectionSide
from app.nfl_data.files import DataSet, name_key, team
from app.nfl_data.hit_rates import GameLog, compute_hit_rates, compute_vs_opp_season, season_label
from app.nfl_data.markets import BOOKS, BOARD_STAT_KEYS, MARKET_ID_SUFFIX, ODDS_TO_BOARD

SOURCE = "plan_data_odds"
DEFAULT_ODDS_GLOB = "prop_lines_week*_*.json"


def _parse_fetched_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _player_slug(name: str) -> str:
    parts = re.sub(r"[^a-z0-9\s]", "", (name or "").lower()).split()
    return parts[-1] if parts else "player"


def _external_id(name: str, market_key: str, used: set[str]) -> str:
    base = f"{_player_slug(name)}-{MARKET_ID_SUFFIX.get(market_key, market_key)}"
    if base not in used:
        return base
    n = 2
    while f"{base}{n}" in used:
        n += 1
    return f"{base}{n}"


def _line_label(side: str, line: Decimal | None) -> str:
    if line is None:
        return "Yes" if side in ("yes", "over") else "No"
    word = {"over": "Over", "under": "Under"}.get(side, side.capitalize())
    text = format(line.normalize(), "f")
    if "." not in text:
        text = f"{text}.0"
    return f"{word} {text}"


def _stat_value(g: PlayerGameStat, board_market: str) -> float:
    if board_market == "Anytime TD":
        return float((g.receiving_tds or 0) + (g.rushing_tds or 0) + (g.passing_tds or 0))
    key = BOARD_STAT_KEYS.get(board_market)
    return float(getattr(g, key, 0) or 0) if key else 0.0


def _hit_rate_blob(
    *,
    logs: list[PlayerGameStat],
    prev_logs: list[PlayerGameStat],
    games_played: int,
    line: Decimal | None,
    side: SelectionSide,
    board_market: str,
    opponent: str | None,
    season: int,
    through_week: int,
) -> dict[str, Any] | None:
    """Compute HitRateOut-shaped dict for one line. anytime_td uses total TDs vs 0.5."""
    if board_market == "Anytime TD":
        use_line: Decimal | float = Decimal("0.5")
        use_side = SelectionSide.OVER  # Yes ≡ at least one TD
    elif line is None:
        return None
    else:
        use_line, use_side = line, side

    played = [g for g in logs if g.played and g.week <= through_week]
    # Leakage: caller already filtered weeks < game.week into `logs`.
    hr = compute_hit_rates(
        [GameLog(g.season, g.week, g.opponent_abbr, _stat_value(g, board_market), g.played) for g in played],
        games_played=games_played if games_played > 0 else len(played),
        line=use_line,
        side=use_side,
        opponent=opponent,
    )
    out: dict[str, Any] = {
        "season": {"pct": hr.season.pct, "hit": hr.season.hit, "total": hr.season.total},
        "last5": {"pct": hr.last5.pct, "hit": hr.last5.hit, "total": hr.last5.total},
        "vsOpp": None,
        "vsOppLastSeason": None,
    }
    if hr.vs_opp is not None:
        out["vsOpp"] = {
            "pct": hr.vs_opp.pct,
            "hit": hr.vs_opp.hit,
            "total": hr.vs_opp.total,
            "label": hr.vs_opp_label or "",
            "season": season,
            "opponent": opponent,
        }
    if opponent and prev_logs:
        last = compute_vs_opp_season(
            [
                GameLog(
                    g.season,
                    g.week,
                    g.opponent_abbr,
                    _stat_value(g, board_market),
                    g.played,
                    g.season_type,
                )
                for g in prev_logs
            ],
            season=season - 1,
            opponent=opponent,
            line=use_line,
            side=use_side,
        )
        out["vsOppLastSeason"] = {
            "pct": last.pct,
            "hit": last.hit,
            "total": last.total,
            "label": season_label(opponent, season - 1),
            "season": season - 1,
            "opponent": opponent,
        }
    return out


def _apply_blob_to_prop(prop: Prop, blob: dict[str, Any] | None, season: int, through_week: int) -> None:
    if not blob:
        return
    s, l5 = blob.get("season") or {}, blob.get("last5") or {}
    prop.hr_season_hit, prop.hr_season_total, prop.hr_season_pct = s.get("hit"), s.get("total"), s.get("pct")
    prop.hr_last5_hit, prop.hr_last5_total, prop.hr_last5_pct = l5.get("hit"), l5.get("total"), l5.get("pct")
    vs = blob.get("vsOpp")
    if vs:
        prop.hr_vs_opp_hit, prop.hr_vs_opp_total, prop.hr_vs_opp_pct = vs.get("hit"), vs.get("total"), vs.get("pct")
        prop.hr_vs_opp_label, prop.hr_vs_opp_season = vs.get("label"), vs.get("season") or season
        prop.hr_vs_opp_opponent = vs.get("opponent")
    last = blob.get("vsOppLastSeason")
    if last:
        prop.hr_vs_opp_last_hit = last.get("hit")
        prop.hr_vs_opp_last_total = last.get("total")
        prop.hr_vs_opp_last_pct = last.get("pct")
        prop.hr_vs_opp_last_label = last.get("label")
        prop.hr_vs_opp_last_season = last.get("season")
    prop.hit_rate_season = season
    prop.hit_rate_through_week = through_week
    if prop.hit_rate_source in (None, "sample", "none"):
        prop.hit_rate_source = "plan_data"


def find_odds_files(directory: Path, season: int, week: int | None) -> list[Path]:
    """Prefer prop_lines_week{N}_{season}.json; else any prop_lines_week*_*.json."""
    odds_dir = directory / "odds"
    if not odds_dir.is_dir():
        return []
    if week is not None:
        exact = odds_dir / f"prop_lines_week{week}_{season}.json"
        if exact.is_file():
            return [exact]
    return sorted(odds_dir.glob("prop_lines_week*_*.json"))


def read_prop_lines(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    if not isinstance(data, list):
        raise ValueError(f"{path.name}: expected a JSON array")
    return data


def apply_prop_lines(
    db: Session,
    ds: DataSet,
    report: Any,
    *,
    odds_path: Path | None = None,
) -> None:
    """Upsert book lines from Plan Data's odds snapshot; create props when needed."""
    paths = [odds_path] if odds_path else find_odds_files(ds.directory, ds.season, ds.schedule_week or ds.matchup_week)
    if not paths:
        report.warnings.append("no odds/prop_lines_week*_*.json: props keep sample / dual-book seed lines")
        report.counts["odds_props_live"] = 0
        _ensure_sample_books(db, ds.season, report)
        return

    path = paths[-1]
    rows = read_prop_lines(path)
    report.counts["odds_file"] = str(path)
    report.counts["odds_rows"] = len(rows)

    games = {
        g.nfl_game_id: g
        for g in db.scalars(
            select(Game)
            .where(Game.season == ds.season, Game.archived_at.is_(None), Game.nfl_game_id.is_not(None))
            .options(selectinload(Game.home_team), selectinload(Game.away_team), selectinload(Game.props))
        )
    }
    # Also index by normalized slug in case nfl_game_id uses LA vs LAR
    games_by_norm = {}
    for gid, g in games.items():
        games_by_norm[gid] = g
        # 2026_05_BUF_LA ↔ our LAR home
        parts = gid.split("_")
        if len(parts) >= 4:
            a, h = team(parts[-2]), team(parts[-1])
            games_by_norm[f"{parts[0]}_{parts[1]}_{a}_{h}"] = g

    players_by_gsis = {p.gsis_id: p for p in db.scalars(select(Player)) if p.gsis_id}

    logs: dict[uuid.UUID, list[PlayerGameStat]] = defaultdict(list)
    for g in db.scalars(
        select(PlayerGameStat).where(PlayerGameStat.season == ds.season, PlayerGameStat.season_type == "REG")
    ):
        logs[g.player_id].append(g)
    prev_logs: dict[uuid.UUID, list[PlayerGameStat]] = defaultdict(list)
    for g in db.scalars(select(PlayerGameStat).where(PlayerGameStat.season == ds.season - 1)):
        prev_logs[g.player_id].append(g)
    season_stats = {
        s.player_id: s
        for s in db.scalars(
            select(PlayerSeasonStat).where(
                PlayerSeasonStat.season == ds.season, PlayerSeasonStat.season_type == "REG"
            )
        )
    }

    created = updated = books_upserted = live = skipped = 0
    for raw in rows:
        market_key = raw.get("market")
        board_market = ODDS_TO_BOARD.get(market_key or "")
        if board_market is None:
            skipped += 1
            continue
        books_in = raw.get("books") or {}
        fd, dk = books_in.get("fanduel"), books_in.get("draftkings")
        if not fd and not dk:
            skipped += 1
            continue
        gsis = (raw.get("player_id") or "").strip() or None
        if not gsis:
            skipped += 1  # DST / unmatched — stay sample / skip
            continue
        player = players_by_gsis.get(gsis)
        if player is None:
            skipped += 1
            continue
        gid = raw.get("game_id") or ""
        game = games.get(gid) or games_by_norm.get(gid)
        if game is None:
            # Try LA↔LAR swap on the home/away tokens
            parts = gid.split("_")
            if len(parts) >= 4:
                alt = f"{parts[0]}_{parts[1]}_{team(parts[-2])}_{team(parts[-1])}"
                game = games_by_norm.get(alt)
        if game is None or game.archived_at is not None:
            skipped += 1
            continue

        used_ids = {p.external_id for p in game.props}
        # Match existing prop: same player_gsis_id + market on this game
        prop = next(
            (
                p
                for p in game.props
                if p.player_gsis_id == gsis and p.market == board_market
            ),
            None,
        )
        if prop is None:
            # Also match by normalised name + market (sample props may lack player_gsis until recompute)
            nk = name_key(raw.get("player_name_odds") or player.full_name)
            prop = next(
                (
                    p
                    for p in game.props
                    if p.market == board_market and name_key(p.player) == nk
                ),
                None,
            )
        if prop is None:
            ext = _external_id(player.full_name, market_key, used_ids)
            # Prefer FD line for the canonical prop fields, else DK
            primary = fd or dk
            side = (primary.get("side") or "over").lower()
            line = (
                None
                if primary.get("line") is None
                else Decimal(str(primary["line"]))
            )
            prop = Prop(
                game_id=game.id,
                external_id=ext,
                player=player.full_name,
                team_abbr=player.team_abbr or team(raw.get("team")) or game.home_team.abbr,
                market=board_market,
                line_label=_line_label(side, line),
                line=line if line is not None else Decimal("0.5"),
                side=SelectionSide.OVER if side in ("over", "yes") else SelectionSide.UNDER,
                odds_american=int(primary["price"]),
                player_id=player.id,
                player_gsis_id=gsis,
                stat_key=BOARD_STAT_KEYS.get(board_market) or (
                    "anytime_td" if board_market == "Anytime TD" else None
                ),
                line_source=SOURCE,
                hit_rate_source="plan_data",
                data_status="ok",
                is_open=True,
            )
            db.add(prop)
            db.flush()
            game.props.append(prop)
            created += 1
        else:
            prop.player_id = player.id
            prop.player_gsis_id = gsis
            prop.team_abbr = player.team_abbr or prop.team_abbr
            prop.stat_key = BOARD_STAT_KEYS.get(board_market) or (
                "anytime_td" if board_market == "Anytime TD" else prop.stat_key
            )
            updated += 1

        fetched = _parse_fetched_at(raw.get("fetched_at"))
        existing_books = {b.book: b for b in (prop.books or [])}
        # Opponent = other team on the card
        game_teams = {game.home_team.abbr, game.away_team.abbr}
        real_team = prop.team_abbr
        opponent = (game_teams - {real_team}).pop() if real_team in game_teams else None

        played = [g for g in logs.get(player.id, []) if g.played and g.week < game.week]
        sstat = season_stats.get(player.id)
        excluded = sum(1 for g in logs.get(player.id, []) if g.played and g.week >= game.week)
        games_played = (sstat.games_played - excluded) if sstat else len(played)
        through = min(ds.through_week, game.week - 1)

        book_blobs: dict[str, dict[str, Any] | None] = {}
        for book_key, quote in (("fanduel", fd), ("draftkings", dk)):
            if not quote:
                # Remove stale book row if odds file says this book has no quote — do NOT copy.
                stale = existing_books.pop(book_key, None)
                if stale is not None:
                    db.delete(stale)
                continue
            side = (quote.get("side") or "over").lower()
            line = None if quote.get("line") is None else Decimal(str(quote["line"]))
            price = int(quote["price"])
            sel_side = SelectionSide.OVER if side in ("over", "yes") else SelectionSide.UNDER
            blob = _hit_rate_blob(
                logs=played,
                prev_logs=prev_logs.get(player.id, []),
                games_played=games_played,
                line=line,
                side=sel_side,
                board_market=board_market,
                opponent=opponent,
                season=ds.season,
                through_week=through,
            )
            book_blobs[book_key] = blob
            row = existing_books.get(book_key)
            if row is None:
                row = PropBook(prop_id=prop.id, book=book_key)
                db.add(row)
                if prop.books is not None:
                    prop.books.append(row)
            row.line, row.price, row.side = line, price, side
            row.hit_rate, row.source, row.fetched_at = blob, SOURCE, fetched
            books_upserted += 1

        # Canonical top-level fields: FanDuel preferred, else DraftKings (never invent).
        primary_key = "fanduel" if fd else "draftkings"
        primary = fd or dk
        side = (primary.get("side") or "over").lower()
        line = None if primary.get("line") is None else Decimal(str(primary["line"]))
        prop.line = line if line is not None else Decimal("0.5")
        prop.line_label = _line_label(side, line)
        prop.side = SelectionSide.OVER if side in ("over", "yes") else SelectionSide.UNDER
        prop.odds_american = int(primary["price"])
        prop.line_source = SOURCE
        prop.hr_opponent_source = "matchups" if opponent else prop.hr_opponent_source
        _apply_blob_to_prop(prop, book_blobs.get(primary_key), ds.season, through)
        live += 1

    db.flush()
    report.counts.update(
        odds_props_live=live,
        odds_props_created=created,
        odds_props_updated=updated,
        odds_books_upserted=books_upserted,
        odds_rows_skipped=skipped,
    )
    # Sample props with no odds match still need dual-book rows for the API shape.
    _ensure_sample_books(db, ds.season, report)


def _ensure_sample_books(db: Session, season: int, report: Any) -> None:
    """Props without any PropBook get sample dual-book rows (slightly different FD/DK prices).

    NEVER invent a second book when one live book exists — only fills completely empty props.
    """
    props = db.scalars(
        select(Prop)
        .join(Game, Prop.game_id == Game.id)
        .where(Game.season == season, Game.archived_at.is_(None))
        .options(selectinload(Prop.books))
    ).all()
    seeded = 0
    for prop in props:
        if prop.books:
            continue
        # FanDuel = canonical; DraftKings = same line, price nudged ±5 (demo divergence).
        fd_price = prop.odds_american
        dk_price = fd_price + 5 if fd_price < 0 else fd_price - 5
        if abs(dk_price) < 100:
            dk_price = -110 if dk_price <= 0 else 100
        side = "over" if prop.side == SelectionSide.OVER else "under"
        for book, price in (("fanduel", fd_price), ("draftkings", dk_price)):
            db.add(
                PropBook(
                    prop_id=prop.id,
                    book=book,
                    line=prop.line,
                    price=price,
                    side=side,
                    hit_rate=None,  # serializer falls back to prop chips
                    source="sample",
                )
            )
        seeded += 1
    db.flush()
    report.counts["odds_sample_books_seeded"] = seeded
