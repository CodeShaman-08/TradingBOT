"""Small defensive helpers for broker/API payloads whose shapes vary."""

from __future__ import annotations

from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or (isinstance(value, str) and not value.strip()):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def first_present(mapping: Any, keys: tuple[str, ...] | list[str], default: Any = None) -> Any:
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    # case-insensitive fallback
    lowered = {str(k).lower(): v for k, v in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None:
            return value
    return default


def extract_ltp(payload: Any) -> float:
    """Recursively find the first plausible last-traded price in a payload."""
    if isinstance(payload, dict):
        for key in ("last_price", "lastPrice", "ltp", "LTP", "lastTradedPrice", "close"):
            value = payload.get(key)
            if isinstance(value, (int, float)) and float(value) > 0:
                return float(value)
        for value in payload.values():
            found = extract_ltp(value)
            if found > 0:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = extract_ltp(item)
            if found > 0:
                return found
    return 0.0
