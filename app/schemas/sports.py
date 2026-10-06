"""API shapes aligned with plan-frontend/mock/data.js (Next.js contract)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import Field

from app.models.enums import GameStatus, SeasonType, Theme
from app.schemas.common import APIModel


class TeamBrief(APIModel):
    """Board team blob — matches mock `{ abbr, name, short, color }`."""

    abbr: str
    name: str
    short: str
    color: str | None = None


class TeamOut(APIModel):
    """Full team record (teams index)."""

    id: UUID
    abbr: str
    location: str
    name: str
    conference: str
    division: str
    primary_color: str | None


class SpreadSideOut(APIModel):
    line: str = Field(description='American-style line string, e.g. "+7.5" or "-3"')
    price: int = Field(description="American odds")


class SpreadOut(APIModel):
    away: SpreadSideOut
    home: SpreadSideOut


class MoneyOut(APIModel):
    away: int
    home: int


class TotalOut(APIModel):
    line: float
    over: int
    under: int


class HitRateBucket(APIModel):
    pct: int | None = Field(description="Whole percent, round half up; null when total is 0")
    hit: int
    total: int = Field(description="Season: games_played (played games only). last5: up to 5 most recent")


class HitRateVsOpp(HitRateBucket):
    label: str = Field(description='"vs DAL" (current season) or "vs DAL \'25" (prior season)')
    season: int | None = Field(None, description="Season the games come from, e.g. 2026 (vsOpp) / 2025")
    opponent: str | None = Field(None, description='Opponent team abbr, e.g. "DAL"')


class HitRateOut(APIModel):
    season: HitRateBucket | None = None
    last5: HitRateBucket | None = None
    vsOpp: HitRateVsOpp | None = Field(None, description="Current season vs this week's opponent")
    vsOppLastSeason: HitRateVsOpp | None = Field(
        None,
        description="Prior REGULAR season vs the same opponent, against this line (playoffs excluded). "
        "total 0 → pct null. null when no prior-season data is loaded.",
    )


class BookLineOut(APIModel):
    """One sportsbook's quote for a prop. `line` is numeric (null for anytime TD)."""

    line: float | None = Field(description="O/U threshold; null for anytime_td Yes/No")
    price: int = Field(description="American odds")
    hitRate: HitRateOut | None = None


class PropBooksOut(APIModel):
    """Per-book quotes. A missing book is null and MUST NEVER be copied from the other."""

    fanduel: BookLineOut | None = None
    draftkings: BookLineOut | None = None


class PropOut(APIModel):
    """Player prop — Plan's hit-rate differentiator. Matches mock prop objects."""

    id: str = Field(description='Public prop id, e.g. "godwin-rec"')
    player: str
    team: str = Field(description="Team abbr")
    market: str = Field(description='e.g. "Receptions", "Receiving Yards", "Passing Yards"')
    line: str = Field(description='e.g. "Over 4.5" (from the preferred book; see `book`)')
    price: int = Field(description="American odds from the preferred book (see `book`)")
    hitRate: HitRateOut | None = None
    books: PropBooksOut = Field(
        default_factory=PropBooksOut,
        description="FanDuel and DraftKings quotes; null means that book has no line",
    )
    book: str | None = Field(
        None,
        description='Which book top-level line/price/hitRate came from: "fanduel" | "draftkings". '
        "Frontend shows a tag when this differs from the user's skin.",
    )
    selectionKey: str = Field(
        description='Bet-slip key: "{gameId}|prop|{id}". Pass as selection_key on POST /bets.'
    )
    playerId: str | None = Field(None, description="GSIS player id (nflverse), when matched to real data")
    isSample: bool = Field(
        True,
        description="False only when playerId is set AND at least one non-sample book quote is present",
    )
    lineSource: str = Field(
        "sample", description='Where line/price come from: "sample" | "odds_api" | "plan_data_odds"'
    )
    hitRateSource: str = Field(
        "sample", description='"plan_data" (real game logs) | "sample" (not yet loaded) | "none" (no data)'
    )
    hitRateThroughWeek: int | None = Field(None, description="Last completed week included in hit rates")
    unlocked: bool = Field(
        True,
        description="False when the paywall hides this prop's full hit rates for the caller. "
        "Open GET /games/{gameId}/props/{id} to unlock (1 try; re-opening the same ET day is free).",
    )
    locked: list[str] = Field(
        default_factory=list,
        description='Hit-rate fields withheld until unlocked ("season", "vs_opponent", '
        '"vs_opponent_last_season", and "last5" when PAYWALL_BOARD_TEASER=none); the corresponding '
        "hitRate entries are null.",
    )


class PropDetailOut(PropOut):
    gameId: str
    charged: bool = Field(description="True if this request consumed a try (false on a same-day re-open)")
    bucket: str | None = Field(None, description="free | paid | unlimited — what paid for the unlock")
    usage: UsageOut


class HitRateWindowOut(APIModel):
    propId: str
    window: str
    hitRate: HitRateBucket | None


class GameBoardOut(APIModel):
    """Weekly board / game-detail shape — mirrors plan-frontend/mock/data.js."""

    id: str = Field(description='Public game id (slug), e.g. "tb-dal" / "kc-buf"')
    uuid: UUID = Field(description="Internal UUID (stable across re-seeds of same slug)")
    away: TeamBrief
    home: TeamBrief
    kickoff: datetime = Field(description="ISO-8601 kickoff (tz-aware)")
    kickoffLabel: str | None = None
    dateGroup: str | None = None
    week: int
    season: int
    seasonType: SeasonType
    status: GameStatus
    venue: str | None = None
    homeScore: int | None = None
    awayScore: int | None = None
    spread: SpreadOut | None = None
    money: MoneyOut | None = None
    total: TotalOut | None = None
    nflGameId: str | None = Field(
        None, description='Real schedule id, e.g. "2026_05_BUF_LA" (null for sample games)'
    )
    source: str = Field("sample", description='"plan_data" (real schedule) | "sample" (mock slate)')
    linesSource: str = Field(
        "sample",
        description='Spread/total/moneyline source: "nflverse_schedule" | "sample" | "mixed" | "none"',
    )
    isSampleLines: bool = Field(
        True, description="True unless every game line comes from the real schedule file"
    )
    props: list[PropOut] = Field(default_factory=list)


class PreferencesOut(APIModel):
    theme: Theme | None = Field(
        description="Sportsbook look-and-feel. null → frontend shows the skin picker."
    )


class PreferencesUpdate(APIModel):
    theme: Theme


from app.schemas.billing import UsageOut  # noqa: E402

PropDetailOut.model_rebuild()
