import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import get_settings
from .engine import TradingEngine
from .models import CloseRequest, OpenRequest

settings = get_settings()
engine = TradingEngine(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await engine.refresh_chain(force=True)
    market_task = asyncio.create_task(engine.market_loop())
    schedule_task = asyncio.create_task(engine.scheduler())
    yield
    market_task.cancel()
    schedule_task.cancel()


app = FastAPI(title="BTC Iron Condor", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "environment": settings.environment, "live_enabled": settings.can_trade_live}


@app.get("/api/config")
async def config():
    return {"environment": settings.environment, "live_enabled": settings.can_trade_live, "testnet": settings.bybit_testnet, "auto_open": settings.auto_open, "max_risk_usd": settings.max_risk_usd, "leg_qty": settings.leg_qty, "target_dte_days": settings.target_dte_days, "market_refresh_seconds": settings.market_refresh_seconds, "quote_stale_seconds": settings.quote_stale_seconds, "max_spread_bps": settings.max_spread_bps, "bbo_poll_seconds": settings.bbo_poll_seconds, "bbo_order_timeout_seconds": settings.bbo_order_timeout_seconds, "failed_leg_retry_delay_seconds": settings.failed_leg_retry_delay_seconds, "failed_leg_position_checks": settings.failed_leg_position_checks, "failed_leg_position_check_interval_seconds": settings.failed_leg_position_check_interval_seconds, "estimated_taker_fee_rate": settings.estimated_taker_fee_rate, "portfolio_margin_buffer_pct": settings.portfolio_margin_buffer_pct, "margin_mode": settings.margin_mode, "option_mm_factor": settings.option_mm_factor, "option_max_im_factor": settings.option_max_im_factor, "option_min_im_factor": settings.option_min_im_factor, "option_liquidation_fee_rate": settings.option_liquidation_fee_rate, "option_fee_cap_pct": settings.option_fee_cap_pct, "open_time": f"Friday {settings.open_hour_utc:02d}:{settings.open_minute_utc:02d} UTC"}


@app.get("/api/chain")
async def chain():
    await engine.refresh_chain()
    return {"source": engine.chain_source, "btc_price": engine.btc_price, "updated_at": engine.chain_updated_at, "items": [item.model_dump(mode="json") for item in engine.chain]}


@app.post("/api/market/refresh")
async def refresh_market():
    items = await engine.refresh_chain(force=True)
    return {"source": engine.chain_source, "count": len(items)}


@app.get("/api/strategy/preview")
async def preview(quantity: float | None = Query(default=None, gt=0)):
    try:
        return (await engine.make_preview(quantity)).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/trading/open")
async def open_trade(request: OpenRequest):
    try:
        results = await engine.open_position(request)
        return {"results": [item.model_dump(mode="json") for item in results], "executions": [item.model_dump(mode="json") for item in engine.last_executions], "live": settings.can_trade_live and request.confirm_live}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/trading/close")
async def close_trade(request: CloseRequest):
    try:
        results, executions = await engine.close_position(request)
        return {"results": [item.model_dump(mode="json") for item in results], "executions": [item.model_dump(mode="json") for item in executions], "live": settings.can_trade_live and request.confirm_live}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/trading/executions")
async def executions():
    return {"items": [item.model_dump(mode="json") for item in await engine.load_recent_executions()]}


@app.get("/api/positions")
async def positions():
    return {"items": [item.model_dump() for item in await engine.load_positions()]}


@app.get("/api/account/health")
async def account_health():
    return (await engine.load_account_health()).model_dump(mode="json")


@app.get("/api/logs")
async def logs():
    return {"items": [item.model_dump(mode="json") for item in engine.logs]}
