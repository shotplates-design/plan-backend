import uuid

from fastapi import APIRouter, Query, Response, status

from app.api.deps import DB, CurrentUser
from app.core.errors import ERROR_RESPONSES
from app.models.enums import BetStatus
from app.schemas.bets import BetOut, BetQuoteOut, BetSlipIn, PlaceBetRequest
from app.schemas.common import Page
from app.services import betting
from app.services.serializers import bet_out

router = APIRouter(prefix="/bets", tags=["bets"], responses=ERROR_RESPONSES)


@router.post("/quote", response_model=BetQuoteOut)
def quote_bet(body: BetSlipIn, user: CurrentUser, db: DB) -> BetQuoteOut:
    """Price a bet slip at current odds without placing it (powers the live bet slip)."""
    q, _ = betting.quote(db, body)
    return q


@router.post("", response_model=BetOut, status_code=status.HTTP_201_CREATED)
def place_bet(body: PlaceBetRequest, response: Response, user: CurrentUser, db: DB) -> BetOut:
    """Place a single or parlay bet.

    Validates selections are open, the submitted odds/lines still match (409 `ODDS_CHANGED`
    otherwise, unless `accept_odds_changes`), stake limits, and play-money balance.
    Debits the stake via the wallet ledger. Retrying with the same `idempotency_key` returns the
    original bet with HTTP 200.
    """
    bet, created = betting.place_bet(db, user.id, body)
    if not created:
        response.status_code = status.HTTP_200_OK
    return bet_out(bet)


@router.get("", response_model=Page[BetOut])
def list_my_bets(
    user: CurrentUser,
    db: DB,
    status: BetStatus | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> Page[BetOut]:
    bets, total = betting.list_bets(db, user.id, status, limit, offset)
    return Page[BetOut](items=[bet_out(b) for b in bets], total=total, limit=limit, offset=offset)


@router.get("/{bet_id}", response_model=BetOut)
def get_my_bet(bet_id: uuid.UUID, user: CurrentUser, db: DB) -> BetOut:
    return bet_out(betting.get_bet(db, user.id, bet_id))
