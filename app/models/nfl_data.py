"""Real player data from Plan Data (nflverse-derived), loaded by `python -m app.cli load-nfl-data`.

Hit-rate rules (Plan Data, 2026-10-06): the denominator is `games_played` from the season file;
only game rows with `played` = true count (>= 1 offensive snap or an offensive stat). Zero-stat
played games are present as zero rows and count as misses.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models._types import TimestampMixin, new_uuid, utcnow

_JSON = JSON().with_variant(JSONB, "postgresql")


class Player(TimestampMixin, Base):
    __tablename__ = "players"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    gsis_id: Mapped[str] = mapped_column(String(16), unique=True)  # nflverse player_id, "00-0033921"
    sleeper_id: Mapped[str | None] = mapped_column(String(16))
    full_name: Mapped[str] = mapped_column(String(80))
    name_key: Mapped[str] = mapped_column(String(80), index=True)  # normalised for matching
    first_name: Mapped[str | None] = mapped_column(String(40))
    last_name: Mapped[str | None] = mapped_column(String(40))
    team_abbr: Mapped[str | None] = mapped_column(String(4), index=True)  # our abbr (LA → LAR)
    position: Mapped[str | None] = mapped_column(String(8))
    position_group: Mapped[str | None] = mapped_column(String(8))
    depth_chart_position: Mapped[str | None] = mapped_column(String(8))
    jersey_number: Mapped[int | None] = mapped_column(SmallInteger)
    status: Mapped[str | None] = mapped_column(String(24))  # Active, Reserve/IR, Practice Squad, …
    status_code: Mapped[str | None] = mapped_column(String(8))  # ACT | RES | DEV | INA | EXE
    headshot_url: Mapped[str | None] = mapped_column(String(255))
    # players_active | season_stats (stats but not on the roster file) | history (prior-season logs only)
    roster_source: Mapped[str] = mapped_column(String(24), default="players_active")


_STAT_COLS = (
    "completions",
    "attempts",
    "passing_yards",
    "passing_tds",
    "passing_interceptions",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "receptions",
    "targets",
    "receiving_yards",
    "receiving_tds",
)


class _StatsMixin:
    completions: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    passing_yards: Mapped[int] = mapped_column(Integer, default=0)
    passing_tds: Mapped[int] = mapped_column(Integer, default=0)
    passing_interceptions: Mapped[int] = mapped_column(Integer, default=0)
    carries: Mapped[int] = mapped_column(Integer, default=0)
    rushing_yards: Mapped[int] = mapped_column(Integer, default=0)
    rushing_tds: Mapped[int] = mapped_column(Integer, default=0)
    receptions: Mapped[int] = mapped_column(Integer, default=0)
    targets: Mapped[int] = mapped_column(Integer, default=0)
    receiving_yards: Mapped[int] = mapped_column(Integer, default=0)
    receiving_tds: Mapped[int] = mapped_column(Integer, default=0)
    fantasy_points_ppr: Mapped[Decimal] = mapped_column(Numeric(7, 2), default=0)
    extra: Mapped[dict[str, Any] | None] = mapped_column(_JSON)  # remaining numeric columns


class PlayerGameStat(_StatsMixin, TimestampMixin, Base):
    """One PLAYED game (offense definition) for a player. Unplayed games never get a row."""

    __tablename__ = "player_game_stats"
    __table_args__ = (
        UniqueConstraint("player_id", "season", "season_type", "week", name="uq_player_game_week"),
        Index("ix_player_game_stats_player_season", "player_id", "season"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"))
    season: Mapped[int] = mapped_column(SmallInteger)
    season_type: Mapped[str] = mapped_column(String(8), default="REG")  # REG | POST
    game_type: Mapped[str | None] = mapped_column(String(8))  # REG | WC | DIV | CON | SB (when provided)
    week: Mapped[int] = mapped_column(SmallInteger)
    nfl_game_id: Mapped[str] = mapped_column(String(32))  # "2026_01_ARI_LAC"
    team_abbr: Mapped[str] = mapped_column(String(4))
    opponent_abbr: Mapped[str] = mapped_column(String(4))
    played: Mapped[bool] = mapped_column(Boolean, default=True)
    stats_row_imputed: Mapped[bool] = mapped_column(Boolean, default=False)  # zero-filled played game
    offense_snaps: Mapped[int | None] = mapped_column(SmallInteger)
    offense_snap_pct: Mapped[float | None] = mapped_column(Float)


class PlayerSeasonStat(_StatsMixin, TimestampMixin, Base):
    __tablename__ = "player_season_stats"
    __table_args__ = (UniqueConstraint("player_id", "season", "season_type", name="uq_player_season"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    season: Mapped[int] = mapped_column(SmallInteger)
    season_type: Mapped[str] = mapped_column(String(8), default="REG")
    team_abbr: Mapped[str | None] = mapped_column(String(4))
    games_played: Mapped[int] = mapped_column(SmallInteger)  # hit-rate denominator
    games_played_any_snaps: Mapped[int | None] = mapped_column(SmallInteger)
    team_games: Mapped[int | None] = mapped_column(SmallInteger)
    offense_snaps_total: Mapped[int | None] = mapped_column(Integer)


class PlayerMatchup(TimestampMixin, Base):
    """A player's current team and opponent for one upcoming week (weekN_{season}_matchups.csv)."""

    __tablename__ = "player_matchups"
    __table_args__ = (UniqueConstraint("player_id", "season", "week", name="uq_player_matchup_week"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    player_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("players.id", ondelete="CASCADE"), index=True)
    season: Mapped[int] = mapped_column(SmallInteger)
    week: Mapped[int] = mapped_column(SmallInteger)
    team_abbr: Mapped[str] = mapped_column(String(4))
    opponent_abbr: Mapped[str | None] = mapped_column(String(4))  # null on a bye
    is_bye: Mapped[bool] = mapped_column(Boolean, default=False)
    nfl_game_id: Mapped[str | None] = mapped_column(String(32))  # "2026_05_TB_DAL"
    home_away: Mapped[str | None] = mapped_column(String(8))
    kickoff_et: Mapped[str | None] = mapped_column(String(24))  # "2026-10-11 16:25" (America/New_York)
    has_prev_vs_opp: Mapped[bool | None] = mapped_column(Boolean)


class NflDataLoad(Base):
    """Audit row per `load-nfl-data` run (what was loaded, from where, validation results)."""

    __tablename__ = "nfl_data_loads"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=new_uuid)
    source: Mapped[str] = mapped_column(String(32), default="plan-nfl-data")
    source_dir: Mapped[str] = mapped_column(String(255))
    season: Mapped[int] = mapped_column(SmallInteger)
    through_week: Mapped[int] = mapped_column(SmallInteger)
    file_hashes: Mapped[dict[str, Any]] = mapped_column(_JSON)
    counts: Mapped[dict[str, Any]] = mapped_column(_JSON)
    warnings: Mapped[list[Any]] = mapped_column(_JSON)
    loaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


STAT_COLUMNS = _STAT_COLS
