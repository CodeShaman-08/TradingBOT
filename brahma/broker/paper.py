"""Paper broker + synthetic option premium.

This broker is used only with the offline replay feed. The replay provider knows
just the index price, so the option premium is modelled as a linear function of
the underlying around a base premium. Live paper trading uses the same paper
ledger but prices fills from real Dhan option LTPs (see ``dryrun.py``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..models import OrderIntent, OrderResult, Snapshot
from ..timeutils import now_ist
from ..utils import safe_float, safe_int
from .base import Broker, BrokerError

log = logging.getLogger("brahma.broker.paper")


class SyntheticOptionPricer:
    def __init__(
        self,
        spot: Callable[[], float],
        premium_pct: float = 0.02,
        delta: float = 0.5,
    ) -> None:
        self._spot = spot
        self._premium_pct = float(premium_pct)
        self._delta = float(delta)
        self._lock = threading.RLock()
        self._base_spot: float | None = None
        self._base_premium: float | None = None

    def price(self, security_id: str = "") -> float:
        spot = safe_float(self._spot(), 0.0)
        if spot <= 0:
            return 0.0
        with self._lock:
            if self._base_spot is None:
                self._base_spot = spot
                self._base_premium = max(1.0, spot * self._premium_pct)
            raw = self._base_premium + self._delta * (spot - self._base_spot)
            # A linear delta model collapses over a large index move. Floor the
            # premium at 25% of base so the demo stays in a plausible range.
            floor = max(0.5, self._base_premium * 0.25)
            return max(floor, raw)


class PaperBroker(Broker):
    name = "paper"
    is_live = False

    def __init__(self, settings, ltp_fn: Callable[[str], float], account_path: Path | None = None) -> None:
        self.settings = settings
        self._ltp_fn = ltp_fn
        self.path = Path(account_path or settings.paper_account)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._orders: dict[str, OrderResult] = {}
        self._last_external_fill: dict | None = None
        self.account = self._load()

    # ------------------------------------------------------------ persistence
    def _default_account(self) -> dict:
        return {
            "date": now_ist(self.settings.tz).date().isoformat(),
            "cash": float(self.settings.paper_starting_cash),
            "realized_today": 0.0,
            "realized_total": 0.0,
            "position": None,
            "sl": None,
            "updated_at": now_ist(self.settings.tz).isoformat(),
        }

    def _load(self) -> dict:
        if not self.path.exists():
            account = self._default_account()
            self._save_account(account)
            return account
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
        except (OSError, ValueError, json.JSONDecodeError):
            account = self._default_account()
            account["cash"] = 0.0
            account["position"] = None
            return account

        account = self._default_account()
        account.update(raw)
        today = now_ist(self.settings.tz).date().isoformat()
        if account.get("date") != today:
            account["date"] = today
            account["realized_today"] = 0.0
        return account

    def _save_account(self, account: dict | None = None) -> None:
        account = account or self.account
        account["updated_at"] = now_ist(self.settings.tz).isoformat()
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(account, indent=2, default=str))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)

    def _roll_day_locked(self) -> None:
        today = now_ist(self.settings.tz).date().isoformat()
        if self.account.get("date") != today:
            self.account["date"] = today
            self.account["realized_today"] = 0.0
            self._save_account()

    # ---------------------------------------------------------------- broker
    def get_snapshot(self, security_id: str) -> Snapshot:
        with self._lock:
            self._roll_day_locked()
            position = self.account.get("position")
            qty = 0
            avg = 0.0
            ltp = 0.0
            if position and str(position.get("security_id")) == str(security_id):
                qty = safe_int(position.get("qty"))
                avg = safe_float(position.get("avg_price"))
                if qty != 0:
                    ltp = safe_float(self._ltp_fn(security_id))
                    if ltp > 0:
                        # A resting stop order can fill while we are reading.
                        self._maybe_trigger_sl_locked(ltp)
                        position = self.account.get("position")
                        qty = safe_int(position.get("qty")) if position else 0
                        avg = safe_float(position.get("avg_price")) if position else 0.0
            unrealized = (ltp - avg) * qty if qty != 0 and ltp > 0 else 0.0
            realized = safe_float(self.account.get("realized_today"))
            return Snapshot(
                net_qty=qty,
                avg_price=avg,
                unrealized=unrealized,
                realized=realized,
                day_pnl=realized + unrealized,
                ltp=ltp,
                pnl_source="paper",
                source="paper",
            )

    def _maybe_trigger_sl_locked(self, ltp: float) -> None:
        sl = self.account.get("sl")
        position = self.account.get("position")
        if not sl or not position or ltp <= 0:
            return
        if str(sl.get("security_id")) != str(position.get("security_id")):
            self.account["sl"] = None
            self._save_account()
            return

        trigger = safe_float(sl.get("trigger"))
        if trigger <= 0 or ltp > trigger:
            return

        held = safe_int(position.get("qty"))
        qty = min(safe_int(sl.get("qty")), held)
        if qty <= 0:
            self.account["sl"] = None
            self._save_account()
            return

        slip = float(self.settings.slippage_bps) / 10_000.0
        fill = ltp * (1.0 - slip)
        pnl = (fill - safe_float(position.get("avg_price"))) * qty
        self.account["cash"] = safe_float(self.account.get("cash")) + fill * qty
        self.account["realized_today"] = safe_float(self.account.get("realized_today")) + pnl
        self.account["realized_total"] = safe_float(self.account.get("realized_total")) + pnl
        remaining = held - qty
        self.account["position"] = None if remaining == 0 else {**position, "qty": remaining}
        self.account["sl"] = None
        self._save_account()
        self._last_external_fill = {
            "security_id": str(position.get("security_id")),
            "qty": qty,
            "price": fill,
            "pnl": pnl,
            "reason": "BROKER_SL",
            "order_id": sl.get("order_id"),
            "trigger": trigger,
        }
        log.warning("Paper broker-side SL triggered at %.2f (fill %.2f, pnl %.2f)", trigger, fill, pnl)

    def consume_external_fill(self) -> dict | None:
        with self._lock:
            fill = self._last_external_fill
            self._last_external_fill = None
            return fill

    def place_order(self, intent: OrderIntent, ltp: float) -> OrderResult:
        with self._lock:
            self._roll_day_locked()
            side = intent.side.upper()
            qty = safe_int(intent.qty)
            if qty <= 0:
                return OrderResult(False, "REJECTED", correlation_id=intent.correlation_id, message="qty <= 0")
            if ltp <= 0:
                ltp = safe_float(self._ltp_fn(intent.security_id))
            if ltp <= 0:
                return OrderResult(False, "REJECTED", correlation_id=intent.correlation_id, message="no paper LTP")

            slip = float(self.settings.slippage_bps) / 10_000.0
            position = self.account.get("position")

            if str(intent.kind or "").upper() == "BROKER_SL":
                if not position or str(position.get("security_id")) != str(intent.security_id):
                    return OrderResult(
                        False,
                        "REJECTED",
                        correlation_id=intent.correlation_id,
                        message="no position to protect with a stop order",
                    )
                trigger = safe_float(intent.trigger_price)
                if trigger <= 0:
                    return OrderResult(
                        False,
                        "REJECTED",
                        correlation_id=intent.correlation_id,
                        message="broker SL needs a positive trigger price",
                    )
                order_id = f"PAPER-SL-{intent.correlation_id[:12]}"
                self.account["sl"] = {
                    "order_id": order_id,
                    "correlation_id": intent.correlation_id,
                    "security_id": str(intent.security_id),
                    "qty": min(qty, safe_int(position.get("qty"))),
                    "trigger": trigger,
                }
                self._save_account()
                result = OrderResult(
                    accepted=True,
                    status="PENDING",
                    order_id=order_id,
                    correlation_id=intent.correlation_id,
                    message=f"paper SL resting at {trigger:.2f}",
                )
                self._orders[intent.correlation_id] = result
                return result

            if side == "BUY":
                fill = ltp * (1.0 + slip)
                cost = fill * qty
                if cost > safe_float(self.account.get("cash")):
                    return OrderResult(
                        False,
                        "REJECTED",
                        correlation_id=intent.correlation_id,
                        message="insufficient paper cash",
                    )
                if position and str(position.get("security_id")) != str(intent.security_id):
                    return OrderResult(
                        False,
                        "REJECTED",
                        correlation_id=intent.correlation_id,
                        message="already holding a different security",
                    )
                old_qty = safe_int(position.get("qty")) if position else 0
                old_avg = safe_float(position.get("avg_price")) if position else 0.0
                new_qty = old_qty + qty
                new_avg = ((old_avg * old_qty) + fill * qty) / new_qty
                self.account["cash"] = safe_float(self.account.get("cash")) - cost
                self.account["position"] = {
                    "security_id": str(intent.security_id),
                    "qty": new_qty,
                    "avg_price": new_avg,
                    "opened_at": (position or {}).get("opened_at") or now_ist(self.settings.tz).isoformat(),
                    "reason": intent.reason,
                }

            elif side == "SELL":
                if not position or str(position.get("security_id")) != str(intent.security_id):
                    return OrderResult(
                        False,
                        "REJECTED",
                        correlation_id=intent.correlation_id,
                        message="no long position to sell",
                    )
                held = safe_int(position.get("qty"))
                if qty > held:
                    return OrderResult(
                        False,
                        "REJECTED",
                        correlation_id=intent.correlation_id,
                        message=f"sell qty {qty} exceeds held {held}",
                    )
                fill = ltp * (1.0 - slip)
                pnl = (fill - safe_float(position.get("avg_price"))) * qty
                self.account["cash"] = safe_float(self.account.get("cash")) + fill * qty
                self.account["realized_today"] = safe_float(self.account.get("realized_today")) + pnl
                self.account["realized_total"] = safe_float(self.account.get("realized_total")) + pnl
                remaining = held - qty
                if remaining == 0:
                    self.account["position"] = None
                else:
                    position["qty"] = remaining
            else:
                return OrderResult(False, "REJECTED", correlation_id=intent.correlation_id, message="bad side")

            self._save_account()
            result = OrderResult(
                accepted=True,
                status="TRADED",
                order_id=f"PAPER-{intent.correlation_id[:12]}",
                correlation_id=intent.correlation_id,
                filled_qty=qty,
                avg_fill_price=fill,
                message="paper fill",
            )
            self._orders[intent.correlation_id] = result
            return result

    def get_order(self, correlation_id: str, order_id: str | None = None) -> OrderResult:
        with self._lock:
            result = self._orders.get(correlation_id)
            if result:
                return result
            return OrderResult(
                accepted=True,
                status="PENDING",
                order_id=order_id,
                correlation_id=correlation_id,
                message="paper order not in memory (restart?)",
            )

    def cancel_order(self, order_id: str) -> bool:
        with self._lock:
            sl = self.account.get("sl")
            if sl and str(sl.get("order_id")) == str(order_id):
                self.account["sl"] = None
                self._save_account()
            return True

    def get_ltp(self, security_id: str) -> float:
        return safe_float(self._ltp_fn(security_id))

    def get_funds(self) -> dict:
        with self._lock:
            return {
                "cash": round(safe_float(self.account.get("cash")), 2),
                "realized_today": round(safe_float(self.account.get("realized_today")), 2),
                "realized_total": round(safe_float(self.account.get("realized_total")), 2),
                "position": self.account.get("position"),
                "sl": self.account.get("sl"),
            }

    def reset(self) -> None:
        with self._lock:
            self.account = self._default_account()
            self._orders.clear()
            self._last_external_fill = None
            self._save_account()

    def health(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "is_live": False,
                "cash": round(safe_float(self.account.get("cash")), 2),
                "position": self.account.get("position"),
                "account_file": str(self.path),
            }
