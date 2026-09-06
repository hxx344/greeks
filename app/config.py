from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bybit_api_key: str = ""
    bybit_api_secret: str = ""
    bybit_testnet: bool = True
    live_trading: bool = False
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

    @property
    def environment(self) -> str:
        return "live" if self.live_trading and not self.bybit_testnet else "testnet"

    @property
    def can_trade_live(self) -> bool:
        return bool(self.live_trading and self.bybit_api_key and self.bybit_api_secret and not self.bybit_testnet)


@lru_cache
def get_settings() -> Settings:
    return Settings()
