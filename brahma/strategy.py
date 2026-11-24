"""Signal engine: HMA / HMA3 + optional Kalman layer.

The strategy is a pure function of candles. It never talks to a broker, which
makes it directly reusable by the backtester and the dashboard chart.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .indicators import crossover, ema, hma, hma3, kalman, slope
from .models import Signal
from .timeutils import ensure_aware, iso, now_ist, timeframe_seconds


@dataclass(frozen=True)
class StrategyConfig:
    length: int = 22
    gain: float = 0.7
    use_kalman: bool = True
    use_trend_filter: bool = False
    trend_ema: int = 50
    min_bars_between_signals: int = 0

    @classmethod
    def from_settings(cls, settings) -> "StrategyConfig":
        return cls(
            length=settings.length,
            gain=settings.gain,
            use_kalman=settings.use_kalman,
            use_trend_filter=settings.use_trend_filter,
            trend_ema=settings.trend_ema,
            min_bars_between_signals=settings.min_bars_between_signals,
        )


@dataclass
class StrategyResult:
    candles: pd.DataFrame
    line_a: np.ndarray
    line_b: np.ndarray
    signal: Signal
    ready: bool
    reason: str

    @property
    def last_candle_ts(self) -> str | None:
        if self.candles.empty:
            return None
        return iso(self.candles.index[-1])


def normalize_candles(df: pd.DataFrame, tz_name: str = "Asia/Kolkata") -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    frame = df.copy()
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    if not isinstance(frame.index, pd.DatetimeIndex):
        if "timestamp" in frame.columns:
            frame = frame.set_index("timestamp")
        elif "date" in frame.columns:
            frame = frame.set_index("date")
        else:
            raise ValueError("Candles need a DatetimeIndex or a timestamp/date column")

    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize(tz_name)
    else:
        index = index.tz_convert(tz_name)
    frame.index = index
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()

    for column in ("open", "high", "low", "close"):
        if column not in frame.columns:
            if column == "close" and "ltp" in frame.columns:
                frame["close"] = frame["ltp"]
            else:
                raise ValueError(f"Missing candle column: {column}")
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    if "volume" not in frame.columns:
        frame["volume"] = 0.0
    frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce").fillna(0.0)

    # Forward-fill tiny gaps, then drop leading rows that are still incomplete.
    frame[["open", "high", "low", "close"]] = frame[["open", "high", "low", "close"]].ffill()
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    return frame


def drop_unfinished(df: pd.DataFrame, now: datetime | None = None, timeframe: str = "1m") -> pd.DataFrame:
    """Remove the currently-forming bar so signals only use closed candles."""
    if df is None or len(df) == 0:
        return df
    current = ensure_aware(now or now_ist())
    last = ensure_aware(df.index[-1])
    bar_seconds = timeframe_seconds(timeframe)
    if last + timedelta(seconds=bar_seconds) > current:
        return df.iloc[:-1]
    return df


class SignalEngine:
    def __init__(self, config: StrategyConfig) -> None:
        self.config = config

    def _warmup_bars(self) -> int:
        return max(self.config.length * 3, self.config.trend_ema + 5, 60)

    def compute(
        self,
        df: pd.DataFrame,
        now: datetime | None = None,
        last_signal_candle: str | None = None,
    ) -> StrategyResult:
        cfg = self.config
        current = ensure_aware(now or now_ist())
        candles = normalize_candles(df)

        empty_signal = Signal(
            action="HOLD",
            reason="no data",
            candle_ts=None,
            price=0.0,
            line_a=0.0,
            line_b=0.0,
            prev_a=0.0,
            prev_b=0.0,
            generated_at=iso(current) or "",
        )
        if candles.empty:
            return StrategyResult(candles, np.array([]), np.array([]), empty_signal, False, "no data")

        hl2 = ((candles["high"] + candles["low"]) / 2.0).to_numpy(dtype=float)
        close = candles["close"].to_numpy(dtype=float)

        raw_a = hma(hl2, cfg.length)
        raw_b = hma3(close, cfg.length)
        if cfg.use_kalman:
            line_a = kalman(raw_a, cfg.gain)
            line_b = kalman(raw_b, cfg.gain)
        else:
            line_a = raw_a
            line_b = raw_b

        warmup = self._warmup_bars()
        last_candle_ts = iso(candles.index[-1])

        if len(candles) < warmup:
            signal = Signal(
                action="HOLD",
                reason=f"warming up ({len(candles)}/{warmup} bars)",
                candle_ts=last_candle_ts,
                price=float(close[-1]),
                line_a=float(line_a[-1]) if np.isfinite(line_a[-1]) else 0.0,
                line_b=float(line_b[-1]) if np.isfinite(line_b[-1]) else 0.0,
                prev_a=0.0,
                prev_b=0.0,
                generated_at=iso(current) or "",
            )
            return StrategyResult(candles, line_a, line_b, signal, False, signal.reason)

        last_values = (line_a[-1], line_a[-2], line_b[-1], line_b[-2], close[-1])
        if not all(np.isfinite(float(x)) for x in last_values):
            signal = Signal(
                action="HOLD",
                reason="indicator values not finite",
                candle_ts=last_candle_ts,
                price=float(close[-1]),
                line_a=0.0,
                line_b=0.0,
                prev_a=0.0,
                prev_b=0.0,
                generated_at=iso(current) or "",
            )
            return StrategyResult(candles, line_a, line_b, signal, False, signal.reason)

        cross_up, cross_dn = crossover(line_b, line_a)
        action = "BUY" if cross_up else "SELL" if cross_dn else "HOLD"
        reason = "cross up" if cross_up else "cross down" if cross_dn else "no cross"

        # Optional trend filter: only take crosses in the direction of the slow EMA.
        if action != "HOLD" and cfg.use_trend_filter:
            trend = ema(close, cfg.trend_ema)
            if not np.isfinite(trend[-1]):
                action, reason = "HOLD", "trend EMA not ready"
            elif action == "BUY" and not (close[-1] > trend[-1] and slope(line_a, 1) > 0):
                action, reason = "HOLD", "trend filter blocked BUY"
            elif action == "SELL" and not (close[-1] < trend[-1] and slope(line_a, 1) < 0):
                action, reason = "HOLD", "trend filter blocked SELL"

        # Optional spacing between signals (prevents back-to-back whipsaw entries).
        if action != "HOLD" and cfg.min_bars_between_signals > 0 and last_signal_candle:
            try:
                last_pos = candles.index.get_indexer([pd.Timestamp(last_signal_candle)])[0]
            except (ValueError, TypeError):
                last_pos = -1
            if last_pos >= 0 and (len(candles) - 1 - last_pos) < cfg.min_bars_between_signals:
                action, reason = "HOLD", "min bars between signals"

        signal = Signal(
            action=action,
            reason=reason,
            candle_ts=last_candle_ts,
            price=float(close[-1]),
            line_a=float(line_a[-1]),
            line_b=float(line_b[-1]),
            prev_a=float(line_a[-2]),
            prev_b=float(line_b[-2]),
            generated_at=iso(current) or "",
        )
        return StrategyResult(candles, line_a, line_b, signal, True, reason)

    def chart_payload(self, result: StrategyResult, limit: int = 300) -> dict[str, Any]:
        """Payload shaped for TradingView lightweight-charts."""
        candles = result.candles.tail(limit)
        if candles.empty:
            return {"candles": [], "line_a": [], "line_b": []}

        start = len(result.candles) - len(candles)
        line_a = result.line_a[start:]
        line_b = result.line_b[start:]

        candle_rows = []
        a_rows = []
        b_rows = []
        for i, (ts, row) in enumerate(candles.iterrows()):
            stamp = int(ensure_aware(ts).timestamp())
            candle_rows.append(
                {
                    "time": stamp,
                    "open": round(float(row["open"]), 4),
                    "high": round(float(row["high"]), 4),
                    "low": round(float(row["low"]), 4),
                    "close": round(float(row["close"]), 4),
                }
            )
            if i < len(line_a) and np.isfinite(line_a[i]):
                a_rows.append({"time": stamp, "value": round(float(line_a[i]), 4)})
            if i < len(line_b) and np.isfinite(line_b[i]):
                b_rows.append({"time": stamp, "value": round(float(line_b[i]), 4)})

        return {"candles": candle_rows, "line_a": a_rows, "line_b": b_rows}
