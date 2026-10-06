"""Bet quoting, placement and settlement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from math import floor

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.config import get_settings
from app.core.errors import AppError
from app.core.odds import (
    InvalidOddsError,
    american_to_decimal,
    decimal_to_american,
    parlay_decimal,
    to_decimal,
    validate_american,
)
from app.models import Bet, BetLeg, Game
from app.models.enums import (
    BetStatus,
    BetType,
    GameStatus,
    MarketStatus,
    MarketType,
    SelectionResult,
    SelectionSide,
    WalletTxType,
)
from app.schemas.bets import BetLegIn, BetQuoteOut, BetSlipIn, PlaceBetRequest, QuoteLegOut
from app.services.selection_keys import (
    ResolvedSelection,
    resolve_selection_id,
    resolve_selection_key,
)
from app.services.serializers import game_brief
from app.services.wallet import get_wallet, post_transaction


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _line_matches(resolved: ResolvedSelection, submitted: str | float | None) -> bool:
    """Compare the line the user saw with the current line. Moneyline → always ok."""
    if submitted is None or resolved.line is None:
        return True
    if isinstance(submitted, str):
        # Accept "Over 4.5", "+7.5", "O 47.5", "47.5"
        s = submitted.strip().lower().replace("over ", "").replace("under ", "")
        s = s.replace("o ", "").replace("u ", "").lstrip("+")
        try:
            return Decimal(s) == resolved.line
        except Exception:
            return submitted == resolved.line_label
    return Decimal(str(submitted)) == resolved.line


def _resolve_leg(db: Session, leg: BetLegIn) -> ResolvedSelection:
    if leg.selection_key:
        return resolve_selection_key(db, leg.selection_key)
    assert leg.selection_id is not None
    return resolve_selection_id(db, leg.selection_id)


def _assert_open(resolved: ResolvedSelection) -> None:
    game = resolved.game
    now = datetime.now(UTC)
    if not resolved.is_open or game.status != GameStatus.SCHEDULED or _aware(game.kickoff_at) <= now:
        raise AppError(
            409,
            "SELECTION_UNAVAILABLE",
            "Selection is suspended, closed or started",
            resolved.selection_key,
        )
    if resolved.selection is not None and resolved.selection.market.status != MarketStatus.OPEN:
        raise AppError(409, "SELECTION_UNAVAILABLE", "Market is not open", resolved.selection_key)


def quote(db: Session, slip: BetSlipIn) -> tuple[BetQuoteOut, list[ResolvedSelection]]:
    s = get_settings()
    if not s.min_stake_cents <= slip.stake_cents <= s.max_stake_cents:
        raise AppError(
            400,
            "STAKE_OUT_OF_RANGE",
            "Stake is outside the allowed range",
            {"min_stake_cents": s.min_stake_cents, "max_stake_cents": s.max_stake_cents},
        )
    if len(slip.legs) > s.max_parlay_legs:
        raise AppError(400, "TOO_MANY_LEGS", f"Parlays are limited to {s.max_parlay_legs} legs")

    resolved_list: list[ResolvedSelection] = []
    legs_out: list[QuoteLegOut] = []
    for leg in slip.legs:
        try:
            validate_american(leg.odds_american)
        except InvalidOddsError as exc:
            raise AppError(400, "INVALID_ODDS", str(exc)) from exc
        resolved = _resolve_leg(db, leg)
        _assert_open(resolved)
        resolved_list.append(resolved)
        changed = resolved.odds_american != leg.odds_american or not _line_matches(resolved, leg.line)
        legs_out.append(
            QuoteLegOut(
                selection_key=resolved.selection_key,
                selection_id=resolved.target_id,
                market=resolved.market_label,
                selection_name=resolved.name,
                line=resolved.line_label,
                odds_american=resolved.odds_american,
                requested_odds_american=leg.odds_american,
                odds_changed=changed,
                game=game_brief(resolved.game),
            )
        )

    if slip.type == BetType.PARLAY:
        game_ids = [r.game_id for r in resolved_list]
        if len(set(game_ids)) != len(game_ids):
            raise AppError(
                400,
                "SAME_GAME_PARLAY_NOT_SUPPORTED",
                "Parlay legs must be from different games (same-game parlays not supported yet)",
            )

    current = [r.odds_american for r in resolved_list]
    dec = parlay_decimal(current)
    american = current[0] if len(current) == 1 else decimal_to_american(dec)
    payout = floor(slip.stake_cents * dec)
    if payout > s.max_payout_cents:
        raise AppError(
            400,
            "MAX_PAYOUT_EXCEEDED",
            "Potential payout exceeds the maximum allowed",
            {"max_payout_cents": s.max_payout_cents, "potential_payout_cents": payout},
        )
    q = BetQuoteOut(
        type=slip.type,
        stake_cents=slip.stake_cents,
        odds_american=american,
        odds_decimal=float(to_decimal(dec, 4)),
        potential_payout_cents=payout,
        potential_profit_cents=payout - slip.stake_cents,
        any_odds_changed=any(leg.odds_changed for leg in legs_out),
        legs=legs_out,
    )
    return q, resolved_list


def _find_by_key(db: Session, user_id: uuid.UUID, key: str) -> Bet | None:
    return db.scalars(select(Bet).where(Bet.user_id == user_id, Bet.idempotency_key == key)).one_or_none()


def place_bet(db: Session, user_id: uuid.UUID, req: PlaceBetRequest) -> tuple[Bet, bool]:
    if req.idempotency_key and (existing := _find_by_key(db, user_id, req.idempotency_key)):
        return existing, False

    q, resolved_list = quote(db, req)
    if q.any_odds_changed and not req.accept_odds_changes:
        raise AppError(
            409,
            "ODDS_CHANGED",
            "Odds or lines have changed since the slip was built",
            [leg.model_dump(mode="json") for leg in q.legs if leg.odds_changed],
        )

    wallet = get_wallet(db, user_id, for_update=True)
    legs: list[BetLeg] = []
    for r in resolved_list:
        if r.kind == "prop":
            legs.append(
                BetLeg(
                    selection_id=None,
                    prop_id=r.target_id,
                    odds_american=r.odds_american,
                    line=r.line,
                    line_label=r.line_label,
                )
            )
        else:
            legs.append(
                BetLeg(
                    selection_id=r.target_id,
                    prop_id=None,
                    odds_american=r.odds_american,
                    line=r.line,
                    line_label=r.line_label,
                )
            )
    bet = Bet(
        user_id=user_id,
        type=req.type,
        stake_cents=req.stake_cents,
        odds_american=q.odds_american,
        odds_decimal=to_decimal(parlay_decimal([r.odds_american for r in resolved_list])),
        potential_payout_cents=q.potential_payout_cents,
        idempotency_key=req.idempotency_key,
        legs=legs,
    )
    db.add(bet)
    try:
        db.flush()
        post_transaction(
            db,
            wallet,
            WalletTxType.BET_STAKE,
            -req.stake_cents,
            bet_id=bet.id,
            description=f"Stake on {req.type.value} bet",
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        if req.idempotency_key and (existing := _find_by_key(db, user_id, req.idempotency_key)):
            return existing, False
        raise
    except AppError:
        db.rollback()
        raise
    db.refresh(bet)
    return bet, True


def list_bets(
    db: Session, user_id: uuid.UUID, status: BetStatus | None, limit: int, offset: int
) -> tuple[list[Bet], int]:
    base = select(Bet).where(Bet.user_id == user_id)
    if status:
        base = base.where(Bet.status == status)
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.scalars(base.order_by(Bet.placed_at.desc()).limit(limit).offset(offset)).all()
    return list(rows), total


def get_bet(db: Session, user_id: uuid.UUID, bet_id: uuid.UUID) -> Bet:
    bet = db.get(Bet, bet_id)
    if bet is None or bet.user_id != user_id:
        raise AppError(404, "BET_NOT_FOUND", "Bet not found")
    return bet


def grade(
    market_type: MarketType,
    side: SelectionSide,
    line: Decimal | None,
    home_score: int,
    away_score: int,
) -> SelectionResult:
    if market_type == MarketType.TOTAL:
        assert line is not None
        total = Decimal(home_score + away_score)
        diff = total - line if side == SelectionSide.OVER else line - total
    else:
        mine, theirs = (home_score, away_score) if side == SelectionSide.HOME else (away_score, home_score)
        adj = Decimal(0) if market_type == MarketType.MONEYLINE else (line or Decimal(0))
        diff = Decimal(mine) + adj - Decimal(theirs)
    if diff > 0:
        return SelectionResult.WON
    if diff < 0:
        return SelectionResult.LOST
    return SelectionResult.PUSH


def settle_game(db: Session, game: Game, *, commit: bool = True) -> int:
    """Grade a final game (or void a cancelled/postponed one) and settle affected bets.

    commit=False lets callers (e.g. the data loader retiring a slate) keep it in their transaction.
    """
    void = game.status in (GameStatus.CANCELLED, GameStatus.POSTPONED)
    if not void and (game.status != GameStatus.FINAL or game.home_score is None or game.away_score is None):
        raise AppError(400, "GAME_NOT_FINAL", "Game must be final with scores (or cancelled)")

    selection_ids: list[uuid.UUID] = []
    for market in game.markets:
        for sel in market.selections:
            sel.result = (
                SelectionResult.VOID
                if void
                else grade(market.type, sel.side, sel.line, game.home_score, game.away_score)
            )
            sel.is_open = False
            selection_ids.append(sel.id)
        market.status = MarketStatus.SETTLED

    # Props: MVP leaves them pending (need actual player stats to grade). Void on cancel.
    if void:
        for prop in game.props:
            prop.result = SelectionResult.VOID
            prop.is_open = False

    legs = db.scalars(
        select(BetLeg)
        .join(Bet)
        .where(Bet.status == BetStatus.PENDING)
        .options(joinedload(BetLeg.selection), joinedload(BetLeg.prop))
    ).all()
    # Filter to legs that touch this game
    touched: dict[uuid.UUID, Bet] = {}
    for leg in legs:
        if leg.selection_id and leg.selection_id in selection_ids:
            sel = leg.selection
            leg.result = (
                SelectionResult.VOID
                if void
                else grade(sel.market.type, sel.side, leg.line, game.home_score, game.away_score)
            )
            touched[leg.bet_id] = leg.bet
        elif leg.prop_id and leg.prop and leg.prop.game_id == game.id:
            if void:
                leg.result = SelectionResult.VOID
                touched[leg.bet_id] = leg.bet
            # else leave pending until prop grading exists
    settled = sum(1 for bet in touched.values() if _try_settle_bet(db, bet))
    if commit:
        db.commit()
    else:
        db.flush()
    return settled


def _try_settle_bet(db: Session, bet: Bet) -> bool:
    results = [leg.result for leg in bet.legs]
    if SelectionResult.LOST in results:
        bet.status, bet.payout_cents = BetStatus.LOST, 0
    elif SelectionResult.PENDING in results:
        return False
    else:
        winners = [leg.odds_american for leg in bet.legs if leg.result == SelectionResult.WON]
        wallet = get_wallet(db, bet.user_id, for_update=True)
        if not winners:
            all_void = all(r == SelectionResult.VOID for r in results)
            bet.status = BetStatus.VOID if all_void else BetStatus.PUSH
            bet.payout_cents = bet.stake_cents
            post_transaction(
                db,
                wallet,
                WalletTxType.BET_REFUND,
                bet.stake_cents,
                bet_id=bet.id,
                description=f"Refund ({bet.status.value})",
            )
        else:
            dec = Fraction(1)
            for o in winners:
                dec *= american_to_decimal(o)
            bet.status = BetStatus.WON
            bet.payout_cents = floor(bet.stake_cents * dec)
            post_transaction(
                db,
                wallet,
                WalletTxType.BET_PAYOUT,
                bet.payout_cents,
                bet_id=bet.id,
                description="Winning bet payout",
            )
    bet.settled_at = datetime.now(UTC)
    return True
