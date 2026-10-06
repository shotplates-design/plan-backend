"""Frontend selection keys ↔ DB rows.

Convention (mirrors plan-frontend/mock/app.js `selKey`):
  {gameId}|spread|{away|home}
  {gameId}|money|{away|home}
  {gameId}|total|{over|under}
  {gameId}|prop|{propId}
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.errors import AppError
from app.models import Game, Market, Prop, Selection
from app.models.enums import MarketType, SelectionSide


def format_line(line: Decimal | None, *, signed: bool = False) -> str | None:
    if line is None:
        return None
    value = float(line)
    core = str(int(value)) if value == int(value) else f"{value:g}"
    if signed and value > 0:
        return f"+{core}"
    return core


def selection_key_for_main(game_slug: str, market_type: MarketType, side: SelectionSide) -> str:
    kind = {
        MarketType.SPREAD: "spread",
        MarketType.MONEYLINE: "money",
        MarketType.TOTAL: "total",
    }[market_type]
    return f"{game_slug}|{kind}|{side.value}"


def selection_key_for_prop(game_slug: str, prop_external_id: str) -> str:
    return f"{game_slug}|prop|{prop_external_id}"


@dataclass
class ResolvedSelection:
    """A bettable thing resolved from a selection_key or UUID."""

    kind: str  # "main" | "prop"
    selection_key: str
    selection: Selection | None
    prop: Prop | None
    game: Game
    odds_american: int
    line: Decimal | None
    line_label: str | None
    market_label: str
    name: str
    is_open: bool
    game_id: uuid.UUID
    # Stable id for BetLeg.selection_id when kind==main; for props we store prop.id
    # in a parallel column — see betting service (uses selection_id nullable + prop_id).
    target_id: uuid.UUID


def parse_selection_key(key: str) -> tuple[str, str, str]:
    parts = key.split("|")
    if len(parts) != 3 or not all(parts):
        raise AppError(
            400,
            "INVALID_SELECTION_KEY",
            'selection_key must look like "tb-dal|spread|away" or "tb-dal|prop|godwin-rec"',
            key,
        )
    return parts[0], parts[1], parts[2]


def resolve_selection_key(db: Session, key: str) -> ResolvedSelection:
    game_slug, kind, side_or_prop = parse_selection_key(key)
    game = (
        db.scalars(
            select(Game)
            .where(Game.external_id == game_slug, Game.archived_at.is_(None))
            .options(joinedload(Game.home_team), joinedload(Game.away_team))
        )
        .unique()
        .one_or_none()
    )
    if game is None:
        # Also accept slug without "sample-YYYY-wWW-" prefix aliases: try endswith
        game = (
            db.scalars(
                select(Game)
                .where(Game.external_id.endswith(f"-{game_slug}"), Game.archived_at.is_(None))
                .options(joinedload(Game.home_team), joinedload(Game.away_team))
            )
            .unique()
            .first()
        )
    if game is None:
        raise AppError(404, "GAME_NOT_FOUND", f"No game matching id {game_slug!r}")

    public_id = _public_game_id(game)

    if kind == "prop":
        prop = db.scalars(
            select(Prop).where(Prop.game_id == game.id, Prop.external_id == side_or_prop)
        ).one_or_none()
        if prop is None:
            raise AppError(404, "SELECTION_NOT_FOUND", f"Unknown prop {side_or_prop!r}")
        return ResolvedSelection(
            kind="prop",
            selection_key=selection_key_for_prop(public_id, prop.external_id),
            selection=None,
            prop=prop,
            game=game,
            odds_american=prop.odds_american,
            line=prop.line,
            line_label=prop.line_label,
            market_label=prop.market,
            name=f"{prop.player} {prop.line_label}",
            is_open=prop.is_open,
            game_id=game.id,
            target_id=prop.id,
        )

    type_map = {
        "spread": MarketType.SPREAD,
        "money": MarketType.MONEYLINE,
        "total": MarketType.TOTAL,
    }
    if kind not in type_map:
        raise AppError(400, "INVALID_SELECTION_KEY", f"Unknown market kind {kind!r}", key)
    try:
        side = SelectionSide(side_or_prop)
    except ValueError as exc:
        raise AppError(400, "INVALID_SELECTION_KEY", f"Unknown side {side_or_prop!r}", key) from exc

    market = (
        db.scalars(
            select(Market)
            .where(
                Market.game_id == game.id,
                Market.type == type_map[kind],
                Market.period == "full_game",
            )
            .options(joinedload(Market.selections))
        )
        .unique()
        .one_or_none()
    )
    if market is None:
        raise AppError(404, "SELECTION_NOT_FOUND", f"No {kind} market for {public_id}")
    sel = next((s for s in market.selections if s.side == side), None)
    if sel is None:
        raise AppError(404, "SELECTION_NOT_FOUND", f"No {kind}/{side.value} for {public_id}")

    labels = {"spread": "Spread", "money": "Moneyline", "total": "Total"}
    line_label = None
    if kind == "spread":
        line_label = format_line(sel.line, signed=True)
    elif kind == "total":
        line_label = f"{'O' if side == SelectionSide.OVER else 'U'} {format_line(sel.line)}"

    return ResolvedSelection(
        kind="main",
        selection_key=selection_key_for_main(public_id, type_map[kind], side),
        selection=sel,
        prop=None,
        game=game,
        odds_american=sel.odds_american,
        line=sel.line,
        line_label=line_label,
        market_label=labels[kind],
        name=sel.name,
        is_open=sel.is_open and market.status.value == "open",
        game_id=game.id,
        target_id=sel.id,
    )


def resolve_selection_id(db: Session, selection_id: uuid.UUID) -> ResolvedSelection:
    sel = (
        db.scalars(
            select(Selection)
            .where(Selection.id == selection_id)
            .options(joinedload(Selection.market).joinedload(Market.game).joinedload(Game.home_team))
        )
        .unique()
        .one_or_none()
    )
    # also need away_team
    if sel is None:
        prop = (
            db.scalars(select(Prop).where(Prop.id == selection_id).options(joinedload(Prop.game)))
            .unique()
            .one_or_none()
        )
        if prop is None:
            raise AppError(404, "SELECTION_NOT_FOUND", "Selection not found")
        game = prop.game
        public_id = _public_game_id(game)
        return ResolvedSelection(
            kind="prop",
            selection_key=selection_key_for_prop(public_id, prop.external_id),
            selection=None,
            prop=prop,
            game=game,
            odds_american=prop.odds_american,
            line=prop.line,
            line_label=prop.line_label,
            market_label=prop.market,
            name=f"{prop.player} {prop.line_label}",
            is_open=prop.is_open,
            game_id=game.id,
            target_id=prop.id,
        )
    game = sel.market.game
    public_id = _public_game_id(game)
    return ResolvedSelection(
        kind="main",
        selection_key=selection_key_for_main(public_id, sel.market.type, sel.side),
        selection=sel,
        prop=None,
        game=game,
        odds_american=sel.odds_american,
        line=sel.line,
        line_label=format_line(sel.line, signed=sel.market.type.value == "spread"),
        market_label={"moneyline": "Moneyline", "spread": "Spread", "total": "Total"}[sel.market.type.value],
        name=sel.name,
        is_open=sel.is_open and sel.market.status.value == "open",
        game_id=game.id,
        target_id=sel.id,
    )


def _public_game_id(game: Game) -> str:
    """Prefer short slug (tb-dal) when external_id is sample-2026-w05-tb-dal."""
    eid = game.external_id
    if eid.startswith("sample-") and eid.count("-") >= 3:
        # sample-2026-w05-tb-dal → tb-dal (last two tokens)
        parts = eid.split("-")
        return f"{parts[-2]}-{parts[-1]}"
    return eid
