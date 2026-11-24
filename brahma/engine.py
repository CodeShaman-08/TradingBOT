"""The bot loop.

Control flow (this is the fix for the original script's biggest flaw):

    every tick:  snapshot -> protective exits (stop / daily loss / square-off)
    new candle:  strategy signal -> entry / strategy exit

Entries can be blocked without ever disabling protective exits. A pending order
is reconciled before anything else happens, so a crash cannot silently
double-submit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from datetime import datetime
from uuid import uuid4

from .broker import build_broker
from .config import Settings
from .instruments import InstrumentMaster, UnderlyingInfo, fallback_instrument
from .models import Instrument, OrderIntent, OrderResult, Signal, Snapshot
from .providers import DataError, build_provider
from .providers.base import MarketDataProvider
from .risk import RiskManager
from .state import JsonlJournal, StateStore
from .strategy import SignalEngine, StrategyConfig, drop_unfinished
from .timeutils import candle_age_seconds, iso, market_state, now_ist

log = logging.getLogger("brahma.engine")


class BotEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.live_trading = bool(settings.live_trading)
        self.state = StateStore(settings.state_file, settings.tz)
        self.trade_journal = JsonlJournal(settings.trade_journal)
        self.order_journal = JsonlJournal(settings.order_journal)
        self.risk = RiskManager(settings)
        self.strategy = SignalEngine(StrategyConfig.from_settings(settings))

        self.provider: MarketDataProvider | None = None
        self.broker = None
        self.instrument: Instrument | None = None
        self.underlying: UnderlyingInfo | None = None
        self.master: InstrumentMaster | None = None

        self.last_result = None
        self.ready = False
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._order_lock = asyncio.Lock()

        self.status: dict = {
            "started_at": None,
            "last_tick": None,
            "last_tick_ms": 0.0,
            "ticks": 0,
            "session": None,
            "snapshot": None,
            "signal": None,
            "market": {},
            "broker_error": None,
            "data_error": None,
            "last_error": None,
        }

    # =============================================================== lifecycle
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        if not self.ready:
            await self.startup()
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="brahma-engine")
        log.info("Engine task started")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self.provider is not None:
            self.provider.close()
        self.state.flush()
        log.info("Engine stopped")

    async def startup(self) -> None:
        self.settings.validate()
        self.settings.ensure_dirs()
        self.state.load()
        self.status["started_at"] = iso(now_ist(self.settings.tz))

        await self._startup_provider()
        await self._startup_instrument()
        await self._startup_broker()

        if self.broker is not None:
            await self._reconcile_pending()

        self.ready = True
        log.info(
            "Startup complete | mode=%s broker=%s provider=%s instrument=%s lot=%s",
            self.mode_label,
            self.settings.broker,
            self.settings.data_provider,
            self.instrument.security_id if self.instrument else "unresolved",
            self.instrument.lot_size if self.instrument else "-",
        )

    async def _startup_provider(self) -> None:
        try:
            if self.settings.data_provider == "replay":
                self.provider = build_provider(self.settings)
                self.underlying = UnderlyingInfo(
                    symbol=self.settings.underlying_symbol,
                    security_id="REPLAY",
                    exchange_segment="IDX_I",
                    instrument_type="INDEX",
                    source="replay",
                )
                return

            self.master = InstrumentMaster(self.settings)
            resolved = await asyncio.to_thread(self.master.resolve_underlying, self.settings.underlying_symbol)
            if resolved is not None:
                self.underlying = resolved
            elif self.settings.underlying_security_id:
                self.underlying = UnderlyingInfo(
                    symbol=self.settings.underlying_symbol,
                    security_id=self.settings.underlying_security_id,
                    exchange_segment=self.settings.underlying_exchange_segment,
                    instrument_type=self.settings.underlying_instrument_type,
                    source="config",
                )
            else:
                raise DataError(
                    "Could not resolve the underlying index. Set UNDERLYING_SECURITY_ID."
                )

            self.provider = build_provider(
                self.settings,
                underlying_security_id=self.underlying.security_id,
                exchange_segment=self.underlying.exchange_segment,
                instrument_type=self.underlying.instrument_type,
            )
        except Exception as exc:  # noqa: BLE001
            self.status["data_error"] = str(exc)
            log.exception("Market-data startup failed")
            self.state.set_entries_blocked(True, f"data provider unavailable: {exc}")

    async def _startup_instrument(self) -> None:
        try:
            if self.settings.option_security_id:
                instrument = None
                if self.master is not None:
                    try:
                        instrument = await asyncio.to_thread(
                            self.master.resolve_by_security_id, self.settings.option_security_id
                        )
                    except Exception:  # noqa: BLE001
                        instrument = None
                self.instrument = instrument or fallback_instrument(self.settings)

            elif self.settings.broker == "paper":
                # Offline paper mode: no master needed, use a synthetic contract.
                self.instrument = Instrument(
                    security_id="PAPER-OPT",
                    trading_symbol=f"{self.settings.underlying_symbol}-{self.settings.option_side}",
                    lot_size=int(self.settings.option_lot_size) or 30,
                    exchange_segment="NSE_FNO",
                    instrument_type="OPTIDX",
                    option_type=self.settings.option_side,
                    source="paper",
                )

            elif self.master is not None and self.provider is not None:
                spot = await asyncio.to_thread(self.provider.get_ltp)
                if spot <= 0:
                    frame = await asyncio.to_thread(self.provider.get_candles, 10)
                    spot = float(frame["close"].iloc[-1])
                self.instrument = await asyncio.to_thread(
                    self.master.resolve_option,
                    spot,
                    self.settings.option_side,
                    self.settings.expiry or None,
                    self.settings.strike_mode,
                )
            else:
                self.instrument = fallback_instrument(self.settings)

            if self.instrument is None:
                self.state.set_entries_blocked(True, "option instrument unresolved")
                return

            if self.instrument.lot_size <= 0:
                self.instrument.lot_size = int(self.settings.option_lot_size) or 30

            if self.settings.option_lot_size > 0:
                self.instrument.lot_size = int(self.settings.option_lot_size)

            self.status["instrument"] = self.instrument.to_dict()
            self.status["underlying"] = self.underlying.to_dict() if self.underlying else None
            log.info(
                "Instrument resolved: %s (%s) lot=%s expiry=%s strike=%s",
                self.instrument.trading_symbol,
                self.instrument.security_id,
                self.instrument.lot_size,
                self.instrument.expiry,
                self.instrument.strike,
            )
        except Exception as exc:  # noqa: BLE001
            self.instrument = fallback_instrument(self.settings)
            self.state.set_entries_blocked(True, f"instrument resolution failed: {exc}")
            log.exception("Instrument resolution failed")

    async def _startup_broker(self) -> None:
        if self.provider is None:
            return
        if self.instrument is None:
            self.state.set_entries_blocked(True, "option instrument unresolved; broker not started")
            log.error("Broker not started: option instrument is unresolved")
            return
        try:
            self.broker = build_broker(
                self.settings,
                self.instrument,
                underlying_ltp_fn=self._safe_underlying_ltp,
            )
            self.status["broker"] = self.broker.health()
        except Exception as exc:  # noqa: BLE001
            self.broker = None
            self.status["broker_error"] = str(exc)
            self.state.set_entries_blocked(True, f"broker unavailable: {exc}")
            log.exception("Broker startup failed")

    def _safe_underlying_ltp(self) -> float:
        if self.provider is None:
            return 0.0
        try:
            return float(self.provider.get_ltp())
        except Exception:  # noqa: BLE001
            return 0.0

    @property
    def mode_label(self) -> str:
        if self.live_trading:
            return "LIVE"
        if self.settings.data_provider == "replay":
            return "DEMO"
        return "PAPER"

    # ==================================================================== loop
    async def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self._tick()
                self.status["last_error"] = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.status["last_error"] = str(exc)
                log.exception("Tick failed")

            self.status["ticks"] += 1
            self.status["last_tick_ms"] = round((time.monotonic() - started) * 1000.0, 1)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=float(self.settings.poll_interval_sec))
            except asyncio.TimeoutError:
                continue

    async def _tick(self) -> None:
        now = now_ist(self.settings.tz)
        session = market_state(
            now,
            self.settings.market_open,
            self.settings.market_close,
            self.settings.holidays,
            self.settings.tz,
        )
        self.status["last_tick"] = iso(now)
        self.status["session"] = session

        if self.provider is None:
            await self._refresh_market_data(now)
            return

        # 1) Never act while an order's outcome is unknown.
        if not await self._reconcile_pending():
            await self._refresh_market_data(now)
            return

        # 2) Broker snapshot + protective exits on EVERY tick.
        snapshot: Snapshot | None = None
        if self.broker is not None and self.instrument is not None:
            try:
                snapshot = await asyncio.to_thread(self.broker.get_snapshot, self.instrument.security_id)
                self.status["snapshot"] = snapshot.to_dict()
                self.status["broker_error"] = None
            except Exception as exc:  # noqa: BLE001
                self.status["broker_error"] = str(exc)
                log.error("Broker snapshot failed: %s", exc)

        if snapshot is not None:
            await self._reconcile_position(snapshot)
            exit_decision = self.risk.in_trade(
                snapshot,
                self.state.state.get("position"),
                now,
                session,
            )
            if exit_decision.should_exit:
                log.warning("Protective exit requested: %s", exit_decision.reason)
                await self._execute_exit(exit_decision.reason, snapshot)
                return

            if snapshot.day_pnl <= -self.settings.max_daily_loss:
                if not self.state.state.get("entries_blocked"):
                    self.state.set_entries_blocked(True, "daily loss limit reached")

        # 3) Market data / signal (also powers the dashboard when closed).
        result = await self._refresh_market_data(now)
        if result is None or not result.ready:
            return

        # 4) Signal logic only on a NEW closed candle.
        if result.last_candle_ts == self.state.state.get("last_candle"):
            return

        self.state.push_signal(result.signal.to_dict(), last_candle=result.last_candle_ts)

        if self.broker is None or self.instrument is None or snapshot is None:
            return

        action = result.signal.action
        entry_action = "BUY" if self.settings.option_side == "CE" else "SELL"
        exit_action = "SELL" if entry_action == "BUY" else "BUY"

        if action == entry_action:
            if snapshot.net_qty != 0:
                # Already long this option; repeated entry crosses are not adds.
                return
            if self.state.state.get("entries_blocked") or self.state.state.get("kill_switch"):
                return
            qty = self.risk.order_quantity(self.instrument)
            age = candle_age_seconds(result.candles.index[-1], now, self.settings.tz)
            decision = self.risk.pre_trade(
                snapshot,
                self.state.state,
                now,
                session,
                self.instrument,
                age,
                qty,
            )
            if decision.allowed:
                await self._execute_entry(result.signal, qty, snapshot)
            else:
                log.info("Entry blocked: %s", decision.reason)
                if decision.fatal:
                    self.state.set_kill_switch(True, decision.reason)

        elif action == exit_action and snapshot.net_qty > 0:
            await self._execute_exit("STRATEGY_EXIT", snapshot)

    # ============================================================== market data
    async def _refresh_market_data(self, now: datetime):
        if self.provider is None:
            return None
        try:
            limit = max(int(self.settings.lookback_days) * 400, 500)
            frame = await asyncio.to_thread(self.provider.get_candles, limit)
            frame = drop_unfinished(frame, now, self.settings.timeframe)
            if frame is None or len(frame) == 0:
                raise DataError("provider returned no closed candles")

            last_ts = iso(frame.index[-1])
            if self.settings.data_provider == "replay":
                # Replay timestamps are simulated; wall-clock staleness is meaningless.
                age = 0.0
            else:
                age = candle_age_seconds(frame.index[-1], now, self.settings.tz)

            if self.last_result is not None and self.last_result.last_candle_ts == last_ts:
                self.status["market"] = {
                    **self._market_status(self.last_result),
                    "age_sec": round(age, 1),
                }
                return self.last_result

            previous_signal = (self.state.state.get("last_signal") or {}).get("candle_ts")
            result = self.strategy.compute(frame, now, last_signal_candle=previous_signal)
            self.last_result = result
            self.status["market"] = {**self._market_status(result), "age_sec": round(age, 1)}
            self.status["signal"] = result.signal.to_dict()
            self.status["data_error"] = None
            return result
        except Exception as exc:  # noqa: BLE001
            self.status["market"] = {"error": str(exc)}
            self.status["data_error"] = str(exc)
            log.error("Market data failed: %s", exc)
            return None

    def _market_status(self, result) -> dict:
        return {
            "last_candle": result.last_candle_ts,
            "close": result.signal.price,
            "line_a": result.signal.line_a,
            "line_b": result.signal.line_b,
            "ready": result.ready,
            "reason": result.reason,
            "bars": int(len(result.candles)),
            "replay": self.settings.data_provider == "replay",
            "error": None,
        }

    async def refresh_for_display(self) -> None:
        if self.last_result is None and self.provider is not None:
            await self._refresh_market_data(now_ist(self.settings.tz))

    # ================================================================== orders
    def _safe_option_ltp(self, snapshot: Snapshot | None) -> float:
        if self.broker is not None and self.instrument is not None:
            try:
                price = float(self.broker.get_ltp(self.instrument.security_id))
                if price > 0:
                    return price
            except Exception:  # noqa: BLE001
                pass
        if snapshot is not None:
            return float(snapshot.ltp or snapshot.avg_price or 0.0)
        return 0.0

    async def _execute_entry(self, signal: Signal, qty: int, snapshot: Snapshot) -> None:
        if self.state.state.get("kill_switch"):
            return
        if self.instrument is None:
            return
        ltp = self._safe_option_ltp(snapshot)
        intent = OrderIntent(
            correlation_id=uuid4().hex[:20],
            side="BUY",
            qty=int(qty),
            security_id=self.instrument.security_id,
            exchange_segment=self.instrument.exchange_segment,
            reason=f"ENTRY:{signal.reason}",
            kind="ENTRY",
            created_at=iso(now_ist(self.settings.tz)) or "",
            ref_price=ltp,
        )
        await self._submit_order(intent, ltp)

    async def _execute_exit(self, reason: str, snapshot: Snapshot) -> None:
        if self.instrument is None or self.broker is None:
            return
        qty = abs(int(snapshot.net_qty))
        if qty <= 0:
            return
        await self._cancel_broker_sl()
        ltp = self._safe_option_ltp(snapshot)
        intent = OrderIntent(
            correlation_id=uuid4().hex[:20],
            side="SELL",
            qty=qty,
            security_id=self.instrument.security_id,
            exchange_segment=self.instrument.exchange_segment,
            reason=reason,
            kind="EXIT",
            created_at=iso(now_ist(self.settings.tz)) or "",
            ref_price=ltp,
        )
        await self._submit_order(intent, ltp)

    async def _submit_order(self, intent: OrderIntent, ltp: float) -> bool:
        async with self._order_lock:
            if self.state.state.get("pending_order"):
                log.warning("Refusing new order: another order is pending")
                return False

            # Write-ahead: intent is durable BEFORE the broker sees it.
            self.order_journal.append({"event": "intent", **intent.to_dict()})
            self.state.update(pending_order=intent.to_dict())

            try:
                result = await asyncio.to_thread(self.broker.place_order, intent, ltp)
            except Exception as exc:  # noqa: BLE001
                self.order_journal.append(
                    {"event": "exception", "correlation_id": intent.correlation_id, "error": str(exc)}
                )
                self.state.update(pending_order=None)
                self.state.set_entries_blocked(True, f"order exception: {exc}")
                log.exception("Order submission raised")
                return False

            self.order_journal.append(
                {"event": "submit", "correlation_id": intent.correlation_id, "result": result.to_dict()}
            )

            if not result.accepted:
                self.state.update(pending_order=None)
                self.state.set_entries_blocked(True, f"order rejected: {result.message}")
                return False

            pending = {
                **intent.to_dict(),
                "order_id": result.order_id,
                "status": result.status,
            }
            self.state.update(pending_order=pending)

            if result.is_filled:
                if result.filled_qty <= 0:
                    result.filled_qty = int(intent.qty)
                if result.avg_fill_price <= 0:
                    result.avg_fill_price = float(ltp)
                await self._apply_fill(pending, result)
                return True

            final = await self._await_order(intent.correlation_id, result.order_id, intent.qty)
            if final is None:
                self.state.set_entries_blocked(True, f"order {intent.correlation_id} unconfirmed")
                log.critical("Order %s unconfirmed; entries blocked until reconciled", intent.correlation_id)
                return False

            if final.is_filled:
                if final.filled_qty <= 0:
                    final.filled_qty = int(intent.qty)
                if final.avg_fill_price <= 0:
                    final.avg_fill_price = float(ltp)
                await self._apply_fill(pending, final)
                return True

            if final.is_failed:
                self.order_journal.append(
                    {
                        "event": "failed",
                        "correlation_id": intent.correlation_id,
                        "status": final.status,
                        "message": final.message,
                    }
                )
                self.state.update(pending_order=None)
                self.state.set_entries_blocked(True, f"order {final.status}: {final.message}")
                return False

            self.state.set_entries_blocked(True, f"order still {final.status}")
            return False

    async def _await_order(self, correlation_id: str, order_id: str | None, qty: int) -> OrderResult | None:
        deadline = time.monotonic() + float(self.settings.order_confirm_timeout_sec)
        while time.monotonic() < deadline:
            try:
                result = await asyncio.to_thread(self.broker.get_order, correlation_id, order_id)
                if result.is_filled or result.is_failed:
                    return result
            except Exception as exc:  # noqa: BLE001
                log.warning("Order status check failed: %s", exc)
            await asyncio.sleep(0.5)
        return None

    async def _reconcile_pending(self) -> bool:
        if self.broker is None:
            return True
        pending = self.state.state.get("pending_order")
        if not pending:
            return True

        async with self._order_lock:
            pending = self.state.state.get("pending_order")
            if not pending:
                return True
            try:
                result = await asyncio.to_thread(
                    self.broker.get_order,
                    str(pending.get("correlation_id") or ""),
                    pending.get("order_id"),
                )
            except Exception as exc:  # noqa: BLE001
                self.state.set_entries_blocked(True, f"cannot reconcile order: {exc}")
                log.error("Pending-order reconciliation failed: %s", exc)
                return False

            if result.is_filled:
                if result.filled_qty <= 0:
                    result.filled_qty = int(pending.get("qty") or 0)
                if result.avg_fill_price <= 0:
                    result.avg_fill_price = float(pending.get("ref_price") or 0.0)
                await self._apply_fill(pending, result)
                return True

            if result.is_failed:
                self.order_journal.append(
                    {
                        "event": "reconciled_failed",
                        "correlation_id": pending.get("correlation_id"),
                        "status": result.status,
                        "message": result.message,
                    }
                )
                self.state.update(pending_order=None)
                self.state.set_entries_blocked(True, f"previous order {result.status}: {result.message}")
                return True

            self.state.set_entries_blocked(
                True, f"order {pending.get('correlation_id')} still {result.status}"
            )
            return False

    async def _reconcile_position(self, snapshot: Snapshot) -> None:
        """Detect a position that changed outside the engine.

        Two cases matter:
        * the broker is flat but state thinks we are long (broker SL, manual
          square-off, expiry) -> record the close and block entries for review;
        * the broker is long but state is empty (restart after a crash) ->
          adopt the position so stops keep working.
        """
        if self.instrument is None:
            return
        position = self.state.state.get("position")

        if snapshot.net_qty < 0:
            reason = "unexpected short position at broker"
            if self.state.state.get("entries_block_reason") != reason:
                self.state.set_entries_blocked(True, reason)
            return

        if snapshot.net_qty > 0 and not position:
            adopted = {
                "security_id": self.instrument.security_id,
                "qty": int(snapshot.net_qty),
                "avg_price": float(snapshot.avg_price or snapshot.ltp or 0.0),
                "opened_at": iso(now_ist(self.settings.tz)),
                "reason": "ADOPTED_FROM_BROKER",
                "order_id": None,
                "correlation_id": None,
            }
            self.state.update(position=adopted)
            self.trade_journal.append({"event": "ADOPTED", **adopted, "ts": iso(now_ist(self.settings.tz))})
            log.warning("Adopted an existing broker position: %s", adopted)
            return

        if not position or snapshot.net_qty != 0:
            return

        external = None
        consume = getattr(self.broker, "consume_external_fill", None)
        if consume is not None:
            try:
                external = await asyncio.to_thread(consume)
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not consume external fill: %s", exc)

        entry = float(position.get("avg_price") or 0.0)
        qty = int(position.get("qty") or 0)
        price = float((external or {}).get("price") or snapshot.ltp or entry)
        pnl = (external or {}).get("pnl")
        if pnl is None:
            pnl = (price - entry) * qty if entry > 0 else 0.0
        reason = str((external or {}).get("reason") or "EXTERNAL_CLOSE")
        now = now_ist(self.settings.tz)

        self.state.update(
            position=None,
            last_exit={"ts": iso(now), "pnl": round(float(pnl), 2), "reason": reason},
            sl_order_id=None,
            sl_correlation_id=None,
        )
        self.trade_journal.append(
            {
                "event": "CLOSE",
                "side": "SELL",
                "qty": qty,
                "price": round(price, 4),
                "entry_price": entry,
                "pnl": round(float(pnl), 2),
                "security_id": position.get("security_id"),
                "reason": reason,
                "order_id": (external or {}).get("order_id"),
                "estimated": external is None,
                "ts": iso(now),
            }
        )
        self.state.set_entries_blocked(
            True, f"{reason}: position closed outside the engine; review and resume"
        )
        log.warning(
            "Position closed outside the engine (%s): qty=%s price=%.2f pnl=%.2f%s",
            reason,
            qty,
            price,
            float(pnl),
            " [estimated]" if external is None else "",
        )

    async def _apply_fill(self, pending: dict, result: OrderResult) -> None:
        kind = str(pending.get("kind") or "").upper()
        qty = int(result.filled_qty or pending.get("qty") or 0)
        fill = float(result.avg_fill_price or pending.get("ref_price") or 0.0)
        now = now_ist(self.settings.tz)

        self.order_journal.append(
            {
                "event": "fill",
                "kind": kind,
                "side": pending.get("side"),
                "qty": qty,
                "price": fill,
                "order_id": result.order_id,
                "correlation_id": pending.get("correlation_id"),
                "reason": pending.get("reason"),
                "ts": iso(now),
            }
        )

        if kind == "ENTRY":
            position = {
                "security_id": pending.get("security_id"),
                "qty": qty,
                "avg_price": fill,
                "opened_at": iso(now),
                "reason": pending.get("reason"),
                "order_id": result.order_id,
                "correlation_id": pending.get("correlation_id"),
            }
            self.state.update(
                pending_order=None,
                position=position,
                entries=int(self.state.state.get("entries", 0)) + 1,
                last_exit=None,
            )
            self.trade_journal.append(
                {
                    "event": "OPEN",
                    "side": pending.get("side"),
                    "qty": qty,
                    "price": fill,
                    "security_id": pending.get("security_id"),
                    "reason": pending.get("reason"),
                    "order_id": result.order_id,
                    "ts": iso(now),
                }
            )
            self._clear_order_block()
            log.info("ENTRY filled qty=%s @ %.2f (%s)", qty, fill, pending.get("reason"))
            if self.settings.use_broker_sl:
                await self._place_broker_sl(fill, qty)

        elif kind == "EXIT":
            position = self.state.state.get("position") or {}
            entry = float(position.get("avg_price") or 0.0)
            pnl = (fill - entry) * qty if entry > 0 else 0.0
            reason = str(pending.get("reason") or "")
            self.state.update(
                pending_order=None,
                position=None,
                last_exit={"ts": iso(now), "pnl": round(pnl, 2), "reason": reason},
                sl_order_id=None,
                sl_correlation_id=None,
            )
            self.trade_journal.append(
                {
                    "event": "CLOSE",
                    "side": pending.get("side"),
                    "qty": qty,
                    "price": fill,
                    "entry_price": entry,
                    "pnl": round(pnl, 2),
                    "security_id": pending.get("security_id"),
                    "reason": reason,
                    "order_id": result.order_id,
                    "ts": iso(now),
                }
            )
            if reason.upper().startswith("DAILY_LOSS"):
                self.state.set_entries_blocked(True, "daily loss limit reached")
            else:
                self._clear_order_block()
            log.info("EXIT filled qty=%s @ %.2f pnl=%.2f (%s)", qty, fill, pnl, reason)

        else:
            self.state.update(pending_order=None)

    def _clear_order_block(self) -> None:
        reason = str(self.state.state.get("entries_block_reason") or "").lower()
        if reason.startswith(("order", "exit", "previous order", "cannot reconcile", "unconfirmed")):
            self.state.set_entries_blocked(False)

    # ------------------------------------------------------------- broker SL
    async def _place_broker_sl(self, fill_price: float, qty: int) -> None:
        if self.broker is None or self.instrument is None:
            return
        trigger = round(fill_price * (1.0 - float(self.settings.broker_sl_pct)), 2)
        intent = OrderIntent(
            correlation_id=uuid4().hex[:20],
            side="SELL",
            qty=int(qty),
            security_id=self.instrument.security_id,
            exchange_segment=self.instrument.exchange_segment,
            reason="BROKER_SL",
            kind="BROKER_SL",
            created_at=iso(now_ist(self.settings.tz)) or "",
            ref_price=fill_price,
            trigger_price=trigger,
        )
        self.order_journal.append({"event": "sl_intent", **intent.to_dict()})
        try:
            result = await asyncio.to_thread(self.broker.place_order, intent, fill_price)
        except Exception as exc:  # noqa: BLE001
            log.critical("Broker-side SL failed to submit: %s", exc)
            self.order_journal.append(
                {"event": "sl_failed", "correlation_id": intent.correlation_id, "error": str(exc)}
            )
            return
        if result.accepted and result.order_id:
            self.state.update(sl_order_id=result.order_id, sl_correlation_id=intent.correlation_id)
            self.order_journal.append(
                {
                    "event": "sl_submitted",
                    "correlation_id": intent.correlation_id,
                    "order_id": result.order_id,
                    "trigger": trigger,
                }
            )
            log.info("Broker-side SL placed at trigger %.2f (order %s)", trigger, result.order_id)
        else:
            log.critical("Broker-side SL rejected: %s", result.message)
            self.order_journal.append(
                {"event": "sl_failed", "correlation_id": intent.correlation_id, "message": result.message}
            )

    async def _cancel_broker_sl(self) -> None:
        order_id = self.state.state.get("sl_order_id")
        if not order_id or self.broker is None:
            return
        try:
            ok = await asyncio.to_thread(self.broker.cancel_order, str(order_id))
            self.order_journal.append({"event": "sl_cancel", "order_id": order_id, "ok": bool(ok)})
            log.info("Broker-side SL cancel %s -> %s", order_id, ok)
        except Exception as exc:  # noqa: BLE001
            log.warning("Broker-side SL cancel failed: %s", exc)
        finally:
            self.state.update(sl_order_id=None, sl_correlation_id=None)

    # ================================================================ controls
    def halt(self, reason: str = "manual halt") -> None:
        self.state.set_kill_switch(True, reason)
        log.warning("Kill switch enabled: %s", reason)

    def resume(self) -> tuple[bool, str]:
        pending = self.state.state.get("pending_order")
        if pending:
            return False, f"Cannot resume: order {pending.get('correlation_id')} is still pending"
        self.state.set_kill_switch(False)
        self.state.set_entries_blocked(False)
        log.warning("Kill switch cleared manually")
        return True, "resumed"

    def block_entries(self, reason: str = "manual block") -> None:
        self.state.set_entries_blocked(True, reason)

    def allow_entries(self) -> None:
        if self.state.state.get("kill_switch"):
            return
        self.state.set_entries_blocked(False)

    async def flatten(self, reason: str = "MANUAL_FLATTEN") -> tuple[bool, str]:
        if self.broker is None or self.instrument is None:
            return False, "broker/instrument unavailable"
        try:
            snapshot = await asyncio.to_thread(self.broker.get_snapshot, self.instrument.security_id)
        except Exception as exc:  # noqa: BLE001
            return False, f"snapshot failed: {exc}"
        if snapshot.net_qty <= 0:
            return True, "already flat"
        await self._execute_exit(reason, snapshot)
        return True, "flatten order submitted"

    def reset_paper(self) -> tuple[bool, str]:
        if self.live_trading:
            return False, "not available in live mode"
        reset = getattr(self.broker, "reset", None)
        if reset is None:
            return False, "paper broker cannot reset"
        reset()
        self.state.update(
            position=None,
            last_exit=None,
            entries=0,
            sl_order_id=None,
            sl_correlation_id=None,
        )
        return True, "paper account reset"

    async def _flat_and_idle(self) -> tuple[bool, str]:
        if self.broker is None or self.instrument is None:
            return False, "broker/instrument unavailable"
        if self.state.state.get("pending_order"):
            return False, "an order is still pending"
        try:
            snapshot = await asyncio.to_thread(
                self.broker.get_snapshot, self.instrument.security_id
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot read position: {exc}"
        if snapshot.net_qty != 0:
            return False, "close the open position before switching modes"
        return True, "ok"

    async def go_live(self) -> tuple[bool, str]:
        if self.live_trading:
            return False, "already live"
        if self.settings.broker != "dhan":
            return False, "set BROKER=dhan in .env to enable live trading"
        if not (self.settings.client_id and self.settings.access_token):
            return False, "Dhan credentials are missing"
        ok, message = await self._flat_and_idle()
        if not ok:
            return False, message
        live_settings = replace(self.settings, live_trading=True)
        try:
            broker = build_broker(
                live_settings,
                self.instrument,
                underlying_ltp_fn=self._safe_underlying_ltp,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot start live broker: {exc}"
        self.broker = broker
        self.live_trading = True
        self.status["broker"] = broker.health()
        self.status["broker_error"] = None
        self.state.update(sl_order_id=None, sl_correlation_id=None)
        # Two-step safety: real orders stay blocked until the operator resumes.
        self.state.set_entries_blocked(True, "live trading enabled; RESUME to allow real orders")
        log.critical("LIVE TRADING ENABLED from the dashboard")
        return True, "live trading enabled; entries blocked until RESUME"

    async def go_paper(self) -> tuple[bool, str]:
        if not self.live_trading:
            return False, "already in paper mode"
        ok, message = await self._flat_and_idle()
        if not ok:
            return False, message
        paper_settings = replace(self.settings, live_trading=False)
        try:
            broker = build_broker(
                paper_settings,
                self.instrument,
                underlying_ltp_fn=self._safe_underlying_ltp,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"cannot start paper broker: {exc}"
        self.broker = broker
        self.live_trading = False
        self.status["broker"] = broker.health()
        self.status["broker_error"] = None
        reason = str(self.state.state.get("entries_block_reason") or "").lower()
        if reason.startswith("live trading enabled"):
            self.state.set_entries_blocked(False)
        log.warning("Switched back to PAPER mode")
        return True, "paper mode enabled"

    # ================================================================== views
    def health(self) -> dict:
        return {
            "running": bool(self._task is not None and not self._task.done()),
            "ready": self.ready,
            "mode": self.mode_label,
            "session": self.status.get("session"),
            "ticks": self.status.get("ticks", 0),
            "last_tick": self.status.get("last_tick"),
            "last_tick_ms": self.status.get("last_tick_ms"),
            "last_error": self.status.get("last_error"),
            "broker_error": self.status.get("broker_error"),
            "data_error": self.status.get("data_error"),
            "persist_error": self.state.persist_error,
            "provider": self.provider.health() if self.provider is not None else None,
            "broker": self.broker.health() if self.broker is not None else None,
            "instrument": self.instrument.to_dict() if self.instrument is not None else None,
            "underlying": self.underlying.to_dict() if self.underlying is not None else None,
            "master": self.master.health() if self.master is not None else None,
        }

    def status_payload(self) -> dict:
        state = self.state.snapshot()
        snapshot = self.status.get("snapshot")
        stop_amount = None
        if snapshot:
            stop_amount = round(
                self.risk.stop_amount(
                    Snapshot(
                        net_qty=int(snapshot.get("net_qty") or 0),
                        avg_price=float(snapshot.get("avg_price") or 0),
                        unrealized=float(snapshot.get("unrealized") or 0),
                        realized=float(snapshot.get("realized") or 0),
                        day_pnl=float(snapshot.get("day_pnl") or 0),
                        ltp=float(snapshot.get("ltp") or 0),
                    )
                ),
                2,
            )
        config = self.settings.public_dict()
        config["mode"] = self.mode_label
        config["live_trading"] = self.live_trading
        return {
            "engine": self.health(),
            "state": state,
            "snapshot": snapshot,
            "signal": self.status.get("signal"),
            "market": self.status.get("market"),
            "config": config,
            "risk": {
                "stop_amount": stop_amount,
                "stop_loss_pct": self.settings.stop_loss_pct,
                "max_trade_loss": self.settings.max_trade_loss,
                "max_daily_loss": self.settings.max_daily_loss,
            },
        }

    def chart_payload(self, limit: int = 300) -> dict:
        if self.last_result is None:
            return {"candles": [], "line_a": [], "line_b": []}
        return self.strategy.chart_payload(self.last_result, limit)
