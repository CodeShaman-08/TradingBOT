"""Durable state + append-only journals. No database.

* state.json      : atomic snapshot of daily counters, halt flags, position cache
* orders.jsonl    : write-ahead order intents + terminal results
* trades.jsonl    : round-trip trade records
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .timeutils import now_ist

log = logging.getLogger("brahma.state")


def _default_state(tz_name: str) -> dict[str, Any]:
    return {
        "date": now_ist(tz_name).date().isoformat(),
        "entries": 0,
        "last_candle": None,
        "last_signal": None,
        "kill_switch": False,
        "kill_reason": None,
        "entries_blocked": False,
        "entries_block_reason": None,
        "pending_order": None,
        "position": None,
        "last_exit": None,
        "sl_order_id": None,
        "sl_correlation_id": None,
        "signal_history": [],
        "updated_at": now_ist(tz_name).isoformat(),
    }


class StateStore:
    def __init__(self, path: Path, tz_name: str = "Asia/Kolkata") -> None:
        self.path = Path(path)
        self.tz_name = tz_name
        self._lock = threading.RLock()
        self.state: dict[str, Any] = _default_state(tz_name)
        self.persist_error: str | None = None
        self.save_failures = 0

    # ------------------------------------------------------------------ load
    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                self.state = _default_state(self.tz_name)
                self._save_locked()
                return self.state

            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("state root is not an object")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                quarantine = self.path.with_name(
                    f"{self.path.stem}.corrupt-{datetime.now().strftime('%Y%m%d%H%M%S')}{self.path.suffix}"
                )
                try:
                    self.path.replace(quarantine)
                except OSError:
                    pass
                log.critical("State file unreadable (%s). Quarantined to %s; starting HALTED.", exc, quarantine)
                self.state = _default_state(self.tz_name)
                self.state["kill_switch"] = True
                self.state["kill_reason"] = f"State file was corrupt: {exc}"
                self._save_locked()
                return self.state

            merged = _default_state(self.tz_name)
            merged.update(raw)
            today = now_ist(self.tz_name).date().isoformat()
            if merged.get("date") != today:
                log.info("New trading day %s: resetting daily counters (halt flags preserved).", today)
                merged["date"] = today
                merged["entries"] = 0
                merged["last_candle"] = None
                merged["signal_history"] = []
                # A daily-loss block expires with the day; a manual kill switch does not.
                reason = str(merged.get("entries_block_reason") or "").lower()
                if reason.startswith("daily loss"):
                    merged["entries_blocked"] = False
                    merged["entries_block_reason"] = None
            merged["updated_at"] = now_ist(self.tz_name).isoformat()
            self.state = merged
            self._save_locked()
            return self.state

    # ------------------------------------------------------------------ save
    def _save_locked(self) -> None:
        """Persist state without ever taking the bot down.

        Windows (especially OneDrive-synced folders and antivirus scanners) can
        briefly hold ``state.json`` open, which makes ``os.replace`` fail with
        WinError 5. A transient lock must not abort a trading tick, so we retry
        and, if the atomic rename still loses, fall back to an in-place write.
        The in-memory state stays authoritative and the journals remain the
        audit trail.
        """
        self.state["updated_at"] = now_ist(self.tz_name).isoformat()
        payload = json.dumps(self.state, indent=2, default=str)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("State directory could not be created: %s", exc)
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")

        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            self._replace_with_retry(tmp)
        except OSError as exc:
            self.save_failures += 1
            self.persist_error = f"{type(exc).__name__}: {exc}"
            if self.save_failures == 1:
                log.error("Atomic state save failed (%s); falling back to direct write.", exc)
            self._direct_write_locked(payload)
        else:
            if self.persist_error:
                log.info("State persistence recovered after %s failure(s).", self.save_failures)
            self.persist_error = None
            self.save_failures = 0
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def _replace_with_retry(self, tmp: Path, attempts: int = 8) -> None:
        last: OSError | None = None
        for attempt in range(attempts):
            try:
                os.replace(tmp, self.path)
                return
            except OSError as exc:
                last = exc
                winerror = getattr(exc, "winerror", None)
                # 5 = access denied, 32/33 = sharing violation / lock violation.
                if winerror is not None and winerror not in (5, 32, 33):
                    raise
                if attempt < attempts - 1:
                    time.sleep(0.015 * (attempt + 1))
        if last is not None:
            raise last

    def _direct_write_locked(self, payload: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
        except OSError as exc:
            self.persist_error = f"direct write failed: {exc}"
            log.error("State could not be written at all: %s", exc)

    def save(self) -> None:
        with self._lock:
            self._save_locked()

    def flush(self) -> None:
        """Force a save (used on shutdown)."""
        with self._lock:
            self._save_locked()

    def update(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.state.update(kwargs)
            self._save_locked()
            return self.state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self.state, default=str))

    # ------------------------------------------------------------- helpers
    def set_kill_switch(self, enabled: bool, reason: str | None = None) -> None:
        with self._lock:
            self.state["kill_switch"] = enabled
            self.state["kill_reason"] = reason if enabled else None
            if enabled:
                self.state["entries_blocked"] = True
                self.state["entries_block_reason"] = reason or "kill switch"
            self._save_locked()

    def set_entries_blocked(self, blocked: bool, reason: str | None = None) -> None:
        with self._lock:
            self.state["entries_blocked"] = blocked
            self.state["entries_block_reason"] = reason if blocked else None
            self._save_locked()

    def push_signal(self, signal: dict[str, Any], last_candle: str | None = None) -> None:
        with self._lock:
            history = list(self.state.get("signal_history") or [])
            history.append(signal)
            self.state["signal_history"] = history[-50:]
            self.state["last_signal"] = signal
            if last_candle is not None:
                self.state["last_candle"] = last_candle
            self._save_locked()


class JsonlJournal:
    """Append-only JSONL journal. One fsync per record; safe for small volumes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, default=str)
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())

    def tail(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit <= 0 or not self.path.exists():
            return []
        with self._lock:
            try:
                with open(self.path, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    block = min(size, max(64 * 1024, limit * 512))
                    fh.seek(size - block)
                    payload = fh.read().decode("utf-8", errors="replace")
            except OSError:
                return []
        lines = payload.splitlines()
        # A byte-tail can start mid-line; drop that partial first line.
        if block < size and lines:
            lines = lines[1:]
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                if isinstance(item, dict):
                    out.append(item)
            except json.JSONDecodeError:
                continue
        return out
