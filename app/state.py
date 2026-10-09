from typing import Literal
from math import isfinite
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import ExecutionRecord, PerformanceSample, TradeTaskRequest


class JournalEntry(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow", allow_inf_nan=False)
    symbol: str = Field(min_length=1)
    side: Literal["Buy", "Sell"]
    qty: float = Field(gt=0)
    reduce_only: bool
    status: str
    terminal: bool
    filledQty: float = Field(ge=0)

    @model_validator(mode="after")
    def check_fill(self):
        if self.filledQty > self.qty + 1e-9:
            raise ValueError("Cumulative fill exceeds order quantity")
        return self


class EngineState(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)
    schema_version: Literal[1] = 1
    exchange_network: Literal["mainnet", "testnet"] | None = None
    last_open_week: str | None
    active_strategy_symbols: list[str]
    active_strategy_sizes: dict[str, float]
    active_strategy_group_id: str | None = None
    rfq_state: dict = Field(default_factory=dict)
    execution_groups: dict[str, dict] = Field(default_factory=dict)
    execution_group_links: dict[str, str] = Field(default_factory=dict)
    pm_baseline: dict = Field(default_factory=dict)
    order_journal: dict[str, JournalEntry] = Field(default_factory=dict)
    performance_executions: dict[str, ExecutionRecord] = Field(default_factory=dict)
    performance_start_ms: int | None = None
    performance_cursor_ms: int | None = None
    performance_samples: dict[str, list[PerformanceSample]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_sizes(self):
        for key, value in self.active_strategy_sizes.items():
            symbol, separator, side = key.rpartition("|")
            if not separator or not symbol or side not in {"Buy", "Sell"} or value < 0:
                raise ValueError("Invalid tracked position quantity or key")
            if symbol not in self.active_strategy_symbols:
                raise ValueError("Tracked quantity has no corresponding strategy symbol")
        for group in self.execution_groups.values():
            if not isinstance(group.get("type"), str) or group["type"] not in {"open", "close"}:
                raise ValueError("Invalid execution group type")
            if "legs" in group and (not isinstance(group["legs"], dict) or any(not isinstance(leg, dict) for leg in group["legs"].values())):
                raise ValueError("Invalid execution group legs")
            if "task_version" in group:
                if group["task_version"] != 1 or group.get("execution_status") not in {"accepted", "running", "stopping", "recovering", "recovery_needed", "completed", "partial", "stopped", "failed"}:
                    raise ValueError("Invalid execution task version or status")
                if (not isinstance(group.get("stop_requested"), bool) or not isinstance(group.get("live"), bool)
                        or not isinstance(group.get("request_fingerprint"), dict)
                        or any(not isinstance(group.get(key), str) or not group[key] for key in ("plan_id", "request_id", "accepted_at"))
                        or not group.get("legs")):
                    raise ValueError("Invalid execution task identity or flags")
                fingerprint = TradeTaskRequest.model_validate(group["request_fingerprint"])
                if (fingerprint.plan_id != group["plan_id"] or fingerprint.request_id != group["request_id"]
                        or group["live"] and not fingerprint.confirm_live
                        or group.get("environment") not in {"dry-run", "testnet", "live"}):
                    raise ValueError("Execution task request fingerprint does not match its identity")
                for key in ("accepted_at", "created_at", "started_at", "finished_at", "stop_requested_at"):
                    value = group.get(key)
                    if value is None and key not in {"accepted_at", "created_at"}:
                        continue
                    try:
                        timestamp = datetime.fromisoformat(value)
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Invalid execution task timestamp") from exc
                    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                        raise ValueError("Execution task timestamps must include a timezone")
                for symbol, leg in group["legs"].items():
                    if leg.get("symbol") != symbol or leg.get("side") not in {"Buy", "Sell"} or leg.get("option_type") not in {"Call", "Put"}:
                        raise ValueError("Invalid confirmed execution leg")
                    for key in ("qty", "strike", "reference_price"):
                        value = leg.get(key)
                        if not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value) or value <= 0:
                            raise ValueError("Invalid confirmed execution quantity or price")
                    try:
                        expiry = datetime.fromisoformat(leg.get("expiry"))
                    except (TypeError, ValueError) as exc:
                        raise ValueError("Invalid confirmed execution expiry") from exc
                    if expiry.tzinfo is None or expiry.utcoffset() is None:
                        raise ValueError("Confirmed execution expiry must include a timezone")
                limit = group.get("min_net_income_usd" if group["type"] == "open" else "max_net_cost_usd")
                if not isinstance(limit, (int, float)) or isinstance(limit, bool) or not isfinite(limit):
                    raise ValueError("Invalid confirmed net price constraint")
                requested_limit = fingerprint.min_net_income_usd if group["type"] == "open" else fingerprint.max_net_cost_usd
                if requested_limit is not None and requested_limit != limit:
                    raise ValueError("Execution task price constraint differs from its request")
        for group_id, group in self.execution_groups.items():
            if group.get("task_version") != 1:
                continue
            filled = {}
            for link, owner in self.execution_group_links.items():
                if owner != group_id or link not in self.order_journal:
                    continue
                entry = self.order_journal[link]
                leg = group["legs"].get(entry.symbol)
                if (not group["live"] or leg is None or entry.side != leg["side"] or entry.qty > leg["qty"] + 1e-9
                        or entry.reduce_only != (group["type"] == "close")):
                    raise ValueError("Execution journal does not match its confirmed task")
                filled[entry.symbol] = filled.get(entry.symbol, 0) + entry.filledQty
                if filled[entry.symbol] > leg["qty"] + 1e-9:
                    raise ValueError("Execution task fills exceed the confirmed quantity")
        for original in [*self.execution_groups.values(), self.rfq_state]:
            if original.get("strategy_mode", "iron_condor") not in {"iron_condor", "short_strangle"}:
                raise ValueError("Invalid stored strategy mode")
            if "risk_budget_usd" in original:
                budget = original["risk_budget_usd"]
                if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not isfinite(budget) or budget <= 0:
                    raise ValueError("Invalid stored strategy risk budget")
        if "legs" in self.rfq_state:
            legs = self.rfq_state["legs"]
            if not isinstance(legs, list):
                raise ValueError("Invalid RFQ legs")
            for leg in legs:
                if (not isinstance(leg, dict) or not isinstance(leg.get("symbol"), str) or not leg["symbol"]
                        or not isinstance(leg.get("side"), str) or leg["side"] not in {"Buy", "Sell"}):
                    raise ValueError("Invalid RFQ leg")
                try:
                    qty = float(leg.get("qty", 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("Invalid RFQ quantity") from exc
                if not isfinite(qty) or qty <= 0:
                    raise ValueError("Invalid RFQ quantity")
        return self
