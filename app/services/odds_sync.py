"""Upsert provider data (teams, games, markets, selections, props) into the database."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Game, Market, Prop, PropBook, Selection, Team
from app.models.enums import MarketType, SeasonType, SelectionSide
from app.odds_provider.base import OddsProvider
from app.odds_provider.sample_props import SAMPLE_PROPS


@dataclass
class SyncResult:
    teams: int = 0
    games: int = 0
    markets: int = 0
    selections: int = 0
    props: int = 0
    skipped_games: int = 0  # real (plan_data) or archived games: never overwritten by a sample feed


def sync_teams(db: Session, provider: OddsProvider) -> dict[str, Team]:
    existing = {t.abbr: t for t in db.scalars(select(Team))}
    for pt in provider.get_teams():
        team = existing.get(pt.abbr)
        if team is None:
            team = Team(abbr=pt.abbr)
            db.add(team)
            existing[pt.abbr] = team
        team.location, team.name = pt.location, pt.name
        team.conference, team.division, team.primary_color = (
            pt.conference,
            pt.division,
            pt.primary_color,
        )
    db.flush()
    return existing


def _selection_name(side: SelectionSide, home: Team, away: Team, override: str | None) -> str:
    if override:
        return override
    if side == SelectionSide.HOME:
        return f"{home.abbr} {home.name}"
    if side == SelectionSide.AWAY:
        return f"{away.abbr} {away.name}"
    return side.value.capitalize()


def _slug(away: str, home: str) -> str:
    return f"{away.lower()}-{home.lower()}"


def _sync_props_for_game(db: Session, game: Game, slug: str) -> int:
    n = 0
    for sp in SAMPLE_PROPS.get(slug, []):
        # Same season/week, any game: props may have been moved to their player's real game.
        prop = db.scalars(
            select(Prop)
            .join(Game, Prop.game_id == Game.id)
            .where(Game.season == game.season, Game.week == game.week, Prop.external_id == sp.id)
        ).first()
        if prop is None:
            prop = Prop(game_id=game.id, external_id=sp.id)
            db.add(prop)
        prop.market = sp.market
        prop.line_label = sp.line_label
        prop.line = sp.line
        prop.odds_american = sp.price
        prop.line_source = "sample"
        prop.side = SelectionSide.OVER if sp.line_label.lower().startswith("over") else SelectionSide.UNDER
        if prop.hit_rate_source not in (None, "sample"):
            # Real hit rates from `load-nfl-data` win; re-run that loader after changing lines.
            n += 1
            continue
        prop.player = sp.player
        prop.team_abbr = sp.team
        prop.hit_rate_source = "sample"
        if sp.season:
            prop.hr_season_pct, prop.hr_season_hit, prop.hr_season_total = (
                sp.season.pct,
                sp.season.hit,
                sp.season.total,
            )
        if sp.last5:
            prop.hr_last5_pct, prop.hr_last5_hit, prop.hr_last5_total = (
                sp.last5.pct,
                sp.last5.hit,
                sp.last5.total,
            )
        if sp.vs_opp:
            prop.hr_vs_opp_pct = sp.vs_opp.pct
            prop.hr_vs_opp_hit = sp.vs_opp.hit
            prop.hr_vs_opp_total = sp.vs_opp.total
            prop.hr_vs_opp_label = sp.vs_opp.label
            prop.hr_vs_opp_season = game.season
            prop.hr_vs_opp_opponent = (sp.vs_opp.label or "").removeprefix("vs ").strip() or None
            prop.hr_opponent_source = "sample"
        else:
            prop.hr_vs_opp_pct = prop.hr_vs_opp_hit = prop.hr_vs_opp_total = None
            prop.hr_vs_opp_label = prop.hr_vs_opp_season = prop.hr_vs_opp_opponent = None
            prop.hr_opponent_source = None
        # The sample feed has no prior-season window.
        prop.hr_vs_opp_last_pct = prop.hr_vs_opp_last_hit = prop.hr_vs_opp_last_total = None
        prop.hr_vs_opp_last_label = prop.hr_vs_opp_last_season = None
        _seed_sample_books(db, prop)
        n += 1
    return n


def _seed_sample_books(db: Session, prop: Prop) -> None:
    """Ensure FanDuel + DraftKings sample rows (slightly different prices). Skip if live books exist."""
    # Session autoflush is off, so a newly added prop has no id until flush.
    if prop.id is None:
        db.flush()
    existing = {
        b.book: b
        for b in db.scalars(select(PropBook).where(PropBook.prop_id == prop.id))
    }
    if any(b.source != "sample" for b in existing.values()):
        return  # live odds win
    side = "over" if prop.side == SelectionSide.OVER else "under"
    fd_price = prop.odds_american
    dk_price = fd_price + 5 if fd_price < 0 else fd_price - 5
    if abs(dk_price) < 100:
        dk_price = -110 if dk_price <= 0 else 100
    for book, price in (("fanduel", fd_price), ("draftkings", dk_price)):
        row = existing.get(book)
        if row is None:
            db.add(
                PropBook(
                    prop_id=prop.id, book=book, line=prop.line, price=price, side=side, source="sample"
                )
            )
        else:
            row.line, row.price, row.side, row.source = prop.line, price, side, "sample"


def sync_week(
    db: Session,
    provider: OddsProvider,
    season: int,
    week: int,
    season_type: SeasonType = SeasonType.REG,
) -> SyncResult:
    result = SyncResult()
    teams = sync_teams(db, provider)
    result.teams = len(teams)

    for pg in provider.get_week(season, week, season_type):
        home, away = teams[pg.home_abbr], teams[pg.away_abbr]
        game = db.scalars(select(Game).where(Game.external_id == pg.external_id)).one_or_none()
        if game is not None and (game.source != "sample" or game.archived_at is not None):
            # Real schedule game (load-nfl-data) or retired sample game: leave it alone.
            result.skipped_games += 1
            result.props += _sync_props_for_game(db, game, _slug(pg.away_abbr, pg.home_abbr))
            continue
        if game is None:
            game = Game(external_id=pg.external_id)
            db.add(game)
        game.season, game.week, game.season_type = pg.season, pg.week, pg.season_type
        game.home_team_id, game.away_team_id = home.id, away.id
        game.kickoff_at, game.venue, game.status = pg.kickoff_at, pg.venue, pg.status
        game.home_score, game.away_score = pg.home_score, pg.away_score
        db.flush()
        result.games += 1

        markets: dict[tuple[MarketType, str], Market] = {
            (m.type, m.period): m for m in db.scalars(select(Market).where(Market.game_id == game.id))
        }
        for pm in pg.markets:
            market = markets.get((pm.type, pm.period))
            if market is None:
                market = Market(game_id=game.id, type=pm.type, period=pm.period)
                db.add(market)
                db.flush()
            result.markets += 1
            sels = {s.side: s for s in db.scalars(select(Selection).where(Selection.market_id == market.id))}
            for ps in pm.selections:
                sel = sels.get(ps.side)
                if sel is None:
                    sel = Selection(market_id=market.id, side=ps.side)
                    db.add(sel)
                sel.name = _selection_name(ps.side, home, away, ps.name)
                sel.line = ps.line
                sel.odds_american = ps.odds_american
                result.selections += 1

        result.props += _sync_props_for_game(db, game, _slug(pg.away_abbr, pg.home_abbr))
    db.commit()
    return result
