from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class OptionInstrument(BaseModel):
    symbol: str
    expiry: datetime
    strike: float
    option_type: Literal["Call", "Put"]
    delta: float
    mark_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    iv: float = 0.0
    volume: float = 0.0
    open_interest: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    min_qty: float = 0.01
    qty_step: float = 0.01
    max_qty: float = 500.0
    price_tick: float = 0.01


class StrategyLeg(BaseModel):
    symbol: str
    side: Literal["Buy", "Sell"]
    option_type: Literal["Call", "Put"]
    strike: float
    delta: float
    qty: float
    mark_price: float
    target_delta: float
    estimated_fee_usd: float = 0.0
    fee_cap_usd: float = 0.0
    fee_basis_price: float = 0.0


class StrategyPreview(BaseModel):
    expiry: datetime
    legs: list[StrategyLeg]
    net_credit_usd: float
    max_loss_usd: float
    max_profit_usd: float
    risk_reward: float
    generated_at: datetime
    source: Literal["bybit", "demo"]
    market_timestamp: datetime | None = None
    btc_price: float | None = None
    estimated_margin_usd: float = 0.0
    estimated_trading_cost_usd: float = 0.0
    estimated_fee_rate: float = 0.0
    margin_buffer_pct: float = 0.0
    estimated_initial_margin_usd: float = 0.0
    estimated_maintenance_margin_usd: float = 0.0
    margin_mode: str = "REGULAR_MARGIN"
    margin_formula_status: str = ""
    fee_cap_pct: float = 0.07


class OpenRequest(BaseModel):
    confirm_live: bool = False
    quantity: float | None = Field(default=None, gt=0)


class CloseRequest(BaseModel):
    confirm_live: bool = False


class OrderResult(BaseModel):
    symbol: str
    side: str
    qty: float
    status: str
    order_id: str | None = None
    message: str | None = None
    order_link_id: str | None = None
    exec_fee: float | None = None
    fee_currency: str | None = None
    exec_price: float | None = None
    exec_qty: float | None = None
    execution_id: str | None = None
    exec_time: datetime | None = None


class ExecutionRecord(BaseModel):
    symbol: str
    side: str
    order_id: str
    order_link_id: str
    exec_id: str
    exec_fee: float
    fee_currency: str
    exec_price: float
    exec_qty: float
    fee_rate: float | None = None
    exec_time: datetime
    reduce_only: bool = False


class Position(BaseModel):
    symbol: str
    side: str
    size: float
    avg_price: float
    mark_price: float
    unrealised_pnl: float
    source: str = "demo"


class LogEntry(BaseModel):
    timestamp: datetime
    level: Literal["INFO", "WARNING", "ERROR"]
    message: str


class AccountHealth(BaseModel):
    available_balance_usd: float | None = None
    margin_balance_usd: float | None = None
    total_equity_usd: float | None = None
    wallet_balance_usd: float | None = None
    initial_margin_usd: float | None = None
    maintenance_margin_usd: float | None = None
    initial_margin_rate: float | None = None
    maintenance_margin_rate: float | None = None
    margin_mode: str | None = None
    updated_at: datetime | None = None
    available: bool = False
    message: str | None = None
