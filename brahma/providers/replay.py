"""Offline replay provider.

Two modes:
  * REPLAY_CSV points to a CSV -> that file is replayed.
  * otherwise                -> deterministic synthetic 1-minute candles are
                                generated for the last REPLAY_DAYS sessions.

A cursor advances with wall-clock time at REPLAY_BARS_PER_SEC, so the
dashboard looks live without needing any broker/API access.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ..strategy import normalize_candles
from ..timeutils import (
    get_tz,
    is_trading_day,
    now_ist,
    session_bounds,
)
from .base import DataError, MarketDataProvider


class ReplayProvider(MarketDataProvider):
    name = "replay"
    is_replay = True

    def __init__(self, settings) -> None:
        self.settings = settings
        self.tz_name = settings.tz
        self._lock = threading.RLock()
        self._source = "synthetic"
        self._full = self._load(settings)
        if self._full is None or len(self._full) < 50:
            raise DataError("Replay provider needs at least 50 candles")

        self._total = len(self._full)
        initial = min(max(int(settings.replay_initial_bars), 50), self._total)
        self._cursor = self._total - initial
        self._start_cursor = self._cursor
        self._start_monotonic = time.monotonic()
        self._last_error: str | None = None

    # ------------------------------------------------------------------ load
    def _load(self, settings) -> pd.DataFrame:
        if settings.replay_csv:
            path = Path(settings.replay_csv)
            if not path.is_absolute():
                path = settings.root / path
            if path.is_file():
                self._source = f"csv:{path.name}"
                return self._load_csv(path)
            raise DataError(f"REPLAY_CSV not found: {path}")
        return self._synthetic(settings)

    def _load_csv(self, path: Path) -> pd.DataFrame:
        frame = pd.read_csv(path)
        frame.columns = [str(c).strip().lower() for c in frame.columns]
        ts_col = next(
            (c for c in ("timestamp", "datetime", "date", "time") if c in frame.columns),
            None,
        )
        if ts_col is None:
            raise DataError(f"{path.name}: no timestamp/datetime/date/time column")

        raw = frame[ts_col]
        try:
            stamps = pd.to_datetime(raw)
        except (ValueError, TypeError):
            stamps = pd.to_datetime(pd.to_numeric(raw, errors="coerce"), unit="s")

        frame = frame.drop(columns=[ts_col])
        frame.index = pd.DatetimeIndex(stamps)
        return normalize_candles(frame, self.tz_name)

    def _recent_sessions(self, now: datetime, days: int) -> list[tuple[datetime, datetime]]:
        sessions: list[tuple[datetime, datetime]] = []
        cursor = now.date()
        guard = 0
        while len(sessions) < days and guard < days * 6 + 40:
            if is_trading_day(cursor, self.settings.holidays):
                start, close = session_bounds(
                    cursor,
                    self.settings.market_open,
                    self.settings.market_close,
                    self.tz_name,
                )
                if cursor < now.date() or now >= start:
                    end = close
                    if cursor == now.date():
                        end = min(close, now)
                    if end > start:
                        sessions.append((start, end))
            cursor -= timedelta(days=1)
            guard += 1
        return list(reversed(sessions))

    def _synthetic(self, settings) -> pd.DataFrame:
        now = now_ist(self.tz_name)
        sessions = self._recent_sessions(now, max(int(settings.replay_days), 1))

        stamps: list[datetime] = []
        for start, end in sessions:
            ts = start
            while ts < end:
                # Only include bars that are fully closed.
                if ts + timedelta(minutes=1) <= now:
                    stamps.append(ts)
                ts += timedelta(minutes=1)

        if not stamps:
            raise DataError("Could not build any synthetic candles")

        n = len(stamps)
        rng = np.random.default_rng(int(settings.replay_seed))
        base = 48_000.0
        t = np.arange(n, dtype=float)

        # Multi-scale cycles + noise: produces realistic trends and crossovers.
        cycle = (
            0.00030 * np.sin(t / 37.0)
            + 0.00020 * np.sin(t / 113.0 + 1.1)
            + 0.00012 * np.sin(t / 211.0 + 2.3)
        )
        noise = rng.normal(0.0, 0.00045, n)
        log_returns = 0.000008 + cycle + noise
        close = base * np.exp(np.cumsum(log_returns))

        open_ = np.empty(n, dtype=float)
        open_[0] = base
        open_[1:] = close[:-1] * (1.0 + rng.normal(0.0, 0.00012, n - 1))

        spread = np.abs(rng.normal(0.0, 0.00035, n))
        high = np.maximum(open_, close) * (1.0 + spread)
        low = np.minimum(open_, close) * (1.0 - spread)
        volume = rng.integers(8_000, 90_000, n).astype(float)

        frame = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
            index=pd.DatetimeIndex(stamps, tz=get_tz(self.tz_name)),
        )
        return normalize_candles(frame, self.tz_name)

    # ----------------------------------------------------------------- cursor
    def _advance_locked(self) -> None:
        elapsed = time.monotonic() - self._start_monotonic
        target = self._start_cursor + int(elapsed * float(self.settings.replay_bars_per_sec))
        self._cursor = min(self._total, max(self._start_cursor, target))

    def get_candles(self, limit: int = 2000) -> pd.DataFrame:
        with self._lock:
            self._advance_locked()
            end = self._cursor
            start = max(0, end - max(int(limit), 1))
            frame = self._full.iloc[start:end].copy()
        if frame.empty:
            raise DataError("Replay cursor is empty")
        return frame

    def get_ltp(self) -> float:
        with self._lock:
            self._advance_locked()
            index = min(max(self._cursor - 1, 0), self._total - 1)
            return float(self._full["close"].iloc[index])

    def history(self, limit: int = 5000) -> pd.DataFrame:
        """All generated candles (not just the revealed cursor) for backtests."""
        with self._lock:
            return self._full.tail(max(int(limit), 1)).copy()

    def health(self) -> dict:
        with self._lock:
            self._advance_locked()
            return {
                "name": self.name,
                "is_replay": True,
                "source": self._source,
                "bars_total": self._total,
                "bars_visible": self._cursor,
                "bars_per_sec": self.settings.replay_bars_per_sec,
                "last_error": self._last_error,
            }
