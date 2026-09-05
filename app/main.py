import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .bybit import BybitError
from .config import get_settings
from .engine import TradingEngine
from .models import CloseRequest, OpenRequest, RfqCancelRequest, RfqCreateRequest, RfqExecuteRequest

# The dashboard polls several endpoints frequently; HTTP 200 access lines are
# noise in production logs. Application warnings and errors remain visible.
logging.getLogger("uvicorn.access").disabled = True

settings = get_settings()
engine = TradingEngine(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await engine.refresh_chain(force=True)
    market_task = asyncio.create_task(engine.market_loop())
    schedule_task = asyncio.create_task(engine.scheduler())
    try:
        yield
    finally:
        market_task.cancel()
        schedule_task.cancel()
        await asyncio.gather(market_task, schedule_task, return_exceptions=True)
        await engine.client.close()


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
    return config_payload()


def config_payload():
    return {"environment": settings.environment, "live_enabled": settings.can_trade_live, "testnet": settings.bybit_testnet, "auto_open": settings.auto_open, "max_risk_usd": settings.max_risk_usd, "leg_qty": settings.leg_qty, "target_dte_days": settings.target_dte_days, "expiry_rule": "Friday entry / Sunday UTC expiry", "market_refresh_seconds": settings.market_refresh_seconds, "instrument_refresh_seconds": settings.instrument_refresh_seconds, "quote_stale_seconds": settings.quote_stale_seconds, "max_spread_bps": settings.max_spread_bps, "bbo_poll_seconds": settings.bbo_poll_seconds, "bbo_order_timeout_seconds": settings.bbo_order_timeout_seconds, "allow_market_fallback": settings.allow_market_fallback, "failed_leg_retry_delay_seconds": settings.failed_leg_retry_delay_seconds, "failed_leg_position_checks": settings.failed_leg_position_checks, "failed_leg_position_check_interval_seconds": settings.failed_leg_position_check_interval_seconds, "estimated_taker_fee_rate": settings.estimated_taker_fee_rate, "portfolio_margin_buffer_pct": settings.portfolio_margin_buffer_pct, "margin_mode": settings.margin_mode, "option_mm_factor": settings.option_mm_factor, "option_max_im_factor": settings.option_max_im_factor, "option_min_im_factor": settings.option_min_im_factor, "option_liquidation_fee_rate": settings.option_liquidation_fee_rate, "option_fee_cap_pct": settings.option_fee_cap_pct, "open_time": f"Friday {settings.open_hour_utc:02d}:{settings.open_minute_utc:02d} UTC"}


@app.get("/api/dashboard/market")
async def dashboard_market(quantity: float | None = Query(default=None, gt=0)):
    try:
        strategy = await engine.make_preview(quantity)
        expiry_items = [item for item in engine.chain if item.expiry == strategy.expiry]
        return {"config": config_payload(), "preview": strategy.model_dump(mode="json"), "chain": {"source": engine.chain_source, "btc_price": engine.btc_price, "updated_at": engine.chain_updated_at, "items": [item.model_dump(mode="json") for item in expiry_items]}}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/dashboard/account")
async def dashboard_account():
    position_result, health_result, execution_result = await asyncio.gather(engine.load_positions(), engine.load_account_health(), engine.load_recent_executions(), return_exceptions=True)
    if isinstance(position_result, Exception):
        engine.log("WARNING", f"Could not refresh dashboard positions: {position_result}")
        position_result = engine.positions
    if isinstance(health_result, Exception):
        engine.log("WARNING", f"Could not refresh dashboard account health: {health_result}")
        health_result = engine.account_health
    if isinstance(execution_result, Exception):
        engine.log("WARNING", f"Could not refresh dashboard executions: {execution_result}")
        execution_result = engine.last_executions
    return {"positions": {"items": [item.model_dump(mode="json") for item in position_result]}, "health": health_result.model_dump(mode="json"), "executions": {"items": [item.model_dump(mode="json") for item in execution_result]}, "logs": {"items": [item.model_dump(mode="json") for item in engine.logs]}}


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
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Open request failed: {exc}") from exc


@app.post("/api/trading/close")
async def close_trade(request: CloseRequest):
    try:
        results, executions = await engine.close_position(request)
        return {"results": [item.model_dump(mode="json") for item in results], "executions": [item.model_dump(mode="json") for item in executions], "live": settings.can_trade_live and request.confirm_live}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Close request failed: {exc}") from exc


@app.get("/api/rfq/config")
async def rfq_config():
    try:
        return await engine.client.rfq_config()
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc


@app.get("/api/rfq/status")
async def rfq_status(refresh: bool = Query(default=True)):
    try:
        return await engine.refresh_rfq() if refresh else engine.rfq_state
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc


@app.post("/api/rfq/create")
async def rfq_create(request: RfqCreateRequest):
    try:
        return await engine.create_rfq(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc


@app.post("/api/rfq/execute")
async def rfq_execute(request: RfqExecuteRequest):
    try:
        return await engine.execute_rfq(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc


@app.post("/api/rfq/cancel")
async def rfq_cancel(request: RfqCancelRequest):
    try:
        return await engine.cancel_rfq(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc


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
