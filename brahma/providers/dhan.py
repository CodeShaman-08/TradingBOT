"""Dhan intraday data provider (REST polling).

Why polling instead of the websocket for v1: it is dramatically simpler to make
correct, and a 1-minute strategy only needs a fresh bar every 60s. The provider
caches the last fetch and only asks Dhan for *today's* candles on each poll;
previous sessions are fetched once and reused.

If your Dhan plan exposes a live feed later, implement the same
``MarketDataProvider`` interface and switch DATA_PROVIDER — the engine does not
care how candles arrive.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta
from typing import Any

import pandas as pd

from ..strategy import normalize_candles
from ..timeutils import get_tz, now_ist
from ..utils import extract_ltp
from .base import DataError, MarketDataProvider


class DhanDataProvider(MarketDataProvider):
    name = "dhan"
    is_replay = False

    def __init__(
        self,
        settings,
        security_id: str,
        exchange_segment: str = "IDX_I",
        instrument_type: str = "INDEX",
        client: Any | None = None,
    ) -> None:
        if not str(security_id).strip():
            raise DataError(
                "No underlying security id. Set UNDERLYING_SECURITY_ID or let the "
                "instrument master resolve it."
            )
        self.settings = settings
        self.security_id = str(security_id).strip()
        self.exchange_segment = exchange_segment
        self.instrument_type = instrument_type
        self.tz_name = settings.tz
        self._lock = threading.RLock()
        self._last_error: str | None = None
        self._last_fetch_mono = 0.0
        self._last_fetch_at: str | None = None
        self._history: pd.DataFrame | None = None
        self._today: pd.DataFrame | None = None
        self._cache_ttl = max(2.0, float(settings.poll_interval_sec) * 0.75)

        if client is None:
            from dhanhq import DhanContext, dhanhq

            client = dhanhq(DhanContext(settings.client_id, settings.access_token))
        self._client = client

    # ---------------------------------------------------------------- fetch
    def _fetch_day(self, day) -> pd.DataFrame:
        response = self._client.intraday_minute_data(
            security_id=self.security_id,
            exchange_segment=self.exchange_segment,
            instrument_type=self.instrument_type,
            from_date=day.isoformat(),
            to_date=day.isoformat(),
            interval=1,
        )
        return self._parse(response)

    def _build_history(self) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        today = now_ist(self.tz_name).date()
        days = max(int(self.settings.lookback_days), 1)
        for offset in range(days, 0, -1):
            day = today - timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            try:
                frame = self._fetch_day(day)
                if not frame.empty:
                    frames.append(frame)
            except Exception as exc:  # noqa: BLE001 - history is best-effort
                self._last_error = f"history {day}: {exc}"
        if not frames:
            return pd.DataFrame()
        return normalize_candles(pd.concat(frames), self.tz_name)

    def get_candles(self, limit: int = 2000) -> pd.DataFrame:
        with self._lock:
            now_mono = time.monotonic()
            if self._history is None:
                self._history = self._build_history()

            if now_mono - self._last_fetch_mono >= self._cache_ttl:
                today = now_ist(self.tz_name).date()
                try:
                    frame = self._fetch_day(today)
                    if not frame.empty:
                        self._today = frame
                    self._last_error = None
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
                self._last_fetch_mono = now_mono
                self._last_fetch_at = now_ist(self.tz_name).isoformat()

            parts = [p for p in (self._history, self._today) if p is not None and not p.empty]
            if not parts:
                raise DataError(f"Dhan returned no candles: {self._last_error or 'unknown error'}")
            combined = normalize_candles(pd.concat(parts), self.tz_name)
            return combined.tail(max(int(limit), 1))

    # ---------------------------------------------------------------- parse
    def _parse(self, response: Any) -> pd.DataFrame:
        if not isinstance(response, dict):
            raise DataError(f"Unexpected Dhan response type: {type(response).__name__}")

        status = str(response.get("status", "")).lower()
        if status and status != "success":
            raise DataError(f"Dhan data error: {str(response)[:300]}")

        data = response.get("data")
        if data is None:
            raise DataError(f"Dhan response has no data: {str(response)[:300]}")

        if isinstance(data, dict):
            if "candles" in data:
                frame = self._rows_to_frame(data["candles"])
            else:
                frame = self._dict_to_frame(data)
        elif isinstance(data, list):
            frame = self._rows_to_frame(data)
        else:
            raise DataError(f"Unsupported Dhan data payload: {type(data).__name__}")

        if frame is None or frame.empty:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        frame.columns = [str(c).strip().lower() for c in frame.columns]
        ts_col = next(
            (c for c in ("timestamp", "time", "date", "datetime", "start_time") if c in frame.columns),
            None,
        )
        if ts_col is None:
            raise DataError(f"Dhan candles have no timestamp column: {list(frame.columns)}")

        stamps = self._to_datetime(frame[ts_col])
        frame = frame.drop(columns=[ts_col])
        frame.index = pd.DatetimeIndex(stamps)
        return normalize_candles(frame, self.tz_name)

    def _dict_to_frame(self, data: dict) -> pd.DataFrame:
        lower = {str(k).lower(): v for k, v in data.items()}
        ts = None
        for key in ("timestamp", "time", "date", "datetime", "start_time"):
            if key in lower:
                ts = lower[key]
                break
        if ts is None:
            raise DataError(f"Dhan candle dict has no timestamp: {list(lower)}")
        frame = pd.DataFrame({"timestamp": ts})
        for column in ("open", "high", "low", "close", "volume"):
            if column in lower:
                frame[column] = lower[column]
        return frame

    def _rows_to_frame(self, rows: Any) -> pd.DataFrame:
        if not isinstance(rows, list):
            raise DataError("Dhan candles are not a list")
        if not rows:
            return pd.DataFrame()
        if isinstance(rows[0], dict):
            return pd.DataFrame(rows)
        width = max(len(row) for row in rows if isinstance(row, (list, tuple)))
        names = ["timestamp", "open", "high", "low", "close", "volume", "oi", "extra"][:width]
        return pd.DataFrame(rows, columns=names)

    def _to_datetime(self, series: pd.Series) -> pd.Series:
        numeric = pd.to_numeric(series, errors="coerce")
        if numeric.notna().all():
            peak = float(numeric.max())
            if peak > 1e17:
                unit = "ns"
            elif peak > 1e14:
                unit = "us"
            elif peak > 1e11:
                unit = "ms"
            else:
                unit = "s"
            return pd.to_datetime(numeric, unit=unit, utc=True).dt.tz_convert(get_tz(self.tz_name))
        stamps = pd.to_datetime(series, errors="coerce")
        if getattr(stamps.dt, "tz", None) is None:
            return stamps.dt.tz_localize(get_tz(self.tz_name))
        return stamps.dt.tz_convert(get_tz(self.tz_name))

    # ------------------------------------------------------------------ ltp
    def get_ltp(self) -> float:
        try:
            response = self._client.ticker_data({self.exchange_segment: [self.security_id]})
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            raise DataError(f"Dhan ticker_data failed: {exc}") from exc
        value = extract_ltp(response)
        if value <= 0:
            raise DataError(f"Dhan ticker_data returned no usable LTP: {str(response)[:300]}")
        return value

    def history(self, limit: int = 5000) -> pd.DataFrame:
        with self._lock:
            if self._history is None:
                self._history = self._build_history()
            if self._today is None:
                try:
                    self._today = self._fetch_day(now_ist(self.tz_name).date())
                except Exception as exc:  # noqa: BLE001
                    self._last_error = str(exc)
            parts = [p for p in (self._history, self._today) if p is not None and not p.empty]
            if not parts:
                raise DataError(f"No history available: {self._last_error or 'unknown error'}")
            return normalize_candles(pd.concat(parts), self.tz_name).tail(max(int(limit), 1))

    def health(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "is_replay": False,
                "security_id": self.security_id,
                "segment": self.exchange_segment,
                "cached_bars": int(len(self._today) if self._today is not None else 0),
                "history_bars": int(len(self._history) if self._history is not None else 0),
                "last_fetch_at": self._last_fetch_at,
                "last_error": self._last_error,
            }
