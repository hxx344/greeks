"""In-memory order observations and a read-only, whitelisted dashboard view."""
from datetime import datetime, timezone
from math import isfinite


PHASES = {"submitting", "amending", "cancelling", "reconciling", "working", "terminal", "unknown"}
ACTIVE_PHASES = PHASES - {"terminal", "unknown"}
ORDER_STATUSES = {"unknown", "filled", "partial", "timeout_cancelled", "not_submitted"}
EXCHANGE_STATUSES = {"New", "PartiallyFilled", "Untriggered", "Triggered", "Filled", "Cancelled",
                     "Rejected", "PartiallyFilledCanceled", "Deactivated"}


def _number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value) and value >= 0:
        return value
    return None


def _text(value):
    return value if isinstance(value, str) and value else None


def _choice(value, choices):
    return value if isinstance(value, str) and value in choices else None


def _timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def observe_order(activity: dict, link: str, event: dict) -> None:
    """Never persist heartbeats or import arbitrary exchange/request fields."""
    phase = _choice(event.get("phase"), PHASES)
    if phase is None:
        return
    now = datetime.now(timezone.utc).isoformat()
    current = dict(activity.get(link, {}))
    current.update(phase=phase, updated_at=now)
    order_id = _text(event.get("order_id"))
    if order_id is not None:
        current["order_id"] = order_id
    if "requested_price" in event:
        current["requested_price"] = _number(event["requested_price"])
    if event.get("confirmed") is True and _choice(event.get("exchange_status"), EXCHANGE_STATUSES):
        current.update(exchange_status=event["exchange_status"],
                       confirmed_price=_number(event.get("confirmed_price")), last_confirmed_at=now)
    activity[link] = current


def order_dashboard(journal: dict, activity: dict, *, stale_seconds: float, groups: dict | None = None,
                    group_links: dict | None = None, now: datetime | None = None, terminal_limit: int | None = 30) -> dict:
    """No awaits, exchange access, state writes, or engine transaction lock."""
    now = now or datetime.now(timezone.utc)
    active, completed = [], []
    for link, entry in journal.items():
        observation = activity.get(link, {})
        terminal = entry.get("terminal") is True
        phase = _choice(observation.get("phase"), ACTIVE_PHASES) or "unknown"
        # Saved actions are never resumed as live UI activity after a restart.
        phase = "terminal" if terminal else phase
        qty, filled = _number(entry.get("qty")), _number(entry.get("filledQty"))
        last_confirmed = _timestamp(observation.get("last_confirmed_at"))
        age = (now - datetime.fromisoformat(last_confirmed)).total_seconds() if last_confirmed else None
        status = entry.get("status")
        operation = _choice((groups or {}).get((group_links or {}).get(link), {}).get("type"), {"open", "close"})
        if operation is None:
            operation = "close" if entry.get("reduce_only") is True else "open" if entry.get("reduce_only") is False else None
        item = {
            "order_link_id": link,
            "order_id": _text(observation.get("order_id")) or _text(entry.get("orderId")),
            "symbol": _text(entry.get("symbol")),
            "side": _choice(entry.get("side"), {"Buy", "Sell"}),
            "qty": qty,
            "filled_qty": filled,
            "remaining_qty": round(max(0.0, qty - filled), 10) if qty is not None and filled is not None else None,
            "terminal": terminal,
            "status": _choice(status, ORDER_STATUSES) or "unknown",
            "phase": phase,
            "exchange_status": _choice(observation.get("exchange_status"), EXCHANGE_STATUSES),
            "execution_type": _choice(entry.get("execution_type"), {"BBO", "IOC"}),
            "operation": operation,
            "requested_price": _number(observation.get("requested_price")),
            "confirmed_price": _number(observation.get("confirmed_price")),
            "created_at": _timestamp(entry.get("created_at")),
            "updated_at": _timestamp(observation.get("updated_at")) or _timestamp(entry.get("updated_at")),
            "last_confirmed_at": last_confirmed,
            "stale": not terminal and (age is None or age < 0 or age > stale_seconds),
        }
        (completed if terminal else active).append(item)
    newest = lambda item: (item["updated_at"] or item["created_at"] or "", item["order_link_id"])
    active.sort(key=newest, reverse=True)
    completed.sort(key=newest, reverse=True)
    if terminal_limit is not None:
        completed = completed[:terminal_limit]
    return {"generated_at": now.isoformat(), "execution_active": any(item["phase"] in ACTIVE_PHASES for item in active),
            "active_count": len(active), "terminal_count": len(completed), "items": active + completed}
