"""Idempotent loader: Plan Data CSVs → players / player_*_stats → prop hit rates.

Re-running with the same files changes nothing; a new week's files add/replace rows. For the
loaded season, the files are the source of truth: stat rows absent from the files are deleted.
Players are never deleted (props may reference them).
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, selectinload

from app.models import Game, NflDataLoad, Player, PlayerGameStat, PlayerMatchup, PlayerSeasonStat, Prop
from app.models.nfl_data import STAT_COLUMNS
from app.nfl_data.files import (
    DataError,
    DataSet,
    extra,
    name_key,
    team,
    to_bool,
    to_float,
    to_int,
    to_opt_int,
)
from app.nfl_data.hit_rates import (
    MARKET_STAT_KEYS,
    GameLog,
    compute_hit_rates,
    compute_vs_opp_season,
    pct,
    season_label,
)
from app.nfl_data.prop_lines import apply_prop_lines
from app.nfl_data.schedule import import_schedule, retire_sample_games
from app.services.serializers import HIDDEN_PROP_STATUSES as HIDDEN_STATUSES

log = logging.getLogger("plan.nfl_data")

SOURCE = "plan_data"
_POSITION_FOR_STAT = {
    "passing_yards": {"QB"},
    "attempts": {"QB"},
    "completions": {"QB"},
    "passing_tds": {"QB"},
}


@dataclass
class LoadReport:
    season: int
    through_week: int
    counts: dict[str, int] = field(default_factory=dict)
    sanity: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    gaps: list[dict[str, Any]] = field(default_factory=list)
    validation: dict[str, int] = field(default_factory=dict)
    moves: list[dict[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- upserts


def _apply(obj: Any, values: dict[str, Any]) -> bool:
    changed = False
    for k, v in values.items():
        cur = getattr(obj, k)
        if isinstance(cur, Decimal) and v is not None:
            v = Decimal(str(v))
        if cur != v:
            setattr(obj, k, v)
            changed = True
    return changed


def _stats(row: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {c: to_int(row[c]) for c in STAT_COLUMNS}
    out["fantasy_points_ppr"] = Decimal(str(to_float(row.get("fantasy_points_ppr")) or 0)).quantize(
        Decimal("0.01")
    )
    out["extra"] = extra(row)
    return out


def upsert_players(db: Session, ds: DataSet, report: LoadReport) -> dict[str, Player]:
    existing = {p.gsis_id: p for p in db.scalars(select(Player))}
    created = updated = 0

    def upsert(gsis: str, values: dict[str, Any]) -> None:
        nonlocal created, updated
        p = existing.get(gsis)
        if p is None:
            p = Player(gsis_id=gsis, **values)
            db.add(p)
            existing[gsis] = p
            created += 1
        elif _apply(p, values):
            updated += 1

    active_ids = set()
    for r in ds.players:
        gsis = r["player_id"].strip()
        if not gsis:
            continue
        active_ids.add(gsis)
        upsert(
            gsis,
            {
                "sleeper_id": (r.get("sleeper_id") or "").split(".")[0] or None,
                "full_name": r["full_name"],
                "name_key": name_key(r["full_name"]),
                "first_name": r.get("first_name") or None,
                "last_name": r.get("last_name") or None,
                "team_abbr": team(r.get("team")),
                "position": r.get("position") or None,
                "position_group": r.get("position_group") or None,
                "depth_chart_position": r.get("depth_chart_position") or None,
                "jersey_number": to_opt_int(r.get("jersey_number")),
                "status": r.get("status") or None,
                "status_code": (r.get("status_code") or "").strip().upper() or None,
                "headshot_url": (r.get("headshot_url") or None),
                "roster_source": "players_active",
            },
        )
    # Players with stats but not on the active-roster file (e.g. whole teams missing from it).
    fallback = 0
    for r in ds.season_rows:
        gsis = r["player_id"].strip()
        if gsis in active_ids:
            continue
        fallback += 1
        upsert(
            gsis,
            {
                "full_name": r["full_name"],
                "name_key": name_key(r["full_name"]),
                "team_abbr": team(r.get("team")),
                "position": r.get("position") or None,
                "position_group": r.get("position_group") or None,
                "roster_source": "season_stats",
            },
        )
    # Players only in prior-season game logs (retired / free agents): identity only, no current team.
    known = active_ids | {r["player_id"].strip() for r in ds.season_rows}
    history = 0
    for r in ds.prev_game_rows:
        gsis = r["player_id"].strip()
        if not gsis or gsis in known:
            continue
        known.add(gsis)
        history += 1
        upsert(
            gsis,
            {
                "full_name": r["full_name"],
                "name_key": name_key(r["full_name"]),
                "team_abbr": None,
                "position": r.get("position") or None,
                "position_group": r.get("position_group") or None,
                "status": None,
                "roster_source": "history",
            },
        )
    db.flush()
    roster_teams = {team(r.get("team")) for r in ds.players if r.get("team")}
    stat_teams = {team(r.get("team")) for r in ds.season_rows if r.get("team")}
    missing = sorted(t for t in stat_teams - roster_teams if t)
    if missing:
        report.warnings.append(
            f"teams missing from {len(roster_teams)}-team players_active.csv (identity/team taken from "
            f"season stats instead): {', '.join(missing)}"
        )
    report.counts.update(
        players_total=len(existing),
        players_created=created,
        players_updated=updated,
        players_from_season_stats_only=fallback,
        players_from_prev_season_logs_only=history,
        players_active_rows=sum(1 for r in ds.players if r["player_id"].strip()),
        players_active_teams=len(roster_teams),
    )
    return existing


def upsert_season_stats(db: Session, ds: DataSet, players: dict[str, Player], report: LoadReport) -> None:
    existing = {
        (s.player_id, s.season_type): s
        for s in db.scalars(select(PlayerSeasonStat).where(PlayerSeasonStat.season == ds.season))
    }
    seen: set[tuple[uuid.UUID, str]] = set()
    created = updated = 0
    for r in ds.season_rows:
        p = players[r["player_id"].strip()]
        key = (p.id, r["season_type"])
        seen.add(key)
        values = {
            "team_abbr": team(r.get("team")),
            "games_played": to_int(r["games_played"]),
            "games_played_any_snaps": to_opt_int(r.get("games_played_any_snaps")),
            "team_games": to_opt_int(r.get("team_games")),
            "offense_snaps_total": to_opt_int(r.get("offense_snaps_total")),
            **_stats(r),
        }
        row = existing.get(key)
        if row is None:
            db.add(PlayerSeasonStat(player_id=p.id, season=ds.season, season_type=r["season_type"], **values))
            created += 1
        elif _apply(row, values):
            updated += 1
    stale = [s.id for k, s in existing.items() if k not in seen]
    if stale:
        db.execute(delete(PlayerSeasonStat).where(PlayerSeasonStat.id.in_(stale)))
    db.flush()
    report.counts.update(
        season_rows=len(ds.season_rows),
        season_created=created,
        season_updated=updated,
        season_deleted=len(stale),
    )


def upsert_game_stats(
    db: Session,
    season: int,
    rows: list[dict[str, str]],
    players: dict[str, Player],
    report: LoadReport,
    prefix: str = "game",
) -> None:
    """Upsert one season's played game rows; rows for that season missing from `rows` are deleted."""
    existing = {
        (g.player_id, g.season_type, g.week): g
        for g in db.scalars(select(PlayerGameStat).where(PlayerGameStat.season == season))
    }
    seen: set[tuple[uuid.UUID, str, int]] = set()
    created = updated = 0
    for r in rows:
        p = players.get(r["player_id"].strip())
        if p is None:  # every game row's player is upserted first; be defensive
            report.warnings.append(f"{season} game row for unknown player {r['player_id']} skipped")
            continue
        key = (p.id, r["season_type"], to_int(r["week"]))
        seen.add(key)
        values = {
            "nfl_game_id": r["game_id"],
            "game_type": r.get("game_type") or None,
            "team_abbr": team(r["team"]),
            "opponent_abbr": team(r["opponent_team"]),
            "played": to_bool(r["played"]),
            "stats_row_imputed": to_bool(r.get("stats_row_imputed")),
            "offense_snaps": to_opt_int(r.get("offense_snaps")),
            "offense_snap_pct": to_float(r.get("offense_snap_pct")),
            **_stats(r),
        }
        row = existing.get(key)
        if row is None:
            db.add(
                PlayerGameStat(
                    player_id=p.id, season=season, season_type=r["season_type"], week=key[2], **values
                )
            )
            created += 1
        elif _apply(row, values):
            updated += 1
    stale = [g.id for k, g in existing.items() if k not in seen]
    if stale:
        db.execute(delete(PlayerGameStat).where(PlayerGameStat.id.in_(stale)))
    db.flush()
    report.counts.update(
        {
            f"{prefix}_rows": len(rows),
            f"{prefix}_rows_imputed_zero": sum(to_bool(r.get("stats_row_imputed")) for r in rows),
            f"{prefix}_created": created,
            f"{prefix}_updated": updated,
            f"{prefix}_deleted": len(stale),
        }
    )


def upsert_matchups(db: Session, ds: DataSet, players: dict[str, Player], report: LoadReport) -> None:
    """Upcoming-week matchups (current team + opponent / BYE); replaces that week's rows."""
    if ds.matchup_week is None:
        report.counts.update(matchup_rows=0)
        return
    existing = {
        m.player_id: m
        for m in db.scalars(
            select(PlayerMatchup).where(
                PlayerMatchup.season == ds.season, PlayerMatchup.week == ds.matchup_week
            )
        )
    }
    seen: set[uuid.UUID] = set()
    created = updated = skipped = 0
    for r in ds.matchups:
        p = players.get(r["player_id"].strip())
        if p is None:
            skipped += 1
            continue
        seen.add(p.id)
        bye = to_bool(r["is_bye"]) or r["opponent_team"].strip().upper() == "BYE"
        values = {
            "team_abbr": team(r["team"]),
            "opponent_abbr": None if bye else team(r["opponent_team"]),
            "is_bye": bye,
            "nfl_game_id": r.get("game_id") or None,
            "home_away": r.get("home_away") or None,
            "kickoff_et": r.get("kickoff_et") or None,
            "has_prev_vs_opp": to_bool(r["has_2025_vs_opp_games"])
            if r.get("has_2025_vs_opp_games")
            else None,
        }
        row = existing.get(p.id)
        if row is None:
            db.add(PlayerMatchup(player_id=p.id, season=ds.season, week=ds.matchup_week, **values))
            created += 1
        elif _apply(row, values):
            updated += 1
    stale = [m.id for k, m in existing.items() if k not in seen]
    if stale:
        db.execute(delete(PlayerMatchup).where(PlayerMatchup.id.in_(stale)))
    db.flush()
    report.counts.update(
        matchup_week=ds.matchup_week,
        matchup_rows=len(seen),
        matchup_byes=sum(1 for r in ds.matchups if r["player_id"].strip() and to_bool(r["is_bye"])),
        matchup_skipped_no_player=skipped,
        matchup_created=created,
        matchup_updated=updated,
        matchup_deleted=len(stale),
    )


# --------------------------------------------------------------------------- props


def _choose_player(cands: list[Player], game_teams: set[str], stat_key: str) -> Player | None:
    if not cands:
        return None
    if len(cands) == 1:
        return cands[0]
    in_game = [p for p in cands if p.team_abbr in game_teams]
    pool = in_game or cands
    want = _POSITION_FOR_STAT.get(stat_key)
    if want:
        pool = [p for p in pool if p.position in want] or pool
    active = [p for p in pool if p.status == "Active"]
    return (active or pool)[0]


def _clear_hit_rates(prop: Prop) -> None:
    for f in ("season", "last5", "vs_opp", "vs_opp_last"):
        setattr(prop, f"hr_{f}_pct", None)
        setattr(prop, f"hr_{f}_hit", None)
        setattr(prop, f"hr_{f}_total", None)
    prop.hr_vs_opp_label = prop.hr_vs_opp_last_label = None
    prop.hr_vs_opp_season = prop.hr_vs_opp_last_season = None
    prop.hr_vs_opp_opponent = prop.hr_opponent_source = None


def _gap(game: Game, prop: Prop, gap: str, **kw: Any) -> dict[str, Any]:
    return {"prop": f"{game.external_id}/{prop.external_id}", "gap": gap, **kw}


def _roster_inactive(player: Player) -> str | None:
    """Roster status if the player is known NOT to be active (IR, practice squad, …), else None."""
    code = (player.status_code or "").upper()
    if code:
        return None if code == "ACT" else (player.status or code)
    if player.status and player.status != "Active" and player.roster_source == "players_active":
        return player.status
    return None


def recompute_prop_hit_rates(
    db: Session,
    season: int,
    through_week: int,
    report: LoadReport,
    *,
    prev_vs_opp_reg: dict[tuple[str, str], int] | None = None,
    real_games: dict[str, Game] | None = None,
    schedule_week: int | None = None,
) -> None:
    """Recompute hit rates for every prop on a game in `season` from the loaded game logs.

    - Current team / opponent come from player_matchups for the prop's week when present (the
      player's REAL upcoming game), else from the player's roster team and the other team on our game.
    - season/last5/vsOpp use `season` REG logs before the game's week; vsOppLastSeason uses the
      prior season's REG logs vs the same opponent. All against the prop's (sample) line.
    - `prev_vs_opp_reg`: (gsis, opponent) → games_played_reg from the summary file, cross-checked.
    - `real_games` (nfl_game_id → Game) + `schedule_week`: when the real schedule was imported, each
      prop in that week MOVES to its player's real game (matchups game_id). Bye-week players and
      players whose roster status isn't Active (IR, practice squad, …) are hidden and closed.
    """
    real_games = real_games or {}
    prev = season - 1
    props = db.scalars(
        select(Prop)
        .join(Game, Prop.game_id == Game.id)
        .where(Game.season == season)
        .options(
            selectinload(Prop.game).selectinload(Game.home_team),
            selectinload(Prop.game).selectinload(Game.away_team),
        )
    ).all()
    players_by_key: dict[str, list[Player]] = defaultdict(list)
    players_by_id: dict[uuid.UUID, Player] = {}
    for p in db.scalars(select(Player)):
        players_by_key[p.name_key].append(p)
        players_by_id[p.id] = p
    season_stats = {
        s.player_id: s
        for s in db.scalars(
            select(PlayerSeasonStat).where(
                PlayerSeasonStat.season == season, PlayerSeasonStat.season_type == "REG"
            )
        )
    }
    logs: dict[uuid.UUID, list[PlayerGameStat]] = defaultdict(list)
    for g in db.scalars(
        select(PlayerGameStat).where(PlayerGameStat.season == season, PlayerGameStat.season_type == "REG")
    ):
        logs[g.player_id].append(g)
    prev_logs: dict[uuid.UUID, list[PlayerGameStat]] = defaultdict(list)
    for g in db.scalars(select(PlayerGameStat).where(PlayerGameStat.season == prev)):
        prev_logs[g.player_id].append(g)
    have_prev = bool(prev_logs)
    matchups = {
        (m.player_id, m.week): m
        for m in db.scalars(select(PlayerMatchup).where(PlayerMatchup.season == season))
    }
    # Our (sample) slate: team → game slug per week, to say where a hidden prop's player plays.
    slate: dict[tuple[int, str], str] = {}
    for g in db.scalars(
        select(Game)
        .where(Game.season == season)
        .options(selectinload(Game.home_team), selectinload(Game.away_team))
    ):
        slate[(g.week, g.home_team.abbr)] = slate[(g.week, g.away_team.abbr)] = g.external_id

    counts: dict[str, int] = defaultdict(int)
    for prop in props:
        game = prop.game
        game_teams = {game.home_team.abbr, game.away_team.abbr}
        stat_key = MARKET_STAT_KEYS.get(prop.market)
        prop.stat_key = stat_key
        prop.hit_rate_season = season
        if stat_key is None:
            prop.data_status, prop.hit_rate_source = "unsupported_market", "none"
            prop.hit_rate_through_week = None
            _clear_hit_rates(prop)
            report.gaps.append(_gap(game, prop, "unsupported_market", market=prop.market))
            counts["unsupported_market"] += 1
            continue

        player = players_by_id.get(prop.player_id) if prop.player_id else None
        if player is None:
            player = _choose_player(players_by_key.get(name_key(prop.player), []), game_teams, stat_key)
        if player is None:
            prop.data_status, prop.hit_rate_source = "player_not_found", "none"
            prop.player_id = prop.player_gsis_id = None
            prop.hit_rate_through_week = None
            _clear_hit_rates(prop)
            report.gaps.append(_gap(game, prop, "player_not_found", player=prop.player))
            counts["player_not_found"] += 1
            continue

        prop.player_id, prop.player_gsis_id = player.id, player.gsis_id
        sstat = season_stats.get(player.id)
        mu = matchups.get((player.id, game.week))
        week_is_real = schedule_week is not None and game.week == schedule_week
        if week_is_real and mu is not None and not mu.is_bye and mu.nfl_game_id in real_games:
            target = real_games[mu.nfl_game_id]
            if target.id != game.id:
                clash = db.scalars(
                    select(Prop.id).where(Prop.game_id == target.id, Prop.external_id == prop.external_id)
                ).first()
                if clash is None:
                    report.moves.append(
                        {"prop": prop.external_id, "from": game.external_id, "to": target.external_id}
                    )
                    prop.game_id, prop.game = target.id, target
                    game = target
                    game_teams = {game.home_team.abbr, game.away_team.abbr}
                    counts["moved_to_real_game"] += 1
                else:
                    report.warnings.append(f"{prop.external_id}: id already used on {target.external_id}")
        real_team = (
            (mu.team_abbr if mu else None)
            or player.team_abbr
            or (sstat.team_abbr if sstat else None)
            or prop.team_abbr
        )
        was_hidden = prop.data_status in HIDDEN_STATUSES
        sample_opp = (game_teams - {real_team}).pop() if real_team in game_teams else None
        if mu is not None and not mu.is_bye and mu.opponent_abbr:
            opponent, opp_source = mu.opponent_abbr, "matchups"
        else:
            opponent, opp_source = sample_opp, ("sample_game" if sample_opp else None)

        inactive = _roster_inactive(player)
        if week_is_real and mu is not None and mu.is_bye:
            prop.data_status = "player_on_bye"
            prop.is_open = False
            opponent, opp_source = None, None
            report.gaps.append(_gap(game, prop, "player_on_bye", player=player.full_name, team=real_team))
            counts["player_on_bye"] += 1
        elif inactive and real_team in game_teams:
            prop.data_status = "player_inactive"
            prop.is_open = False
            report.gaps.append(_gap(game, prop, "player_inactive", player=player.full_name, status=inactive))
            counts["player_inactive"] += 1
        elif real_team in game_teams:
            prop.data_status = "ok"
            if was_hidden and prop.result.value == "pending":
                prop.is_open = True  # re-open only what this loader closed
            counts["ok"] += 1
            if mu is not None and mu.is_bye:
                report.gaps.append(
                    _gap(game, prop, "bye_in_real_schedule", player=player.full_name, team=real_team,
                         vs_opp_from="sample_game", opponent=sample_opp)
                )  # fmt: skip
                counts["real_bye_week"] += 1
            elif mu is not None and opponent != sample_opp:
                report.gaps.append(
                    _gap(game, prop, "real_opponent_differs", player=player.full_name, team=real_team,
                         sample_opponent=sample_opp, real_opponent=opponent, real_game=mu.nfl_game_id)
                )  # fmt: skip
                counts["real_opponent_differs"] += 1
        else:
            prop.data_status = "player_not_in_game"
            prop.is_open = False  # not bettable; hidden from listings
            restorable = mu is not None and not mu.is_bye and bool(mu.nfl_game_id)
            report.gaps.append(
                _gap(game, prop, "player_not_in_game", player=player.full_name,
                     game_teams="/".join(sorted(game_teams)), real_team=real_team, status=player.status,
                     real_week_game=mu.nfl_game_id if mu else None,
                     real_opponent=(None if mu is None or mu.is_bye else mu.opponent_abbr),
                     restored_by_real_schedule_import=restorable,
                     sample_slate_game_for_team=slate.get((game.week, real_team)))
            )  # fmt: skip
            counts["player_not_in_game"] += 1
            if restorable:
                counts["hidden_restorable_by_real_schedule"] += 1
        prop.team_abbr = real_team or prop.team_abbr
        counts[f"opponent_from_{opp_source or 'none'}"] += 1

        # Current season: only games BEFORE this prop's week (no leakage once later weeks load).
        played = [g for g in logs.get(player.id, []) if g.played and g.week < game.week]
        excluded = sum(1 for g in logs.get(player.id, []) if g.played and g.week >= game.week)
        games_played = (sstat.games_played - excluded) if sstat else len(played)
        def _val(g, key=stat_key):
            if key == "anytime_td":
                return (g.receiving_tds or 0) + (g.rushing_tds or 0) + (g.passing_tds or 0)
            return getattr(g, key)
        hr = compute_hit_rates(
            [GameLog(g.season, g.week, g.opponent_abbr, _val(g), g.played) for g in played],
            games_played=games_played,
            line=prop.line,
            side=prop.side,
            opponent=opponent,
        )
        prop.hr_season_hit, prop.hr_season_total, prop.hr_season_pct = (
            hr.season.hit,
            hr.season.total,
            hr.season.pct,
        )
        prop.hr_last5_hit, prop.hr_last5_total, prop.hr_last5_pct = hr.last5.hit, hr.last5.total, hr.last5.pct
        prop.hr_vs_opp_opponent, prop.hr_opponent_source = opponent, opp_source
        if hr.vs_opp is not None:
            prop.hr_vs_opp_hit, prop.hr_vs_opp_total = hr.vs_opp.hit, hr.vs_opp.total
            prop.hr_vs_opp_pct, prop.hr_vs_opp_label = hr.vs_opp.pct, hr.vs_opp_label
            prop.hr_vs_opp_season = season
        else:
            prop.hr_vs_opp_hit = prop.hr_vs_opp_total = prop.hr_vs_opp_pct = None
            prop.hr_vs_opp_label = prop.hr_vs_opp_season = None

        # Prior regular season vs the same opponent (game logs, so it's against this line).
        if opponent and have_prev:
            last = compute_vs_opp_season(
                [
                    GameLog(g.season, g.week, g.opponent_abbr, _val(g), g.played, g.season_type)
                    for g in prev_logs.get(player.id, [])
                ],
                season=prev,
                opponent=opponent,
                line=prop.line,
                side=prop.side,
            )
            prop.hr_vs_opp_last_hit, prop.hr_vs_opp_last_total, prop.hr_vs_opp_last_pct = (
                last.hit,
                last.total,
                last.pct,
            )
            prop.hr_vs_opp_last_label, prop.hr_vs_opp_last_season = season_label(opponent, prev), prev
            counts["vs_opp_last_season_nonempty" if last.total else "vs_opp_last_season_empty"] += 1
            if prev_vs_opp_reg is not None:
                expected = prev_vs_opp_reg.get((player.gsis_id, opponent), 0)
                if expected != last.total:
                    counts["vs_opp_last_season_cross_check_mismatches"] += 1
                    report.warnings.append(
                        f"{game.external_id}/{prop.external_id}: {prev} REG logs vs {opponent} = "
                        f"{last.total}, "
                        f"player_vs_opponent games_played_reg = {expected}"
                    )
                else:
                    counts["vs_opp_last_season_cross_checked"] += 1
        else:
            prop.hr_vs_opp_last_hit = prop.hr_vs_opp_last_total = prop.hr_vs_opp_last_pct = None
            prop.hr_vs_opp_last_label = prop.hr_vs_opp_last_season = None
            counts["vs_opp_last_season_null"] += 1

        prop.hit_rate_source = SOURCE
        prop.hit_rate_through_week = min(through_week, game.week - 1)
        if not prop.line_source:
            prop.line_source = "sample"
    db.flush()
    report.counts.update({f"props_{k}": v for k, v in sorted(counts.items())}, props_total=len(props))
    if schedule_week is not None:
        # Every listed prop's vsOpp / vsOppLastSeason opponent must be the other team on its card.
        mismatches = 0
        for prop in props:
            g = prop.game
            if g.week != schedule_week or prop.data_status in HIDDEN_STATUSES or g.archived_at is not None:
                continue
            card = {g.home_team.abbr, g.away_team.abbr}
            other = (card - {prop.team_abbr}).pop() if prop.team_abbr in card else None
            last_opp = (prop.hr_vs_opp_last_label or "").split(" ")[1:2]
            if prop.hit_rate_source == SOURCE and (
                other is None
                or prop.hr_vs_opp_opponent != other
                or (prop.hr_vs_opp_last_label and last_opp != [other])
            ):
                mismatches += 1
                report.warnings.append(
                    f"{g.external_id}/{prop.external_id}: vsOpp {prop.hr_vs_opp_opponent} != card {other}"
                )
        report.validation["card_opponent_mismatches"] = mismatches
    if prev_vs_opp_reg is not None:
        report.validation["prev_season_prop_vs_opp_cross_check_mismatches"] = counts[
            "vs_opp_last_season_cross_check_mismatches"
        ]


# --------------------------------------------------------------------------- sanity


def sanity_metrics(ds: DataSet) -> dict[str, Any]:
    """WR/TE 1+ catch over played games, via the same hit-rate function the props use."""
    rows_by_player: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in ds.game_rows:
        rows_by_player[r["player_id"]].append(r)
    hit = total = 0
    for s in ds.season_rows:
        if s.get("position") not in ("WR", "TE"):
            continue
        rows = rows_by_player.get(s["player_id"], [])
        hr = compute_hit_rates(
            [
                GameLog(to_int(r["season"]), to_int(r["week"]), r["opponent_team"], to_int(r["receptions"]))
                for r in rows
            ],
            games_played=to_int(s["games_played"]),
            line=Decimal("0.5"),
        )
        hit += hr.season.hit
        total += hr.season.total
    return {
        "wr_te_1plus_catch": {"hit": hit, "total": total, "pct": pct(hit, total),
                              "rate": round(hit / total, 4) if total else None},
    }  # fmt: skip


# --------------------------------------------------------------------------- entry point


def load_dataset(db: Session, ds: DataSet, *, dry_run: bool = False, strict: bool = False) -> LoadReport:
    """Upsert everything, recompute prop hit rates, record an nfl_data_loads row, commit.

    dry_run → roll back after computing. strict → roll back and raise DataError if any validation
    count (file reconciliation or prop-level cross-check) is non-zero.
    """
    report = LoadReport(season=ds.season, through_week=ds.through_week, validation=dict(ds.validation))
    report.warnings.extend(ds.warnings)
    players = upsert_players(db, ds, report)
    upsert_season_stats(db, ds, players, report)
    upsert_game_stats(db, ds.season, ds.game_rows, players, report)
    if ds.prev_season is not None:
        upsert_game_stats(db, ds.prev_season, ds.prev_game_rows, players, report, prefix="prev_game")
        report.counts["prev_season"] = ds.prev_season
    upsert_matchups(db, ds, players, report)
    real_games = import_schedule(db, ds, report)
    vs_reg = (
        {(r["player_id"].strip(), team(r["opponent_team"]) or ""): to_int(r["games_played_reg"])
         for r in ds.prev_vs_opp_rows}
        if ds.prev_vs_opp_rows
        else None
    )  # fmt: skip
    recompute_prop_hit_rates(
        db,
        ds.season,
        ds.through_week,
        report,
        prev_vs_opp_reg=vs_reg,
        real_games=real_games,
        schedule_week=ds.schedule_week if real_games else None,
    )
    if real_games and ds.schedule_week is not None:
        retire_sample_games(db, ds.season, ds.schedule_week, report)
    apply_prop_lines(db, ds, report)
    report.sanity = sanity_metrics(ds)
    if strict and any(report.validation.values()):
        db.rollback()
        bad = {k: v for k, v in report.validation.items() if v}
        raise DataError(f"validation failed {bad}; nothing was written")
    if dry_run:
        db.rollback()
        return report
    db.add(
        NflDataLoad(
            source_dir=str(ds.directory),
            season=ds.season,
            through_week=ds.through_week,
            file_hashes=ds.file_hashes,
            counts={**report.counts, **{f"validation_{k}": v for k, v in report.validation.items()}},
            warnings=report.warnings + [f"gap: {g}" for g in report.gaps],
        )
    )
    db.commit()
    return report
