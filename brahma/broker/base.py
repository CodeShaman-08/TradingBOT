"""Broker contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from ..models import OrderIntent, OrderResult, Snapshot


class BrokerError(RuntimeError):
    """Raised when broker state cannot be trusted."""


class Broker(ABC):
    name: str = "base"
    is_live: bool = False

    @abstractmethod
    def get_snapshot(self, security_id: str) -> Snapshot:
        ...

    @abstractmethod
    def place_order(self, intent: OrderIntent, ltp: float) -> OrderResult:
        ...

    @abstractmethod
    def get_order(self, correlation_id: str, order_id: str | None = None) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        ...

    @abstractmethod
    def get_ltp(self, security_id: str) -> float:
        ...

    def get_funds(self) -> dict[str, Any]:
        return {}

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "is_live": self.is_live}
