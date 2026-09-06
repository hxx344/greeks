from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    dashboard_username: str = Field(default="admin", min_length=1)
    dashboard_password: SecretStr = SecretStr("")
    account_cache_seconds: float = Field(default=2.0, ge=0, le=10)
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = True
    live_trading: bool = False
    trading_mode: Literal["dry-run", "testnet", "live"] | None = None
    reconciliation_seconds: float = Field(default=15.0, ge=1, le=300)
    performance_sample_seconds: int = Field(default=60, ge=15, le=3600)
    live_confirmation: str = ""
    max_risk_usd: float = Field(default=2500.0, gt=0)
    leg_qty: float = Field(default=1.0, gt=0)
    open_day: int = Field(default=4, ge=4, le=4)
    open_hour_utc: int = Field(default=21, ge=0, le=23)
    open_minute_utc: int = Field(default=0, ge=0, le=59)
    open_window_seconds: int = Field(default=60, ge=1, le=300)
    target_dte_days: int = 2
    recv_window_ms: int = Field(default=5000, gt=0)
    auto_open: bool = False
    market_refresh_seconds: int = Field(default=10, ge=1)
    instrument_refresh_seconds: int = Field(default=3600, ge=60)
    quote_stale_seconds: int = Field(default=30, ge=5)
    max_spread_bps: float = Field(default=0.0, ge=0)
    failed_leg_retry_delay_seconds: float = Field(default=0.8, ge=0, le=5)
    failed_leg_position_checks: int = Field(default=5, ge=1, le=10)
    failed_leg_position_check_interval_seconds: float = Field(default=1.0, ge=0.1, le=5)
    estimated_taker_fee_rate: float = Field(default=0.0003, ge=0, le=0.1)
    portfolio_margin_buffer_pct: float = Field(default=0.10, ge=0, le=2)
    margin_mode: str = Field(default="REGULAR_MARGIN", pattern="^(REGULAR_MARGIN|PORTFOLIO_MARGIN)$")
    option_mm_factor: float = Field(default=0.03, ge=0, le=1)
    option_max_im_factor: float = Field(default=0.10, ge=0, le=1)
    option_min_im_factor: float = Field(default=0.05, ge=0, le=1)
    option_liquidation_fee_rate: float = Field(default=0.002, ge=0, le=1)
    option_fee_cap_pct: float = Field(default=0.07, ge=0, le=1)
    state_file: str = "data/engine_state.json"
    bbo_poll_seconds: float = Field(default=1.0, ge=0.2, le=10)
    bbo_order_timeout_seconds: int = Field(default=600, ge=60, le=1800)
    allow_market_fallback: bool = False
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore", allow_inf_nan=False)

    @field_validator("dashboard_password")
    @classmethod
    def validate_dashboard_password(cls, value: SecretStr) -> SecretStr:
        if value.get_secret_value() and len(value.get_secret_value()) < 12:
            raise ValueError("Dashboard password must contain at least 12 characters")
        return value

    @property
    def environment(self) -> str:
        if self.trading_mode is not None:
            return self.trading_mode
        return "live" if self.live_trading and not self.bybit_testnet else "dry-run"

    @property
    def private_testnet(self) -> bool:
        if self.trading_mode in {"testnet", "live"}:
            return self.trading_mode == "testnet"
        return self.bybit_testnet

    @property
    def can_send_orders(self) -> bool:
        enabled = self.environment == "testnet" or (self.environment == "live" and self.live_trading)
        return bool(enabled and self.bybit_api_key and self.bybit_api_secret)

    @property
    def can_trade_live(self) -> bool:
        return self.environment == "live" and self.can_send_orders


@lru_cache
def get_settings() -> Settings:
    return Settings()
