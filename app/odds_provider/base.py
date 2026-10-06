"""Odds-provider contract.

The data agent / a real vendor integration implements `OddsProvider`. The backend only depends on
these plain dataclasses; `app.services.odds_sync.sync_week` upserts them into the database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.models.enums import GameStatus, MarketType, SeasonType, SelectionSide


@dataclass(frozen=True)
class ProviderTeam:
    abbr: str
    location: str
    name: str
    conference: str
    division: str
    primary_color: str | None = None


@dataclass(frozen=True)
class ProviderSelection:
    side: SelectionSide
    odds_american: int
    line: Decimal | None = None  # spread points for this side / total points; None for ML
    name: str | None = None  # defaults derived from team / side if omitted


@dataclass(frozen=True)
class ProviderMarket:
    type: MarketType
    selections: list[ProviderSelection]
    period: str = "full_game"


@dataclass(frozen=True)
class ProviderGame:
    external_id: str
    season: int
    week: int
    home_abbr: str
    away_abbr: str
    kickoff_at: datetime  # tz-aware
    season_type: SeasonType = SeasonType.REG
    venue: str | None = None
    status: GameStatus = GameStatus.SCHEDULED
    home_score: int | None = None
    away_score: int | None = None
    markets: list[ProviderMarket] = field(default_factory=list)


@runtime_checkable
class OddsProvider(Protocol):
    name: str
    is_sample_data: bool

    def get_teams(self) -> list[ProviderTeam]:
        """All teams the provider knows about."""
        ...

    def get_week(
        self, season: int, week: int, season_type: SeasonType = SeasonType.REG
    ) -> list[ProviderGame]:
        """Schedule + current odds (markets) for one week."""
        ...
