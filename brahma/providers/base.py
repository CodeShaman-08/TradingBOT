"""Provider contract shared by replay and Dhan."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class DataError(RuntimeError):
    """Raised when candles/ltp cannot be trusted. Never silently swallowed."""


class MarketDataProvider(ABC):
    name: str = "base"
    is_replay: bool = False

    @abstractmethod
    def get_candles(self, limit: int = 2000) -> pd.DataFrame:
        """Return OHLCV candles with a tz-aware IST DatetimeIndex."""

    @abstractmethod
    def get_ltp(self) -> float:
        """Last traded price of the underlying (0.0 if unavailable)."""

    def history(self, limit: int = 5000) -> pd.DataFrame:
        """Full available history for backtesting (defaults to visible candles)."""
        return self.get_candles(limit)

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "is_replay": self.is_replay}

    def close(self) -> None:  # pragma: no cover - providers are cheap
        pass
