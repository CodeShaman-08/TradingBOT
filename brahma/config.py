"""Configuration loaded from environment / .env.

Deliberately dependency-light (no pydantic-settings) so the project runs on a
plain Python install. Every value is validated before the bot starts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _s(key: str, default: str) -> str:
    value = os.getenv(key)
    return default if value is None else value.strip()


def _b(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _i(key: str, default: int) -> int:
    raw = os.getenv(key)
    return default if raw is None or raw.strip() == "" else int(raw)


def _f(key: str, default: float) -> float:
    raw = os.getenv(key)
    return default if raw is None or raw.strip() == "" else float(raw)


def _csv(key: str, default: str = "") -> tuple[str, ...]:
    raw = _s(key, default)
    return tuple(x.strip() for x in raw.split(",") if x.strip())


@dataclass(frozen=True)
class Settings:
    # ---------------------------------------------------------------- paths
    root: Path = ROOT
    data_dir: Path = field(default_factory=lambda: ROOT / "data")

    # ---------------------------------------------------------- credentials
    client_id: str = field(default_factory=lambda: _s("DHAN_CLIENT_ID", ""))
    access_token: str = field(default_factory=lambda: _s("DHAN_ACCESS_TOKEN", ""))

    # ---------------------------------------------------------------- modes
    live_trading: bool = field(default_factory=lambda: _b("LIVE_TRADING_ENABLED", False))
    broker: str = field(default_factory=lambda: _s("BROKER", "dhan").lower())
    data_provider: str = field(default_factory=lambda: _s("DATA_PROVIDER", "dhan").lower())

    # ----------------------------------------------------------- instrument
    underlying_symbol: str = field(default_factory=lambda: _s("UNDERLYING_SYMBOL", "BANKNIFTY"))
    underlying_security_id: str = field(default_factory=lambda: _s("UNDERLYING_SECURITY_ID", ""))
    underlying_exchange_segment: str = field(default_factory=lambda: _s("UNDERLYING_EXCHANGE_SEGMENT", "IDX_I"))
    underlying_instrument_type: str = field(default_factory=lambda: _s("UNDERLYING_INSTRUMENT_TYPE", "INDEX"))
    option_security_id: str = field(default_factory=lambda: _s("OPTION_SECURITY_ID", ""))
    option_side: str = field(default_factory=lambda: _s("OPTION_SIDE", "CE").upper())
    option_lot_size: int = field(default_factory=lambda: _i("OPTION_LOT_SIZE", 0))
    lots: int = field(default_factory=lambda: _i("NUMBER_OF_LOTS", 1))
    max_lots: int = field(default_factory=lambda: _i("MAX_LOTS", 1))
    expiry: str = field(default_factory=lambda: _s("OPTION_EXPIRY", ""))
    strike_mode: str = field(default_factory=lambda: _s("STRIKE_MODE", "ATM").upper())

    # ---------------------------------------------------------------- data
    timeframe: str = field(default_factory=lambda: _s("TIMEFRAME", "1m"))
    lookback_days: int = field(default_factory=lambda: _i("LOOKBACK_DAYS", 5))
    max_candle_age_sec: int = field(default_factory=lambda: _i("MAX_CANDLE_AGE_SEC", 180))
    poll_interval_sec: int = field(default_factory=lambda: _i("CHECK_INTERVAL", 5))

    # ------------------------------------------------------------ strategy
    length: int = field(default_factory=lambda: _i("LENGTH", 22))
    gain: float = field(default_factory=lambda: _f("GAIN", 0.7))
    use_kalman: bool = field(default_factory=lambda: _b("USE_KALMAN", True))
    use_trend_filter: bool = field(default_factory=lambda: _b("USE_TREND_FILTER", False))
    trend_ema: int = field(default_factory=lambda: _i("TREND_EMA", 50))
    min_bars_between_signals: int = field(default_factory=lambda: _i("MIN_BARS_BETWEEN_SIGNALS", 0))

    # ---------------------------------------------------------------- risk
    max_daily_loss: float = field(default_factory=lambda: _f("MAX_DAILY_LOSS", 1000.0))
    max_trade_loss: float = field(default_factory=lambda: _f("MAX_TRADE_LOSS", 500.0))
    stop_loss_pct: float = field(default_factory=lambda: _f("STOP_LOSS_PERCENT", 0.20))
    max_trades_per_day: int = field(default_factory=lambda: _i("MAX_TRADES_PER_DAY", 3))
    max_holding_minutes: int = field(default_factory=lambda: _i("MAX_HOLDING_MINUTES", 0))
    cooldown_after_loss_min: int = field(default_factory=lambda: _i("COOLDOWN_AFTER_LOSS_MIN", 0))

    # -------------------------------------------------------------- session
    tz: str = field(default_factory=lambda: _s("TIMEZONE", "Asia/Kolkata"))
    market_open: str = field(default_factory=lambda: _s("MARKET_OPEN", "09:15"))
    market_close: str = field(default_factory=lambda: _s("MARKET_CLOSE", "15:30"))
    square_off: str = field(default_factory=lambda: _s("SQUARE_OFF_TIME", "15:15"))
    no_entry_after: str = field(default_factory=lambda: _s("NO_ENTRY_AFTER", "14:45"))
    holidays: tuple[str, ...] = field(default_factory=lambda: _csv("MARKET_HOLIDAYS", ""))
    ignore_market_hours: bool = field(default_factory=lambda: _b("IGNORE_MARKET_HOURS", False))

    # ------------------------------------------------------------ execution
    product_type: str = field(default_factory=lambda: _s("PRODUCT_TYPE", "INTRADAY").upper())
    order_type: str = field(default_factory=lambda: _s("ORDER_TYPE", "MARKET").upper())
    use_broker_sl: bool = field(default_factory=lambda: _b("USE_BROKER_SL", False))
    broker_sl_pct: float = field(default_factory=lambda: _f("BROKER_SL_PERCENT", 0.15))
    order_confirm_timeout_sec: int = field(default_factory=lambda: _i("ORDER_CONFIRM_TIMEOUT_SEC", 10))
    slippage_bps: float = field(default_factory=lambda: _f("PAPER_SLIPPAGE_BPS", 5.0))
    paper_starting_cash: float = field(default_factory=lambda: _f("PAPER_STARTING_CASH", 1_000_000.0))
    paper_premium_pct: float = field(default_factory=lambda: _f("PAPER_PREMIUM_PCT", 0.02))
    paper_delta: float = field(default_factory=lambda: _f("PAPER_DELTA", 0.5))

    # ------------------------------------------------------------ dashboard
    host: str = field(default_factory=lambda: _s("DASHBOARD_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _i("DASHBOARD_PORT", 8000))

    # --------------------------------------------------------------- replay
    replay_csv: str = field(default_factory=lambda: _s("REPLAY_CSV", ""))
    replay_days: int = field(default_factory=lambda: _i("REPLAY_DAYS", 20))
    replay_initial_bars: int = field(default_factory=lambda: _i("REPLAY_INITIAL_BARS", 150))
    replay_bars_per_sec: float = field(default_factory=lambda: _f("REPLAY_BARS_PER_SEC", 0.5))
    replay_seed: int = field(default_factory=lambda: _i("REPLAY_SEED", 7))

    # ----------------------------------------------------------------- misc
    log_level: str = field(default_factory=lambda: _s("LOG_LEVEL", "INFO").upper())
    log_file: Path = field(default_factory=lambda: Path(_s("BOT_LOG_FILE", str(ROOT / "data" / "brahma.log"))))
    state_file: Path = field(default_factory=lambda: Path(_s("BOT_STATE_FILE", str(ROOT / "data" / "state.json"))))
    trade_journal: Path = field(default_factory=lambda: Path(_s("TRADE_JOURNAL", str(ROOT / "data" / "trades.jsonl"))))
    order_journal: Path = field(default_factory=lambda: Path(_s("ORDER_JOURNAL", str(ROOT / "data" / "orders.jsonl"))))
    paper_account: Path = field(default_factory=lambda: Path(_s("PAPER_ACCOUNT_FILE", str(ROOT / "data" / "paper_account.json"))))
    instrument_master: Path = field(default_factory=lambda: Path(_s("INSTRUMENT_MASTER_FILE", str(ROOT / "data" / "instrument_master.csv"))))
    engine_enabled: bool = field(default_factory=lambda: _b("ENABLE_ENGINE", True))

    # ------------------------------------------------------------ validation
    def validate(self) -> None:
        errors: list[str] = []

        if self.broker not in {"paper", "dhan"}:
            errors.append("BROKER must be 'paper' or 'dhan'")
        if self.data_provider not in {"replay", "dhan"}:
            errors.append("DATA_PROVIDER must be 'replay' or 'dhan'")
        if self.live_trading and self.broker != "dhan":
            errors.append("LIVE_TRADING_ENABLED=true requires BROKER=dhan")
        if (self.broker == "dhan" or self.data_provider == "dhan") and not (
            self.client_id and self.access_token
        ):
            errors.append("DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN are required for Dhan broker/data")

        if self.option_side not in {"CE", "PE"}:
            errors.append("OPTION_SIDE must be CE or PE")
        if self.strike_mode not in {"ATM", "ITM1", "OTM1", "ITM2", "OTM2"}:
            errors.append("STRIKE_MODE must be ATM/ITM1/OTM1/ITM2/OTM2")
        if self.lots <= 0:
            errors.append("NUMBER_OF_LOTS must be > 0")
        if self.max_lots <= 0:
            errors.append("MAX_LOTS must be > 0")
        if self.lots > self.max_lots:
            errors.append("NUMBER_OF_LOTS cannot exceed MAX_LOTS")
        if self.option_lot_size < 0:
            errors.append("OPTION_LOT_SIZE cannot be negative (0 = auto-resolve)")

        if self.length < 5:
            errors.append("LENGTH must be >= 5")
        if not 0 < self.gain < 1:
            errors.append("GAIN must be between 0 and 1")
        if self.trend_ema < 2:
            errors.append("TREND_EMA must be >= 2")
        if self.min_bars_between_signals < 0:
            errors.append("MIN_BARS_BETWEEN_SIGNALS cannot be negative")

        if self.max_daily_loss <= 0:
            errors.append("MAX_DAILY_LOSS must be > 0")
        if self.max_trade_loss <= 0:
            errors.append("MAX_TRADE_LOSS must be > 0")
        if not 0 < self.stop_loss_pct < 1:
            errors.append("STOP_LOSS_PERCENT must be between 0 and 1")
        if self.max_trades_per_day < 0:
            errors.append("MAX_TRADES_PER_DAY cannot be negative")
        if self.max_holding_minutes < 0:
            errors.append("MAX_HOLDING_MINUTES cannot be negative")
        if self.cooldown_after_loss_min < 0:
            errors.append("COOLDOWN_AFTER_LOSS_MIN cannot be negative")

        if self.poll_interval_sec < 2:
            errors.append("CHECK_INTERVAL must be >= 2 seconds")
        if self.max_candle_age_sec < 30:
            errors.append("MAX_CANDLE_AGE_SEC must be >= 30")
        if self.lookback_days < 1:
            errors.append("LOOKBACK_DAYS must be >= 1")
        if not 0 <= self.slippage_bps <= 1000:
            errors.append("PAPER_SLIPPAGE_BPS must be between 0 and 1000")
        if self.paper_starting_cash <= 0:
            errors.append("PAPER_STARTING_CASH must be > 0")
        if not 0 < self.paper_premium_pct < 1:
            errors.append("PAPER_PREMIUM_PCT must be between 0 and 1")
        if not 0 < self.paper_delta <= 2:
            errors.append("PAPER_DELTA must be between 0 and 2")
        if not 0 < self.broker_sl_pct < 1:
            errors.append("BROKER_SL_PERCENT must be between 0 and 1")
        if self.order_confirm_timeout_sec < 1:
            errors.append("ORDER_CONFIRM_TIMEOUT_SEC must be >= 1")
        if not 1 <= self.port <= 65535:
            errors.append("DASHBOARD_PORT must be a valid port")
        if self.replay_bars_per_sec <= 0:
            errors.append("REPLAY_BARS_PER_SEC must be > 0")
        if self.replay_initial_bars < 50:
            errors.append("REPLAY_INITIAL_BARS must be >= 50")
        if self.replay_days < 1:
            errors.append("REPLAY_DAYS must be >= 1")

        if errors:
            raise ValueError("Invalid configuration:\n  - " + "\n  - ".join(errors))

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        for path in (
            self.state_file,
            self.trade_journal,
            self.order_journal,
            self.paper_account,
            self.instrument_master,
            self.log_file,
        ):
            path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def mode_label(self) -> str:
        if self.live_trading:
            return "LIVE"
        if self.data_provider == "replay":
            return "DEMO"
        return "PAPER"

    def public_dict(self) -> dict:
        """Config safe to expose on the dashboard (no secrets)."""
        return {
            "mode": self.mode_label,
            "live_trading": self.live_trading,
            "broker": self.broker,
            "data_provider": self.data_provider,
            "underlying_symbol": self.underlying_symbol,
            "option_side": self.option_side,
            "strike_mode": self.strike_mode,
            "expiry": self.expiry or "nearest",
            "lots": self.lots,
            "lot_size": self.option_lot_size or "auto",
            "product_type": self.product_type,
            "order_type": self.order_type,
            "use_broker_sl": self.use_broker_sl,
            "strategy": {
                "length": self.length,
                "gain": self.gain,
                "use_kalman": self.use_kalman,
                "use_trend_filter": self.use_trend_filter,
                "trend_ema": self.trend_ema,
                "min_bars_between_signals": self.min_bars_between_signals,
            },
            "risk": {
                "max_daily_loss": self.max_daily_loss,
                "max_trade_loss": self.max_trade_loss,
                "stop_loss_pct": self.stop_loss_pct,
                "max_trades_per_day": self.max_trades_per_day,
                "max_holding_minutes": self.max_holding_minutes,
                "cooldown_after_loss_min": self.cooldown_after_loss_min,
            },
            "session": {
                "timezone": self.tz,
                "market_open": self.market_open,
                "market_close": self.market_close,
                "square_off": self.square_off,
                "no_entry_after": self.no_entry_after,
                "ignore_market_hours": self.ignore_market_hours,
                "holidays": list(self.holidays),
            },
            "poll_interval_sec": self.poll_interval_sec,
            "max_candle_age_sec": self.max_candle_age_sec,
        }


def get_settings() -> Settings:
    settings = Settings()
    settings.validate()
    settings.ensure_dirs()
    return settings
