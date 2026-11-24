"""Dhan live broker adapter.

Design rules:
* Never invent a flat position when the API is unhappy.
* Every order carries a correlation id (`tag`) so it can be reconciled after a
  crash with ``get_order_by_correlationID``.
* P&L is read from the broker when available. If only unrealized P&L is
  missing, it is derived from LTP and average price and flagged as such.
"""

from __future__ import annotations

import logging
from typing import Any

from ..models import Instrument, OrderIntent, OrderResult, Snapshot
from ..utils import extract_ltp, first_present, safe_float, safe_int
from .base import Broker, BrokerError

log = logging.getLogger("brahma.broker.dhan")


def _data_list(response: Any, operation: str) -> list[dict]:
    if not isinstance(response, dict):
        raise BrokerError(f"{operation}: unexpected response type {type(response).__name__}")
    status = str(response.get("status", "")).lower()
    if status and status != "success":
        raise BrokerError(f"{operation} failed: {str(response)[:400]}")
    data = response.get("data")
    if data is None:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        # Some endpoints wrap the list one level deeper.
        for key in ("data", "orders", "positions"):
            nested = data.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [data]
    raise BrokerError(f"{operation}: unsupported data payload {type(data).__name__}")


class DhanBroker(Broker):
    name = "dhan"
    is_live = True

    def __init__(self, settings, instrument: Instrument, client: Any | None = None) -> None:
        self.settings = settings
        self.instrument = instrument
        self.security_id = str(instrument.security_id)
        self.segment = instrument.exchange_segment or "NSE_FNO"
        if client is None:
            from dhanhq import DhanContext, dhanhq

            client = dhanhq(DhanContext(settings.client_id, settings.access_token))
        self._client = client

    # ------------------------------------------------------------- positions
    def get_snapshot(self, security_id: str) -> Snapshot:
        positions = _data_list(self._client.get_positions(), "get_positions")
        target = None
        for position in positions:
            if str(first_present(position, ("securityId", "security_id", "securityID"), "")) == str(security_id):
                target = position
                break

        if target is None:
            return Snapshot(source="dhan", pnl_source="broker")

        net_qty = safe_int(first_present(target, ("netQty", "net_qty", "netQuantity"), 0))
        ltp = 0.0
        try:
            ltp = self.get_ltp(security_id)
        except BrokerError as exc:
            log.warning("Option LTP unavailable while reading position: %s", exc)

        unreal_raw = first_present(
            target,
            ("unrealizedProfit", "unrealized_profit", "unrealizedPnl", "unrealized_pnl"),
        )
        realized_raw = first_present(
            target,
            ("realizedProfit", "realized_profit", "realizedPnl", "realized_pnl"),
        )
        buy_avg = safe_float(first_present(target, ("buyAvg", "buyAverage", "buyAvgPrice"), 0.0))
        sell_avg = safe_float(first_present(target, ("sellAvg", "sellAverage", "sellAvgPrice"), 0.0))
        cost_price = safe_float(first_present(target, ("costPrice", "cost_price"), 0.0))

        avg_price = buy_avg if net_qty > 0 and buy_avg > 0 else sell_avg if net_qty < 0 and sell_avg > 0 else cost_price

        unrealized: float
        pnl_source = "broker"
        if unreal_raw is not None:
            unrealized = safe_float(unreal_raw)
        else:
            if net_qty != 0 and ltp > 0 and avg_price > 0:
                unrealized = (ltp - avg_price) * net_qty
                pnl_source = "derived"
            elif net_qty == 0:
                unrealized = 0.0
            else:
                raise BrokerError(
                    "Position exists but P&L and LTP are unavailable; refusing to guess the stop-loss."
                )

        if avg_price <= 0 and net_qty != 0 and ltp > 0 and unreal_raw is not None:
            # Recover average price from broker unrealized P&L.
            avg_price = ltp - safe_float(unreal_raw) / net_qty
        if avg_price <= 0 and net_qty != 0:
            raise BrokerError(
                "Position exists but average price is unavailable; stop-loss cannot be evaluated."
            )

        realized = safe_float(realized_raw) if realized_raw is not None else 0.0
        if realized_raw is None:
            pnl_source = "partial"

        return Snapshot(
            net_qty=net_qty,
            avg_price=avg_price,
            unrealized=unrealized,
            realized=realized,
            day_pnl=unrealized + realized,
            ltp=ltp,
            pnl_source=pnl_source,
            source="dhan",
        )

    # ---------------------------------------------------------------- orders
    def place_order(self, intent: OrderIntent, ltp: float) -> OrderResult:
        # Defence in depth: this adapter must never send an order unless the
        # operator explicitly enabled live trading. build_broker routes paper
        # mode to DryRunBroker; this guard covers direct construction too.
        if not self.settings.live_trading:
            return OrderResult(
                accepted=False,
                status="REJECTED",
                correlation_id=intent.correlation_id,
                message="LIVE_TRADING_ENABLED=false: refusing to send order to Dhan",
            )

        order_type = self.settings.order_type
        price = 0.0
        trigger = 0.0

        if intent.kind == "BROKER_SL":
            order_type = "SLM"
            trigger = intent.trigger_price
        elif order_type == "LIMIT":
            price = intent.limit_price or ltp

        try:
            response = self._client.place_order(
                security_id=self.security_id,
                exchange_segment=self.segment,
                transaction_type=intent.side.upper(),
                quantity=int(intent.qty),
                order_type=order_type,
                product_type=self.settings.product_type,
                price=price,
                trigger_price=trigger,
                validity="DAY",
                tag=intent.correlation_id[:20],
            )
        except Exception as exc:  # noqa: BLE001
            return OrderResult(
                accepted=False,
                status="REJECTED",
                correlation_id=intent.correlation_id,
                message=f"place_order exception: {exc}",
            )

        if not isinstance(response, dict) or str(response.get("status", "")).lower() != "success":
            return OrderResult(
                accepted=False,
                status="REJECTED",
                correlation_id=intent.correlation_id,
                message=f"place_order failed: {str(response)[:300]}",
            )

        data = response.get("data")
        if isinstance(data, list):
            data = data[0] if data else {}
        if not isinstance(data, dict):
            data = {}

        order_id = first_present(data, ("orderId", "order_id", "orderID"))
        status = str(first_present(data, ("orderStatus", "order_status", "status"), "PENDING"))
        return OrderResult(
            accepted=True,
            status=status,
            order_id=str(order_id) if order_id else None,
            correlation_id=intent.correlation_id,
            message=str(first_present(data, ("omsErrorDescription", "errorMessage"), "")) or "submitted",
        )

    def get_order(self, correlation_id: str, order_id: str | None = None) -> OrderResult:
        response: Any = None
        errors: list[str] = []
        if correlation_id:
            try:
                response = self._client.get_order_by_correlationID(correlation_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"by correlation: {exc}")
        if response is None and order_id:
            try:
                response = self._client.get_order_by_id(order_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"by id: {exc}")

        if response is None:
            raise BrokerError("order status unavailable: " + "; ".join(errors))

        rows = _data_list(response, "get_order")
        if not rows:
            return OrderResult(
                accepted=True,
                status="PENDING",
                order_id=order_id,
                correlation_id=correlation_id,
                message="order not found yet",
            )

        row = rows[0]
        status = str(first_present(row, ("orderStatus", "order_status", "status"), "PENDING"))
        filled = safe_int(
            first_present(
                row,
                ("tradedQuantity", "filledQty", "filled_quantity", "traded_quantity"),
                0,
            )
        )
        avg = safe_float(
            first_present(
                row,
                ("averageTradedPrice", "avgFillPrice", "average_traded_price", "avgTradedPrice"),
                0.0,
            )
        )
        return OrderResult(
            accepted=True,
            status=status,
            order_id=str(first_present(row, ("orderId", "order_id"), order_id or "")) or order_id,
            correlation_id=correlation_id,
            filled_qty=filled,
            avg_fill_price=avg,
            message=str(first_present(row, ("omsErrorDescription", "errorMessage"), "")),
        )

    def cancel_order(self, order_id: str) -> bool:
        try:
            response = self._client.cancel_order(order_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel_order(%s) failed: %s", order_id, exc)
            return False
        return isinstance(response, dict) and str(response.get("status", "")).lower() == "success"

    # ------------------------------------------------------------------ misc
    def get_ltp(self, security_id: str) -> float:
        try:
            response = self._client.ticker_data({self.segment: [str(security_id)]})
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"ticker_data failed: {exc}") from exc
        value = extract_ltp(response)
        if value <= 0:
            raise BrokerError(f"ticker_data returned no LTP: {str(response)[:300]}")
        return value

    def get_funds(self) -> dict:
        try:
            response = self._client.get_fund_limits()
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        rows = _data_list(response, "get_fund_limits")
        return rows[0] if rows else {}

    def health(self) -> dict:
        return {
            "name": self.name,
            "is_live": True,
            "security_id": self.security_id,
            "segment": self.segment,
        }
