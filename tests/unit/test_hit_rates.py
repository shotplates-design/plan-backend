"""Hit-rate math: games_played denominator, zero rows = misses, last5 ordering, vsOpp honesty."""

from decimal import Decimal

import pytest

from app.models.enums import SelectionSide
from app.nfl_data.files import name_key, team
from app.nfl_data.hit_rates import (
    GameLog,
    compute_hit_rates,
    compute_vs_opp_season,
    is_hit,
    pct,
    season_label,
)


def g(week, value, opp="XXX", played=True, season=2026):
    return GameLog(season=season, week=week, opponent=opp, value=value, played=played)


def test_denominator_is_games_played_not_row_count():
    # 3 hit rows, but games_played says 4 → the 4th played game (missing row) is a miss.
    hr = compute_hit_rates([g(1, 6), g(2, 7), g(3, 8)], games_played=4, line=4.5)
    assert (hr.season.hit, hr.season.total, hr.season.pct) == (3, 4, 75)


def test_zero_stat_played_game_counts_as_miss():
    logs = [g(1, 5), g(2, 0), g(3, 6), g(4, 0)]  # weeks 2/4: zero-filled played games
    hr = compute_hit_rates(logs, games_played=4, line=0.5)
    assert (hr.season.hit, hr.season.total) == (2, 4)
    assert (hr.last5.hit, hr.last5.total) == (2, 4)


def test_unplayed_rows_never_count():
    logs = [g(1, 9), g(2, 9, played=False), g(3, 0)]
    hr = compute_hit_rates(logs, games_played=2, line=4.5)
    assert (hr.season.hit, hr.season.total) == (1, 2)
    assert hr.last5.total == 2


def test_more_rows_than_games_played_never_reports_hit_over_total():
    hr = compute_hit_rates([g(1, 9), g(2, 9)], games_played=1, line=4.5)
    assert hr.season.hit <= hr.season.total == 2


def test_last5_is_five_most_recent_by_week_regardless_of_input_order():
    # weeks 1-2 hit, weeks 3-7 miss; input shuffled
    logs = [g(5, 0), g(1, 10), g(7, 0), g(3, 0), g(2, 10), g(6, 0), g(4, 0)]
    hr = compute_hit_rates(logs, games_played=7, line=4.5)
    assert (hr.last5.hit, hr.last5.total) == (0, 5)
    assert (hr.season.hit, hr.season.total) == (2, 7)
    newest = [g(1, 0), g(2, 0), g(7, 10)]
    assert compute_hit_rates(newest, games_played=3, line=4.5).last5.hit == 1


def test_last5_orders_across_seasons():
    logs = [g(18, 10, season=2025), g(1, 0), g(2, 0), g(3, 0), g(4, 0), g(5, 0)]
    assert compute_hit_rates(logs, games_played=6, line=4.5).last5.hit == 0  # 2025 game is 6th-newest


def test_vs_opp_counts_only_that_opponent_and_is_honest_when_empty():
    logs = [g(1, 6, "DAL"), g(2, 2, "NYG"), g(3, 3, "DAL")]
    hr = compute_hit_rates(logs, games_played=3, line=4.5, opponent="DAL")
    assert (hr.vs_opp.hit, hr.vs_opp.total, hr.vs_opp.pct, hr.vs_opp_label) == (1, 2, 50, "vs DAL")
    none = compute_hit_rates(logs, games_played=3, line=4.5, opponent="TB")
    assert (none.vs_opp.hit, none.vs_opp.total, none.vs_opp.pct, none.vs_opp_label) == (0, 0, None, "vs TB")
    assert compute_hit_rates(logs, games_played=3, line=4.5).vs_opp is None  # no opponent


def test_no_games_played():
    hr = compute_hit_rates([], games_played=0, line=4.5, opponent="DAL")
    assert (hr.season.total, hr.season.pct, hr.last5.total, hr.last5.pct) == (0, None, 0, None)


@pytest.mark.parametrize(
    ("hit", "total", "expected"),
    [(3, 8, 38), (2, 3, 67), (1, 3, 33), (1, 2, 50), (0, 4, 0), (4, 4, 100), (0, 0, None)],
)
def test_pct_rounds_half_up(hit, total, expected):
    assert pct(hit, total) == expected


def test_over_under_and_push():
    assert is_hit(5, Decimal("4.5"), SelectionSide.OVER) and not is_hit(4, 4.5, SelectionSide.OVER)
    assert is_hit(0, 0.5, SelectionSide.UNDER)  # zero rows are hits for Unders
    assert not is_hit(5, 5, SelectionSide.OVER) and not is_hit(5, 5, SelectionSide.UNDER)  # push


def test_team_and_name_normalisation():
    assert team("LA") == "LAR" and team("KC") == "KC" and team("") is None
    assert name_key("Chris Godwin Jr.") == name_key("Chris Godwin") == "chris godwin"
    assert name_key("Ja'Marr Chase") == "ja marr chase" and name_key("A.J. Brown") == "a j brown"


# --------------------------------------------------------------------------- prior season vs opponent


def pg(week, value, opp="DAL", stype="REG", season=2025):
    return GameLog(season=season, week=week, opponent=opp, value=value, played=True, season_type=stype)


def test_vs_opp_season_regular_season_only():
    logs = [pg(3, 6), pg(14, 2), pg(20, 9, stype="POST")]  # playoff hit excluded
    b = compute_vs_opp_season(logs, season=2025, opponent="DAL", line=4.5)
    assert (b.hit, b.total, b.pct) == (1, 2, 50)


def test_vs_opp_season_zero_rows_are_misses_and_other_seasons_ignored():
    logs = [pg(3, 0), pg(14, 7), pg(2, 9, season=2026), pg(5, 9, opp="NYG")]
    b = compute_vs_opp_season(logs, season=2025, opponent="DAL", line=4.5)
    assert (b.hit, b.total) == (1, 2)


def test_vs_opp_season_empty_is_null_pct():
    b = compute_vs_opp_season([pg(1, 9, opp="NYG")], season=2025, opponent="DAL", line=4.5)
    assert (b.hit, b.total, b.pct) == (0, 0, None)


def test_season_label():
    assert season_label("DAL", 2025) == "vs DAL '25" and season_label("LAR", 2009) == "vs LAR '09"
