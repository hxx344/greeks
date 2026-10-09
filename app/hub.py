"""Read-only projection of snapshots already held by the trading engine."""

from datetime import datetime, timezone
from math import isfinite


def _timestamp(value, now):
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    value = value.astimezone(timezone.utc)
    return value if value <= now else None


def _price(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value > 0 else None


def build_summary(engine, now=None):
    # No engine methods, exchange calls, performance reconstruction or writes.
    now = now or datetime.now(timezone.utc)
    settings = engine.settings
    market_at = _timestamp(engine.chain_updated_at, now)
    recovery_at = _timestamp(engine.reconciliation_last_success, now)
    quote_ttl = min(86400, max(1, int(settings.quote_stale_seconds)))
    recovery_ttl = min(86400, max(60, int(settings.reconciliation_seconds * 3)))
    pending = sum(not entry.get("terminal") for entry in engine.order_journal.values())
    rfq = engine.rfq_state
    rfq_pending = (rfq.get("status") in {"ExecutionUnknown", "PendingFill", "CreationUnknown", "CancelUnknown"}
                   or (rfq.get("status") == "Filled" and not rfq.get("tracking_applied", False))
                   or bool(rfq.get("selected_quote_id") and not rfq.get("execution_resolved", False)))
    tracked = len(engine.active_strategy_symbols)
    needs_recovery = bool(settings.can_send_orders or tracked or pending or rfq_pending)
    ttl = min(quote_ttl, recovery_ttl) if needs_recovery else quote_ttl
    updated_at = min(market_at, recovery_at) if needs_recovery and market_at and recovery_at else (None if needs_recovery else market_at)
    diagnostics = []

    def diagnostic(identifier, kind, message):
        diagnostics.append({"id": identifier, "kind": kind, "message": message})

    state = "online"
    message = "行情与本地策略状态正常"
    price = _price(engine.btc_price)
    if engine.chain_source != "bybit" or not engine.chain:
        state, message = "offline", "尚无可用的 Bybit 行情快照"
        diagnostic("market:unavailable", "fault", message)
        price = None
        updated_at = None
    elif market_at is None or (now - market_at).total_seconds() > quote_ttl:
        state, message = "stale", "行情时间缺失或已过期"
        diagnostic("market:stale", "fault", message)
        price = None
    else:
        if price is None:
            diagnostic("market:price", "fault", "BTC 价格暂不可用")
        future = [item for item in engine.chain if item.expiry > now]
        sunday = [item for item in future if item.expiry.weekday() == 6]
        if not future:
            diagnostic("market:contracts", "fault", "行情快照没有尚未到期的合约")
        elif not sunday:
            diagnostic("market:listing", "notice", "等待周日到期合约上线；备用盘口仅供查看")
        elif not all(any(item.option_type == kind and _price(item.mark_price) is not None
                         and isfinite(item.delta) and 0 < abs(item.delta) <= 1 for item in sunday)
                     for kind in ("Call", "Put")):
            diagnostic("market:quotes", "fault", "周日到期合约报价暂不完整")

    if engine.state_error:
        diagnostic("state:unavailable", "action", "策略状态无法可靠读取，交易已阻止；请检查原项目")
    if pending:
        diagnostic("orders:pending", "fault", "存在未决订单，等待后台核对")
    if rfq_pending:
        diagnostic("rfq:pending", "fault", "RFQ 执行状态尚未确认，等待后台核对")
    if needs_recovery:
        if not settings.can_send_orders:
            diagnostic("tracking:paused", "action", "已保存交易跟踪状态，但当前未启用交易对账；请检查原项目配置")
        elif engine.reconciliation_error:
            diagnostic("tracking:error", "fault", "后台对账未完成，请在原项目检查订单与持仓")
        elif recovery_at is None:
            diagnostic("tracking:unknown", "fault", "尚无成功对账记录，跟踪状态未确认")
        elif (now - recovery_at).total_seconds() > recovery_ttl:
            diagnostic("tracking:stale", "fault", "成功对账记录已过期，跟踪状态待确认")
    if state == "online" and any(item["kind"] != "notice" for item in diagnostics):
        state, message = "partial", "部分运行状态待确认，请查看诊断"
    elif state == "online" and diagnostics:
        message = "行情正常，等待周日到期合约上线"
    if state == "online" and updated_at and (now - updated_at).total_seconds() > ttl:
        state, message = "stale", "来源快照已过期"
    tracking = "本地模拟" if not needs_recovery else "对账正常"
    if needs_recovery and (not recovery_at or engine.reconciliation_error or pending or rfq_pending
                           or not settings.can_send_orders or (now - recovery_at).total_seconds() > recovery_ttl):
        tracking = "待核对" if settings.can_send_orders else "已暂停"
    if engine.state_error:
        tracking = "状态不可用"

    return {"schemaVersion": 2, "data": {
        "updatedAt": updated_at.isoformat() if updated_at else None,
        "health": {"state": state, "message": message, "staleAfterSeconds": ttl},
        "metrics": [
            {"key": "trading_mode", "label": "交易模式", "value": settings.environment,
             "detail": "实盘启用" if settings.can_trade_live else "测试网交易启用" if settings.can_send_orders else "不发送交易所订单"},
            {"key": "btc_price", "label": "BTC 价格", "value": price, "unit": "USDT",
             "detail": "Bybit 测试网标的参考价" if settings.environment == "testnet" else "Bybit 主网标的参考价"},
            {"key": "tracking", "label": "策略跟踪", "value": tracking},
            {"key": "tracked_legs", "label": "跟踪合约", "value": None if engine.state_error else tracked, "unit": "个",
             "detail": "引擎保存的策略合约数量，不代表账户持仓或资产估值"},
            {"key": "pending_orders", "label": "未决订单", "value": None if engine.state_error else pending, "unit": "笔"},
            {"key": "pending_rfq", "label": "未决询价", "value": None if engine.state_error else int(rfq_pending), "unit": "个"},
        ],
        "diagnostics": diagnostics,
    }}
