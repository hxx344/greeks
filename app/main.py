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
from .execution_activity import ACTIVE_EXECUTIONS, execution_dashboard
from .trade_tasks import TradeConflict
from .cache import SnapshotCache
from .lease import StateLease
from .hub import build_summary
from .security import authorize_dashboard
from .strategy import StrategyUnavailable, SundayExpiryUnavailable
from .models import CloseRequest, OpenRequest, Position, RfqCancelRequest, RfqCreateRequest, RfqExecuteRequest, StrategyMode, TradePlanRequest, TradeTaskRequest

# The dashboard polls several endpoints frequently; HTTP 200 access lines are
# noise in production logs. Application warnings and errors remain visible.
logging.getLogger("uvicorn.access").disabled = True

settings = get_settings()
engine = TradingEngine(settings)
account_cache = SnapshotCache(settings.account_cache_seconds)


@asynccontextmanager
async def lifespan(_: FastAPI):
    with StateLease(settings.state_file):
        tasks = []
        try:
            engine._load_state()
            engine._initialize_trade_tasks(persist=True)
            account_cache.invalidate()
            await engine.refresh_chain(force=True)
            tasks = [asyncio.create_task(engine.market_loop()), asyncio.create_task(engine.scheduler()), asyncio.create_task(engine.reconciliation_loop()), asyncio.create_task(engine.performance_loop())]
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await engine.shutdown_trade_tasks()
            finally:
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


@app.get("/api/hub/summary")
async def hub_summary():
    return build_summary(engine)


@app.get("/api/config")
async def config():
    return config_payload()


def config_payload():
    return {"trading_blocked_reason": engine.state_error, "environment": settings.environment, "live_enabled": settings.can_trade_live, "trading_enabled": settings.can_send_orders, "testnet": settings.private_testnet, "market_testnet": settings.environment == "testnet", "opening_blocked_reason": "Unresolved RFQ; awaiting reconciliation" if engine._rfq_unresolved() else None, "auto_open": settings.auto_open, "max_risk_usd": settings.max_risk_usd, "max_margin_usd": settings.max_margin_usd, "strategy_mode": settings.strategy_mode, "leg_qty": settings.leg_qty, "target_dte_days": settings.target_dte_days, "expiry_rule": "Friday entry / Sunday UTC expiry", "market_refresh_seconds": settings.market_refresh_seconds, "instrument_refresh_seconds": settings.instrument_refresh_seconds, "quote_stale_seconds": settings.quote_stale_seconds, "max_spread_bps": settings.max_spread_bps, "bbo_poll_seconds": settings.bbo_poll_seconds, "bbo_order_timeout_seconds": settings.bbo_order_timeout_seconds, "allow_market_fallback": settings.allow_market_fallback, "failed_leg_retry_delay_seconds": settings.failed_leg_retry_delay_seconds, "failed_leg_position_checks": settings.failed_leg_position_checks, "failed_leg_position_check_interval_seconds": settings.failed_leg_position_check_interval_seconds, "estimated_taker_fee_rate": settings.estimated_taker_fee_rate, "portfolio_margin_buffer_pct": settings.portfolio_margin_buffer_pct, "margin_mode": settings.margin_mode, "option_mm_factor": settings.option_mm_factor, "option_max_im_factor": settings.option_max_im_factor, "option_min_im_factor": settings.option_min_im_factor, "option_liquidation_fee_rate": settings.option_liquidation_fee_rate, "option_fee_cap_pct": settings.option_fee_cap_pct, "open_time": f"Friday {settings.open_hour_utc:02d}:{settings.open_minute_utc:02d} UTC"}


@app.get("/api/dashboard/market")
async def dashboard_market(quantity: float | None = Query(default=None, gt=0, allow_inf_nan=False), strategy_mode: StrategyMode | None = None):
    try:
        strategy = await engine.make_preview(quantity, strategy_mode=strategy_mode)
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
    except StrategyUnavailable as exc:
        now = datetime.now(timezone.utc)
        age = (now - engine.chain_updated_at).total_seconds() if engine.chain_updated_at else None
        if engine.chain_source != "bybit" or age is None or not 0 <= age <= settings.quote_stale_seconds:
            raise HTTPException(status_code=503, detail="行情暂不可用或已过期，请等待行情恢复后重新核对策略") from exc
        messages = {
            "short_strike_order": "当前候选卖出 Put 的行权价不低于卖出 Call，暂不满足策略结构要求；盘口可继续查看，系统将自动重新检查。",
            "missing_protective_wings": "当前到期合约缺少符合要求的保护腿，暂时无法生成四腿策略；盘口可继续查看，系统将自动重新检查。",
            "missing_option_side": "当前到期合约缺少可用的 Call 或 Put 报价，暂时无法生成策略；盘口可继续查看，系统将自动重新检查。",
        }
        # This is a read-only display result. Opening, RFQ and confirmed task
        # admission still receive the original strategy validation exception.
        expiry_items = [item for item in engine.chain if item.expiry == exc.expiry]
        return {"status": "strategy_unavailable", "read_only": True, "reason_code": exc.reason_code,
                "message": messages[exc.reason_code], "config": config_payload(), "preview": None,
                "chain": {"source": engine.chain_source, "btc_price": engine.btc_price, "updated_at": engine.chain_updated_at,
                          "expiry": exc.expiry, "items": [item.model_dump(mode="json") for item in expiry_items]}}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/dashboard/account")
async def dashboard_account():
    return await account_cache.get(_build_account_dashboard)


@app.get("/api/dashboard/orders")
async def dashboard_orders():
    snapshot = engine.order_snapshot()
    groups = _execution_cards()
    active = [group for group in groups if group["execution_status"] in ACTIVE_EXECUTIONS]
    return {**snapshot, "groups": groups,
            "execution_active": bool(active or snapshot["active_count"] or engine.trading_operation_active()),
            "active_execution_id": active[0]["execution_id"] if active else None}


def _execution_cards(*, history_limit=30):
    records = [*engine.performance_executions.values(), *engine.last_executions,
               *(record for rows in engine.execution_details.values() for record in rows)]
    return execution_dashboard(engine.execution_groups, engine.execution_group_links, engine.order_journal,
                               engine.order_activity, records, history_limit=history_limit,
                               stale_seconds=max(15, engine.settings.reconciliation_seconds * 2,
                                                 engine.settings.bbo_poll_seconds * 3))


performance_cache = SnapshotCache(15)


@app.get("/api/dashboard/performance")
async def dashboard_performance():
    async def build():
        await engine.sync_performance()
        await engine.sample_performance()
        account = await account_cache.get(_build_account_dashboard)
        return engine.performance_report(positions_available=account["positions"].get("available", False) and not engine.lock.locked(),
                                         positions=[Position.model_validate(item) for item in account["positions"]["items"]])
    return await performance_cache.get(build)


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
async def preview(quantity: float | None = Query(default=None, gt=0, allow_inf_nan=False), strategy_mode: StrategyMode | None = None):
    try:
        return (await engine.make_preview(quantity, strategy_mode=strategy_mode)).model_dump(mode="json")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/trading/open")
async def open_trade(request: OpenRequest):
    raise HTTPException(status_code=409, detail="请先通过 /api/trading/plans 生成具体开仓方案，再通过 /api/trading/tasks 确认执行")


@app.post("/api/trading/close")
async def close_trade(request: CloseRequest):
    raise HTTPException(status_code=409, detail="请先通过 /api/trading/plans 生成具体平仓方案，再通过 /api/trading/tasks 确认执行")


@app.post("/api/trading/plans")
async def prepare_trade_plan(request: TradePlanRequest):
    try:
        return await engine.prepare_trade_plan(request)
    except TradeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc


@app.post("/api/trading/tasks", status_code=202)
async def start_trade_task(request: TradeTaskRequest):
    try:
        return await engine.start_trade_task(request)
    except TradeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc
    except httpx.HTTPError:
        raise
    except Exception as exc:
        logging.getLogger(__name__).exception("Trade task admission failed")
        raise HTTPException(status_code=500, detail="任务提交结果尚未确认，请按请求编号查询执行记录") from exc


@app.get("/api/trading/tasks")
async def trade_tasks(
    request_id: str | None = Query(default=None, min_length=1, max_length=128),
    plan_id: str | None = Query(default=None, min_length=1, max_length=128),
):
    # Keep this snapshot synchronous: task admission rechecks the plan's age
    # after its last await, then persists the accepted task without yielding.
    now = datetime.now(timezone.utc)
    groups = _execution_cards(history_limit=None if request_id is not None else 30)
    items = [group for group in groups if request_id is None or group["request_id"] == request_id]
    admitting = engine._trade_admitting
    saved = engine.trade_plans.get(plan_id) if plan_id else None
    unavailable = saved is None or now >= datetime.fromisoformat(saved["plan"]["expires_at"])
    # A missing plan cannot be accepted after a restart. A state read failure
    # cannot prove the durable task inventory is complete, so remain uncertain.
    closed = bool(request_id and plan_id and not items and not admitting and not engine.state_error and unavailable)
    return {"items": items, "request_id": request_id, "plan_id": plan_id,
            "server_time": now.isoformat(), "admission_active": admitting, "admission_closed": closed}


def _trade_task(execution_id: str):
    group = next((item for item in _execution_cards(history_limit=None) if item["execution_id"] == execution_id), None)
    if group is None:
        raise HTTPException(status_code=404, detail="执行任务不存在，请刷新执行记录")
    return group


@app.get("/api/trading/tasks/{execution_id}")
async def trade_task(execution_id: str):
    return _trade_task(execution_id)


@app.post("/api/trading/tasks/{execution_id}/stop")
async def stop_trade_task(execution_id: str):
    if execution_id not in engine.execution_groups:
        raise HTTPException(status_code=404, detail="执行任务不存在，请刷新执行记录")
    try:
        engine.stop_trade_task(execution_id)
        return _trade_task(execution_id)
    except TradeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    except TradeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc


@app.post("/api/rfq/execute")
async def rfq_execute(request: RfqExecuteRequest):
    try:
        return await engine.execute_rfq(request)
    except TradeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except BybitError as exc:
        raise HTTPException(status_code=502, detail=f"Bybit error: {exc}") from exc


@app.post("/api/rfq/cancel")
async def rfq_cancel(request: RfqCancelRequest):
    try:
        return await engine.cancel_rfq(request)
    except TradeConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
