"""The Odds API client (optional). Primary week-5 lines come from Plan Data's JSON snapshot.

Without ODDS_API_KEY the client is inert — load-nfl-data uses
`plan-nfl-data/odds/prop_lines_week5_2026.json` instead. This module is scaffolding for
future re-pulls (see plan-nfl-data/scripts/fetch_odds_props_week5.py).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import get_settings

log = logging.getLogger("plan.odds_api")

BASE = "https://api.the-odds-api.com/v4"
SPORT = "americanfootball_nfl"
# Markets we care about (Odds API keys). Normalized names live in app.nfl_data.markets.
PROP_MARKETS = (
    "player_receptions",
    "player_reception_yds",
    "player_rush_yds",
    "player_rush_attempts",
    "player_pass_yds",
    "player_pass_tds",
    "player_pass_completions",
    "player_anytime_td",
)
BOOKMAKERS = ("fanduel", "draftkings")


@dataclass
class OddsApiClient:
    """Thin HTTP wrapper. Never logs or echoes the API key."""

    api_key: str
    sport: str = SPORT
    base_url: str = BASE
    last_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_settings(cls) -> OddsApiClient | None:
        key = get_settings().odds_api_key.get_secret_value().strip()
        return cls(api_key=key) if key else None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        q = urlencode({**params, "apiKey": self.api_key})
        req = Request(f"{self.base_url}{path}?{q}", headers={"Accept": "application/json"})
        try:
            with urlopen(req, timeout=30) as resp:
                self.last_headers = {
                    k: resp.headers.get(k, "")
                    for k in ("x-requests-remaining", "x-requests-used", "x-requests-last")
                }
                import json

                return json.loads(resp.read().decode())
        except HTTPError as exc:
            body = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Odds API HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise RuntimeError(f"Odds API network error: {exc}") from exc

    def list_events(self) -> list[dict[str, Any]]:
        """GET /sports/{sport}/events — free (does not consume quota on most plans)."""
        return self._get(f"/sports/{self.sport}/events", {})

    def event_prop_odds(
        self,
        event_id: str,
        *,
        markets: tuple[str, ...] = PROP_MARKETS,
        bookmakers: tuple[str, ...] = BOOKMAKERS,
        regions: str = "us",
    ) -> dict[str, Any]:
        """GET /sports/{sport}/events/{eventId}/odds — consumes credits."""
        return self._get(
            f"/sports/{self.sport}/events/{event_id}/odds",
            {
                "regions": regions,
                "markets": ",".join(markets),
                "bookmakers": ",".join(bookmakers),
                "oddsFormat": "american",
            },
        )


def remaining_credits(client: OddsApiClient) -> int | None:
    raw = client.last_headers.get("x-requests-remaining") or ""
    try:
        return int(raw)
    except ValueError:
        return None
