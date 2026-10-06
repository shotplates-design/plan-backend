"""Dual-book prop shape: null books stay null; top-level falls back; book field names the source."""

from decimal import Decimal

from app.billing.paywall import Access, UsageState
from app.models.enums import SelectionSide
from app.models.sports import Prop, PropBook
from app.services.serializers import prop_out


def _prop(**kw) -> Prop:
    p = Prop(
        external_id=kw.get("id", "godwin-rec"),
        player="Chris Godwin",
        team_abbr="TB",
        market="Receptions",
        line_label="Over 4.5",
        line=Decimal("4.5"),
        side=SelectionSide.OVER,
        odds_american=-125,
        player_gsis_id=kw.get("gsis", "00-0033921"),
        line_source=kw.get("line_source", "plan_data_odds"),
        hit_rate_source="plan_data",
        hr_season_pct=50,
        hr_season_hit=2,
        hr_season_total=4,
        hr_last5_pct=50,
        hr_last5_hit=2,
        hr_last5_total=4,
        hr_vs_opp_pct=None,
        hr_vs_opp_hit=0,
        hr_vs_opp_total=0,
        hr_vs_opp_label="vs DAL",
        hr_vs_opp_season=2026,
        hr_vs_opp_opponent="DAL",
    )
    p.books = kw.get("books", [])
    return p


def _book(book, line, price, source="plan_data_odds") -> PropBook:
    return PropBook(
        book=book,
        line=None if line is None else Decimal(str(line)),
        price=price,
        side="over",
        source=source,
        hit_rate=None,
    )


def _access(theme: str | None = "fanduel") -> Access:
    return Access(
        state=UsageState(paywall_enabled=False, free_limit=10),
        preferred_book=theme,
    )


def test_missing_book_is_null_never_copied():
    p = _prop(books=[_book("fanduel", 3.5, 112)])
    out = prop_out(p, "tb-dal", _access("draftkings"))
    assert out.books.fanduel is not None and out.books.draftkings is None
    assert out.book == "fanduel"  # fallback
    assert out.line == "Over 3.5" and out.price == 112
    # Preferred was DK but DK is null — must NOT invent DK from FD
    assert out.books.draftkings is None


def test_preferred_book_used_when_both_present():
    p = _prop(books=[_book("fanduel", 5.5, -166), _book("draftkings", 6.5, 115)])
    fd = prop_out(p, "tb-dal", _access("fanduel"))
    assert fd.book == "fanduel" and fd.price == -166 and "5.5" in fd.line
    dk = prop_out(p, "tb-dal", _access("draftkings"))
    assert dk.book == "draftkings" and dk.price == 115 and "6.5" in dk.line
    assert fd.books.fanduel.line == 5.5 and fd.books.draftkings.line == 6.5


def test_is_sample_false_only_for_live_odds_books():
    live = prop_out(_prop(books=[_book("fanduel", 3.5, 112)]), "tb-dal", _access())
    assert live.isSample is False and live.lineSource == "plan_data_odds"
    sample = prop_out(
        _prop(
            line_source="sample",
            books=[
                _book("fanduel", 4.5, -125, source="sample"),
                _book("draftkings", 4.5, -120, source="sample"),
            ],
        ),
        "tb-dal",
        _access(),
    )
    assert sample.isSample is True and sample.lineSource == "sample"
    unmatched = prop_out(_prop(gsis=None, books=[_book("fanduel", 3.5, 112)]), "tb-dal", _access())
    assert unmatched.isSample is True


def test_anonymous_defaults_to_fanduel_then_draftkings():
    p = _prop(books=[_book("draftkings", 6.5, 115)])  # FD missing
    out = prop_out(p, "tb-dal", _access(None))
    assert out.book == "draftkings" and out.books.fanduel is None
