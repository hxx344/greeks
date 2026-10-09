"""Read-only execution summaries; order intents never count as exchange fills."""
from collections import defaultdict
from datetime import datetime, timezone
from math import isclose, isfinite

from .order_activity import _choice, _text, _timestamp, order_dashboard
from .performance import settlement_currency


ACTIVE_EXECUTIONS = {"accepted", "running", "stopping", "recovering", "recovery_needed"}
EXECUTION_STATUSES = ACTIVE_EXECUTIONS | {"completed", "partial", "stopped", "failed"}


def _status_message(group, status):
    if status in {"recovering", "recovery_needed"}:
        return "等待交易所确认已有委托；恢复期间不会补发剩余订单。"
    if status == "stopping":
        return "已请求停止，正在撤销并核对未成交委托；已成交部分保留。"
    if group.get("net_price_blocked"):
        return "价格已超出确认的净价限制，后续挂单与补单已停止。"
    if group.get("risk_blocked"):
        return "执行价格超过组合风险预算，后续挂单与补单已停止。"
    if group.get("stop_requested") and status == "partial":
        return "停止核对已结束，组合部分成交；已成交部分保留。"
    return {"accepted": "任务已接纳，等待开始执行。", "running": "按已确认的合约、数量和净价限制执行。",
            "partial": "组合部分成交，剩余委托已结束，请核对当前持仓。",
            "stopped": "任务已停止，未产生新成交。", "failed": "任务未完成，请查看系统日志和逐腿结果。",
            "completed": "模拟组合已完成。" if group.get("live") is False else "组合各腿已确认完成。"}.get(status, "")


def _finite(value, *, positive=False):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value):
        return value if not positive or value > 0 else None
    return None


def _records_by_group(records, groups, links):
    buckets, seen, conflicts = defaultdict(list), {}, set()
    fields = ("symbol", "side", "order_link_id", "exec_qty", "exec_price", "exec_fee", "fee_currency")
    for original in records:
        row = original.model_dump() if hasattr(original, "model_dump") else original
        if not isinstance(row, dict) or not _text(row.get("exec_id")):
            continue
        link = row.get("order_link_id")
        group_id = links.get(link)
        # Older RFQ fills have no single-order journal, but do have persisted attribution.
        if group_id is None and not groups.get(row.get("execution_group"), {}).get("order_tracking"):
            group_id = row.get("execution_group")
        if group_id not in groups or row.get("exec_type", "Trade") != "Trade":
            continue
        identity = tuple(row.get(key) for key in fields)
        previous = seen.get(row["exec_id"])
        if previous is not None:
            if previous != (group_id, identity):
                conflicts.update((group_id, previous[0]))
            continue
        seen[row["exec_id"]] = (group_id, identity)
        buckets[group_id].append(row)
    return buckets, conflicts


def _amounts(legs, records, *, conflict=False, simulated=False):
    currencies = {settlement_currency(leg["symbol"]) for leg in legs}
    currency = next(iter(currencies)) if len(currencies) == 1 else None
    quantities, gross, fees = defaultdict(float), 0.0, 0.0
    targets = {(leg["symbol"], leg["side"]): leg for leg in legs}
    valid = bool(targets) and currency is not None and not conflict and not simulated
    order_links = {order["order_link_id"]: (leg["symbol"], leg["side"])
                   for leg in legs for order in leg["orders"]}
    for row in records:
        key = (row.get("symbol"), row.get("side"))
        qty, price, fee = (_finite(row.get("exec_qty"), positive=True),
                           _finite(row.get("exec_price"), positive=True), _finite(row.get("exec_fee")))
        if (key not in targets or qty is None or price is None or fee is None
                or row.get("fee_currency") != currency
                or (order_links and order_links.get(row.get("order_link_id")) != key)):
            valid = False
            continue
        quantities[key] += qty
        gross += (1 if key[1] == "Sell" else -1) * price * qty
        fees += fee
    for key, leg in targets.items():
        if not isclose(quantities[key], leg["filled_qty"], rel_tol=0, abs_tol=1e-8):
            valid = False
    if not all(isfinite(value) for value in (gross, fees, gross - fees)):
        valid = False
    return {"amounts_complete": valid, "currency": currency,
            "gross_amount": round(gross, 8) if valid else None,
            "fee_amount": round(fees, 8) if valid else None,
            "net_amount": round(gross - fees, 8) if valid else None}


def execution_dashboard(groups, group_links, journal, activity, execution_records=(), *, now=None,
                        stale_seconds=30, history_limit=30):
    """Build all active groups and recent history without locks, IO or state changes."""
    now = now or datetime.now(timezone.utc)
    order_items = order_dashboard(journal, activity, groups=groups, group_links=group_links,
                                  stale_seconds=stale_seconds, now=now, terminal_limit=None)["items"]
    orders_by_group = defaultdict(list)
    for order in order_items:
        group_id = group_links.get(order["order_link_id"])
        if group_id in groups:
            orders_by_group[group_id].append(order)
    records_by_group, conflicts = _records_by_group(execution_records, groups, group_links)
    active, history = [], []
    for group_id, group in groups.items():
        if group.get("type") not in {"open", "close"}:
            continue
        orders = orders_by_group[group_id]
        targets = {}
        for symbol, leg in (group.get("legs") or {}).items():
            qty = _finite(leg.get("qty"), positive=True)
            if _text(symbol) and leg.get("side") in {"Buy", "Sell"} and qty is not None:
                targets[symbol, leg["side"]] = qty
        # Legacy close groups have no targets. An IOC quantity is remaining size,
        # never another target to add to its original order.
        if not targets:
            for order in orders:
                if order["qty"] is not None and order["qty"] > 0 and order["execution_type"] != "IOC":
                    key = (order["symbol"], order["side"])
                    targets[key] = max(targets.get(key, 0), order["qty"])
        legs, inconsistent = [], False
        simulated = group.get("live") is False
        simulation = group.get("simulated_fills") or {}
        for (symbol, side), target in targets.items():
            leg_orders = [order for order in orders if order["symbol"] == symbol and order["side"] == side]
            filled = sum(order["filled_qty"] or 0 for order in leg_orders)
            if simulated:
                filled = _finite(simulation.get(symbol, 0)) or 0
            elif not group.get("order_tracking") and not leg_orders:
                filled = sum(_finite(row.get("exec_qty"), positive=True) or 0 for row in records_by_group[group_id]
                             if (row.get("symbol"), row.get("side")) == (symbol, side))
            overflow = filled > target + 1e-8
            unresolved = any(not item["terminal"] and item["phase"] == "unknown" for item in leg_orders)
            inconsistent |= overflow
            complete = (isclose(filled, target, rel_tol=0, abs_tol=1e-8)
                        and all(item["terminal"] for item in leg_orders) and not overflow)
            legs.append({"symbol": symbol, "side": side, "target_qty": target,
                         "filled_qty": round(filled, 10), "remaining_qty": round(max(0, target - filled), 10),
                         "progress_ratio": min(1.0, max(0.0, filled / target)), "complete": complete,
                         "unknown": unresolved or overflow, "orders": leg_orders})
        if any((item["symbol"], item["side"]) not in targets for item in orders):
            inconsistent = True
        if not legs and not group.get("task_version"):
            continue
        total = sum(leg["target_qty"] for leg in legs)
        filled = sum(leg["filled_qty"] for leg in legs)
        pending_orders = any(not item["terminal"] for item in orders)
        has_unknown = inconsistent or any(leg["unknown"] for leg in legs)
        complete_count = sum(leg["complete"] for leg in legs)
        status = _choice(group.get("execution_status"), EXECUTION_STATUSES)
        if status is None:
            status = ("recovery_needed" if has_unknown else "running" if pending_orders else
                      "completed" if legs and complete_count == len(legs) else "partial" if filled > 0 else "stopped")
        if inconsistent or (pending_orders and status not in ACTIVE_EXECUTIONS):
            status, has_unknown = "recovery_needed", True
        has_unknown |= status in {"recovering", "recovery_needed"}
        created = _timestamp(group.get("created_at"))
        accepted = _timestamp(group.get("accepted_at"))
        started = _timestamp(group.get("started_at"))
        finished = _timestamp(group.get("finished_at"))
        start = started or accepted or created
        elapsed = None
        if start:
            end = datetime.fromisoformat(finished) if finished else now if status in ACTIVE_EXECUTIONS else None
            elapsed = max(0, (end - datetime.fromisoformat(start)).total_seconds()) if end else None
        card = {"execution_id": group_id, "request_id": _text(group.get("request_id")),
                "plan_id": _text(group.get("plan_id")), "operation": group["type"],
                "strategy_mode": _choice(group.get("strategy_mode", "iron_condor"), {"iron_condor", "short_strangle"}),
                "execution_status": status, "created_at": created, "accepted_at": accepted, "started_at": started,
                "status_message": _status_message(group, status),
                "finished_at": finished, "stop_requested_at": _timestamp(group.get("stop_requested_at")),
                "can_stop": group.get("task_version") == 1 and status in ACTIVE_EXECUTIONS,
                "elapsed_seconds": elapsed, "completed_legs": complete_count, "total_legs": len(legs),
                "progress_ratio": min(1.0, max(0.0, filled / total)) if total else 0,
                "has_unknown": has_unknown, "simulated": simulated, "legs": legs,
                **_amounts(legs, records_by_group[group_id], conflict=group_id in conflicts, simulated=simulated)}
        (active if status in ACTIVE_EXECUTIONS else history).append(card)
    newest = lambda card: (card["accepted_at"] or card["created_at"] or "", card["execution_id"])
    active.sort(key=newest, reverse=True)
    history.sort(key=newest, reverse=True)
    return active + (history if history_limit is None else history[:history_limit])
