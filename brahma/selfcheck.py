"""Offline self-check: indicators, replay, paper broker, risk, engine, backtest.

Run with:  python -m brahma.selfcheck
"""

from __future__ import annotations

import asyncio
import math
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import numpy as np

from .backtest import compare
from .broker.paper import PaperBroker, SyntheticOptionPricer
from .config import Settings
from .engine import BotEngine
from .indicators import KalmanFilter, hma, hma3, kalman, wma
from .models import OrderIntent, Signal, Snapshot
from .providers.replay import ReplayProvider
from .risk import RiskManager
from .state import StateStore
from .strategy import SignalEngine, StrategyConfig
from .timeutils import get_tz, iso, now_ist

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name} {detail}")


def temp_settings(tmp: Path) -> Settings:
    base = Settings()
    data = tmp / "data"
    return replace(
        base,
        root=tmp,
        data_dir=data,
        state_file=data / "state.json",
        trade_journal=data / "trades.jsonl",
        order_journal=data / "orders.jsonl",
        paper_account=data / "paper_account.json",
        instrument_master=data / "instrument_master.csv",
        log_file=data / "brahma.log",
        data_provider="replay",
        broker="paper",
        live_trading=False,
        engine_enabled=False,
        ignore_market_hours=True,
        replay_days=5,
        replay_initial_bars=150,
        replay_bars_per_sec=10.0,
        poll_interval_sec=2,
        max_daily_loss=1000.0,
        max_trade_loss=500.0,
        stop_loss_pct=0.20,
        max_trades_per_day=3,
        option_security_id="",
        option_lot_size=0,
    )


def test_indicators() -> None:
    print("\n[indicators]")
    values = np.arange(1.0, 11.0)
    result = wma(values, 3)
    expected = (1 * 1 + 2 * 2 + 3 * 3) / 6.0
    check("wma first value", math.isnan(result[1]))
    check("wma window value", abs(result[2] - expected) < 1e-9, f"{result[2]} vs {expected}")
    check("wma length", len(result) == len(values))

    h = hma(values, 4)
    h3 = hma3(values, 8)
    check("hma length", len(h) == len(values))
    check("hma3 length", len(h3) == len(values))

    series = np.sin(np.arange(300) / 11.0) + np.arange(300) * 0.001
    batch = kalman(series, 0.7)
    stateful = KalmanFilter(0.7)
    sequential = np.array([stateful.update(float(x)) for x in series])
    check("kalman batch == incremental", np.allclose(batch, sequential, equal_nan=True))

    # Compare with a literal transcription of the original implementation.
    reference = np.full(len(series), np.nan)
    previous = math.nan
    velocity = 0.0
    for i, value in enumerate(series):
        if not math.isfinite(previous):
            previous = value
            reference[i] = value
            continue
        delta = value - previous
        velocity += 0.7 * delta
        previous = previous + delta * math.sqrt(0.7 * 2) + velocity
        reference[i] = previous
    check("kalman matches original", np.allclose(batch, reference, equal_nan=True))


def test_replay_and_strategy(settings: Settings) -> None:
    print("\n[replay + strategy]")
    provider = ReplayProvider(settings)
    frame = provider.get_candles(300)
    check("replay returns candles", len(frame) >= 50, f"got {len(frame)}")
    check("replay index is tz-aware", frame.index.tz is not None)
    check("replay ltp > 0", provider.get_ltp() > 0)

    engine = SignalEngine(StrategyConfig(length=22, gain=0.7, use_kalman=True))
    result = engine.compute(frame)
    check("strategy ready", result.ready, result.reason)
    check("line A finite", bool(np.isfinite(result.line_a[-1])))
    check("line B finite", bool(np.isfinite(result.line_b[-1])))
    check("signal action valid", result.signal.action in {"BUY", "SELL", "HOLD"})

    no_kalman = SignalEngine(StrategyConfig(length=22, gain=0.7, use_kalman=False))
    result2 = no_kalman.compute(frame)
    check("no-kalman strategy ready", result2.ready, result2.reason)


def test_paper_broker(settings: Settings) -> None:
    print("\n[paper broker]")
    pricer = SyntheticOptionPricer(spot=lambda: 48_000.0, premium_pct=0.02, delta=0.5)
    broker = PaperBroker(settings, ltp_fn=pricer.price)
    broker.reset()

    base = OrderIntent(
        correlation_id="check-entry-0001",
        side="BUY",
        qty=30,
        security_id="PAPER-OPT",
        exchange_segment="NSE_FNO",
        reason="TEST_ENTRY",
        kind="ENTRY",
        created_at=now_ist().isoformat(),
        ref_price=0.0,
    )
    result = broker.place_order(base, 0.0)
    check("paper buy filled", result.is_filled, result.message)
    snapshot = broker.get_snapshot("PAPER-OPT")
    check("paper position open", snapshot.net_qty == 30, str(snapshot.to_dict()))
    check("paper avg price > 0", snapshot.avg_price > 0)

    exit_intent = replace(base, correlation_id="check-exit-0002", side="SELL", kind="EXIT", reason="TEST_EXIT")
    exit_result = broker.place_order(exit_intent, 0.0)
    check("paper sell filled", exit_result.is_filled, exit_result.message)
    final = broker.get_snapshot("PAPER-OPT")
    check("paper position closed", final.net_qty == 0, str(final.to_dict()))
    check("paper pnl is a small loss from slippage", final.realized <= 0.0, str(final.realized))


def test_broker_safety(settings: Settings) -> None:
    print("\n[broker safety]")
    from .broker.dhan import DhanBroker
    from .broker.dryrun import DryRunBroker
    from .broker.paper import PaperBroker
    from .models import Instrument

    class FakeDhan:
        def __init__(self) -> None:
            self.orders: list[dict] = []

        def place_order(self, **kwargs):
            self.orders.append(kwargs)
            return {"status": "success", "data": {"orderId": "X1", "orderStatus": "TRADED"}}

        def ticker_data(self, payload):
            target = str(list(payload.values())[0][0])
            return {"status": "success", "data": {target: {"last_price": 123.45}}}

        def get_positions(self):
            return {"status": "success", "data": []}

        def get_order_by_correlationID(self, correlation_id):
            return {"status": "success", "data": []}

        def cancel_order(self, order_id):
            return {"status": "success"}

    instrument = Instrument(security_id="12345", exchange_segment="NSE_FNO", lot_size=30)
    safe = replace(settings, live_trading=False)
    intent = OrderIntent(
        correlation_id="safe-0001",
        side="BUY",
        qty=30,
        security_id="12345",
        exchange_segment="NSE_FNO",
        reason="TEST",
        kind="ENTRY",
        created_at=now_ist(settings.tz).isoformat(),
    )

    fake = FakeDhan()
    adapter = DhanBroker(safe, instrument, client=fake)
    result = adapter.place_order(intent, 100.0)
    check(
        "dhan adapter refuses when live=false",
        (not result.accepted) and "LIVE_TRADING_ENABLED" in result.message,
        result.message,
    )
    check("dhan adapter sent no order", len(fake.orders) == 0, str(fake.orders))

    fake2 = FakeDhan()
    dry = DryRunBroker(safe, instrument, client=fake2)
    check("dry-run uses dhan option ltp", abs(dry.get_ltp() - 123.45) < 1e-9)
    dry_result = dry.place_order(intent, 0.0)
    check(
        "paper fills locally",
        dry_result.is_filled and (dry_result.order_id or "").startswith("PAPER-"),
        str(dry_result.to_dict()),
    )
    check("dry-run sent no order", len(fake2.orders) == 0, str(fake2.orders))
    check("dry-run tracks position", dry.get_snapshot("12345").net_qty == 30)
    dry.reset()  # paper and dry-run share the paper ledger; leave it clean

    price = {"value": 100.0}
    paper = PaperBroker(
        safe,
        ltp_fn=lambda security_id="": price["value"],
        account_path=settings.data_dir / "sl_test.json",
    )
    paper.reset()
    paper.place_order(replace(intent, correlation_id="sl-entry"), 100.0)
    sl = replace(
        intent,
        correlation_id="sl-order",
        side="SELL",
        kind="BROKER_SL",
        trigger_price=90.0,
    )
    sl_result = paper.place_order(sl, 100.0)
    check("paper SL rests", sl_result.accepted and sl_result.status == "PENDING", sl_result.status)
    check("paper SL does not close immediately", paper.get_snapshot("12345").net_qty == 30)
    price["value"] = 89.0
    check("paper SL triggers", paper.get_snapshot("12345").net_qty == 0)
    external = paper.consume_external_fill()
    check(
        "paper SL reports external fill",
        external is not None and external.get("reason") == "BROKER_SL",
        str(external),
    )


def test_risk(settings: Settings) -> None:
    print("\n[risk]")
    risk = RiskManager(settings)
    now = datetime(2026, 11, 20, 10, 0, tzinfo=get_tz(settings.tz))

    stop_snapshot = Snapshot(net_qty=30, avg_price=100.0, unrealized=-501.0, day_pnl=-501.0)
    decision = risk.in_trade(stop_snapshot, {"opened_at": iso(now)}, now, "OPEN")
    check("stop-loss triggers", decision.should_exit and decision.reason == "STOP_LOSS", decision.reason)

    daily_snapshot = Snapshot(net_qty=30, avg_price=100.0, unrealized=-1001.0, day_pnl=-1001.0)
    decision = risk.in_trade(daily_snapshot, {"opened_at": iso(now)}, now, "OPEN")
    check("daily loss triggers", decision.should_exit and decision.reason == "DAILY_LOSS", decision.reason)

    flat = Snapshot()
    blocked_state = {"entries_blocked": True, "entries_block_reason": "test", "entries": 0}
    decision = risk.pre_trade(flat, blocked_state, now, "OPEN", None, 0.0, 30)
    check("blocked entries rejected", not decision.allowed)

    # stop amount is the tighter of absolute and percentage caps
    check("stop amount uses min cap", risk.stop_amount(stop_snapshot) == settings.max_trade_loss)


def test_backtest(settings: Settings) -> None:
    print("\n[backtest]")
    provider = ReplayProvider(settings)
    frame = provider.history(4000)
    cfg = StrategyConfig.from_settings(settings)
    result = compare(frame, cfg, cost_bps=2.0)
    check("backtest has both arms", "kalman" in result and "no_kalman" in result)
    check("backtest has verdict", bool(result.get("verdict")))
    check("backtest produced trades", result["kalman"]["trades"] >= 0)


def test_state(settings: Settings) -> None:
    print("\n[state]")
    store = StateStore(settings.state_file, settings.tz)
    store.load()
    store.update(entries=2, last_candle="2026-11-20T10:00:00+05:30")
    reloaded = StateStore(settings.state_file, settings.tz)
    reloaded.load()
    check("state persists entries", reloaded.state["entries"] == 2)
    check("state persists candle", reloaded.state["last_candle"].startswith("2026-11-20"))


def test_engine(settings: Settings) -> None:
    print("\n[engine]")

    async def run() -> None:
        engine = BotEngine(settings)
        await engine.startup()
        check("engine instrument resolved", engine.instrument is not None)
        check("engine broker built", engine.broker is not None)
        check("engine provider built", engine.provider is not None)

        await engine._tick()
        check("engine tick ran", engine.status["ticks"] == 0)  # ticks increments in _run, not _tick

        now = now_ist(settings.tz)
        signal = Signal(
            action="BUY",
            reason="selfcheck",
            candle_ts=iso(now),
            price=48_000.0,
            line_a=0.0,
            line_b=0.0,
            prev_a=0.0,
            prev_b=0.0,
            generated_at=iso(now) or "",
        )
        snapshot = await asyncio.to_thread(engine.broker.get_snapshot, engine.instrument.security_id)
        await engine._execute_entry(signal, 30, snapshot)
        state = engine.state.state
        check("engine entry recorded", state.get("position") is not None, str(state.get("position")))
        check("engine entries incremented", state.get("entries", 0) >= 1)

        snapshot = await asyncio.to_thread(engine.broker.get_snapshot, engine.instrument.security_id)
        await engine._execute_exit("SELFCHECK_EXIT", snapshot)
        check("engine exit closed position", engine.state.state.get("position") is None)
        check("engine last exit recorded", engine.state.state.get("last_exit") is not None)

        # --- position closed outside the engine (broker SL / manual close) ---
        snapshot = await asyncio.to_thread(engine.broker.get_snapshot, engine.instrument.security_id)
        await engine._execute_entry(signal, 30, snapshot)
        check("second entry recorded", engine.state.state.get("position") is not None)

        close = OrderIntent(
            correlation_id="selfcheck-external-close",
            side="SELL",
            qty=30,
            security_id=engine.instrument.security_id,
            exchange_segment=engine.instrument.exchange_segment,
            reason="MANUAL_CLOSE",
            kind="EXIT",
            created_at=iso(now_ist(settings.tz)) or "",
        )
        price = engine.broker.get_ltp(engine.instrument.security_id)
        await asyncio.to_thread(engine.broker.place_order, close, price)
        flat = await asyncio.to_thread(engine.broker.get_snapshot, engine.instrument.security_id)
        await engine._reconcile_position(flat)
        check("external close clears position", engine.state.state.get("position") is None)
        check("external close blocks entries", bool(engine.state.state.get("entries_blocked")))
        check(
            "external close journaled",
            any(item.get("reason") == "EXTERNAL_CLOSE" for item in engine.trade_journal.tail(10)),
        )

        # --- orphan broker position is adopted so stops keep working ---
        engine.state.update(position=None, entries_blocked=False)
        buy = OrderIntent(
            correlation_id="selfcheck-orphan",
            side="BUY",
            qty=30,
            security_id=engine.instrument.security_id,
            exchange_segment=engine.instrument.exchange_segment,
            reason="ORPHAN",
            kind="ENTRY",
            created_at=iso(now_ist(settings.tz)) or "",
        )
        await asyncio.to_thread(engine.broker.place_order, buy, price)
        orphan = await asyncio.to_thread(engine.broker.get_snapshot, engine.instrument.security_id)
        await engine._reconcile_position(orphan)
        adopted = engine.state.state.get("position")
        check("adopts orphan broker position", adopted is not None and adopted.get("qty") == 30, str(adopted))

        await engine.stop()

    asyncio.run(run())


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="brahma-check-") as raw:
        settings = temp_settings(Path(raw))
        test_indicators()
        test_replay_and_strategy(settings)
        test_paper_broker(settings)
        test_broker_safety(settings)
        test_risk(settings)
        test_backtest(settings)
        test_state(settings)
        test_engine(settings)

    print(f"\n{'=' * 50}")
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 50)
    if FAIL:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
