import uuid
from datetime import datetime
from typing import Self

from pydantic import Field, model_validator

from app.models.enums import BetStatus, BetType, SelectionResult
from app.schemas.common import APIModel


class BetLegIn(APIModel):
    """One leg of a slip.

    Prefer `selection_key` matching the frontend mock:
      "{gameId}|spread|away" | "{gameId}|money|home" | "{gameId}|total|over"
      "{gameId}|prop|{propId}"
    """

    selection_key: str | None = Field(
        default=None,
        max_length=128,
        description='Frontend key, e.g. "tb-dal|spread|away" or "tb-dal|prop|godwin-rec"',
    )
    selection_id: uuid.UUID | None = Field(
        default=None, description="Internal UUID (alternate to selection_key)"
    )
    odds_american: int = Field(description="Price the user saw on the slip")
    line: str | float | None = Field(
        default=None,
        description='Line the user saw (e.g. "+7.5", "Over 4.5", or 47.5 for totals)',
    )

    @model_validator(mode="after")
    def _need_key(self) -> Self:
        if not self.selection_key and not self.selection_id:
            raise ValueError("Provide selection_key or selection_id")
        return self


class BetSlipIn(APIModel):
    type: BetType
    stake_cents: int = Field(gt=0, description="Stake in play-money cents (100 = 1.00)")
    legs: list[BetLegIn] = Field(min_length=1)
    accept_odds_changes: bool = Field(
        default=False,
        description="If true, place at current odds even if they moved; otherwise 409 ODDS_CHANGED",
    )

    @model_validator(mode="after")
    def _legs_match_type(self) -> Self:
        if self.type == BetType.SINGLE and len(self.legs) != 1:
            raise ValueError("single bets must have exactly 1 leg")
        if self.type == BetType.PARLAY and len(self.legs) < 2:
            raise ValueError("parlays must have at least 2 legs")
        keys = [(leg.selection_key, str(leg.selection_id)) for leg in self.legs]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate selection in legs")
        return self


class PlaceBetRequest(BetSlipIn):
    idempotency_key: str | None = Field(
        default=None, max_length=64, description="Client-generated key; retries return the same bet"
    )


class GameBrief(APIModel):
    id: str  # public slug
    home_abbr: str
    away_abbr: str
    kickoff: datetime


class QuoteLegOut(APIModel):
    selection_key: str
    selection_id: uuid.UUID | None = None
    market: str  # "Spread" | "Moneyline" | "Total" | prop market name
    selection_name: str
    line: str | None
    odds_american: int
    requested_odds_american: int
    odds_changed: bool
    game: GameBrief


class BetQuoteOut(APIModel):
    type: BetType
    stake_cents: int
    odds_american: int
    odds_decimal: float
    potential_payout_cents: int
    potential_profit_cents: int
    any_odds_changed: bool
    legs: list[QuoteLegOut]


class BetLegOut(APIModel):
    id: uuid.UUID
    selection_key: str
    market: str
    selection_name: str
    line: str | None
    odds_american: int
    result: SelectionResult
    game: GameBrief


class BetOut(APIModel):
    id: uuid.UUID
    type: BetType
    status: BetStatus
    stake_cents: int
    odds_american: int
    odds_decimal: float
    potential_payout_cents: int
    payout_cents: int | None
    placed_at: datetime
    settled_at: datetime | None
    legs: list[BetLegOut]
