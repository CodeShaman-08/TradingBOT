"""Shared typed records used across provider / broker / engine / API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

FILLED_STATUSES = {"TRADED", "FILLED", "COMPLETE", "COMPLETED", "EXECUTED"}
FAILED_STATUSES = {"REJECTED", "CANCELLED", "CANCELED", "EXPIRED", "FAILED", "FAILURE"}


@dataclass
class Signal:
    action: str  # BUY | SELL | HOLD (direction of the UNDERLYING)
    reason: str
    candle_ts: str | None
    price: float
    line_a: float
    line_b: float
    prev_a: float
    prev_b: float
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "candle_ts": self.candle_ts,
            "price": self.price,
            "line_a": self.line_a,
            "line_b": self.line_b,
            "prev_a": self.prev_a,
            "prev_b": self.prev_b,
            "generated_at": self.generated_at,
        }


@dataclass
class Snapshot:
    net_qty: int = 0
    avg_price: float = 0.0
    unrealized: float = 0.0
    realized: float = 0.0
    day_pnl: float = 0.0
    ltp: float = 0.0
    pnl_source: str = "broker"
    source: str = "paper"

    def to_dict(self) -> dict[str, Any]:
        return {
            "net_qty": self.net_qty,
            "avg_price": round(self.avg_price, 4),
            "unrealized": round(self.unrealized, 2),
            "realized": round(self.realized, 2),
            "day_pnl": round(self.day_pnl, 2),
            "ltp": round(self.ltp, 4),
            "pnl_source": self.pnl_source,
            "source": self.source,
        }


@dataclass
class OrderIntent:
    correlation_id: str
    side: str
    qty: int
    security_id: str
    exchange_segment: str
    reason: str
    kind: str  # ENTRY | EXIT | BROKER_SL
    created_at: str
    ref_price: float = 0.0
    limit_price: float = 0.0
    trigger_price: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "side": self.side,
            "qty": self.qty,
            "security_id": self.security_id,
            "exchange_segment": self.exchange_segment,
            "reason": self.reason,
            "kind": self.kind,
            "created_at": self.created_at,
            "ref_price": self.ref_price,
            "limit_price": self.limit_price,
            "trigger_price": self.trigger_price,
        }


@dataclass
class OrderResult:
    accepted: bool
    status: str
    order_id: str | None = None
    correlation_id: str | None = None
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    message: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status.upper() in FILLED_STATUSES and self.filled_qty > 0

    @property
    def is_failed(self) -> bool:
        return (not self.accepted) or self.status.upper() in FAILED_STATUSES

    @property
    def is_pending(self) -> bool:
        return not self.is_filled and not self.is_failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "status": self.status,
            "order_id": self.order_id,
            "correlation_id": self.correlation_id,
            "filled_qty": self.filled_qty,
            "avg_fill_price": self.avg_fill_price,
            "message": self.message,
        }


@dataclass
class Instrument:
    security_id: str
    trading_symbol: str = ""
    lot_size: int = 0
    tick_size: float = 0.05
    freeze_qty: int = 0
    exchange_segment: str = "NSE_FNO"
    instrument_type: str = "OPTIDX"
    expiry: str | None = None
    strike: float | None = None
    option_type: str | None = None
    source: str = "config"

    def to_dict(self) -> dict[str, Any]:
        return {
            "security_id": self.security_id,
            "trading_symbol": self.trading_symbol,
            "lot_size": self.lot_size,
            "tick_size": self.tick_size,
            "freeze_qty": self.freeze_qty,
            "exchange_segment": self.exchange_segment,
            "instrument_type": self.instrument_type,
            "expiry": self.expiry,
            "strike": self.strike,
            "option_type": self.option_type,
            "source": self.source,
        }
