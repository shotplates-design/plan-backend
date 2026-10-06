import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.api.deps import DB, CurrentUser
from app.billing.paywall import (
    Access,
    get_access,
    get_optional_access,
    require_prop_unlocked,
    resolve_access,
    unlock_prop,
)
from app.billing.views import usage_out
from app.core.errors import ERROR_RESPONSES, AppError, ErrorResponse
from app.models import Game, Market, Prop, Team
from app.models.enums import GameStatus, SeasonType
from app.schemas.sports import (
    GameBoardOut,
    HitRateBucket,
    HitRateVsOpp,
    HitRateWindowOut,
    PropDetailOut,
    PropOut,
    TeamOut,
)
from app.services.serializers import game_board_out, prop_listed, prop_out

_GATED = {
    **ERROR_RESPONSES,
    402: {
        "model": ErrorResponse,
        "description": "UPGRADE_REQUIRED (details.reason: prop_locked | out_of_tries)",
    },
}
router = APIRouter(tags=["games"], responses=ERROR_RESPONSES)

_GAME_LOAD = (
    selectinload(Game.markets).selectinload(Market.selections),
    selectinload(Game.props).selectinload(Prop.books),
)


def _find_game(db: DB, game_id: str) -> Game | None:
    """Accept public slug (tb-dal), external_id, or UUID."""
    try:
        uid = uuid.UUID(game_id)
        return (
            db.scalars(select(Game).where(Game.id == uid, Game.archived_at.is_(None)).options(*_GAME_LOAD))
            .unique()
            .first()
        )
    except ValueError:
        pass
    game = (
        db.scalars(
            select(Game).where(Game.external_id == game_id, Game.archived_at.is_(None)).options(*_GAME_LOAD)
        )
        .unique()
        .first()
    )
    if game:
        return game
    return (
        db.scalars(
            select(Game)
            .where(Game.external_id.endswith(f"-{game_id}"), Game.archived_at.is_(None))
            .options(*_GAME_LOAD)
        )
        .unique()
        .first()
    )


def _find_prop(db: DB, game_id: str, prop_id: str) -> tuple[Game, Prop]:
    game = _find_game(db, game_id)
    if game is None:
        raise AppError(404, "GAME_NOT_FOUND", "Game not found")
    prop = next((p for p in game.props if p.external_id == prop_id and prop_listed(p)), None)
    if prop is None:
        raise AppError(404, "PROP_NOT_FOUND", "Prop not found")
    return game, prop


@router.get("/teams", response_model=list[TeamOut])
def list_teams(db: DB) -> list[TeamOut]:
    teams = db.scalars(select(Team).order_by(Team.conference, Team.division, Team.abbr)).all()
    return [TeamOut.model_validate(t) for t in teams]


@router.get("/games", response_model=list[GameBoardOut])
def list_games(
    db: DB,
    access: Access = Depends(get_optional_access),
    season: int | None = Query(None, ge=1920, le=2100, examples=[2026]),
    week: int | None = Query(None, ge=0, le=25, examples=[5]),
    season_type: SeasonType = SeasonType.REG,
    status: GameStatus | None = None,
    team: str | None = Query(None, max_length=4, description="Team abbr, e.g. KC"),
    include_props: bool = Query(True, description="Embed props + hit rates (heavier payload)"),
) -> list[GameBoardOut]:
    """Weekly board (shape mirrors plan-frontend/mock/data.js). Auth optional.

    Paywall: props the caller hasn't unlocked show only the `hitRate.last5` teaser; `season` and
    `vsOpp` are null, `unlocked=false`, and the withheld fields are listed in `prop.locked`.
    Listing never consumes tries.
    """
    stmt = (
        select(Game)
        .where(Game.season_type == season_type, Game.archived_at.is_(None))  # retired slates hidden
        .options(*_GAME_LOAD)
    )
    if season is not None:
        stmt = stmt.where(Game.season == season)
    if week is not None:
        stmt = stmt.where(Game.week == week)
    if status is not None:
        stmt = stmt.where(Game.status == status)
    if team:
        t = db.scalars(select(Team).where(Team.abbr == team.upper())).one_or_none()
        if t is None:
            return []
        stmt = stmt.where((Game.home_team_id == t.id) | (Game.away_team_id == t.id))
    games = db.scalars(stmt.order_by(Game.kickoff_at, Game.external_id)).unique().all()
    return [game_board_out(g, include_props=include_props, access=access) for g in games]


@router.get("/games/{game_id}", response_model=GameBoardOut)
def get_game(game_id: str, db: DB, access: Access = Depends(get_optional_access)) -> GameBoardOut:
    """Game with markets + props (paywall-filtered like the board). Auth optional; free."""
    game = _find_game(db, game_id)
    if game is None:
        raise AppError(404, "GAME_NOT_FOUND", "Game not found")
    return game_board_out(game, include_props=True, access=access)


@router.get("/games/{game_id}/props", response_model=list[PropOut])
def get_game_props(game_id: str, db: DB, access: Access = Depends(get_optional_access)) -> list[PropOut]:
    """Props list (same objects/filtering as GameBoardOut.props). Free."""
    game = _find_game(db, game_id)
    if game is None:
        raise AppError(404, "GAME_NOT_FOUND", "Game not found")
    return game_board_out(game, access=access).props


def _detail(db: DB, user, game, prop, *, charged: bool = False, bucket: str | None = None) -> PropDetailOut:
    access = resolve_access(db, user)
    base = prop_out(prop, game.external_id, access)
    return PropDetailOut(
        **base.model_dump(),
        gameId=game.external_id,
        charged=charged,
        bucket=bucket,
        usage=usage_out(db, access),
    )


@router.get("/games/{game_id}/props/{prop_id}", response_model=PropDetailOut, responses=_GATED)
def get_prop_detail(game_id: str, prop_id: str, user: CurrentUser, db: DB) -> PropDetailOut:
    """Prop hit-rate detail. Auth required. **This is the metered action: 1 try.**

    Free tries are used first (FREE_TRIES, default 10 per user), then purchased tries.
    Re-opening the same prop within the re-open window (same ET day by default) is free, so
    refreshes/retries never double-charge. `charged` says whether this call used a try.
    402 UPGRADE_REQUIRED (details.reason=out_of_tries, with packs) when none remain.
    """
    game, prop = _find_prop(db, game_id, prop_id)
    res = unlock_prop(db, user, prop)
    return _detail(db, user, game, prop, charged=res.charged, bucket=res.bucket.value if res.bucket else None)


@router.get(
    "/games/{game_id}/props/{prop_id}/hit-rates",
    response_model=HitRateWindowOut,
    responses=_GATED,
)
def get_prop_hit_rate_window(
    game_id: str,
    prop_id: str,
    db: DB,
    window: Literal["L5", "season"] = Query(..., description="Hit-rate window"),
    access: Access = Depends(get_access),
) -> HitRateWindowOut:
    """One hit-rate window (never consumes a try). `L5` follows the board teaser; `season`
    requires the prop detail to have been opened in the re-open window (402 prop_locked)."""
    _, prop = _find_prop(db, game_id, prop_id)
    shown = prop_out(prop, "", access).hitRate
    if window == "season" or shown is None or shown.last5 is None:
        require_prop_unlocked(access, prop)
    full = prop_out(prop, "", None).hitRate
    bucket: HitRateBucket | None = None
    if full is not None:
        bucket = full.last5 if window == "L5" else full.season
    return HitRateWindowOut(propId=prop.external_id, window=window, hitRate=bucket)


@router.get(
    "/games/{game_id}/props/{prop_id}/splits/vs-opponent",
    response_model=HitRateVsOpp | None,
    responses=_GATED,
)
def get_prop_vs_opponent(
    game_id: str, prop_id: str, db: DB, access: Access = Depends(get_access)
) -> HitRateVsOpp | None:
    """Vs-opponent split (never consumes a try). 402 UPGRADE_REQUIRED (prop_locked) until the
    prop detail has been opened in the re-open window."""
    _, prop = _find_prop(db, game_id, prop_id)
    require_prop_unlocked(access, prop)
    full = prop_out(prop, "", None).hitRate
    return full.vsOpp if full else None
