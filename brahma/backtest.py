"""Signal-quality backtester + Kalman A/B comparison.

This backtests the *underlying* signal (long/short flip on crossovers), not the
option premium. It answers the question "does the Kalman layer add edge?" — not
"what would my option P&L be?". Treat it as a filter, not a promise.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .config import get_settings
from .indicators import hma, hma3, kalman
from .strategy import StrategyConfig, normalize_candles


@dataclass
class BacktestMetrics:
    trades: int = 0
    win_rate: float = 0.0
    total_return_pct: float = 0.0
    buy_hold_pct: float = 0.0
    avg_trade_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    exposure_pct: float = 0.0
    sharpe: float = 0.0
    final_equity: float = 1.0

    def to_dict(self) -> dict:
        return {
            "trades": self.trades,
            "win_rate": round(self.win_rate, 4),
            "total_return_pct": round(self.total_return_pct, 4),
            "buy_hold_pct": round(self.buy_hold_pct, 4),
            "avg_trade_pct": round(self.avg_trade_pct, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "profit_factor": round(self.profit_factor, 4),
            "exposure_pct": round(self.exposure_pct, 4),
            "sharpe": round(self.sharpe, 4),
            "final_equity": round(self.final_equity, 6),
        }


def _lines(close: np.ndarray, hl2: np.ndarray, cfg: StrategyConfig, use_kalman: bool):
    raw_a = hma(hl2, cfg.length)
    raw_b = hma3(close, cfg.length)
    if use_kalman:
        return kalman(raw_a, cfg.gain), kalman(raw_b, cfg.gain)
    return raw_a, raw_b


def run_backtest(
    candles: pd.DataFrame,
    cfg: StrategyConfig,
    use_kalman: bool,
    cost_bps: float = 2.0,
) -> BacktestMetrics:
    frame = normalize_candles(candles)
    if len(frame) < max(cfg.length * 3, 60):
        raise ValueError("Not enough candles for a backtest")

    close = frame["close"].to_numpy(dtype=float)
    hl2 = ((frame["high"] + frame["low"]) / 2.0).to_numpy(dtype=float)
    line_a, line_b = _lines(close, hl2, cfg, use_kalman)

    warmup = max(cfg.length * 3, cfg.trend_ema + 5, 60)
    n = len(frame)
    if n <= warmup + 2:
        raise ValueError("Not enough candles after warmup")

    cost = float(cost_bps) / 10_000.0
    position = 0
    entry_price = 0.0
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    trade_returns: list[float] = []
    bar_returns: list[float] = []
    exposed_bars = 0

    for i in range(warmup, n):
        if position != 0 and i > 0:
            bar_return = position * (close[i] / close[i - 1] - 1.0)
            equity *= 1.0 + bar_return
            bar_returns.append(bar_return)
            exposed_bars += 1
        else:
            bar_returns.append(0.0)

        a1, a0 = line_a[i], line_a[i - 1]
        b1, b0 = line_b[i], line_b[i - 1]
        if not all(math.isfinite(float(x)) for x in (a1, a0, b1, b0)):
            target = position
        elif b1 > a1 and b0 <= a0:
            target = 1
        elif a1 > b1 and a0 <= b0:
            target = -1
        else:
            target = position

        if target != position:
            equity *= 1.0 - cost
            if position != 0 and entry_price > 0:
                trade_return = position * (close[i] / entry_price - 1.0) - cost
                trade_returns.append(trade_return)
            position = target
            entry_price = close[i] if position != 0 else 0.0

        peak = max(peak, equity)
        drawdown = equity / peak - 1.0
        max_dd = min(max_dd, drawdown)

    if position != 0 and entry_price > 0:
        trade_return = position * (close[-1] / entry_price - 1.0) - cost
        trade_returns.append(trade_return)

    wins = [r for r in trade_returns if r > 0]
    losses = [r for r in trade_returns if r < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_win / gross_loss if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    bar_series = np.asarray(bar_returns, dtype=float)
    std = float(bar_series.std(ddof=0)) if bar_series.size else 0.0
    sharpe = float(bar_series.mean() / std * math.sqrt(375 * 252)) if std > 0 else 0.0

    buy_hold = (close[-1] / close[warmup] - 1.0) * 100.0
    return BacktestMetrics(
        trades=len(trade_returns),
        win_rate=(len(wins) / len(trade_returns)) if trade_returns else 0.0,
        total_return_pct=(equity - 1.0) * 100.0,
        buy_hold_pct=buy_hold,
        avg_trade_pct=(float(np.mean(trade_returns)) * 100.0) if trade_returns else 0.0,
        max_drawdown_pct=max_dd * 100.0,
        profit_factor=profit_factor,
        exposure_pct=(exposed_bars / max(len(bar_returns), 1)) * 100.0,
        sharpe=sharpe,
        final_equity=equity,
    )


def compare(candles: pd.DataFrame, cfg: StrategyConfig, cost_bps: float = 2.0) -> dict:
    frame = normalize_candles(candles)
    with_k = run_backtest(frame, cfg, use_kalman=True, cost_bps=cost_bps).to_dict()
    without_k = run_backtest(frame, cfg, use_kalman=False, cost_bps=cost_bps).to_dict()

    def delta(key: str) -> float:
        return round(with_k[key] - without_k[key], 4)

    return {
        "bars": int(len(frame)),
        "from": str(frame.index[0]),
        "to": str(frame.index[-1]),
        "config": {
            "length": cfg.length,
            "gain": cfg.gain,
            "cost_bps": cost_bps,
        },
        "kalman": with_k,
        "no_kalman": without_k,
        "delta": {
            "total_return_pct": delta("total_return_pct"),
            "max_drawdown_pct": delta("max_drawdown_pct"),
            "profit_factor": delta("profit_factor"),
            "win_rate": delta("win_rate"),
            "sharpe": delta("sharpe"),
            "trades": with_k["trades"] - without_k["trades"],
        },
        "verdict": _verdict(with_k, without_k),
    }


def _verdict(with_k: dict, without_k: dict) -> str:
    score_k = with_k["total_return_pct"] + with_k["max_drawdown_pct"] * 0.5 + with_k["profit_factor"]
    score_n = without_k["total_return_pct"] + without_k["max_drawdown_pct"] * 0.5 + without_k["profit_factor"]
    if abs(score_k - score_n) < 0.25:
        return "inconclusive — too close to call"
    return "kalman looks better" if score_k > score_n else "kalman does not add edge here"


def load_csv(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame.columns = [str(c).strip().lower() for c in frame.columns]
    ts_col = next((c for c in ("timestamp", "datetime", "date", "time") if c in frame.columns), None)
    if ts_col is None:
        raise ValueError("CSV needs a timestamp/datetime/date/time column")
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame[ts_col]))
    return normalize_candles(frame.drop(columns=[ts_col]))


def main() -> None:
    parser = argparse.ArgumentParser(description="BRAHMA signal backtest (Kalman A/B)")
    parser.add_argument("--csv", help="CSV with timestamp,open,high,low,close,volume")
    parser.add_argument("--length", type=int, default=None)
    parser.add_argument("--gain", type=float, default=None)
    parser.add_argument("--cost-bps", type=float, default=2.0)
    parser.add_argument("--limit", type=int, default=5000)
    args = parser.parse_args()

    settings = get_settings()
    cfg = StrategyConfig.from_settings(settings)
    if args.length is not None or args.gain is not None:
        cfg = StrategyConfig(
            length=args.length or cfg.length,
            gain=args.gain or cfg.gain,
            use_kalman=cfg.use_kalman,
            use_trend_filter=cfg.use_trend_filter,
            trend_ema=cfg.trend_ema,
            min_bars_between_signals=cfg.min_bars_between_signals,
        )

    if args.csv:
        candles = load_csv(args.csv)
    else:
        from .providers import build_provider

        provider = build_provider(settings)
        candles = provider.get_candles(limit=args.limit)

    result = compare(candles, cfg, cost_bps=args.cost_bps)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
