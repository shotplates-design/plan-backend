"""Management CLI.

python -m app.cli seed [--shift-to-upcoming]
python -m app.cli sync --season 2026 --week 5
python -m app.cli settle --game <external_id> --home 24 --away 17
"""

from __future__ import annotations

import argparse
import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Game
from app.models.enums import GameStatus
from app.odds_provider import get_odds_provider
from app.odds_provider.mock import SAMPLE_SEASON, SAMPLE_WEEK, MockOddsProvider
from app.services.betting import settle_game
from app.services.odds_sync import sync_week


def _upcoming_shift() -> timedelta:
    """Whole-week shift so the sample slate's first kickoff is at least ~1 day away."""
    first = MockOddsProvider.first_kickoff()
    target = datetime.now(UTC) + timedelta(days=1)
    if first >= target:
        return timedelta(0)
    weeks = math.ceil((target - first) / timedelta(weeks=1))
    return timedelta(weeks=weeks)


def cmd_seed(args: argparse.Namespace) -> None:
    shift = _upcoming_shift() if args.shift_to_upcoming else timedelta(0)
    provider = MockOddsProvider(shift=shift)
    with SessionLocal() as db:
        res = sync_week(db, provider, SAMPLE_SEASON, SAMPLE_WEEK)
    print("Seeded SAMPLE data (mock provider — NOT real odds, NOT an official schedule)")
    print(f"  season={SAMPLE_SEASON} week={SAMPLE_WEEK} kickoff shift={shift}")
    print(
        f"  teams={res.teams} games={res.games} markets={res.markets} "
        f"selections={res.selections} props={res.props}"
    )
    if res.skipped_games:
        print(
            f"  skipped {res.skipped_games} game(s) that are real (load-nfl-data) or archived; "
            "re-run load-nfl-data after seeding"
        )


def cmd_sync(args: argparse.Namespace) -> None:
    provider = get_odds_provider(get_settings().odds_provider)
    with SessionLocal() as db:
        res = sync_week(db, provider, args.season, args.week)
    print(f"Synced via {provider.name}: games={res.games} markets={res.markets}")


def cmd_settle(args: argparse.Namespace) -> None:
    with SessionLocal() as db:
        game = db.scalars(select(Game).where(Game.external_id == args.game)).one_or_none()
        if game is None:
            raise SystemExit(f"No game with external_id={args.game!r}")
        if args.cancel:
            game.status = GameStatus.CANCELLED
        else:
            game.status = GameStatus.FINAL
            game.home_score, game.away_score = args.home, args.away
        n = settle_game(db, game)
    print(f"Settled {game.external_id}: {n} bet(s) settled")


def cmd_stripe_setup(args: argparse.Namespace) -> None:
    from app.billing.guard import StripeKeyError
    from app.billing.stripe_setup import run_setup

    try:
        report = run_setup(dry_run=args.dry_run)
    except StripeKeyError as exc:
        raise SystemExit(f"stripe-setup refused: {exc.code}: {exc.message}") from None
    print(
        f"\nDone ({'dry run' if report.dry_run else 'test mode'}): "
        f"{report.created} object(s) created, {len(report.warnings)} warning(s)."
    )


def cmd_grant_tries(args: argparse.Namespace) -> None:
    """Comp a user: add paid tries and/or unlimited access (ledger kind=grant). Local DB only."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.billing.paywall import credit_tries, get_account
    from app.models import User
    from app.models.enums import LedgerKind

    with SessionLocal() as db:
        user = db.scalars(select(User).where(User.email == args.email.lower())).first()
        if user is None:
            raise SystemExit(f"No user with email {args.email!r}")
        if args.tries:
            credit_tries(db, user.id, args.tries, kind=LedgerKind.GRANT, note=args.note or "cli grant")
        acct = get_account(db, user.id, lock=True)
        if args.unlimited_days:
            acct.unlimited_until = datetime.now(UTC) + timedelta(days=args.unlimited_days)
        db.commit()
        print(f"{user.email}: paid_balance={acct.paid_balance} unlimited_until={acct.unlimited_until}")


def cmd_load_nfl_data(args: argparse.Namespace) -> None:
    """Load Plan Data's real player stats and recompute prop hit rates (idempotent)."""
    import json

    from app.nfl_data.files import DataError, read_dataset
    from app.nfl_data.loader import load_dataset

    try:
        ds = read_dataset(args.dir, season=args.season, schedule_week=args.schedule_week)
    except DataError as exc:
        raise SystemExit(f"load-nfl-data: {exc}") from None
    mismatches = sum(ds.validation.values())
    if mismatches and args.strict:
        raise SystemExit(
            f"load-nfl-data: validation failed ({ds.validation}); see warnings:\n" + "\n".join(ds.warnings)
        )
    with SessionLocal() as db:
        try:
            report = load_dataset(db, ds, dry_run=args.dry_run, strict=args.strict)
        except DataError as exc:
            raise SystemExit(f"load-nfl-data: {exc}") from None
    mode = "DRY RUN (rolled back)" if args.dry_run else "loaded"
    print(f"Plan Data {mode}: season={report.season} through week {report.through_week} from {ds.directory}")
    for k, v in report.counts.items():
        print(f"  {k}: {v}")
    print(f"  validation: {report.validation}")
    s = report.sanity["wr_te_1plus_catch"]
    print(
        f"  sanity: WR/TE 1+ catch = {s['hit']}/{s['total']} = {s['rate']:.1%}"
        if s["total"]
        else "  sanity: n/a"
    )
    for w in report.warnings:
        print(f"  WARN {w}")
    for m in report.moves:
        print(f"  MOVE {m['prop']}: {m['from']} -> {m['to']}")
    for g in report.gaps:
        print(f"  GAP  {json.dumps(g)}")


def main() -> None:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("seed", help="Load the sample NFL week from the mock odds provider")
    s.add_argument(
        "--shift-to-upcoming",
        action="store_true",
        help="Move sample kickoffs forward by whole weeks so they are in the future",
    )
    s.set_defaults(func=cmd_seed)

    y = sub.add_parser("sync", help="Sync a week from the configured odds provider")
    y.add_argument("--season", type=int, required=True)
    y.add_argument("--week", type=int, required=True)
    y.set_defaults(func=cmd_sync)

    t = sub.add_parser("settle", help="Record a final score (or cancel) and settle bets")
    t.add_argument("--game", required=True, help="Game external_id")
    t.add_argument("--home", type=int, default=0)
    t.add_argument("--away", type=int, default=0)
    t.add_argument("--cancel", action="store_true")
    t.set_defaults(func=cmd_settle)

    st = sub.add_parser(
        "stripe-setup", help="Idempotently create Stripe TEST products/prices for credit packs"
    )
    st.add_argument("--dry-run", action="store_true", help="Print the plan; no Stripe calls")
    st.set_defaults(func=cmd_stripe_setup)

    n = sub.add_parser(
        "load-nfl-data", help="Load Plan Data real stats + recompute prop hit rates (idempotent)"
    )
    n.add_argument("--dir", default="/workspace/plan-nfl-data", help="Plan Data directory (top-level files)")
    n.add_argument("--season", type=int, default=None, help="Default: _meta.json season")
    n.add_argument("--dry-run", action="store_true", help="Validate + compute, then roll back")
    n.add_argument(
        "--schedule-week",
        type=int,
        default=None,
        help="Week to import from schedule_games_raw.csv (default: matchups week, else through_week + 1)",
    )
    n.add_argument(
        "--strict",
        action="store_true",
        help="Fail (write nothing) if any validation/cross-check count is non-zero",
    )
    n.set_defaults(func=cmd_load_nfl_data)

    g = sub.add_parser("grant-tries", help="Comp a user paid tries and/or unlimited days (no Stripe)")
    g.add_argument("--email", required=True)
    g.add_argument("--tries", type=int, default=0)
    g.add_argument("--unlimited-days", type=int, default=0)
    g.add_argument("--note", default=None)
    g.set_defaults(func=cmd_grant_tries)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
