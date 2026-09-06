from typing import Literal
from math import isfinite

from pydantic import BaseModel, ConfigDict, Field, model_validator
from .models import ExecutionRecord, PerformanceSample


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
