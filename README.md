# Plan backend

NFL-first sports betting API for **Plan** (play money only). Users pick a sportsbook look-and-feel (`fanduel` | `draftkings`); a separate frontend agent owns the UI and a separate data agent owns live odds / hit rates.

> **Stack:** Python 3.11+ · FastAPI · SQLAlchemy 2.0 · Alembic · Postgres · Pydantic v2 · argon2id · JWT (+ httpOnly cookie) · pytest · ruff · uv

## Quick start

```bash
cd /workspace/plan-backend

# 1. Python deps
uv sync

# 2. Env
cp .env.example .env   # JWT_SECRET is already randomised if you keep the generated .env

# 3. Database — pick one:
#    a) Docker (if available):  docker compose up -d db
#    b) Local Postgres (this box uses this): role/db `plan`/`plan`, password `plan`
#    c) SQLite fallback: set DATABASE_URL=sqlite:///./plan.db in .env

# 4. Migrate + seed SAMPLE week (clearly labeled — not real odds)
uv run alembic upgrade head
uv run python -m app.cli seed --shift-to-upcoming
# 4b. REAL week-5 schedule + game lines, real player stats + hit rates from Plan Data.
#     Retires (archives) the sample slate and moves props to their players' real games.
#     Run it AFTER seed (the seed provides the sample props); re-run it after any re-seed.
uv run python -m app.cli load-nfl-data --dir /workspace/plan-nfl-data

# 5. Dev server
uv run uvicorn app.main:app --reload --port 8000
# Docs: http://localhost:8000/docs
# OpenAPI JSON: http://localhost:8000/openapi.json  (also exported to ./openapi.json)
```

Secrets stay in environment variables (or a local `.env` copied from `.env.example`). Commit `.env.example` only — never commit `.env`, `JWT_SECRET`, the Odds API key, or Stripe keys.

### Scripts

| Command | Purpose |
|---------|---------|
| `uv sync` | Install deps (runtime + dev) |
| `uv run alembic upgrade head` | Apply migrations |
| `uv run python -m app.cli seed [--shift-to-upcoming]` | Load mock NFL week + props |
| `uv run python -m app.cli sync --season 2026 --week 5` | Re-sync from configured provider |
| `uv run python -m app.cli settle --game tb-dal --home 24 --away 17` | Grade a final and settle bets |
| `uv run uvicorn app.main:app --reload --port 8000` | Dev server |
| `uv run pytest` | Unit + integration tests |
| `uv run ruff check .` / `uv run ruff format .` | Lint / format |
| `uv run python scripts/export_openapi.py` | Write `openapi.json` for the frontend |
| `uv run python -m app.cli stripe-setup [--dry-run]` | Idempotently create the credit-pack Products/Prices in Stripe **test mode** (`scripts/stripe_setup.py` is an alias) |
| `uv run python -m app.cli load-nfl-data [--dir D] [--season Y] [--dry-run] [--strict]` | Idempotently load Plan Data players + per-game stats and recompute prop hit rates |
| `uv run python -m app.cli grant-tries --email you@x.com --tries 30 [--unlimited-days 7]` | Comp a user (no Stripe) |

## Architecture

```
app/
  main.py              FastAPI app + CORS + error handlers
  config.py            pydantic-settings (env / .env)
  db.py                SQLAlchemy engine + session
  cli.py               seed / sync / settle
  api/v1/              REST routers under /api/v1
  models/              SQLAlchemy ORM (User, Game, Market, Selection, Prop, Bet, Wallet…)
  schemas/             Pydantic request/response (frontend-aligned board shape)
  services/            auth, wallet ledger, betting, odds sync, serializers
  nfl_data/            Plan Data CSV reader/validator, loader, pure hit-rate math
  billing/             paywall (free tries + credits), Stripe guard/client/webhooks/sync, setup script
  core/                odds math, security (argon2 + JWT), errors
  odds_provider/       OddsProvider protocol + MockOddsProvider (+ sample props)
alembic/               migrations
tests/                 unit (odds, grading, paywall, Stripe guard, hit rates) + integration (auth, bets, billing, nfl data loader)
```

**Money** is integer **cents** of play currency (`PLAY`). The wallet is an append-only ledger (`wallet_transactions`); `wallets.balance_cents` is a cached sum updated in the same DB transaction as each bet.

**Odds** are American. Math lives in `app/core/odds.py` (exact `Fraction`s, payouts floored to the cent).

**Odds provider:** `OddsProvider` protocol with a `MockOddsProvider` that serves one SAMPLE 2026 REG week (14 games + 30 player props with hit-rate chips mirrored from `plan-frontend/mock/data.js`). A real feed from the data agent plugs in the same interface.

`load-nfl-data` then replaces the sample slate with the **real 2026 week-5 schedule and game lines**, and player identity and hit rates with real game logs. **Prop** lines and prices stay sample. See [Real player data](#real-player-data-plan-data). Once a game is real or archived, `seed`/`sync` skip it (`skipped_games`).

### Frontend contract (board)

`GET /api/v1/games` returns objects shaped like the static mock:

```json
{
  "id": "tb-dal",
  "away": { "abbr": "TB", "name": "Buccaneers", "short": "TB Buccaneers", "color": "#D50A0A" },
  "home": { "abbr": "DAL", "name": "Cowboys", "short": "DAL Cowboys", "color": "#003594" },
  "kickoff": "2026-10-08T20:15:00-04:00",
  "kickoffLabel": "THU 8:15PM ET",
  "dateGroup": "THU OCT 8TH",
  "week": 5,
  "spread": { "away": { "line": "+8.5", "price": -108 }, "home": { "line": "-8.5", "price": -112 } },
  "money": { "away": 350, "home": -455 },
  "total": { "line": 47.5, "over": -115, "under": -105 },
  "nflGameId": "2026_05_TB_DAL",
  "source": "plan_data",
  "linesSource": "nflverse_schedule",
  "isSampleLines": false,
  "props": [
    {
      "id": "godwin-rec",
      "player": "Chris Godwin",
      "team": "TB",
      "market": "Receptions",
      "line": "Over 4.5",
      "price": -125,
      "selectionKey": "tb-dal|prop|godwin-rec",
      "hitRate": {
        "season": { "pct": 25, "hit": 1, "total": 4 },
        "last5": { "pct": 25, "hit": 1, "total": 4 },
        "vsOpp": { "pct": null, "hit": 0, "total": 0, "label": "vs DAL", "season": 2026, "opponent": "DAL" },
        "vsOppLastSeason": { "pct": null, "hit": 0, "total": 0, "label": "vs DAL '25", "season": 2025, "opponent": "DAL" }
      },
      "playerId": "00-0033921",
      "isSample": true,
      "lineSource": "sample",
      "hitRateSource": "plan_data",
      "hitRateThroughWeek": 4,
      "unlocked": true,
      "locked": []
    }
  ]
}
```

**Bet slip keys** (same as `plan-frontend/mock/app.js`):

| Selection | `selection_key` |
|-----------|-----------------|
| Spread away/home | `{gameId}\|spread\|away` |
| Moneyline | `{gameId}\|money\|home` |
| Total | `{gameId}\|total\|over` |
| Prop | `{gameId}\|prop\|{propId}` |

Game fields added for the real schedule (all additive):

| Field | Meaning |
|-------|---------|
| `nflGameId` | Schedule id, e.g. `2026_05_BUF_LA` (raw Plan Data/nflverse code; `null` for sample games) |
| `source` | `plan_data` (real schedule) \| `sample` |
| `linesSource` | `nflverse_schedule` (spread/total/moneyline from `schedule_games_raw.csv`) \| `sample` \| `mixed` \| `none` |
| `isSampleLines` | `false` only when every game line is from the schedule file |

`kickoff` is stored as an aware timestamp (UTC in Postgres). It's serialized as ISO-8601 with its offset, e.g. `2026-10-08T20:15:00-04:00` = `2026-10-09T00:15:00Z`.

**Id changes (real week-5 import).** Game ids keep the `away-home` slug style in lowercase, using our team abbreviations. The Rams are `LAR`, and the schedule file's `LA` is normalised, so the Monday game is `buf-lar`.
- **Unchanged:** `tb-dal` and `sf-sea` (same rows/UUIDs).
- **New:** `phi-jax`, `chi-gb`, `cin-mia`, `cle-nyj`, `hou-ten`, `ind-pit`, `lv-ne`, `min-no`, `nyg-was`, `den-lac`, `det-ari`, `bal-atl`, `buf-lar`.
- **Gone from `/games` (archived → 404):** `det-gb`, `bal-cin`, `nyj-mia`, `cle-pit`, `ind-jax`, `car-atl`, `ten-hou`, `lv-den`, `kc-buf`, `ari-lar`, `chi-min`, `phi-nyg`.

Prop ids never change, but moved props are on a new game, so their `selectionKey` changes:

| Prop(s) | Old game | New game |
|---------|----------|----------|
| `allen-pass`, `allen-rush`, `cook-rush`, `cook-rec2` | `kc-buf` | `buf-lar` |
| `hurts-pass`, `saquon-rush` | `phi-nyg` | `phi-jax` |
| `nabers-yds` | `phi-nyg` | `nyg-was` |
| `jackson-pass`, `henry-rush` | `bal-cin` | `bal-atl` |
| `chase-rec`, `chase-yds` | `bal-cin` | `cin-mia` |
| `gibbs-rush`, `stbrown-rec` | `det-gb` | `det-ari` |
| `love-pass` | `det-gb` | `chi-gb` |
| `doubs-yds` | `det-gb` | `lv-ne` |
| `evans-rec`, `evans-yds`, `dowdle-rush` | `tb-dal` | `sf-sea`, `sf-sea`, `ind-pit` |
| `metcalf-yds` | `sf-sea` | `ind-pit` |
| `brown-rec` | `phi-nyg` | `lv-ne` (hidden: IR) |

The key pattern `{gameId}|prop|{propId}` is unchanged. Old keys (e.g. `kc-buf|prop|allen-pass`) return 404.

Prop fields added for real data (all additive):

| Field | Meaning |
|-------|---------|
| `playerId` | GSIS id (`00-00…`) from Plan Data, `null` if unmatched |
| `isSample` | `true` while the **line/price** are sample (always, today; Plan Data has no odds) |
| `lineSource` | `sample` today; a real odds feed would set e.g. `odds_api` |
| `hitRateSource` | `plan_data` (real game logs) \| `sample` (mock chips, loader not run) \| `none` (player not matched → `hitRate: null`) |
| `hitRateThroughWeek` | Last week included in the hit rate |

`hitRate.vsOpp` and `hitRate.vsOppLastSeason` have the same shape `{ pct, hit, total, label, season, opponent }`:
- `vsOpp`: current season (2026), vs this week's opponent.
- `vsOppLastSeason`: prior **regular** season (2025, playoffs excluded), vs the same opponent, against the same line. `null` only when no prior-season data is loaded.

Example with history: Purdy passing yards over 224.5, vsOppLastSeason = `{"pct": 50, "hit": 1, "total": 2, "label": "vs SEA '25", "season": 2025, "opponent": "SEA"}`.

`hitRate.*.pct` is **nullable**: a bucket with `total: 0` (e.g. no prior meeting with this opponent) comes back as `{"pct": null, "hit": 0, "total": 0}`; render it as "—", not 0%. `last5.total` can be < 5 early in the season. `team` is the player's real current team.

The example shows an **unlocked** prop. **The paywall is OFF by default** (`PAYWALL_ENABLED=false`), so every prop is unlocked today. When it is turned on, for props the caller hasn't opened (and for anonymous users) the board sends `"unlocked": false`, `hitRate.season: null`, `hitRate.vsOpp: null`, `hitRate.vsOppLastSeason: null`, `"locked": ["season", "vs_opponent", "vs_opponent_last_season"]` (only the windows that had data); `hitRate.last5` stays as a teaser. See [Paywall & billing](#paywall--billing).

Pass that as `selection_key` on `POST /api/v1/bets` (see below). Theme preference is `fanduel` \| `draftkings` (or `null` → frontend shows the skin picker).

## API overview (`/api/v1`)

| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/health` | — | Also mounted at bare `/health` |
| POST | `/auth/signup` | — | Creates wallet + welcome credits; returns JWT + cookie |
| POST | `/auth/login` | — | |
| POST | `/auth/logout` | ✓ | Revokes session, clears cookie |
| GET | `/auth/me` | ✓ | Adds `usage` (free/paid tries) |
| GET/PATCH | `/users/me/preferences` | ✓ | `{ "theme": "fanduel" \| "draftkings" }` |
| GET | `/teams` | — | 32 NFL teams |
| GET | `/games?season=&week=` | — | Board (spread/money/total/props) |
| GET | `/games/{id}` | — | Detail by slug (`tb-dal`) or UUID |
| GET | `/games/{id}/props` | — | Props + hit rates only (paywall-filtered, free) |
| GET | `/games/{id}/props/{propId}` | ✓ | **Prop hit-rate detail = 1 try** (re-open same ET day free); 402 `UPGRADE_REQUIRED` when out of tries |
| GET | `/games/{id}/props/{propId}/hit-rates?window=L5\|season` | ✓ | Free; `season` needs the prop opened (402 `prop_locked`) |
| GET | `/games/{id}/props/{propId}/splits/vs-opponent` | ✓ | Free; needs the prop opened (402 `prop_locked`) |
| POST | `/bets/quote` | ✓ | Price a slip without placing |
| POST | `/bets` | ✓ | Place single/parlay; 409 `ODDS_CHANGED` unless `accept_odds_changes` |
| GET | `/bets` | ✓ | My bets (paginated) |
| GET | `/bets/{id}` | ✓ | |
| GET | `/wallet` | ✓ | Play-money balance |
| GET | `/wallet/transactions` | ✓ | Ledger |
| GET | `/billing/plans` | optional | Free tries + credit packs (+ caller `usage`); works without Stripe keys |
| POST | `/billing/checkout-session` | ✓ | `{ "pack": "small" \| "large" }` → hosted Stripe Checkout URL (one-time payment) |
| GET | `/billing/usage?session_id=` | ✓ | Usage + recent purchases; `session_id` (success page) syncs/credits that purchase |
| POST | `/webhooks/stripe` | Stripe signature | Credits packs / claws back refunds; idempotent by event id |

Auth: `Authorization: Bearer <jwt>` **or** the `plan_session` httpOnly cookie.

Errors: `{"error":{"code":"...","message":"...","details":...}}`.

### Place-bet example

```bash
TOKEN=...  # from /auth/login
curl -s -X POST localhost:8000/api/v1/bets \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{
    "type": "single",
    "stake_cents": 1100,
    "legs": [{"selection_key":"tb-dal|spread|away","odds_american":-110,"line":"+7.5"}]
  }'
```

## Paywall & billing

> **Currently OFF (owner decision 2026-10-06):** `PAYWALL_ENABLED` defaults to `false` until billing is live. All props return unlocked with full `hitRate`, no tries are consumed, nothing returns 402, and `/auth/me` still returns `usage` with `"paywall_enabled": false, "unlimited": true, "tries_remaining": null`. Set `PAYWALL_ENABLED=true` to turn on everything described below (tests enable it explicitly with the `paywall_on` fixture). It is deliberately a separate flag from `BILLING_ENABLED` so the paywall can be tested before Stripe approval.

**Model (owner-approved):** every user gets **10 free tries** (lifetime). After that, they buy credit packs: **small = 30 tries for $3**, **large = 100 tries for $5**. Purchased tries don't expire, and free tries are used first.

**What a try is:** opening one prop's hit-rate detail (`GET /api/v1/games/{gameId}/props/{propId}`). Re-opening the same prop on the same America/New_York day is free (`PAYWALL_REOPEN_SCOPE=day`; `forever` = once per user+prop). Board/list views, the hit-rate/split sub-endpoints, and play-money bets **never** consume tries.

**Stripe is TEST MODE ONLY.** `sk_live_`/`rk_live_` keys are refused by the API, the webhook handler, and the setup script. There is no override. Livemode events are recorded and ignored.

### How it works

- **Source of truth is our DB:**
  - `usage_accounts` holds the counters and is row-locked on every change.
  - `usage_ledger` is an append-only audit of every free try, paid try, purchase, refund, and grant.
  - `prop_unlocks` records which props each user opened, per ET day.
  - `credit_purchases` has one row per Checkout Session.
  - `billing_customers` maps users to Stripe customers.
  - `stripe_events` gives webhook idempotency and an audit trail.
- **Checkout:** `POST /billing/checkout-session` takes only a pack key. The server maps it to a Price `lookup_key` and creates a `mode=payment` session (with an idempotency key) plus a pending purchase row. Creating a session never credits tries.
- **Fulfilment:** happens on `checkout.session.completed` / `checkout.session.async_payment_succeeded`, or on the success page via `GET /billing/usage?session_id=…`.
  - All three paths re-fetch the session from Stripe and credit **exactly once**: the purchase row is `SELECT … FOR UPDATE` and transitions only pending → paid.
  - `async_payment_failed` marks the purchase failed; `expired` marks it expired.
- **Refunds:** `charge.refunded` (full refund) claws back the pack's tries, but never below 0. Unused tries only.
  - If the refund arrives before fulfilment, the handler fulfils first, then revokes.
  - Partial refunds are ignored (logged) pending a policy decision.
- **Webhook pipeline:**
  1. Signature check (`Stripe-Signature`, 5-min tolerance) → 400 `INVALID_SIGNATURE`.
  2. Dedupe by event id; a repeat returns `{"duplicate": true}`.
  3. Re-fetch the object from Stripe and apply it.
  - Unknown users, other products, and unhandled types → 200 `{"ignored": true}`.
  - Processing errors → 500 `WEBHOOK_FAILED`, so Stripe retries.
- **402 format** (the standard error envelope):

```json
{"error": {"code": "UPGRADE_REQUIRED", "message": "You're out of tries. Buy a pack to keep unlocking props.",
  "details": {"reason": "out_of_tries", "free_limit": 10, "free_used": 10, "free_remaining": 0,
    "paid_balance": 0, "tries_remaining": 0, "purchase_available": true, "prop_id": "kelce-rec", "unlock_cost": 1,
    "packs": [{"key": "small", "name": "30 tries", "tries": 30, "unit_amount": 300, "currency": "usd"},
              {"key": "large", "name": "100 tries", "tries": 100, "unit_amount": 500, "currency": "usd"}]}}}
```

  `reason` is `out_of_tries` (detail endpoint) or `prop_locked` (sub-endpoints before the prop was opened). Reusable gate: `app.billing.paywall.require_entitlement(...)` / `require_paid_entitlement`.
- **Pluggable:** the catalog is `CREDIT_PACKS` (JSON). The free allowance is `FREE_TRIES`. `usage_accounts.unlimited_until` is the hook for comps today (`grant-tries --unlimited-days`) and for a subscription or Season Pass later, without touching the gating code.

### Flags

| Env | Default | Meaning |
|-----|---------|---------|
| `PAYWALL_ENABLED` | `false` | Off until billing is live. `false` → everything unlocked, nothing counted; `true` → free tries + packs |
| `FREE_TRIES` | `10` | Lifetime free tries per user |
| `PAYWALL_REOPEN_SCOPE` | `day` | `day` (ET) \| `forever` |
| `PAYWALL_BOARD_TEASER` | `last5` | What locked props show on lists: `last5` \| `none` |
| `BILLING_ENABLED` | `false` | Purchases on/off. Stands in for Stripe restricted-business approval. While off, checkout → 503 `BILLING_DISABLED`; free and already-bought tries still work |
| `STRIPE_APPROVAL_STATUS` | `pending` | Informational |
| `BILLING_OFFSEASON_MODE` | `none` | Stub: `none` \| `pause` \| `season_pass` (surfaced in `/billing/plans`, not enforced) |

Missing or bad configuration never crashes the server:
- `STRIPE_NOT_CONFIGURED`, `STRIPE_LIVE_KEY_REFUSED`, `STRIPE_KEY_INVALID`, and `PRICE_NOT_CONFIGURED` are all 503s.
- A Stripe API failure returns 502 `STRIPE_ERROR`.

### Local testing with the Stripe CLI (test mode)

```bash
# 1. .env: STRIPE_SECRET_KEY=sk_test_…  STRIPE_PUBLISHABLE_KEY=pk_test_…  BILLING_ENABLED=true
uv run python -m app.cli stripe-setup --dry-run     # prints what it would create; no API calls
uv run python -m app.cli stripe-setup               # creates/verifies 2 Products + 2 one-time Prices (safe to re-run)

# 2. Forward webhooks (prints whsec_… → put it in STRIPE_WEBHOOK_SECRET, restart the API)
stripe listen --forward-to localhost:8000/api/v1/webhooks/stripe \
  --events checkout.session.completed,checkout.session.async_payment_succeeded,checkout.session.async_payment_failed,checkout.session.expired,charge.refunded

# 3. Real end-to-end: POST /api/v1/billing/checkout-session {"pack":"small"} with a user token,
#    open checkout_url, pay with card 4242 4242 4242 4242 (any future date / CVC).
#    Then GET /api/v1/auth/me → usage.paid_balance == 30.

# Synthetic events (signature/plumbing checks; these fixtures aren't tied to a Plan user,
# so the API records them as "ignored"):
stripe trigger checkout.session.completed
stripe trigger charge.refunded
stripe events resend evt_…        # redeliver: second delivery returns {"duplicate": true}
stripe refunds create --payment-intent pi_…   # refund a real test purchase → tries clawed back
```

## Real player data (Plan Data)

`load-nfl-data` reads Plan Data's files and replaces the sample hit-rate chips with numbers computed from **real 2026 game logs**. Lines and prices are **not** in Plan Data, so they stay sample and are flagged `isSample: true`.

```bash
uv run python -m app.cli load-nfl-data --dir /workspace/plan-nfl-data            # load / refresh
uv run python -m app.cli load-nfl-data --dir /workspace/plan-nfl-data --dry-run  # validate + report, roll back
uv run python -m app.cli load-nfl-data --strict   # write nothing + exit non-zero if any validation count ≠ 0
```

**Files used** (`_prev/` is ignored). The season defaults to `_meta.json`'s `season`. Ids are GSIS `player_id`s; Plan Data's `LA` maps to our `LAR`.

| File | Required | Used for |
|------|----------|----------|
| `_meta.json` | – | season, `latest_completed_week` |
| `players_active.csv` | yes | Roster identity + current team (2,525 rows, all 32 teams; 1 row without a GSIS id is skipped) |
| `player_season_stats_{season}_offense_props.csv` | yes | `games_played` denominator |
| `player_game_stats_{season}_offense_props.csv` | yes | Current-season game logs → season / last5 / vsOpp |
| `player_game_stats_{season-1}_offense_props.csv` | optional | Prior-season game logs (REG + POST stored; REG only used) → vsOppLastSeason |
| `player_vs_opponent_{season-1}.csv` | optional | Cross-check only: `games_played_reg` / `games_played` / sums vs the game logs |
| `week{N}_{season}_matchups.csv` | optional | Each player's current team and week-N opponent (or BYE). The latest N is used |

**What it writes (idempotent upserts; a second run changes nothing):**
- `players`:
  - the active roster
  - plus players only in the season stats file (`roster_source=season_stats`)
  - plus players only in prior-season logs (`roster_source=history`, no current team)
- `player_game_stats`: one row per player, season, season_type and week, for both seasons (`season`, `season_type`, `game_type`). Only `played == true` rows (≥1 offensive snap), including zero-stat rows.
- `player_matchups`: one row per player for the matchup week (team, opponent or bye, real game id, kickoff ET).
- `player_season_stats`: includes `games_played`.
- Prop provenance + hit rates on `props`.
- An audit row in `nfl_data_loads` (file hashes, counts, warnings).

Rows that disappear from the files for that season (or that matchup week) are deleted.

**Hit-rate rules:**
- **Denominator** = season file `games_played` (not `games`). A played game with no stat is a 0 and counts as a **miss** for Overs.
- **Hit** = strictly over (or under) the line. A push on a whole-number line is not a hit.
- **last5** = the 5 most recent played games by week.
- **Opponent:** comes from the matchups file (the player's **real** week-N opponent) when present. Otherwise, or on a real **bye** (KC/CAR in week 5), it is the other team on our sample game.
  - Stored as `props.hr_opponent_source` (`matchups` | `sample_game`).
  - When the real opponent differs from the sample game's opponent, the loader prints a `real_opponent_differs` GAP line. Example: Josh Allen on our `kc-buf` card shows `vs LAR`, because BUF really plays LAR in week 5.
- **vsOpp** = this season's played games vs that opponent (before the prop's week). No meetings → `{pct: null, hit: 0, total: 0, label: "vs DAL", season: 2026, opponent: "DAL"}`.
- **vsOppLastSeason** = prior-season **REG** game logs vs the same opponent, against the prop's line. Strictly over; zero rows are misses; playoffs are excluded; total 0 → pct null. Label `"vs DAL '25"`.
  - It's computed from game logs, not the summary sums, so it uses the actual line.
  - Each prop's game count is cross-checked against `games_played_reg` in `player_vs_opponent_{season-1}.csv`. Currently 0 mismatches.
  - Prior-season games never feed season / last5.
- **No leakage:** a prop on a week-N game only uses weeks < N. Loading week 5 doesn't change week-5 props.
- **Validation:**
  - season totals must equal the sum of game rows, and `games_played` must equal the played row count
  - prior-season vs-opp file: `games_played_reg`, `games_played` and stat sums must equal the game logs, and every player/opponent pair in the logs must have a row
  - per-prop vsOppLastSeason totals must equal `games_played_reg`

  Currently 0 mismatches across all checks. Sanity check: WR/TE "1+ catch" = 679/1026 = **66.2%**.

**Matching props to players:** by stored `player_id`, otherwise by normalised name (suffixes like "Jr." are dropped), preferring a player on one of the game's teams.
- The player's current team comes from the matchups file, then the roster. If the player is on neither team of the prop's game (and the prop couldn't be moved), the prop gets `data_status=player_not_in_game`, is closed (`is_open=false`), is hidden from the board, and its detail returns 404. The row is kept because bets reference it. It reopens automatically if a later load puts the player back on a game team.
- Unmatched players → `hitRateSource: "none"`, `hitRate: null`.
- Re-running `seed`/`sync` refreshes sample lines/prices but never overwrites real hit rates or teams.

**Prop lines (FanDuel / DraftKings):** `load-nfl-data` also reads `odds/prop_lines_week{N}_{season}.json` (Plan Data's Odds API snapshot). Props get a `books` object (`fanduel` / `draftkings`, null when that book didn't post — never copied). Top-level `line`/`price`/`hitRate` follow the user's theme (`/users/me/preferences`), with `book` naming the source. `isSample=false` only when `playerId` is set and at least one book quote has a live source (`plan_data_odds`). Without the file, sample dual-book rows are seeded. Optional `ODDS_API_KEY` enables future re-pulls via `app.odds_provider.the_odds_api`; it is not required to boot.

**Real schedule import** (`schedule_games_raw.csv`). It imports the week of the matchups file (else `through_week + 1`; override with `--schedule-week N`). Week 5 = 15 games; KC and CAR are on bye.
- **Games** are matched by `nfl_game_id`. Slugs are `{away}-{home}` with our abbreviations (LA → LAR, reported in a WARN line).
  - A sample game with the same slug, season, week and teams is **adopted**: same row, UUID, unlocks and bets (`tb-dal`, `sf-sea`).
  - If a slug is already taken by an unrelated game, `-{season}-w{week}` is appended.
  - Kickoff = `gameday` + `gametime` in America/New_York → stored as an aware UTC timestamp.
- **Game lines:** moneyline, spread and total with prices come from the file, marked `markets.source` / `games.lines_source = nflverse_schedule`.
  - nflverse `spread_line > 0` means the home team is favoured, so home = `-spread_line` and away = `+spread_line`.
  - A game with no lines in the file gets no markets (`linesSource: "none"`). An adopted game keeps any sample market the file lacks (`mixed`).
- **Retiring the sample slate:** sample games in that week that aren't real are **archived, not deleted**.
  - Archived means `archived_at` is set, `archive_reason=sample_slate_retired` and `status=cancelled`. They are hidden from `/games` and their selection keys return 404.
  - Their markets/selections are settled void through the normal `settle_game` path. **Pending bets on them are voided and the stake refunded** with a `bet_refund` ledger entry. On a parlay, only that leg is voided.
  - Rows stay, so bet history still resolves.
- **Moving props:** every week-5 prop moves to its player's real game (matchups `game_id`). Then:
  - **Bye** (KC) → `data_status=player_on_bye`, closed and hidden. The prop stays on the archived sample game and is never attached to a real or fake game.
  - **Roster status not Active** (`status_code` ≠ `ACT`: Reserve/IR, practice squad, inactive, exempt) → `player_inactive`, closed and hidden. Detailed injury designations (Q/D/O) aren't in the data.
  - These props reopen automatically if a later load makes them OK again.
- **Card check:** for every listed prop, vsOpp and vsOppLastSeason opponent = the other team on its card. `validation.card_opponent_mismatches` must be 0, and `--strict` fails otherwise.
- **Schedule validation:** no team twice in a week, every matchup's `game_id`/teams are in the schedule, and bye teams aren't scheduled.
- **Order:** `seed` (sample props) → `load-nfl-data`. Re-seeding later skips real/archived games, so nothing is resurrected or overwritten. Re-run the loader afterwards.

**Re-run after week 5:**
1. When Plan Data refreshes `/workspace/plan-nfl-data` (`_meta.json` → `latest_completed_week: 5`), run `uv run python -m app.cli load-nfl-data --dir /workspace/plan-nfl-data` (optionally `--dry-run` first).
2. Check the printed validation (all zeros), the sanity line, and the `GAP` lines.
3. It adds the week-5 rows and refreshes players and season totals. If Plan Data ships `week6_2026_matchups.csv`, the latest week file is used. Prior-season (2025) rows don't change, so they reload with 0 changes.
4. Existing week-5 props keep using weeks 1–4.
5. When Plan Data ships `week6_2026_matchups.csv`, the same run imports the real week-6 games and lines from `schedule_games_raw.csv`. There's no props source yet for week 6, so those games come with game lines only. Week-5 games stay until they're settled (`app.cli settle --game tb-dal --home H --away A`).

**Gaps:**
- No **prop** lines/odds source: all 30 prop lines and prices are sample (`isSample: true`). Game lines are real but come from nflverse's schedule file (a community dataset). They are not a licensed or live odds feed, and only refresh when Plan Data refreshes the file.
- Hidden week-5 props (3):
  - `kelce-rec` and `mahomes-pass`: KC is on bye.
  - `brown-rec`: A.J. Brown is on Reserve/IR.

  The 4 previously hidden traded players' props are restored on their real games: Evans on `sf-sea`, Dowdle and Metcalf on `ind-pit`, Doubs on `lv-ne`.
- Props are not graded from these stats yet.
- Licensing: the upstream data is nflverse-derived and Sleeper ids are included. Neither is an official NFL licence; confirm terms before commercial launch.

## Tests

```bash
uv run pytest
# Uses TEST_DATABASE_URL (default: Postgres plan_test). Tables are recreated per session.
```

## Compromises / notes

- **Postgres on this box:** Docker was not available, so Postgres 17 was installed via apt and databases `plan` / `plan_test` were created. `docker-compose.yml` is still provided for other environments. SQLite works for quick hacking (`DATABASE_URL=sqlite:///./plan.db`) but row-level locks / some constraints are weaker.
- **Play money only** for betting — no payment rails for wagers, KYC, or geofencing. Stripe is used only to sell analytics credit packs (test mode).
- **Same-game parlays** and **prop grading** are stubs (props stay pending until the data agent wires real player-stat settlement).
- After `load-nfl-data`, the schedule and game lines are real (`source: plan_data`, `linesSource: nflverse_schedule`). **Prop** lines and odds are still **illustrative** (`isSample: true`). Hit rates are real (`hitRateSource: "plan_data"`).

## Open questions for the product owner

1. Stay play-money indefinitely, or add real-money later (and which payment / compliance path)?
2. Odds / schedule source of truth (data agent feed, The Odds API, Sportradar, official league feed)? Refresh cadence?
3. Auth: keep email+password, or add OAuth / passwordless / SSO?
4. Hosting target (Fly, Render, AWS, Cloud Run) and whether the API is public or BFF-only?
5. Same-game parlays / player-prop settlement timeline? Prop **lines/prices source** (Plan Data has none)? Licensed/live source for **game** lines in production (today: nflverse schedule file)? Should bets placed at sample odds on games that turned out real (e.g. `tb-dal`) stand (current behaviour: they stand at their placed line and odds) or be voided?
6. Should theme live only in the frontend (localStorage) or stay server-side for cross-device sync?
7. **Billing:**
   - Stripe restricted-business eligibility for a betting-adjacent analytics product (BILLING_ENABLED stays false until approved).
   - Partial-refund and dispute policy (currently: full refund claws back unused tries; partial refunds and disputes are not automated).
   - Should purchased tries expire (offseason)?
   - Sales tax (Stripe Tax / tax-inclusive pricing; prices are created with unspecified tax behavior).
   - Should free tries reset (e.g. weekly) or stay lifetime?
