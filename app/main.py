import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles

from .bybit import BybitError
from .config import get_settings
from .engine import TradingEngine
from .cache import SnapshotCache
from .lease import StateLease
from .security import authorize_dashboard
from .strategy import SundayExpiryUnavailable
from .models import CloseRequest, OpenRequest, RfqCancelRequest, RfqCreateRequest, RfqExecuteRequest

# The dashboard polls several endpoints frequently; HTTP 200 access lines are
# noise in production logs. Application warnings and errors remain visible.
logging.getLogger("uvicorn.access").disabled = True

settings = get_settings()
engine = TradingEngine(settings)
account_cache = SnapshotCache(settings.account_cache_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    with StateLease(settings.state_file):
        engine._load_state()
        account_cache.invalidate()
        tasks = []
        try:
            await engine.refresh_chain(force=True)
            tasks = [asyncio.create_task(engine.market_loop()), asyncio.create_task(engine.scheduler()), asyncio.create_task(engine.reconciliation_loop())]
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await engine.client.close()


app = FastAPI(title="BTC Iron Condor", version="0.1.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.middleware("http")
async def protect_dashboard(request: Request, call_next):
    denied = authorize_dashboard(request, settings)
    if denied is not None:
        return denied
    try:
        response = await call_next(request)
    finally:
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            account_cache.invalidate()
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    # Do not echo request inputs (possibly secrets or NaN/Infinity) into JSON.
    errors = [{"loc": item["loc"], "msg": item["msg"], "type": item["type"]} for item in exc.errors()]
    return JSONResponse({"detail": errors}, status_code=422)


@app.exception_handler(httpx.HTTPError)
async def upstream_error(_: Request, exc: httpx.HTTPError):
    engine.log("WARNING", f"Exchange communication failed: {type(exc).__name__}")
    return JSONResponse({"detail": "Exchange communication failed; retry after checking connectivity"}, status_code=502)


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/health")
async def health():
    recovery = engine.reconciliation_health()
    degraded = engine.state_error or recovery["pending_orders"] or recovery["pending_rfq"] or recovery["error"]
    if settings.can_send_orders:
        last = recovery["last_success_at"] or engine.reconciliation_started_at
        degraded = degraded or (datetime.now(timezone.utc) - last).total_seconds() > max(60, settings.reconciliation_seconds * 3)
    return {"status": "degraded" if degraded else "ok", "environment": settings.environment,
            "live_enabled": settings.can_trade_live, "trading_enabled": settings.can_send_orders,
            "trading_blocked_reason": engine.state_error, "reconciliation": recovery}


@app.get("/api/config")
async def config():
    return config_payload()


def config_payload():
    return {"trading_blocked_reason": engine.state_error, "environment": settings.environment, "live_enabled": settings.can_trade_live, "trading_enabled": settings.can_send_orders, "testnet": settings.private_testnet, "market_testnet": settings.environment == "testnet", "opening_blocked_reason": "Unresolved RFQ; awaiting reconciliation" if engine._rfq_unresolved() else None, "auto_open": settings.auto_open, "max_risk_usd": settings.max_risk_usd, "leg_qty": settings.leg_qty, "target_dte_days": settings.target_dte_days, "expiry_rule": "Friday entry / Sunday UTC expiry", "market_refresh_seconds": settings.market_refresh_seconds, "instrument_refresh_seconds": settings.instrument_refresh_seconds, "quote_stale_seconds": settings.quote_stale_seconds, "max_spread_bps": settings.max_spread_bps, "bbo_poll_seconds": settings.bbo_poll_seconds, "bbo_order_timeout_seconds": settings.bbo_order_timeout_seconds, "allow_market_fallback": settings.allow_market_fallback, "failed_leg_retry_delay_seconds": settings.failed_leg_retry_delay_seconds, "failed_leg_position_checks": settings.failed_leg_position_checks, "failed_leg_position_check_interval_seconds": settings.failed_leg_position_check_interval_seconds, "estimated_taker_fee_rate": settings.estimated_taker_fee_rate, "portfolio_margin_buffer_pct": settings.portfolio_margin_buffer_pct, "margin_mode": settings.margin_mode, "option_mm_factor": settings.option_mm_factor, "option_max_im_factor": settings.option_max_im_factor, "option_min_im_factor": settings.option_min_im_factor, "option_liquidation_fee_rate": settings.option_liquidation_fee_rate, "option_fee_cap_pct": settings.option_fee_cap_pct, "open_time": f"Friday {settings.open_hour_utc:02d}:{settings.open_minute_utc:02d} UTC"}


@app.get("/api/dashboard/market")
async def dashboard_market(quantity: float | None = Query(default=None, gt=0, allow_inf_nan=False)):
    try:
        strategy = await engine.make_preview(quantity)
        expiry_items = [item for item in engine.chain if item.expiry == strategy.expiry]
        return {"status": "ready", "config": config_payload(), "preview": strategy.model_dump(mode="json"), "chain": {"source": engine.chain_source, "btc_price": engine.btc_price, "updated_at": engine.chain_updated_at, "items": [item.model_dump(mode="json") for item in expiry_items]}}
    except SundayExpiryUnavailable as exc:
        # Missing quotes for an existing Sunday contract are not a listing wait.
        now = datetime.now(timezone.utc)
        has_sunday = any(item.expiry > now and item.expiry.weekday() == 6 for item in engine.chain)
        age = (now - engine.chain_updated_at).total_seconds() if engine.chain_updated_at else None
        if engine.chain_source != "bybit" or age is None or not 0 <= age <= settings.quote_stale_seconds:
            raise HTTPException(status_code=503, detail="行情暂不可用或已过期，无法确认周日到期合约是否上线") from exc
        if has_sunday or not engine.chain:
            raise HTTPException(status_code=422, detail="周日到期合约报价暂不足以构建策略") from exc
        observation_date = (now + timedelta(days=2)).date()
        observation_expiry = min((item.expiry for item in engine.chain if item.expiry > now and item.expiry.astimezone(timezone.utc).date() == observation_date), default=None)
        observation_items = [item for item in engine.chain if item.expiry == observation_expiry] if observation_expiry else []
        message = (f"周日到期合约尚未上线，暂展示 {observation_date:%Y-%m-%d}（UTC 后天）到期盘口，仅供查看，不能用于开仓或询价。"
                   if observation_items else "周日及 UTC 后天到期合约尚未上线，系统将自动检查；周日合约上线后恢复策略。")
        # Observation quotes never become an execution preview or change the
        # engine's Sunday-only expiry selection used by opening and RFQ APIs.
        return {"status": "waiting_for_listing", "read_only": True, "message": message,
                "config": config_payload(), "preview": None,
                "chain": {"source": engine.chain_source, "btc_price": engine.btc_price, "updated_at": engine.chain_updated_at,
                          "expiry": observation_expiry, "items": [item.model_dump(mode="json") for item in observation_items]}}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/dashboard/account")
async def dashboard_account():
    return await account_cache.get(_build_account_dashboard)


async def _build_account_dashboard():
    position_result, health_result, execution_result = await asyncio.gather(engine.load_positions(), engine.load_account_health(), engine.load_recent_executions(), return_exceptions=True)
    positions_available = not isinstance(position_result, Exception)
    if isinstance(position_result, Exception):
        engine.log("WARNING", f"Could not refresh dashboard positions: {position_result}")
        position_result = engine.positions
    if isinstance(health_result, Exception):
        engine.log("WARNING", f"Could not refresh dashboard account health: {health_result}")
        health_result = engine.account_health
    if isinstance(execution_result, Exception):
        engine.log("WARNING", f"Could not refresh dashboard executions: {execution_result}")
        execution_result = engine.last_executions
    return {"positions": {"available": positions_available, "items": [item.model_dump(mode="json") for item in position_result]}, "health": health_result.model_dump(mode="json"), "executions": {"items": [item.model_dump(mode="json") for item in execution_result]}, "logs": {"items": [item.model_dump(mode="json") for item in engine.logs]}}


@app.get("/api/chain")
async def chain():
    await engine.refresh_chain()
    return {"source": engine.chain_source, "btc_price": engine.btc_price, "updated_at": engine.chain_updated_at, "items": [item.model_dump(mode="json") for item in engine.chain]}


@app.post("/api/market/refresh")
async def refresh_market():
    items = await engine.refresh_chain(force=True, refresh_instruments=True)
    return {"source": engine.chain_source, "count": len(items)}


@app.get("/api/strategy/preview")
async def preview(quantity: float | None = Query(default=None, gt=0, allow_inf_nan=False)):
    try:
        return (await engine.make_preview(quantity)).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/trading/open")
async def open_trade(request: OpenRequest):
    try:
        results = await engine.open_position(request)
        return {"results": [item.model_dump(mode="json") for item in results], "executions": [item.model_dump(mode="json") for item in engine.last_executions], "live": settings.can_trade_live and request.confirm_live, "orders_submitted": settings.can_send_orders and request.confirm_live, "environment": settings.environment}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc
    except httpx.HTTPError as exc:
        return await upstream_error(None, exc)
    except Exception as exc:
        logging.getLogger(__name__).exception("Open request failed")
        raise HTTPException(status_code=500, detail="Open request failed; check server logs and order state") from exc


@app.post("/api/trading/close")
async def close_trade(request: CloseRequest):
    try:
        results, executions = await engine.close_position(request)
        return {"results": [item.model_dump(mode="json") for item in results], "executions": [item.model_dump(mode="json") for item in executions], "live": settings.can_trade_live and request.confirm_live, "orders_submitted": settings.can_send_orders and request.confirm_live, "environment": settings.environment}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc
    except httpx.HTTPError as exc:
        return await upstream_error(None, exc)
    except Exception as exc:
        logging.getLogger(__name__).exception("Close request failed")
        raise HTTPException(status_code=500, detail="Close request failed; check server logs and order state") from exc


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
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
