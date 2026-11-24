"""FastAPI dashboard + control API. No database: files are the source of truth."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .backtest import compare
from .config import Settings, get_settings
from .engine import BotEngine
from .logging_setup import setup_logging
from .strategy import StrategyConfig

WEB_DIR = Path(__file__).resolve().parent / "web"
log = logging.getLogger("brahma.api")


class ConfirmBody(BaseModel):
    confirm: str = ""
    reason: str | None = None


def _require_confirm(body: ConfirmBody, word: str) -> None:
    if (body.confirm or "").strip().upper() != word:
        raise HTTPException(status_code=400, detail=f"Type {word} to confirm this action")


def _tail_file(path: Path, limit: int) -> list[str]:
    if limit <= 0 or not path.exists():
        return []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, 256 * 1024)
            fh.seek(size - block)
            payload = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return payload.splitlines()[-limit:]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(settings)
    engine = BotEngine(settings)
    backtest_cache: dict[str, Any] = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        startup_task: asyncio.Task | None = None
        if settings.engine_enabled:
            # Do not block the dashboard on a slow instrument-master download.
            startup_task = asyncio.create_task(engine.start(), name="brahma-startup")
        app.state.startup_task = startup_task
        yield
        await engine.stop()
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()

    app = FastAPI(title="BRAHMA ALARM", version="2.0.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.settings = settings

    if WEB_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        index_file = WEB_DIR / "index.html"
        if not index_file.exists():
            raise HTTPException(status_code=500, detail="Dashboard assets missing")
        return FileResponse(index_file)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    # ------------------------------------------------------------------- api
    @app.get("/api/health")
    async def health() -> dict:
        return {"ok": True, "engine": engine.health()}

    @app.get("/api/status")
    async def status() -> dict:
        return engine.status_payload()

    @app.get("/api/candles")
    async def candles(limit: int = Query(default=300, ge=20, le=2000)) -> dict:
        await engine.refresh_for_display()
        return engine.chart_payload(limit)

    @app.get("/api/trades")
    async def trades(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
        items = list(reversed(engine.trade_journal.tail(limit)))
        return {"items": items, "count": len(items)}

    @app.get("/api/orders")
    async def orders(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
        items = list(reversed(engine.order_journal.tail(limit)))
        return {"items": items, "count": len(items)}

    @app.get("/api/logs")
    async def logs(limit: int = Query(default=200, ge=1, le=2000)) -> dict:
        return {"lines": _tail_file(settings.log_file, limit)}

    @app.get("/api/backtest")
    async def backtest(
        cost_bps: float = Query(default=2.0, ge=0, le=100),
        limit: int = Query(default=5000, ge=200, le=50000),
    ) -> dict:
        if engine.provider is None:
            raise HTTPException(status_code=503, detail="Data provider is not ready")
        try:
            candles_frame = await asyncio.to_thread(engine.provider.history, limit)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"Cannot load history: {exc}") from exc

        if candles_frame is None or len(candles_frame) < 100:
            raise HTTPException(status_code=503, detail="Not enough candles for a backtest")

        last_ts = str(candles_frame.index[-1])
        key = f"{last_ts}|{cost_bps}|{limit}|{settings.length}|{settings.gain}"
        if key in backtest_cache:
            return backtest_cache[key]

        cfg = StrategyConfig.from_settings(settings)
        try:
            result = await asyncio.to_thread(compare, candles_frame, cfg, cost_bps)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"Backtest failed: {exc}") from exc

        result["cached"] = False
        backtest_cache.clear()
        backtest_cache[key] = result
        return result

    @app.get("/api/instrument")
    async def instrument() -> dict:
        return {
            "instrument": engine.instrument.to_dict() if engine.instrument else None,
            "underlying": engine.underlying.to_dict() if engine.underlying else None,
        }

    # -------------------------------------------------------------- controls
    @app.post("/api/control/halt")
    async def halt(body: ConfirmBody) -> dict:
        _require_confirm(body, "HALT")
        engine.halt(body.reason or "manual halt")
        return {"ok": True, "state": engine.state.snapshot()}

    @app.post("/api/control/resume")
    async def resume(body: ConfirmBody) -> dict:
        _require_confirm(body, "RESUME")
        ok, message = engine.resume()
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        return {"ok": True, "message": message, "state": engine.state.snapshot()}

    @app.post("/api/control/block")
    async def block(body: ConfirmBody) -> dict:
        _require_confirm(body, "BLOCK")
        engine.block_entries(body.reason or "manual block")
        return {"ok": True, "state": engine.state.snapshot()}

    @app.post("/api/control/allow")
    async def allow(body: ConfirmBody) -> dict:
        _require_confirm(body, "ALLOW")
        engine.allow_entries()
        return {"ok": True, "state": engine.state.snapshot()}

    @app.post("/api/control/flatten")
    async def flatten(body: ConfirmBody) -> dict:
        _require_confirm(body, "FLATTEN")
        ok, message = await engine.flatten(body.reason or "MANUAL_FLATTEN")
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        return {"ok": True, "message": message}

    @app.post("/api/control/reset-paper")
    async def reset_paper(body: ConfirmBody) -> dict:
        _require_confirm(body, "RESET")
        ok, message = engine.reset_paper()
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        return {"ok": True, "message": message}

    @app.post("/api/control/go-live")
    async def go_live(body: ConfirmBody) -> dict:
        _require_confirm(body, "LIVE")
        ok, message = await engine.go_live()
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        return {"ok": True, "message": message}

    @app.post("/api/control/go-paper")
    async def go_paper(body: ConfirmBody) -> dict:
        _require_confirm(body, "PAPER")
        ok, message = await engine.go_paper()
        if not ok:
            raise HTTPException(status_code=409, detail=message)
        return {"ok": True, "message": message}

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "brahma.api:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
