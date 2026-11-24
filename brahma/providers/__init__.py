"""Market-data provider factory.

    DATA_PROVIDER=dhan     -> Dhan intraday API (default, live paper trading)
    DATA_PROVIDER=replay   -> offline synthetic/CSV candles (testing only)
"""

from __future__ import annotations

from .base import DataError, MarketDataProvider
from .replay import ReplayProvider


def build_provider(
    settings,
    underlying_security_id: str = "",
    exchange_segment: str = "IDX_I",
    instrument_type: str = "INDEX",
) -> MarketDataProvider:
    if settings.data_provider == "replay":
        return ReplayProvider(settings)

    from .dhan import DhanDataProvider

    return DhanDataProvider(
        settings,
        security_id=underlying_security_id,
        exchange_segment=exchange_segment,
        instrument_type=instrument_type,
    )


__all__ = ["DataError", "MarketDataProvider", "ReplayProvider", "build_provider"]
