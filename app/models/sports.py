import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._types import TimestampMixin, new_uuid, str_enum
from app.models.enums import (
    GameStatus,
    MarketStatus,
    MarketType,
    SeasonType,
    SelectionResult,
    SelectionSide,
)


class Team(TimestampMixin, Base):
    __tablename__ = "teams"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    abbr: Mapped[str] = mapped_column(String(4), unique=True, index=True)  # e.g. "KC"
    location: Mapped[str] = mapped_column(String(64))  # e.g. "Kansas City"
    name: Mapped[str] = mapped_column(String(64))  # e.g. "Chiefs"
    conference: Mapped[str] = mapped_column(String(3))  # AFC / NFC
    division: Mapped[str] = mapped_column(String(8))  # North/South/East/West
    primary_color: Mapped[str | None] = mapped_column(String(7))


class Game(TimestampMixin, Base):
    __tablename__ = "games"
    __table_args__ = (CheckConstraint("home_team_id <> away_team_id", name="ck_games_distinct_teams"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    external_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)  # provider id
    season: Mapped[int] = mapped_column(SmallInteger, index=True)
    season_type: Mapped[SeasonType] = mapped_column(str_enum(SeasonType, 4), default=SeasonType.REG)
    week: Mapped[int] = mapped_column(SmallInteger, index=True)
    home_team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id"))
    kickoff_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    venue: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[GameStatus] = mapped_column(str_enum(GameStatus, 16), default=GameStatus.SCHEDULED)
    home_score: Mapped[int | None] = mapped_column(SmallInteger)
    away_score: Mapped[int | None] = mapped_column(SmallInteger)
    # Provenance. source: sample (mock slate) | plan_data (real schedule, schedule_games_raw.csv).
    nfl_game_id: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)  # "2026_05_BUF_LA"
    source: Mapped[str] = mapped_column(String(16), default="sample", server_default="sample")
    # Game lines: nflverse_schedule | sample | mixed (some markets real, some sample)
    lines_source: Mapped[str | None] = mapped_column(String(24))
    # Archived games (e.g. a retired sample slate) are hidden from /games and closed to bets.
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archive_reason: Mapped[str | None] = mapped_column(String(48))

    home_team: Mapped[Team] = relationship(foreign_keys=[home_team_id], lazy="joined")
    away_team: Mapped[Team] = relationship(foreign_keys=[away_team_id], lazy="joined")
    markets: Mapped[list["Market"]] = relationship(
        back_populates="game", cascade="all, delete-orphan", order_by="Market.type"
    )
    props: Mapped[list["Prop"]] = relationship(
        back_populates="game",
        cascade="all, delete-orphan",
        order_by="Prop.market, Prop.player",
    )


class Market(TimestampMixin, Base):
    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("game_id", "type", "period", name="uq_market_game_type"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    type: Mapped[MarketType] = mapped_column(str_enum(MarketType, 16))
    period: Mapped[str] = mapped_column(String(16), default="full_game")
    status: Mapped[MarketStatus] = mapped_column(str_enum(MarketStatus, 16), default=MarketStatus.OPEN)
    source: Mapped[str] = mapped_column(String(24), default="sample", server_default="sample")

    game: Mapped[Game] = relationship(back_populates="markets")
    selections: Mapped[list["Selection"]] = relationship(
        back_populates="market", cascade="all, delete-orphan", order_by="Selection.side"
    )


class Selection(TimestampMixin, Base):
    """A bettable outcome within a market (a.k.a. Outcome)."""

    __tablename__ = "selections"
    __table_args__ = (
        UniqueConstraint("market_id", "side", name="uq_selection_market_side"),
        CheckConstraint("odds_american >= 100 OR odds_american <= -100", name="ck_selection_valid_odds"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    market_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    side: Mapped[SelectionSide] = mapped_column(str_enum(SelectionSide, 8))
    name: Mapped[str] = mapped_column(String(64))  # e.g. "KC Chiefs", "Over"
    # Spread: points for this side (e.g. -3.5). Total: the total (e.g. 47.5). Moneyline: NULL.
    line: Mapped[Decimal | None] = mapped_column(Numeric(5, 1))
    odds_american: Mapped[int] = mapped_column(Integer)
    is_open: Mapped[bool] = mapped_column(default=True)
    result: Mapped[SelectionResult] = mapped_column(
        str_enum(SelectionResult, 8), default=SelectionResult.PENDING
    )

    market: Mapped[Market] = relationship(back_populates="selections")


class Prop(TimestampMixin, Base):
    """Player prop with Plan hit-rate chips (the product differentiator).

    `external_id` is the public prop id used by the frontend (e.g. "godwin-rec").
    Selection key for the bet slip: `{game.external_id}|prop|{external_id}`.
    """

    __tablename__ = "props"
    __table_args__ = (
        UniqueConstraint("game_id", "external_id", name="uq_prop_game_external"),
        CheckConstraint("odds_american >= 100 OR odds_american <= -100", name="ck_prop_valid_odds"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    game_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("games.id", ondelete="CASCADE"), index=True)
    external_id: Mapped[str] = mapped_column(String(64), index=True)  # "godwin-rec"
    player: Mapped[str] = mapped_column(String(80))
    team_abbr: Mapped[str] = mapped_column(String(4))
    market: Mapped[str] = mapped_column(String(48))  # "Receptions", "Passing Yards", ...
    line_label: Mapped[str] = mapped_column(String(32))  # "Over 4.5"
    line: Mapped[Decimal] = mapped_column(Numeric(6, 1))  # 4.5
    side: Mapped[SelectionSide] = mapped_column(str_enum(SelectionSide, 8), default=SelectionSide.OVER)
    odds_american: Mapped[int] = mapped_column(Integer)
    is_open: Mapped[bool] = mapped_column(default=True)
    result: Mapped[SelectionResult] = mapped_column(
        str_enum(SelectionResult, 8), default=SelectionResult.PENDING
    )

    # Hit-rate chips (sample / computed by data agent). Nullable when unknown.
    hr_season_pct: Mapped[int | None] = mapped_column(SmallInteger)
    hr_season_hit: Mapped[int | None] = mapped_column(SmallInteger)
    hr_season_total: Mapped[int | None] = mapped_column(SmallInteger)
    hr_last5_pct: Mapped[int | None] = mapped_column(SmallInteger)
    hr_last5_hit: Mapped[int | None] = mapped_column(SmallInteger)
    hr_last5_total: Mapped[int | None] = mapped_column(SmallInteger)
    hr_vs_opp_pct: Mapped[int | None] = mapped_column(SmallInteger)
    hr_vs_opp_hit: Mapped[int | None] = mapped_column(SmallInteger)
    hr_vs_opp_total: Mapped[int | None] = mapped_column(SmallInteger)
    hr_vs_opp_label: Mapped[str | None] = mapped_column(String(16))  # "vs DAL"
    hr_vs_opp_season: Mapped[int | None] = mapped_column(SmallInteger)  # 2026
    hr_vs_opp_opponent: Mapped[str | None] = mapped_column(String(4))  # "DAL" (also used by last season)
    # matchups (player's real upcoming opponent) | sample_game (other team on our game) | sample
    hr_opponent_source: Mapped[str | None] = mapped_column(String(16))
    # Prior regular season vs the same opponent, against this prop's line.
    hr_vs_opp_last_pct: Mapped[int | None] = mapped_column(SmallInteger)
    hr_vs_opp_last_hit: Mapped[int | None] = mapped_column(SmallInteger)
    hr_vs_opp_last_total: Mapped[int | None] = mapped_column(SmallInteger)
    hr_vs_opp_last_label: Mapped[str | None] = mapped_column(String(16))  # "vs DAL '25"
    hr_vs_opp_last_season: Mapped[int | None] = mapped_column(SmallInteger)  # 2025

    # Provenance (real data loader). line/price are SAMPLE until a lines feed exists.
    player_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("players.id", ondelete="SET NULL"), index=True
    )
    player_gsis_id: Mapped[str | None] = mapped_column(String(16))  # denormalised for serializers
    stat_key: Mapped[str | None] = mapped_column(String(32))  # "receptions", "passing_yards", …
    line_source: Mapped[str] = mapped_column(String(24), default="sample", server_default="sample")
    # sample | plan_data | none (player not found → hit rates null)
    hit_rate_source: Mapped[str] = mapped_column(String(16), default="sample", server_default="sample")
    hit_rate_season: Mapped[int | None] = mapped_column(SmallInteger)
    hit_rate_through_week: Mapped[int | None] = mapped_column(SmallInteger)
    # ok | player_not_found | unsupported_market (listed) |
    # player_not_in_game | player_on_bye | player_inactive (hidden from listings, closed)
    data_status: Mapped[str] = mapped_column(String(24), default="ok", server_default="ok")

    game: Mapped[Game] = relationship(back_populates="props")
    books: Mapped[list["PropBook"]] = relationship(
        back_populates="prop", cascade="all, delete-orphan", order_by="PropBook.book"
    )

_JSON = JSON().with_variant(JSONB, "postgresql")


class PropBook(TimestampMixin, Base):
    """Per-sportsbook line/price for a prop (FanDuel / DraftKings).

    Missing book = no row (API returns null). NEVER copy one book's line onto the other.
    `hit_rate` is the HitRateOut-shaped blob for THIS book's line (recomputed when lines differ).
    """

    __tablename__ = "prop_books"
    __table_args__ = (UniqueConstraint("prop_id", "book", name="uq_prop_book"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    prop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("props.id", ondelete="CASCADE"), index=True)
    book: Mapped[str] = mapped_column(String(16))  # fanduel | draftkings
    line: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))  # null for anytime_td
    price: Mapped[int] = mapped_column(Integer)
    side: Mapped[str] = mapped_column(String(8), default="over")  # over | under | yes | no
    hit_rate: Mapped[dict | None] = mapped_column(_JSON)  # HitRateOut-shaped; null → use prop's chips
    source: Mapped[str] = mapped_column(String(24), default="sample", server_default="sample")
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    prop: Mapped[Prop] = relationship(back_populates="books")
