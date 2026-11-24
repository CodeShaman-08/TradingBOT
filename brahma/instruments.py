"""Dhan instrument-master resolution.

Removes the hard-coded option security id from the original script. The master
CSV is cached in ``data/instrument_master.csv`` and refreshed daily.

If the master cannot be downloaded (offline / no data plan), the engine falls
back to OPTION_SECURITY_ID / UNDERLYING_SECURITY_ID from the environment.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from .models import Instrument
from .timeutils import now_ist

log = logging.getLogger("brahma.instruments")


class InstrumentError(RuntimeError):
    pass


@dataclass
class UnderlyingInfo:
    symbol: str
    security_id: str
    exchange_segment: str = "IDX_I"
    instrument_type: str = "INDEX"
    source: str = "master"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "exchange_segment": self.exchange_segment,
            "instrument_type": self.instrument_type,
            "source": self.source,
        }


CANONICAL = [
    "security_id",
    "exchange",
    "segment",
    "instrument",
    "trading_symbol",
    "custom_symbol",
    "expiry",
    "strike",
    "option_type",
    "lot_size",
    "tick_size",
    "freeze_qty",
    "underlying",
]


def _pick(columns: list[str], candidates: list[str]) -> str | None:
    lower = {c.lower(): c for c in columns}
    for candidate in candidates:
        if candidate in lower:
            return lower[candidate]
    for c in columns:
        cl = c.lower()
        if any(candidate in cl for candidate in candidates):
            return c
    return None


class InstrumentMaster:
    def __init__(self, settings, client=None) -> None:
        self.settings = settings
        self.path: Path = settings.instrument_master
        self._client = client
        self._df: pd.DataFrame | None = None
        self._lock = threading.RLock()
        self._last_error: str | None = None
        self._downloaded_at: float = 0.0

    # ------------------------------------------------------------- download
    def _urls(self) -> list[str]:
        urls: list[str] = []
        try:
            from dhanhq import dhanhq as Dhan

            urls.extend([Dhan.DETAILED_CSV_URL, Dhan.COMPACT_CSV_URL])
        except Exception:  # noqa: BLE001
            pass
        urls.extend(
            [
                "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
                "https://images.dhan.co/api-data/api-scrip-master.csv",
            ]
        )
        # de-duplicate while preserving order
        return list(dict.fromkeys(urls))

    def _download(self) -> bool:
        request_headers = {"User-Agent": "Mozilla/5.0 (brahma-alarm)"}
        for url in self._urls():
            try:
                request = urllib.request.Request(url, headers=request_headers)
                with urllib.request.urlopen(request, timeout=90) as response:
                    payload = response.read()
                if len(payload) < 5_000:
                    raise InstrumentError(f"suspiciously small master ({len(payload)} bytes)")
                tmp = self.path.with_suffix(".tmp")
                tmp.write_bytes(payload)
                tmp.replace(self.path)
                self._downloaded_at = time.time()
                log.info("Instrument master downloaded (%s bytes) from %s", len(payload), url)
                return True
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{url}: {exc}"
                log.warning("Instrument master download failed from %s: %s", url, exc)
        return False

    def _is_stale(self) -> bool:
        if not self.path.exists():
            return True
        age = time.time() - self.path.stat().st_mtime
        return age > 12 * 3600

    # ------------------------------------------------------------------ load
    def load(self, force: bool = False) -> pd.DataFrame:
        with self._lock:
            if self._df is not None and not force:
                return self._df

            if force or self._is_stale():
                if not self._download() and not self.path.exists():
                    raise InstrumentError(
                        f"Instrument master unavailable and no cached file at {self.path}. "
                        f"Last error: {self._last_error}"
                    )

            if not self.path.exists():
                raise InstrumentError(f"Instrument master missing: {self.path}")

            raw = pd.read_csv(self.path, low_memory=False)
            self._df = self._normalize(raw)
            log.info("Instrument master loaded: %s rows", len(self._df))
            return self._df

    def _normalize(self, raw: pd.DataFrame) -> pd.DataFrame:
        columns = list(raw.columns)
        mapping = {
            "security_id": _pick(columns, ["sem_smst_security_id", "security_id", "securityid"]),
            "exchange": _pick(columns, ["sem_exm_exch_id", "exchange_id", "exchange"]),
            "segment": _pick(columns, ["sem_segment", "segment"]),
            "instrument": _pick(columns, ["sem_instrument_name", "instrument_name", "instrument_type"]),
            "trading_symbol": _pick(columns, ["sem_trading_symbol", "trading_symbol"]),
            "custom_symbol": _pick(columns, ["sem_custom_symbol", "custom_symbol"]),
            "expiry": _pick(columns, ["sem_expiry_date", "expiry_date", "expiry"]),
            "strike": _pick(columns, ["sem_strike_price", "strike_price", "strike"]),
            "option_type": _pick(columns, ["sem_option_type", "option_type"]),
            "lot_size": _pick(columns, ["sem_lot_units", "lot_units", "lot_size"]),
            "tick_size": _pick(columns, ["sem_tick_size", "tick_size"]),
            "freeze_qty": _pick(columns, ["sem_freeze_qty", "freeze_qty"]),
            "underlying": _pick(columns, ["sm_symbol_name", "underlying_symbol", "symbol_name"]),
        }
        if mapping["security_id"] is None:
            raise InstrumentError(f"Cannot find a security-id column in master: {columns[:20]}")

        out = pd.DataFrame()
        for canonical, source in mapping.items():
            out[canonical] = raw[source] if source is not None else pd.NA

        out["security_id"] = out["security_id"].astype(str).str.strip()
        for column in ("exchange", "segment", "instrument", "trading_symbol", "custom_symbol", "option_type", "underlying"):
            out[column] = out[column].astype(str).str.strip()
        out["expiry_date"] = pd.to_datetime(out["expiry"], errors="coerce", dayfirst=False).dt.date
        out["strike"] = pd.to_numeric(out["strike"], errors="coerce")
        out["lot_size"] = pd.to_numeric(out["lot_size"], errors="coerce").fillna(0).astype(int)
        out["tick_size"] = pd.to_numeric(out["tick_size"], errors="coerce").fillna(0.05)
        out["freeze_qty"] = pd.to_numeric(out["freeze_qty"], errors="coerce").fillna(0).astype(int)
        return out

    # ------------------------------------------------------------- resolve
    def resolve_underlying(self, symbol: str) -> UnderlyingInfo | None:
        df = self.load()
        symbol = symbol.upper().strip()
        candidates = df[
            df["instrument"].str.upper().str.contains("INDEX", na=False)
            & (
                df["trading_symbol"].str.upper().eq(symbol)
                | df["custom_symbol"].str.upper().eq(symbol)
                | df["underlying"].str.upper().eq(symbol)
            )
        ]
        if candidates.empty:
            # Some master versions use a different label for the index row.
            candidates = df[
                df["trading_symbol"].str.upper().eq(symbol)
                | df["custom_symbol"].str.upper().eq(symbol)
            ]
        if candidates.empty:
            return None
        row = candidates.iloc[0]
        return UnderlyingInfo(
            symbol=symbol,
            security_id=str(row["security_id"]),
            exchange_segment="IDX_I",
            instrument_type="INDEX",
            source="master",
        )

    def _option_rows(self, symbol: str, side: str) -> pd.DataFrame:
        df = self.load()
        symbol = symbol.upper().strip()
        side = side.upper().strip()
        mask = (
            df["instrument"].str.upper().str.contains("OPT", na=False)
            & df["option_type"].str.upper().eq(side)
            & df["strike"].notna()
            & (df["strike"] > 0)
            & df["expiry_date"].notna()
        )
        # Match BANKNIFTY / NIFTY without matching FINNIFTY etc. by tokenizing
        # the symbol-ish columns on non-alphanumerics.
        haystack = (
            df["trading_symbol"].str.upper().fillna("")
            + "|"
            + df["custom_symbol"].str.upper().fillna("")
            + "|"
            + df["underlying"].str.upper().fillna("")
        )
        token_mask = haystack.str.contains(rf"(^|[^A-Z]){symbol}([^A-Z]|$)", regex=True, na=False)
        rows = df[mask & token_mask].copy()
        today = now_ist(self.settings.tz).date()
        rows = rows[rows["expiry_date"] >= today]
        return rows

    def resolve_option(
        self,
        spot: float,
        side: str | None = None,
        expiry: str | None = None,
        strike_mode: str | None = None,
    ) -> Instrument:
        side = (side or self.settings.option_side).upper()
        strike_mode = (strike_mode or self.settings.strike_mode).upper()
        rows = self._option_rows(self.settings.underlying_symbol, side)
        if rows.empty:
            raise InstrumentError(
                f"No {self.settings.underlying_symbol} {side} options found in master"
            )

        requested_expiry = expiry or self.settings.expiry
        if requested_expiry:
            target = pd.to_datetime(requested_expiry, errors="coerce", dayfirst=True)
            if pd.isna(target):
                raise InstrumentError(f"Unparseable OPTION_EXPIRY: {requested_expiry}")
            rows = rows[rows["expiry_date"] == target.date()]
            if rows.empty:
                raise InstrumentError(f"No {side} options expiring {target.date()}")
            expiry_date = target.date()
        else:
            expiry_date = min(rows["expiry_date"])
            rows = rows[rows["expiry_date"] == expiry_date]

        strikes = sorted(float(x) for x in rows["strike"].dropna().unique())
        if not strikes:
            raise InstrumentError("No strikes available for selected expiry")
        atm_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - float(spot)))

        steps = 0
        if strike_mode.startswith("ITM"):
            steps = -int(strike_mode[-1]) if side == "CE" else int(strike_mode[-1])
        elif strike_mode.startswith("OTM"):
            steps = int(strike_mode[-1]) if side == "CE" else -int(strike_mode[-1])
        target_index = max(0, min(len(strikes) - 1, atm_index + steps))
        strike = strikes[target_index]

        chosen = rows[rows["strike"] == strike].iloc[0]
        return Instrument(
            security_id=str(chosen["security_id"]),
            trading_symbol=str(chosen["trading_symbol"]),
            lot_size=int(chosen["lot_size"]) or int(self.settings.option_lot_size) or 0,
            tick_size=float(chosen["tick_size"]) or 0.05,
            freeze_qty=int(chosen["freeze_qty"]) or 0,
            exchange_segment="NSE_FNO",
            instrument_type="OPTIDX",
            expiry=str(expiry_date),
            strike=strike,
            option_type=side,
            source="master",
        )

    def resolve_by_security_id(self, security_id: str) -> Instrument | None:
        df = self.load()
        rows = df[df["security_id"] == str(security_id)]
        if rows.empty:
            return None
        row = rows.iloc[0]
        return Instrument(
            security_id=str(row["security_id"]),
            trading_symbol=str(row["trading_symbol"]),
            lot_size=int(row["lot_size"]),
            tick_size=float(row["tick_size"]) or 0.05,
            freeze_qty=int(row["freeze_qty"]) or 0,
            exchange_segment="NSE_FNO",
            instrument_type=str(row["instrument"]) or "OPTIDX",
            expiry=str(row["expiry_date"]) if row["expiry_date"] else None,
            strike=float(row["strike"]) if pd.notna(row["strike"]) else None,
            option_type=str(row["option_type"]) if row["option_type"] not in ("", "nan") else None,
            source="master",
        )

    def health(self) -> dict:
        return {
            "path": str(self.path),
            "cached_rows": int(len(self._df)) if self._df is not None else 0,
            "downloaded_at": datetime.fromtimestamp(self._downloaded_at).isoformat()
            if self._downloaded_at
            else None,
            "last_error": self._last_error,
        }


def fallback_instrument(settings) -> Instrument | None:
    security_id = settings.option_security_id.strip()
    if not security_id:
        return None
    return Instrument(
        security_id=security_id,
        trading_symbol=f"{settings.underlying_symbol}-{settings.option_side}",
        lot_size=int(settings.option_lot_size) or 0,
        exchange_segment="NSE_FNO",
        instrument_type="OPTIDX",
        option_type=settings.option_side,
        source="config",
    )
