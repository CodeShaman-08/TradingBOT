"""Pre-trade and in-trade risk checks.

The important architectural rule: protective exits are evaluated on EVERY tick,
independently of new candles and independently of whether entries are blocked.
Only a hard kill switch stops new entries; it never disables the stop-loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Instrument, Snapshot
from .timeutils import at_or_after, ensure_aware, parse_iso


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    fatal: bool = False


@dataclass
class ExitDecision:
    should_exit: bool
    reason: str = ""
    urgent: bool = False


class RiskManager:
    def __init__(self, settings) -> None:
        self.settings = settings

    # ------------------------------------------------------------- sizing
    def stop_amount(self, snapshot: Snapshot) -> float:
        value = max(snapshot.avg_price, 0.0) * abs(snapshot.net_qty)
        pct_amount = value * self.settings.stop_loss_pct if value > 0 else self.settings.max_trade_loss
        return min(self.settings.max_trade_loss, pct_amount)

    def order_quantity(self, instrument: Instrument) -> int:
        lot = int(instrument.lot_size or self.settings.option_lot_size)
        if lot <= 0:
            raise ValueError("Lot size unresolved; refusing to size an order")
        return lot * int(self.settings.lots)

    def validate_quantity(self, instrument: Instrument, qty: int) -> RiskDecision:
        lot = int(instrument.lot_size or self.settings.option_lot_size)
        if lot <= 0:
            return RiskDecision(False, "lot size unresolved")
        if qty <= 0:
            return RiskDecision(False, "quantity <= 0")
        if qty % lot != 0:
            return RiskDecision(False, f"quantity {qty} is not a multiple of lot {lot}")
        if qty > lot * self.settings.max_lots:
            return RiskDecision(False, f"quantity {qty} exceeds MAX_LOTS={self.settings.max_lots}")
        freeze = int(instrument.freeze_qty or 0)
        if freeze > 0 and qty > freeze:
            return RiskDecision(False, f"quantity {qty} exceeds exchange freeze qty {freeze}")
        return RiskDecision(True, "ok")

    # ------------------------------------------------------------ pre-trade
    def pre_trade(
        self,
        snapshot: Snapshot,
        state: dict,
        now: datetime,
        session: str,
        instrument: Instrument | None,
        candle_age_sec: float,
        qty: int,
    ) -> RiskDecision:
        if state.get("kill_switch"):
            return RiskDecision(False, state.get("kill_reason") or "kill switch active", fatal=True)
        if state.get("entries_blocked"):
            return RiskDecision(False, state.get("entries_block_reason") or "entries blocked")
        if state.get("pending_order"):
            return RiskDecision(False, "pending order awaiting confirmation")
        if instrument is None:
            return RiskDecision(False, "instrument unresolved")

        if session != "OPEN" and not self.settings.ignore_market_hours:
            return RiskDecision(False, f"market is {session}")
        if not self.settings.ignore_market_hours and at_or_after(
            now, now.date(), self.settings.no_entry_after, self.settings.tz
        ):
            return RiskDecision(False, "past NO_ENTRY_AFTER")

        if snapshot.net_qty != 0:
            return RiskDecision(False, "existing position detected")

        if snapshot.day_pnl <= -self.settings.max_daily_loss:
            return RiskDecision(False, "daily loss limit reached")

        if state.get("entries", 0) >= self.settings.max_trades_per_day:
            return RiskDecision(False, "maximum daily entries reached")

        last_exit = state.get("last_exit") or {}
        cooldown = int(self.settings.cooldown_after_loss_min)
        if cooldown > 0 and last_exit.get("pnl", 0) < 0:
            exited_at = parse_iso(last_exit.get("ts"), self.settings.tz)
            if exited_at is not None:
                age_min = (ensure_aware(now, self.settings.tz) - exited_at).total_seconds() / 60.0
                if age_min < cooldown:
                    return RiskDecision(False, f"cooldown after loss ({age_min:.1f}/{cooldown} min)")

        if self.settings.data_provider != "replay":
            if candle_age_sec > self.settings.max_candle_age_sec:
                return RiskDecision(False, f"candle is stale ({candle_age_sec:.0f}s)")

        return self.validate_quantity(instrument, qty)

    # ------------------------------------------------------------- in-trade
    def in_trade(
        self,
        snapshot: Snapshot,
        position: dict | None,
        now: datetime,
        session: str,
    ) -> ExitDecision:
        if snapshot.net_qty <= 0:
            return ExitDecision(False)

        # If the market is closed we cannot send an exit; the engine logs it.
        if session != "OPEN" and not self.settings.ignore_market_hours:
            return ExitDecision(False, "market closed; exit deferred")

        if at_or_after(now, now.date(), self.settings.square_off, self.settings.tz):
            if not self.settings.ignore_market_hours:
                return ExitDecision(True, "SQUARE_OFF", urgent=True)

        if snapshot.day_pnl <= -self.settings.max_daily_loss:
            return ExitDecision(True, "DAILY_LOSS", urgent=True)

        stop = self.stop_amount(snapshot)
        if stop > 0 and snapshot.unrealized <= -stop:
            return ExitDecision(True, "STOP_LOSS", urgent=True)

        max_hold = int(self.settings.max_holding_minutes)
        if max_hold > 0 and position:
            opened = parse_iso(position.get("opened_at"), self.settings.tz)
            if opened is not None:
                held_min = (ensure_aware(now, self.settings.tz) - opened).total_seconds() / 60.0
                if held_min >= max_hold:
                    return ExitDecision(True, "MAX_HOLDING_TIME", urgent=True)

        return ExitDecision(False)
