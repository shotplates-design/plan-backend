"""ORM → frontend-aligned API shapes (plan-frontend/mock/data.js)."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from app.config import get_settings
from app.models import Bet, BetLeg, Game, Prop, Selection
from app.models.enums import MarketType, SelectionSide
from app.schemas.bets import BetLegOut, BetOut, GameBrief
from app.schemas.sports import (
    BookLineOut,
    GameBoardOut,
    HitRateBucket,
    HitRateOut,
    HitRateVsOpp,
    MoneyOut,
    PropBooksOut,
    PropOut,
    SpreadOut,
    SpreadSideOut,
    TeamBrief,
    TotalOut,
)
from app.services.selection_keys import (
    format_line,
    selection_key_for_main,
    selection_key_for_prop,
)

if TYPE_CHECKING:
    from app.billing.paywall import Access

ET = ZoneInfo("America/New_York")


def public_game_id(game: Game) -> str:
    return game.external_id


def team_brief(team) -> TeamBrief:
    return TeamBrief(
        abbr=team.abbr,
        name=team.name,
        short=f"{team.abbr} {team.name}",
        color=team.primary_color,
    )


def kickoff_label(kickoff: datetime) -> str:
    local = kickoff.astimezone(ET)
    return local.strftime("%a %-I:%M%p ET").upper().replace("AM", "AM").replace("PM", "PM")


def date_group(kickoff: datetime) -> str:
    local = kickoff.astimezone(ET)
    # Match mock style: "THU OCT 8th"
    day = local.day
    suffix = "th" if 10 <= day % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return local.strftime(f"%a %b {day}{suffix}").upper()


def _side_map(market) -> dict[SelectionSide, Selection]:
    return {s.side: s for s in market.selections}


def board_spread(game: Game) -> SpreadOut | None:
    m = next((x for x in game.markets if x.type == MarketType.SPREAD), None)
    if not m:
        return None
    s = _side_map(m)
    away, home = s.get(SelectionSide.AWAY), s.get(SelectionSide.HOME)
    if not away or not home:
        return None
    return SpreadOut(
        away=SpreadSideOut(line=format_line(away.line, signed=True) or "0", price=away.odds_american),
        home=SpreadSideOut(line=format_line(home.line, signed=True) or "0", price=home.odds_american),
    )


def board_money(game: Game) -> MoneyOut | None:
    m = next((x for x in game.markets if x.type == MarketType.MONEYLINE), None)
    if not m:
        return None
    s = _side_map(m)
    away, home = s.get(SelectionSide.AWAY), s.get(SelectionSide.HOME)
    if not away or not home:
        return None
    return MoneyOut(away=away.odds_american, home=home.odds_american)


def board_total(game: Game) -> TotalOut | None:
    m = next((x for x in game.markets if x.type == MarketType.TOTAL), None)
    if not m:
        return None
    s = _side_map(m)
    over, under = s.get(SelectionSide.OVER), s.get(SelectionSide.UNDER)
    if not over or not under or over.line is None:
        return None
    return TotalOut(line=float(over.line), over=over.odds_american, under=under.odds_american)


BOOKS = ("fanduel", "draftkings")


def _hit_rate_from_prop(prop: Prop) -> HitRateOut | None:
    opponent = prop.hr_vs_opp_opponent or _label_opponent(prop.hr_vs_opp_label)
    if (
        prop.hr_season_total is None
        and prop.hr_last5_total is None
        and prop.hr_vs_opp_total is None
        and prop.hr_vs_opp_last_total is None
    ):
        return None
    return HitRateOut(
        season=(
            HitRateBucket(pct=prop.hr_season_pct, hit=prop.hr_season_hit, total=prop.hr_season_total)
            if prop.hr_season_total is not None
            else None
        ),
        last5=(
            HitRateBucket(pct=prop.hr_last5_pct, hit=prop.hr_last5_hit, total=prop.hr_last5_total)
            if prop.hr_last5_total is not None
            else None
        ),
        vsOpp=(
            HitRateVsOpp(
                pct=prop.hr_vs_opp_pct,
                hit=prop.hr_vs_opp_hit,
                total=prop.hr_vs_opp_total,
                label=prop.hr_vs_opp_label or "",
                season=prop.hr_vs_opp_season or prop.hit_rate_season,
                opponent=opponent,
            )
            if prop.hr_vs_opp_total is not None
            else None
        ),
        vsOppLastSeason=(
            HitRateVsOpp(
                pct=prop.hr_vs_opp_last_pct,
                hit=prop.hr_vs_opp_last_hit,
                total=prop.hr_vs_opp_last_total,
                label=prop.hr_vs_opp_last_label or "",
                season=prop.hr_vs_opp_last_season,
                opponent=opponent,
            )
            if prop.hr_vs_opp_last_total is not None
            else None
        ),
    )


def _hit_rate_from_blob(blob: dict | None, fallback: HitRateOut | None) -> HitRateOut | None:
    if blob is None:
        return fallback
    try:
        return HitRateOut.model_validate(blob)
    except Exception:
        return fallback


def _apply_paywall(hit: HitRateOut | None, unlocked: bool) -> tuple[HitRateOut | None, list[str]]:
    locked: list[str] = []
    if unlocked or hit is None:
        return hit, locked
    # Copy so we don't mutate a shared instance used by multiple books.
    hit = hit.model_copy(deep=True)
    if hit.season is not None:
        hit.season = None
        locked.append("season")
    if hit.last5 is not None and get_settings().paywall_board_teaser == "none":
        hit.last5 = None
        locked.append("last5")
    if hit.vsOpp is not None:
        hit.vsOpp = None
        locked.append("vs_opponent")
    if hit.vsOppLastSeason is not None:
        hit.vsOppLastSeason = None
        locked.append("vs_opponent_last_season")
    return hit, locked


def _line_label(side: str, line: float | None) -> str:
    if line is None:
        return "Yes" if side in ("yes", "over") else "No"
    word = {"over": "Over", "under": "Under"}.get(side, side.capitalize())
    from decimal import Decimal

    d = Decimal(str(line)).normalize()
    text = format(d, "f")
    if "." not in text:
        text = f"{text}.0"
    return f"{word} {text}"


def _pick_book(books: dict[str, object], preferred: str | None) -> str | None:
    """Prefer user's skin; fall back to the other. Never invent a book that is null."""
    order = [preferred, *(b for b in BOOKS if b != preferred)] if preferred in BOOKS else list(BOOKS)
    for b in order:
        if b and books.get(b) is not None:
            return b
    return None


def prop_out(prop: Prop, game_slug: str, access: Access | None = None) -> PropOut:
    """access=None → full data (internal use). Otherwise paywall-tease locked fields.

    Top-level line/price/hitRate come from the user's preferred book (theme), falling back to the
    other book when preferred is null. `book` names the source. A missing book stays null forever.
    """
    prop_hit = _hit_rate_from_prop(prop)
    by_book: dict[str, object] = {b.book: b for b in (prop.books or []) if b.book in BOOKS}
    books_out: dict[str, BookLineOut | None] = {"fanduel": None, "draftkings": None}
    for name in BOOKS:
        row = by_book.get(name)
        if row is None:
            continue
        # Share prop hit rates when this book's line matches the prop's canonical line (or no blob).
        same_line = (
            row.line is not None and prop.line is not None and abs(float(row.line) - float(prop.line)) < 1e-9
        ) or (row.line is None and (prop.line is None or float(prop.line) == 0))
        hr = _hit_rate_from_blob(row.hit_rate, prop_hit if same_line or row.hit_rate is None else None)
        books_out[name] = BookLineOut(
            line=float(row.line) if row.line is not None else None,
            price=row.price,
            hitRate=hr,
        )

    preferred = access.preferred_book if access is not None else None
    chosen = _pick_book(books_out, preferred)
    # Fall back to prop-level sample fields when no book rows exist yet (legacy / mid-migration).
    if chosen is None:
        unlocked = access is None or access.is_unlocked(prop.id)
        hit, locked = _apply_paywall(prop_hit, unlocked)
        return PropOut(
            unlocked=unlocked,
            locked=locked,
            id=prop.external_id,
            player=prop.player,
            team=prop.team_abbr,
            market=prop.market,
            line=prop.line_label,
            price=prop.odds_american,
            hitRate=hit,
            books=PropBooksOut(fanduel=None, draftkings=None),
            book=None,
            selectionKey=selection_key_for_prop(game_slug, prop.external_id),
            playerId=prop.player_gsis_id,
            isSample=True,
            lineSource=prop.line_source or "sample",
            hitRateSource=prop.hit_rate_source or "sample",
            hitRateThroughWeek=prop.hit_rate_through_week,
        )

    top = books_out[chosen]
    assert top is not None
    row = by_book[chosen]
    unlocked = access is None or access.is_unlocked(prop.id)
    # Apply paywall to top-level and each book independently (teaser fields).
    top_hit, locked = _apply_paywall(top.hitRate, unlocked)
    for name, bl in list(books_out.items()):
        if bl is None:
            continue
        hr, _ = _apply_paywall(bl.hitRate, unlocked)
        books_out[name] = BookLineOut(line=bl.line, price=bl.price, hitRate=hr)

    # Live = matched player + at least one book quote that is NOT our sample seed.
    live = prop.player_gsis_id is not None and any(
        b in by_book and by_book[b].source != "sample" for b in BOOKS
    )
    line_source = prop.line_source or "sample"
    if live and line_source == "sample":
        line_source = getattr(row, "source", None) or "odds_api"

    return PropOut(
        unlocked=unlocked,
        locked=locked,
        id=prop.external_id,
        player=prop.player,
        team=prop.team_abbr,
        market=prop.market,
        line=_line_label(row.side, float(row.line) if row.line is not None else None),
        price=top.price,
        hitRate=top_hit,
        books=PropBooksOut(fanduel=books_out["fanduel"], draftkings=books_out["draftkings"]),
        book=chosen,
        selectionKey=selection_key_for_prop(game_slug, prop.external_id),
        playerId=prop.player_gsis_id,
        isSample=not live,
        lineSource=line_source if live else (prop.line_source or "sample"),
        hitRateSource=prop.hit_rate_source or "sample",
        hitRateThroughWeek=prop.hit_rate_through_week,
    )


def _label_opponent(label: str | None) -> str | None:
    """'vs DAL' → 'DAL' (sample props store only the label)."""
    parts = (label or "").split()
    return parts[1] if len(parts) >= 2 and parts[0] == "vs" else None


HIDDEN_PROP_STATUSES = ("player_not_in_game", "player_on_bye", "player_inactive")


def prop_listed(prop: Prop) -> bool:
    """Hidden (kept for bet history): player not on either team, on a bye, or not Active (IR, …)."""
    return prop.data_status not in HIDDEN_PROP_STATUSES


def game_board_out(game: Game, *, include_props: bool = True, access: Access | None = None) -> GameBoardOut:
    slug = public_game_id(game)
    return GameBoardOut(
        id=slug,
        uuid=game.id,
        away=team_brief(game.away_team),
        home=team_brief(game.home_team),
        kickoff=game.kickoff_at,
        kickoffLabel=kickoff_label(game.kickoff_at),
        dateGroup=date_group(game.kickoff_at),
        week=game.week,
        season=game.season,
        seasonType=game.season_type,
        status=game.status,
        venue=game.venue,
        homeScore=game.home_score,
        awayScore=game.away_score,
        spread=board_spread(game),
        money=board_money(game),
        total=board_total(game),
        nflGameId=game.nfl_game_id,
        source=game.source or "sample",
        linesSource=game.lines_source or "sample",
        isSampleLines=(game.lines_source or "sample") != "nflverse_schedule",
        props=[prop_out(p, slug, access) for p in game.props if prop_listed(p)] if include_props else [],
    )


def game_brief(game: Game) -> GameBrief:
    return GameBrief(
        id=public_game_id(game),
        home_abbr=game.home_team.abbr,
        away_abbr=game.away_team.abbr,
        kickoff=game.kickoff_at,
    )


def bet_leg_out(leg: BetLeg) -> BetLegOut:
    # Prefer prop path when prop_id set; else main-market selection
    if getattr(leg, "prop_id", None) and leg.prop is not None:
        prop = leg.prop
        game = prop.game
        slug = public_game_id(game)
        return BetLegOut(
            id=leg.id,
            selection_key=selection_key_for_prop(slug, prop.external_id),
            market=prop.market,
            selection_name=f"{prop.player} {prop.line_label}",
            line=prop.line_label,
            odds_american=leg.odds_american,
            result=leg.result,
            game=game_brief(game),
        )
    sel = leg.selection
    game = sel.market.game
    slug = public_game_id(game)
    line = None
    if sel.market.type == MarketType.SPREAD:
        line = format_line(leg.line, signed=True)
    elif sel.market.type == MarketType.TOTAL:
        prefix = "O" if sel.side == SelectionSide.OVER else "U"
        line = f"{prefix} {format_line(leg.line)}"
    return BetLegOut(
        id=leg.id,
        selection_key=selection_key_for_main(slug, sel.market.type, sel.side),
        market={"moneyline": "Moneyline", "spread": "Spread", "total": "Total"}[sel.market.type.value],
        selection_name=sel.name,
        line=line,
        odds_american=leg.odds_american,
        result=leg.result,
        game=game_brief(game),
    )


def bet_out(b: Bet) -> BetOut:
    return BetOut(
        id=b.id,
        type=b.type,
        status=b.status,
        stake_cents=b.stake_cents,
        odds_american=b.odds_american,
        odds_decimal=float(b.odds_decimal),
        potential_payout_cents=b.potential_payout_cents,
        payout_cents=b.payout_cents,
        placed_at=b.placed_at,
        settled_at=b.settled_at,
        legs=[bet_leg_out(leg) for leg in b.legs],
    )
