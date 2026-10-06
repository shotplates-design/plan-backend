from app.odds_provider.base import (
    OddsProvider,
    ProviderGame,
    ProviderMarket,
    ProviderSelection,
    ProviderTeam,
)
from app.odds_provider.mock import MockOddsProvider
from app.odds_provider.the_odds_api import OddsApiClient


def get_odds_provider(name: str = "mock") -> OddsProvider:
    if name == "mock":
        return MockOddsProvider()
    raise ValueError(f"Unknown odds provider: {name!r}")


__all__ = [
    "MockOddsProvider",
    "OddsApiClient",
    "OddsProvider",
    "ProviderGame",
    "ProviderMarket",
    "ProviderSelection",
    "ProviderTeam",
    "get_odds_provider",
]
