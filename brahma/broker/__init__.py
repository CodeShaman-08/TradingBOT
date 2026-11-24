"""Broker adapters.

BROKER=dhan + LIVE_TRADING_ENABLED=false -> paper fills at real Dhan prices.
BROKER=dhan + LIVE_TRADING_ENABLED=true  -> real Dhan orders.
BROKER=paper is the offline synthetic fallback used with the replay feed.
"""

from __future__ import annotations

from .base import Broker, BrokerError
from .paper import PaperBroker, SyntheticOptionPricer


def build_broker(settings, instrument, underlying_ltp_fn):
    if settings.broker == "paper":
        pricer = SyntheticOptionPricer(
            spot=underlying_ltp_fn,
            premium_pct=settings.paper_premium_pct,
            delta=settings.paper_delta,
        )
        return PaperBroker(settings, ltp_fn=pricer.price)

    # BROKER=dhan without LIVE_TRADING_ENABLED=true is paper mode: real Dhan
    # prices, local fills, no orders leave the machine.
    if not settings.live_trading:
        from .dryrun import DryRunBroker

        return DryRunBroker(settings, instrument)

    from .dhan import DhanBroker

    return DhanBroker(settings, instrument)


__all__ = [
    "Broker",
    "BrokerError",
    "PaperBroker",
    "SyntheticOptionPricer",
    "build_broker",
]
