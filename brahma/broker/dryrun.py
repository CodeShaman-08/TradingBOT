"""Paper execution: real Dhan prices, local simulated fills, zero orders sent.

This is the default mode:

    DATA_PROVIDER=dhan
    BROKER=dhan
    LIVE_TRADING_ENABLED=false

Market data and option LTPs come from Dhan, but fills/positions/P&L are tracked
in ``data/paper_account.json``. The live adapter refuses to send orders unless
``LIVE_TRADING_ENABLED=true``, so this wrapper is a second, independent guard.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Instrument, OrderIntent, OrderResult, Snapshot
from ..utils import extract_ltp
from .base import Broker, BrokerError
from .paper import PaperBroker

log = logging.getLogger("brahma.broker.dryrun")


class DryRunBroker(Broker):
    name = "paper"
    is_live = False

    def __init__(self, settings, instrument: Instrument, client: Any | None = None) -> None:
        self.settings = settings
        self.instrument = instrument
        self.security_id = str(instrument.security_id)
        self.segment = instrument.exchange_segment or "NSE_FNO"
        if client is None:
            from dhanhq import DhanContext, dhanhq

            client = dhanhq(DhanContext(settings.client_id, settings.access_token))
        self._client = client
        self._last_ltp = 0.0
        self._paper = PaperBroker(
            settings, ltp_fn=self.get_ltp, account_path=settings.paper_account
        )
        log.warning("PAPER execution: real prices, simulated fills, never sent to Dhan")

    # ----------------------------------------------------------------- prices
    def get_ltp(self, security_id: str = "") -> float:
        target = str(security_id or self.security_id)
        try:
            response = self._client.ticker_data({self.segment: [target]})
            value = extract_ltp(response)
            if value > 0:
                self._last_ltp = value
                return value
        except Exception as exc:  # noqa: BLE001
            log.warning("Paper option LTP failed: %s", exc)
        if self._last_ltp > 0:
            return self._last_ltp
        raise BrokerError("paper option LTP unavailable")

    # ---------------------------------------------------------------- broker
    def get_snapshot(self, security_id: str) -> Snapshot:
        snapshot = self._paper.get_snapshot(security_id)
        snapshot.source = "paper"
        snapshot.pnl_source = "paper"
        return snapshot

    def place_order(self, intent: OrderIntent, ltp: float) -> OrderResult:
        price = ltp
        if price <= 0:
            try:
                price = self.get_ltp(intent.security_id)
            except BrokerError as exc:
                return OrderResult(
                    accepted=False,
                    status="REJECTED",
                    correlation_id=intent.correlation_id,
                    message=str(exc),
                )
        result = self._paper.place_order(intent, price)
        if result.order_id:
            result.order_id = f"PAPER-{result.order_id}"
        if result.message:
            result.message = f"PAPER (not sent to Dhan): {result.message}"
        return result

    def get_order(self, correlation_id: str, order_id: str | None = None) -> OrderResult:
        return self._paper.get_order(correlation_id, order_id)

    def cancel_order(self, order_id: str) -> bool:
        return self._paper.cancel_order(order_id)

    def get_funds(self) -> dict:
        return {**self._paper.get_funds(), "mode": "PAPER"}

    def consume_external_fill(self) -> dict | None:
        return self._paper.consume_external_fill()

    def reset(self) -> None:
        self._paper.reset()

    def health(self) -> dict:
        return {
            "name": self.name,
            "is_live": False,
            "mode": "PAPER",
            "security_id": self.security_id,
            "segment": self.segment,
            "account_file": str(self._paper.path),
        }
