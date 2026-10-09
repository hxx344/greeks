import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from math import isclose, isfinite
from pathlib import Path
from uuid import uuid4

from .bybit import BybitClient
from .config import Settings
from .models import AccountHealth, CloseRequest, ExecutionRecord, LogEntry, OpenRequest, OrderResult, PerformanceSample, Position, StrategyMode, StrategyPreview, TradePlanRequest, TradeTaskRequest
from .strategy import build_strategy
from .orders import OrderExecutor
from .order_activity import observe_order, order_dashboard
from .state import EngineState
from .risk import leg_count, maximum_loss, reserve_short_margin, strategy_mode as validate_mode, validate_structure
from .rfq import RfqMixin
from .reconciliation import ReconciliationMixin
from .performance import PerformanceMixin
from .trade_tasks import TradeTaskMixin, TradeConflict


class TradingEngine(TradeTaskMixin, RfqMixin, ReconciliationMixin, PerformanceMixin):
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = BybitClient(settings.bybit_api_key, settings.bybit_api_secret, settings.private_testnet, settings.recv_window_ms, market_testnet=settings.environment == "testnet")
        self.chain = []
        self.chain_source = "demo"
        self.chain_updated_at: datetime | None = None
        self.raw_instruments: list[dict] = []
        self.instruments_updated_at: datetime | None = None
        self.btc_price: float | None = None
        self.refresh_lock = asyncio.Lock()
        self.preview: StrategyPreview | None = None
        self.positions: list[Position] = []
        self.logs: list[LogEntry] = []
        self.last_open_week: str | None = None
        self.active_strategy_symbols: set[str] = set()
        self.active_strategy_sizes: dict[str, float] = {}
        self.active_strategy_group_id: str | None = None
        self.rfq_state: dict = {}
        self.execution_groups: dict[str, dict] = {}
        self.execution_group_links: dict[str, str] = {}
        self.pm_baseline: dict = {}
        self.order_journal: dict[str, dict] = {}
        self.order_activity: dict[str, dict] = {}
        self.state_error: str | None = None
        self.performance_executions: dict[str, ExecutionRecord] = {}
        self.performance_start_ms = None
        self.performance_cursor_ms = None
        self.performance_error = None
        self.performance_updated_at = None
        self.performance_lock = asyncio.Lock()
        self.performance_samples: dict[str, list[PerformanceSample]] = {}
        self.performance_sampled_at = None
        self.performance_sample_error = None
        self.reconciliation_last_success = None
        self.reconciliation_error = None
        self.reconciliation_started_at = datetime.now(timezone.utc)
        self._load_state()
        self.account_health = AccountHealth(available=False, message="Live account credentials are not configured")
        self.last_executions: list[ExecutionRecord] = []
        self.execution_details: dict[str, list[ExecutionRecord]] = {}
        self.lock = asyncio.Lock()
        self._initialize_trade_tasks()
        self.log("INFO", f"Engine started in {settings.environment} mode")

    def _load_state(self) -> None:
        self.order_activity.clear()
        try:
            def reject_constant(value):
                raise ValueError(f"Invalid JSON number: {value}")
            parsed = json.loads(Path(self.settings.state_file).read_text(encoding="utf-8"), parse_constant=reject_constant)
            state = EngineState.model_validate(parsed).model_dump()
            network = "testnet" if self.settings.private_testnet else "mainnet"
            recorded_network = state.get("exchange_network")
            has_trades = bool(state.get("order_journal") or state.get("active_strategy_symbols") or state.get("rfq_state"))
            if recorded_network and recorded_network != network:
                raise ValueError("State belongs to another exchange network")
            if not recorded_network and has_trades and self.settings.environment == "testnet":
                raise ValueError("Legacy trading state cannot be adopted by testnet")
            self.last_open_week = state.get("last_open_week")
            self.active_strategy_symbols = set(state.get("active_strategy_symbols", []))
            self.active_strategy_sizes = {key: float(value) for key, value in (state.get("active_strategy_sizes") or {}).items()}
            self.active_strategy_group_id = state.get("active_strategy_group_id")
            self.rfq_state = state.get("rfq_state") or {}
            self.execution_groups = state.get("execution_groups") or {}
            self.execution_group_links = state.get("execution_group_links") or {}
            self.pm_baseline = state.get("pm_baseline") or {}
            self.order_journal = state.get("order_journal") or {}
            self.performance_executions = {key: ExecutionRecord.model_validate(value) for key, value in state["performance_executions"].items()}
            self.performance_start_ms = state["performance_start_ms"]
            self.performance_cursor_ms = state["performance_cursor_ms"]
            self.performance_samples = {key: [PerformanceSample.model_validate(sample) for sample in values] for key, values in state["performance_samples"].items()}
            self.state_error = None
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            self.state_error = f"State file could not be loaded ({type(exc).__name__}); restore a valid file and restart before trading"
            self.log("ERROR", self.state_error)

    def _require_trading_state(self) -> None:
        if self.state_error:
            raise ValueError(self.state_error)

    def _save_state(self) -> None:
        self._require_trading_state()
        target = Path(self.settings.state_file)
        temp = None
        try:
            state = EngineState(exchange_network="testnet" if self.settings.private_testnet else "mainnet", last_open_week=self.last_open_week, active_strategy_symbols=sorted(self.active_strategy_symbols),
                                active_strategy_sizes=self.active_strategy_sizes, active_strategy_group_id=self.active_strategy_group_id,
                                rfq_state=self.rfq_state, execution_groups=self.execution_groups, execution_group_links=self.execution_group_links,
                                pm_baseline=self.pm_baseline, order_journal=self.order_journal, performance_executions=self.performance_executions,
                                performance_start_ms=self.performance_start_ms, performance_cursor_ms=self.performance_cursor_ms, performance_samples=self.performance_samples)
            payload = json.dumps(state.model_dump(mode="json"), allow_nan=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, prefix=target.name + ".", suffix=".tmp", delete=False) as stream:
                temp = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(target)
        except (OSError, ValueError) as exc:
            self.state_error = f"State file could not be saved ({type(exc).__name__}); verify storage and restart before trading"
            self.log("ERROR", self.state_error)
            raise ValueError(self.state_error) from exc
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def is_open_window(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if now.weekday() != self.settings.open_day:
            return False
        scheduled = now.replace(hour=self.settings.open_hour_utc, minute=self.settings.open_minute_utc, second=0, microsecond=0)
        return 0 <= (now - scheduled).total_seconds() < self.settings.open_window_seconds

    def _validate_open_calendar(self, expiry: datetime, now: datetime | None = None) -> None:
        now = now or datetime.now(timezone.utc)
        if now.weekday() != self.settings.open_day:
            raise ValueError("Live Iron Condor entry is only allowed on the configured Friday open day")
        if expiry.weekday() != 6:
            raise ValueError("Live Iron Condor legs must expire on Sunday UTC")
        expected_sunday = (now + timedelta(days=(6 - now.weekday()) % 7)).date()
        if expiry.date() != expected_sunday:
            raise ValueError(f"Live Iron Condor expiry must be Sunday {expected_sunday.isoformat()} UTC")

    def log(self, level: str, message: str) -> None:
        self.logs.insert(0, LogEntry(timestamp=datetime.now(timezone.utc), level=level, message=message))
        self.logs = self.logs[:80]

    @staticmethod
    def _portfolio_margin_metrics(payload: dict) -> dict[str, float | None]:
        def number(value) -> float | None:
            return float(value) if value not in (None, "") else None

        wallet = payload.get("wallet") or {}
        assets = payload.get("assetPnlRange") or []
        btc = next((item for item in assets if item.get("baseCoin") == "BTC"), {})
        asset = btc.get("asset") or {}
        contingency = btc.get("contingency") or {}
        return {
            "account_im": number(wallet.get("accountIM")),
            "account_mm": number(wallet.get("accountMM")),
            "asset_im": number(asset.get("assetIM")),
            "asset_mm": number(asset.get("assetMM")),
            "contingency": number(contingency.get("contingencyComponents")),
            "max_loss_price_move": number(btc.get("maxLossPriceMove")),
            "max_loss_iv_shock": number(btc.get("maxLossIvShock")),
        }

    async def _capture_pm_baseline(self, context: str) -> None:
        try:
            account = await self.client.account_info()
            if account.get("marginMode") != "PORTFOLIO_MARGIN":
                self.pm_baseline = {}
                return
            metrics = self._portfolio_margin_metrics(await self.client.portfolio_margin("BTC"))
            self.pm_baseline = {**metrics, "captured_at": datetime.now(timezone.utc).isoformat(), "context": context}
            self._save_state()
        except Exception as exc:
            self.log("WARNING", f"Could not capture pre-trade portfolio margin baseline: {exc}")

    async def refresh_chain(self, force: bool = False, refresh_instruments: bool = False) -> list:
        now = datetime.now(timezone.utc)
        if not force and self.chain and self.chain_updated_at and (now - self.chain_updated_at).total_seconds() < max(1, self.settings.market_refresh_seconds - 1):
            return self.chain
        async with self.refresh_lock:
            now = datetime.now(timezone.utc)
            if not force and self.chain and self.chain_updated_at and (now - self.chain_updated_at).total_seconds() < max(1, self.settings.market_refresh_seconds - 1):
                return self.chain
            try:
                instruments_stale = refresh_instruments or not self.raw_instruments or not self.instruments_updated_at or (now - self.instruments_updated_at).total_seconds() >= self.settings.instrument_refresh_seconds
                if instruments_stale:
                    raw_instruments, raw_tickers, underlying = await asyncio.gather(self.client.instruments(), self.client.tickers(), self.client.underlying_ticker())
                    self.raw_instruments = raw_instruments
                    self.instruments_updated_at = now
                else:
                    raw_tickers, underlying = await asyncio.gather(self.client.tickers(), self.client.underlying_ticker())
                    raw_instruments = self.raw_instruments
                ticker_map = {item.get("symbol"): item for item in raw_tickers}
                underlying_price = float(underlying.get("indexPrice") or underlying.get("markPrice") or underlying.get("lastPrice") or 0)
                from .models import OptionInstrument
                parsed = []
                for item in raw_instruments:
                    symbol = item.get("symbol", "")
                    expiry = datetime.fromtimestamp(int(item.get("deliveryTime", 0)) / 1000, tz=timezone.utc)
                    strike_value = item.get("strikePrice") or (symbol.split("-")[2] if len(symbol.split("-")) > 2 else 0)
                    if not symbol or expiry <= now or float(strike_value or 0) <= 0:
                        continue
                    kind = "Call" if item.get("optionsType") == "Call" else "Put"
                    ticker = ticker_map.get(symbol, {})
                    lot = item.get("lotSizeFilter") or {}
                    price_filter = item.get("priceFilter") or {}
                    parsed.append(OptionInstrument(symbol=symbol, expiry=expiry, strike=float(strike_value), option_type=kind, delta=float(ticker.get("delta", 0) or 0), mark_price=float(ticker.get("markPrice", 0) or 0), bid=float(ticker.get("bid1Price", 0) or 0), ask=float(ticker.get("ask1Price", 0) or 0), iv=float(ticker.get("markIv", 0) or 0), volume=float(ticker.get("volume24h", 0) or 0), open_interest=float(ticker.get("openInterest", 0) or 0), bid_size=float(ticker.get("bid1Size", 0) or 0), ask_size=float(ticker.get("ask1Size", 0) or 0), min_qty=float(lot.get("minOrderQty", 0.01) or 0.01), qty_step=float(lot.get("qtyStep", 0.01) or 0.01), max_qty=float(lot.get("maxOrderQty", 500) or 500), price_tick=float(price_filter.get("tickSize", 0.01) or 0.01)))
                valid_deltas = [item for item in parsed if abs(item.delta) > 0 and item.mark_price > 0]
                if len(valid_deltas) >= 2 and {item.option_type for item in valid_deltas} == {"Call", "Put"}:
                    # Replace the visible snapshot only after both endpoints and all rows validate.
                    self.chain = parsed
                    self.chain_source = "bybit"
                    self.chain_updated_at = now
                    self.btc_price = underlying_price or None
                    self.log("INFO", f"Loaded {len(parsed)} BTC option instruments from Bybit")
                    return parsed
                raise ValueError("Bybit returned no usable BTC option chain")
            except Exception as exc:
                self.log("WARNING", f"Bybit public market unavailable; retaining previous snapshot: {exc}")
                if self.chain:
                    return self.chain
                # Never fabricate market data. At startup the API reports an
                # unavailable source; after startup the previous good snapshot
                # remains visible until a complete replacement is ready.
                self.chain_source = "unavailable"
                return []

    def _build_preview_from_chain(self, quantity: float | None = None, strategy_mode: StrategyMode | None = None) -> StrategyPreview:
        multiplier = 1.0 if self.chain_source == "bybit" else 0.01
        now = datetime.now(timezone.utc)
        qty = self.settings.leg_qty if quantity is None else quantity
        mode = validate_mode(strategy_mode or self.settings.strategy_mode)
        preview = build_strategy(self.chain, now, self.settings.target_dte_days, qty, multiplier, self.settings.estimated_taker_fee_rate, self.settings.portfolio_margin_buffer_pct, self.btc_price or 0.0, self.settings.margin_mode, self.settings.option_mm_factor, self.settings.option_max_im_factor, self.settings.option_min_im_factor, self.settings.option_liquidation_fee_rate, self.settings.option_fee_cap_pct, strategy_mode=mode)
        preview.source = self.chain_source
        preview.market_timestamp = self.chain_updated_at
        preview.btc_price = self.btc_price
        return preview

    async def make_preview(self, quantity: float | None = None, strategy_mode: StrategyMode | None = None) -> StrategyPreview:
        await self.refresh_chain()
        self.preview = self._build_preview_from_chain(quantity, strategy_mode)
        if self.preview.unbounded_loss:
            level = "WARNING" if self.preview.estimated_margin_usd > self.settings.max_margin_usd else "INFO"
            self.log(level, f"Short strangle preview: unbounded loss, margin budget ${self.preview.estimated_margin_usd:.2f} / ${self.settings.max_margin_usd:.2f}")
        elif self.preview.max_loss_usd > self.settings.max_risk_usd:
            self.log("WARNING", f"Preview risk ${self.preview.max_loss_usd:.2f} exceeds limit ${self.settings.max_risk_usd:.2f}")
        else:
            self.log("INFO", f"Preview ready: credit ${self.preview.net_credit_usd:.2f}, max loss ${self.preview.max_loss_usd:.2f}")
        return self.preview

    def _validate_market_snapshot(self) -> None:
        if self.chain_source != "bybit" or self.chain_updated_at is None:
            raise ValueError("Live orders require a fresh Bybit market snapshot")
        age = (datetime.now(timezone.utc) - self.chain_updated_at).total_seconds()
        if age < 0 or age > self.settings.quote_stale_seconds:
            raise ValueError("Bybit market snapshot is stale; refresh market data before trading")

    def _validate_risk(self, preview: StrategyPreview) -> None:
        mode = validate_mode(preview.strategy_mode)
        if len({leg.symbol for leg in preview.legs}) != len(preview.legs):
            raise ValueError("Strategy contains duplicate instruments")
        validate_structure({leg.symbol: {**leg.model_dump(), "expiry": preview.expiry} for leg in preview.legs}, mode, require_expiry=True)
        if mode == "short_strangle":
            if not preview.unbounded_loss or preview.max_loss_usd is not None or preview.risk_reward is not None:
                raise ValueError("Short-strangle loss must be explicitly unbounded")
            if not isfinite(preview.estimated_margin_usd) or preview.estimated_margin_usd < 0 or preview.estimated_margin_usd > self.settings.max_margin_usd:
                raise ValueError("Margin budget exceeded; reduce quantity or raise MAX_MARGIN_USD")
            self._initial_execution_risk(preview)
            return
        metrics = (preview.max_loss_usd, preview.estimated_margin_usd)
        if preview.unbounded_loss or any(value is None or not isfinite(value) or value < 0 or value > self.settings.max_risk_usd for value in metrics):
            raise ValueError("Risk limit exceeded; reduce quantity or raise MAX_RISK_USD")

    def _margin_context(self) -> dict:
        return {"index_price": self.btc_price, "market_timestamp": self.chain_updated_at.isoformat() if self.chain_updated_at else None,
                "fee_rate": self.settings.estimated_taker_fee_rate, "fee_cap_pct": self.settings.option_fee_cap_pct,
                "mm_factor": self.settings.option_mm_factor, "max_im_factor": self.settings.option_max_im_factor,
                "min_im_factor": self.settings.option_min_im_factor, "liquidation_fee_rate": self.settings.option_liquidation_fee_rate,
                "margin_buffer_pct": self.settings.portfolio_margin_buffer_pct}

    def _initial_execution_risk(self, preview: StrategyPreview) -> dict:
        mode = validate_mode(preview.strategy_mode)
        instruments = {item.symbol: item for item in self.chain}
        bounds = {}
        for leg in preview.legs:
            instrument = instruments.get(leg.symbol)
            if (instrument is None or instrument.expiry != preview.expiry or instrument.strike != leg.strike
                    or instrument.option_type != leg.option_type):
                raise ValueError("Strategy legs no longer match the market instruments")
            bounds[leg.symbol] = {"side": leg.side, "option_type": leg.option_type, "strike": leg.strike,
                                  "qty": leg.qty, "expiry": instrument.expiry.isoformat(), "mark_price": instrument.mark_price,
                                  "price_bound": (instrument.bid if leg.side == "Buy" else instrument.ask) or instrument.mark_price}
        if len(bounds) != len(preview.legs):
            raise ValueError("Strategy contains duplicate instruments")
        validate_structure(bounds, mode, require_expiry=True)
        context = self._margin_context()
        if mode == "short_strangle":
            bounds, reserved = reserve_short_margin(bounds, context)
            budget = self.settings.max_margin_usd
            if reserved > budget:
                raise ValueError("Initial prices exceed short-strangle margin budget; reduce quantity or raise MAX_MARGIN_USD")
        else:
            reserved, budget = maximum_loss(bounds), self.settings.max_risk_usd
            if reserved > budget:
                raise ValueError("Initial BBO prices exceed combination maximum loss limit")
        return {"strategy_mode": mode, "risk_legs": bounds, "risk_context": context,
                "risk_budget_usd": budget, "risk_reserved_usd": reserved}

    def _proposed_execution_risk(self, group: dict, prices: dict[str, float]) -> dict:
        mode = validate_mode(group.get("strategy_mode", "iron_condor"))
        bounds = group.get("risk_legs")
        if not bounds or not set(prices) <= set(bounds):
            raise ValueError("Opening strategy has no execution price risk bounds")
        proposed = {key: dict(value) for key, value in bounds.items()}
        for symbol, price in prices.items():
            if not isfinite(price) or price <= 0:
                raise ValueError("Invalid execution price")
            leg = proposed[symbol]
            if mode == "short_strangle":
                leg["price_floor"] = min(price, leg.get("price_floor", leg["price_bound"]))
                leg["price_ceiling"] = max(price, leg.get("price_ceiling", leg["price_bound"]))
                leg["price_bound"] = price
            else:
                leg["price_bound"] = max(price, leg["price_bound"]) if leg["side"] == "Buy" else min(price, leg["price_bound"])
        if mode == "short_strangle":
            self._validate_market_snapshot()
            context = dict(group.get("risk_context") or {})
            if not context:
                raise ValueError("Short-strangle opening has no saved margin parameters")
            current = self._margin_context()
            for key in ("fee_rate", "fee_cap_pct", "mm_factor", "max_im_factor", "min_im_factor", "liquidation_fee_rate", "margin_buffer_pct"):
                previous = context.get(key)
                if not isinstance(previous, (int, float)) or isinstance(previous, bool) or not isfinite(previous) or previous < 0:
                    raise ValueError("Short-strangle opening has invalid saved margin parameters")
                # A restart with stricter settings must not relax an existing
                # inquiry; lower requirements never free a previous reserve.
                context[key] = max(previous, current[key])
            context.update(index_price=self.btc_price, market_timestamp=self.chain_updated_at.isoformat())
            instruments = {item.symbol: item for item in self.chain}
            for symbol, leg in proposed.items():
                instrument = instruments.get(symbol)
                if (instrument is None or instrument.option_type != leg["option_type"] or instrument.strike != leg["strike"]
                        or instrument.expiry.isoformat() != leg.get("expiry")):
                    raise ValueError("Short-strangle margin snapshot does not match the original legs")
                leg["mark_price"] = instrument.mark_price
            proposed, reserved = reserve_short_margin(proposed, context)
            budget = group.get("risk_budget_usd")
            if not isinstance(budget, (int, float)) or isinstance(budget, bool) or not isfinite(budget) or budget <= 0:
                raise ValueError("Short-strangle opening has no valid saved margin budget")
            budget = min(self.settings.max_margin_usd, budget)
            if reserved > budget:
                raise ValueError(f"Execution prices exceed short-strangle margin budget: {reserved:.2f}")
            return {"risk_legs": proposed, "risk_context": context, "risk_reserved_usd": reserved}
        reserved = maximum_loss(proposed)
        if reserved > self.settings.max_risk_usd:
            raise ValueError(f"Execution prices exceed combination maximum loss limit: {reserved:.2f}")
        return {"risk_legs": proposed, "risk_reserved_usd": reserved}

    def _observe_order(self, link: str, event: dict) -> None:
        observe_order(self.order_activity, link, event)

    def order_snapshot(self) -> dict:
        return order_dashboard(self.order_journal, self.order_activity,
                               stale_seconds=max(15, self.settings.reconciliation_seconds * 2, self.settings.bbo_poll_seconds * 3),
                               groups=self.execution_groups, group_links=self.execution_group_links)

    def _record_order(self, link: str, outcome: dict) -> None:
        entry = self.order_journal[link]
        # Reconciliation carries a copy of the journal; its old display timestamp
        # must not turn unchanged exchange observations into repeated disk writes.
        outcome = {key: value for key, value in outcome.items() if key != "updated_at"}
        previous_filled = float(entry.get("filledQty", 0))
        if float(outcome.get("filledQty", 0)) < previous_filled:
            outcome = {**outcome, "filledQty": previous_filled}
        if all(entry.get(key) == value for key, value in outcome.items()):
            return
        entry.update(outcome)
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        delta = max(0.0, float(entry.get("filledQty", 0)) - previous_filled)
        group_id = self.execution_group_links.get(link)
        group = self.execution_groups.get(group_id, {})
        if delta > 0 and group:
            position_side = ("Sell" if entry["side"] == "Buy" else "Buy") if entry["reduce_only"] else entry["side"]
            key = f"{entry['symbol']}|{position_side}"
            if entry["reduce_only"]:
                self.active_strategy_sizes[key] = max(0.0, self.active_strategy_sizes.get(key, 0) - delta)
                if self.active_strategy_sizes[key] <= 1e-9:
                    self.active_strategy_sizes.pop(key, None)
                    self.active_strategy_symbols.discard(entry["symbol"])
            else:
                self.active_strategy_sizes[key] = self.active_strategy_sizes.get(key, 0) + delta
                self.active_strategy_symbols.add(entry["symbol"])
                self.active_strategy_group_id = group_id
        self._save_state()

    async def _execute_order(self, leg, qty: float, link: str, reduce_only: bool = False, market: bool = False) -> dict:
        group_id = self.execution_group_links.get(link)
        group = self.execution_groups.get(group_id, {})
        if group.get("task_version") == 1:
            self._check_trade_stop(group_id)
        instrument = next((item for item in self.chain if item.symbol == leg.symbol), None)
        if instrument is None:
            raise ValueError(f"Instrument disappeared from fresh market data: {leg.symbol}")
        if link in self.order_journal:
            raise ValueError("Order link has already been used; reconcile it instead of resubmitting")
        self.order_journal[link] = {"symbol": leg.symbol, "side": leg.side, "qty": qty,
                                    "reduce_only": reduce_only, "status": "unknown", "terminal": False, "filledQty": 0.0,
                                    "execution_type": "IOC" if market else "BBO", "created_at": datetime.now(timezone.utc).isoformat()}
        self._save_state()
        executor = OrderExecutor(self.client, self.settings, self.log)
        guard = (lambda price: self._reserve_execution_price(link, leg.symbol, price)) if not reduce_only or group.get("task_version") == 1 else None
        return await executor.execute(instrument, leg.side, qty, link, lambda outcome: self._record_order(link, outcome), reduce_only, market, guard,
                                      observer=lambda event: self._observe_order(link, event),
                                      stop_event=self.trade_stop_events.get(group_id))

    def _reserve_execution_price(self, link: str, symbol: str, price: float | None) -> None:
        self._require_trading_state()
        group = self.execution_groups.get(self.execution_group_links.get(link), {})
        if not group:
            return  # Direct executor use has no strategy context.
        if group.get("task_version") == 1:
            self._check_trade_stop(self.execution_group_links.get(link))
            self._validate_market_snapshot()
            entry = self.order_journal.get(link)
            bound = group["legs"].get(symbol)
            if entry and (not bound or entry["symbol"] != symbol or entry["side"] != bound["side"]
                          or not isfinite(entry["qty"]) or not 0 < entry["qty"] <= bound["qty"]):
                raise ValueError("Order does not match its confirmed plan")
            self._reserve_task_price(group, symbol, price)
            if group["type"] == "close":
                return
        if group.get("risk_blocked"):
            raise ValueError("Combination risk was exceeded; remaining opening orders must stop")
        entry = self.order_journal.get(link)
        bound = (group.get("risk_legs") or {}).get(symbol)
        if entry and (not bound or entry["symbol"] != symbol or entry["side"] != bound["side"]
                      or not isfinite(entry["qty"]) or not 0 < entry["qty"] <= bound["qty"]):
            group["risk_blocked"] = True
            self._save_state()
            raise ValueError("Order does not match its original strategy quantity or side")
        if price is None and group.get("strategy_mode", "iron_condor") == "iron_condor":
            return
        try:
            proposed = self._proposed_execution_risk(group, {} if price is None else {symbol: price})
        except ValueError:
            group["risk_blocked"] = True
            self._save_state()
            raise
        if any(group.get(key) != value for key, value in proposed.items()):
            # Keep the worst bound ever submitted; fills can race amendments.
            group.update(proposed)
            self._save_state()

    async def follow_bbo_order(self, leg, qty: float, order_link_id: str, reduce_only: bool = False) -> dict:
        return await self._execute_order(leg, qty, order_link_id, reduce_only)

    @staticmethod
    def _validate_leg_quantity(instrument, qty: float) -> None:
        if (not isfinite(qty) or qty <= 0 or any(not isfinite(value) or value <= 0
                                               for value in (instrument.min_qty, instrument.max_qty, instrument.qty_step))
                or instrument.max_qty < instrument.min_qty):
            raise ValueError("Invalid quantity or exchange lot limits")
        if qty < instrument.min_qty or qty > instrument.max_qty:
            raise ValueError(f"Quantity for {instrument.symbol} must be between {instrument.min_qty} and {instrument.max_qty}")
        steps = round((qty - instrument.min_qty) / instrument.qty_step)
        if not isclose(instrument.min_qty + steps * instrument.qty_step, qty, rel_tol=0, abs_tol=1e-9):
            raise ValueError(f"Quantity {qty} does not match Bybit step {instrument.qty_step} for {instrument.symbol}")

    async def open_position(self, request: OpenRequest, scheduled: bool = False) -> list[OrderResult]:
        self._require_idle_trading_operation()
        async with self.lock:
            self._require_trading_state()
            if self.settings.can_send_orders and not request.confirm_live:
                raise ValueError("Live opening requires explicit confirmation")
            qty = request.quantity or self.settings.leg_qty
            mode = self.settings.strategy_mode if scheduled else request.strategy_mode or self.settings.strategy_mode
            preview = await self.make_preview(qty, strategy_mode=mode)
            if preview.strategy_mode != mode:
                raise ValueError("Preview strategy mode does not match the opening request")
            live = bool(request.confirm_live and self.settings.can_send_orders)
            if live:
                self._require_resolved_rfq()
                await self._reconcile_pending_orders()
                if self.active_strategy_symbols:
                    await self._sync_positions()
                if self.active_strategy_symbols:
                    raise ValueError("Close the tracked strategy before opening another")
                self._validate_open_calendar(preview.expiry)
            if live and scheduled and not self.is_open_window():
                raise ValueError(f"Live orders are only allowed during Friday {self.settings.open_hour_utc:02d}:{self.settings.open_minute_utc:02d} UTC window")
            if request.confirm_live and not self.settings.can_send_orders:
                self.log("WARNING", "Live confirmation received but live trading is disabled; using dry-run")
            if qty <= 0:
                raise ValueError("Quantity must be greater than zero")
            for leg in preview.legs:
                if not isclose(leg.qty, qty, rel_tol=0, abs_tol=1e-9):
                    raise ValueError("Preview quantity does not match the opening request")
                instrument = next((item for item in self.chain if item.symbol == leg.symbol), None)
                if instrument is None:
                    raise ValueError(f"Instrument disappeared from fresh market data: {leg.symbol}")
                self._validate_leg_quantity(instrument, qty)
                if live and (instrument.bid <= 0 or instrument.ask <= 0):
                    raise ValueError(f"No executable bid/ask for {leg.symbol}")
                spread_bps = (instrument.ask - instrument.bid) / instrument.mark_price * 10000 if instrument.mark_price > 0 else float("inf")
                if live and self.settings.max_spread_bps > 0 and spread_bps > self.settings.max_spread_bps:
                    raise ValueError(f"Spread for {leg.symbol} is {spread_bps:.0f} bps, above limit {self.settings.max_spread_bps:.0f} bps")
            self._validate_risk(preview)
            if live:
                self._validate_market_snapshot()
                await self._capture_pm_baseline("scheduled_open" if scheduled else "manual_open")
                self._validate_market_snapshot()
                request_id = uuid4().hex[:12]
                order_links = [f"ic-{request_id}-{index}" for index, _ in enumerate(preview.legs)]
                risk = self._initial_execution_risk(preview)
                self.execution_groups[request_id] = {"type": "open", "order_tracking": True, "created_at": datetime.now(timezone.utc).isoformat(), **risk,
                                                     "legs": {leg.symbol: {"side": leg.side, "option_type": leg.option_type, "strike": leg.strike,
                                                                           "expiry": preview.expiry.isoformat(), "chain_price": (next(item for item in self.chain if item.symbol == leg.symbol).bid if leg.side == "Sell" else next(item for item in self.chain if item.symbol == leg.symbol).ask), "qty": qty} for leg in preview.legs}}
                for link in order_links:
                    self.execution_group_links[link] = request_id
                self._save_state()
                responses = await asyncio.gather(*[self.follow_bbo_order(leg, qty, order_links[index]) for index, leg in enumerate(preview.legs)], return_exceptions=True)
            else:
                order_links = [None] * len(preview.legs)
                responses = [None] * len(preview.legs)
            results = []
            all_links = [link for link in order_links if link]
            for leg, response, link in zip(preview.legs, responses, order_links):
                if not live:
                    results.append(OrderResult(symbol=leg.symbol, side=leg.side, qty=qty, status="simulated"))
                    self.positions.append(Position(symbol=leg.symbol, side=leg.side, size=qty, avg_price=leg.mark_price,
                                                   mark_price=leg.mark_price, unrealised_pnl=0, source="demo"))
                    continue
                if isinstance(response, BaseException):
                    response = {"status": "unknown", "filledQty": 0.0, "terminal": False, "message": str(response)}
                result = OrderResult(symbol=leg.symbol, side=leg.side, qty=float(response.get("filledQty", 0)),
                                     status=response["status"], order_id=response.get("orderId"), order_link_id=link,
                                     message=response.get("message"))
                results.append(result)
            if live and self.settings.allow_market_fallback:
                await self._market_fallback(preview.legs, qty, order_links, results, request_id, all_links)
            if live:
                await self.load_recent_executions(all_links)
                self._attach_execution_details(results)
                if any(item.status != "filled" for item in results):
                    self.log("ERROR", "Some legs are incomplete or unresolved; inspect the reported quantities and exchange orders")
                else:
                    self.log("INFO", f"All {len(preview.legs)} live legs are confirmed filled")
            else:
                self.log("INFO", f"All {len(preview.legs)} legs were simulated")
            if scheduled:
                self.last_open_week = datetime.now(timezone.utc).strftime("%G-W%V")
                self._save_state()
            self.log("INFO", f"{mode} {'processed on Bybit' if live else 'simulated'} with {len(results)} legs")
            return results

    async def _market_fallback(self, legs, qty, links, results, group_id, all_links) -> None:
        executor = OrderExecutor(self.client, self.settings, self.log)
        await self._trade_delay(group_id, self.settings.failed_leg_retry_delay_seconds)
        for leg, link, result in zip(legs, links, results):
            self._check_trade_stop(group_id)
            original = self.order_journal.get(link)
            if not original or not original.get("terminal") or original.get("status") not in {"partial", "timeout_cancelled"}:
                continue
            # Re-read the original order, even if cancellation was previously
            # confirmed. Neither account holdings nor a missing order proves it safe.
            confirmed = await executor.reconcile(leg.symbol, leg.side, qty, link, original, cancel=False,
                                                 observer=lambda event: self._observe_order(link, event),
                                                 record=lambda outcome: self._record_order(link, outcome))
            self._record_order(link, confirmed)
            self._check_trade_stop(group_id)
            result.qty = confirmed["filledQty"]
            result.status = confirmed["status"]
            if not confirmed["terminal"]:
                result.message = confirmed.get("message")
                continue
            remaining = round(max(0.0, qty - confirmed["filledQty"]), 10)
            if remaining <= 1e-9:
                continue
            instrument = next(item for item in self.chain if item.symbol == leg.symbol)
            if (remaining < instrument.min_qty or remaining > instrument.max_qty
                    or not isclose(remaining / instrument.qty_step, round(remaining / instrument.qty_step), rel_tol=0, abs_tol=1e-9)):
                result.message = "Remaining quantity does not meet exchange lot limits; no fallback sent"
                continue
            retry_link = f"ic-mkt-{uuid4().hex[:12]}"
            self.execution_group_links[retry_link] = group_id
            all_links.append(retry_link)
            self._save_state()
            replacement = await self._execute_order(leg, remaining, retry_link, market=True)
            result.related_order_link_ids = [link, retry_link]
            result.qty = round(confirmed["filledQty"] + replacement["filledQty"], 10)
            result.status = ("filled" if isclose(result.qty, qty, rel_tol=0, abs_tol=1e-9) else "partial" if result.qty else "error") if replacement["terminal"] else "unknown"
            result.message = replacement.get("message")
            self.log("WARNING", f"Market fallback for {leg.symbol}: requested only remaining quantity {remaining}")

    def _attach_execution_details(self, results: list[OrderResult]) -> None:
        by_link: dict[str, list[ExecutionRecord]] = {}
        for execution in self.last_executions:
            by_link.setdefault(execution.order_link_id, []).append(execution)
        for result in results:
            links = result.related_order_link_ids or [result.order_link_id or ""]
            executions = [item for link in set(links) for item in self.execution_details.get(link, by_link.get(link, []))]
            if not executions:
                continue
            result.exec_fee = round(sum(item.exec_fee for item in executions), 8)
            result.fee_currency = executions[0].fee_currency
            result.exec_qty = round(sum(item.exec_qty for item in executions), 8)
            result.exec_price = round(sum(item.exec_price * item.exec_qty for item in executions) / result.exec_qty, 8) if result.exec_qty else None
            latest = max(executions, key=lambda item: item.exec_time)
            result.execution_id = latest.exec_id
            result.exec_time = latest.exec_time

    async def load_recent_executions(self, order_link_ids: list[str] | None = None) -> list[ExecutionRecord]:
        if not self.settings.bybit_api_key or not self.settings.bybit_api_secret:
            return self.last_executions
        try:
            if order_link_ids:
                requested_links = list(dict.fromkeys(order_link_ids))
                raw_groups = await asyncio.gather(*[self.client.executions(order_link_id) for order_link_id in requested_links], return_exceptions=True)
                for group in raw_groups:
                    if isinstance(group, Exception):
                        self.log("WARNING", f"Could not load some order executions; preserving cached records: {group}")
                raw_items = [item for group in raw_groups if isinstance(group, list) for item in group]
            else:
                raw_items = await self.client.executions()
            records = []
            seen = set()
            for item in raw_items:
                exec_id = item.get("execId", "")
                if not exec_id or exec_id in seen:
                    continue
                seen.add(exec_id)
                order_link_id = item.get("orderLinkId", "")
                group_id = self.execution_group_links.get(order_link_id)
                if not group_id:
                    parts = order_link_id.split("-")
                    if len(parts) >= 3 and parts[0] == "ic" and parts[1] not in {"close", "mkt"}:
                        group_id = parts[1]
                group = self.execution_groups.get(group_id or "") or {}
                reduce_only = group.get("type") == "close" or order_link_id.startswith("ic-close-")
                baseline = {} if reduce_only else group.get("legs", {}).get(item.get("symbol", ""), {})
                exec_price = float(item.get("execPrice", 0) or 0)
                exec_qty = float(item.get("execQty", 0) or 0)
                chain_price = float(baseline.get("chain_price", 0) or 0) if baseline else None
                strategy_side = baseline.get("side")
                chain_diff = ((1 if strategy_side == "Sell" else -1) * (exec_price - chain_price) * exec_qty) if chain_price is not None and strategy_side else None
                records.append(ExecutionRecord(symbol=item.get("symbol", ""), side=item.get("side", ""), order_id=item.get("orderId", ""), order_link_id=order_link_id, exec_id=exec_id, exec_fee=float(item.get("execFee", 0) or 0), fee_currency=item.get("feeCurrency", ""), exec_price=exec_price, exec_qty=exec_qty, fee_rate=float(item.get("feeRate", 0) or 0) if item.get("feeRate") not in (None, "") else None, exec_time=datetime.fromtimestamp(int(item.get("execTime", 0)) / 1000, tz=timezone.utc), reduce_only=reduce_only, opening_group=group.get("opening_group"), execution_group=group_id, chain_price_at_create=chain_price, chain_price_diff=chain_diff, closed_size=float(item["closedSize"]) if item.get("closedSize") not in (None, "") else None, exec_type=item.get("execType") or "Trade"))
            if order_link_ids:
                for link, group in zip(requested_links, raw_groups):
                    if isinstance(group, list):
                        self.execution_details.pop(link, None)
                        self.execution_details[link] = [item for item in records if item.order_link_id == link]
                while len(self.execution_details) > 32:
                    self.execution_details.pop(next(iter(self.execution_details)))
            merged = {item.exec_id: item for item in self.last_executions}
            merged.update({item.exec_id: item for item in records})
            self.last_executions = sorted(merged.values(), key=lambda item: item.exec_time, reverse=True)[:100]
            self._archive_performance(records)
        except Exception as exc:
            self.log("WARNING", f"Could not load execution fee records: {exc}")
        return self.last_executions

    async def close_position(self, request: CloseRequest) -> tuple[list[OrderResult], list[ExecutionRecord]]:
        self._require_idle_trading_operation()
        async with self.lock:
            self._require_trading_state()
            if self.settings.can_send_orders and not request.confirm_live:
                raise ValueError("Live closing requires explicit confirmation")
            live = bool(request.confirm_live and self.settings.can_send_orders)
            if live:
                await self._reconcile_pending_orders()
                self._require_resolved_rfq()
                current = await self._sync_positions()
                if self._tracking_needs_recovery():
                    recovered = self._track_filled_rfq(current)
                    if not recovered:
                        await self.load_recent_executions()
                        recovered = self._recover_tracked_open_positions(current)
                    if recovered:
                        self.log("INFO", "Recovered tracked strategy legs from the opening task and current Bybit positions")
            else:
                current = [position for position in self.positions if position.source == "demo"]
            if live and not self.active_strategy_symbols:
                raise ValueError("No tracked live strategy legs found; refusing to close untracked positions")
            symbols = self.active_strategy_symbols if live else {position.symbol for position in current}
            opening = self.execution_groups.get(self.active_strategy_group_id, {})
            mode = validate_mode(opening.get("strategy_mode", "iron_condor"))
            if live and len(symbols) > leg_count(mode):
                raise ValueError("Tracked strategy contains too many symbols for its original mode; refusing bulk close")
            current = [position for position in current if position.symbol in symbols and position.size > 0]
            if live:
                current = [position.model_copy(update={"size": min(position.size, self.active_strategy_sizes.get(f"{position.symbol}|{position.side}", 0.0))}) for position in current if self.active_strategy_sizes.get(f"{position.symbol}|{position.side}", 0.0) > 0]
            if not current:
                raise ValueError("No open strategy legs found to close")
            close_group_id = uuid4().hex[:12]
            links = [f"ic-close-{close_group_id}-{index}" for index, _ in enumerate(current)]
            if live:
                close_legs = [type("CloseLeg", (), {"symbol": position.symbol, "side": "Sell" if position.side == "Buy" else "Buy"})() for position in current]
                self.execution_groups[close_group_id] = {"type": "close", "opening_group": self.active_strategy_group_id, "strategy_mode": mode, "order_tracking": True}
                for link in links:
                    self.execution_group_links[link] = close_group_id
                self._save_state()
                responses = await asyncio.gather(*[self.follow_bbo_order(close_legs[index], position.size, links[index], reduce_only=True) for index, position in enumerate(current)], return_exceptions=True)
            else:
                responses = [None] * len(current)
            results = []
            for position, response, link in zip(current, responses, links):
                if isinstance(response, BaseException):
                    response = {"status": "unknown", "filledQty": 0.0, "message": str(response)}
                results.append(OrderResult(symbol=position.symbol, side="Sell" if position.side == "Buy" else "Buy",
                                           qty=float(response.get("filledQty", 0)) if live else position.size,
                                           status=response["status"] if live else "simulated",
                                           order_id=response.get("orderId") if live else None, order_link_id=link,
                                           message=response.get("message") if live else None))
            if live:
                await self.load_recent_executions(links)
                self._attach_execution_details(results)
                # Quantities are deducted incrementally by _record_order,
                # including successful legs when another close leg is unresolved.
                if not self.active_strategy_symbols and all(item.status == "filled" for item in results):
                    completed_group_id = self.active_strategy_group_id
                    if completed_group_id in self.execution_groups:
                        self.execution_groups[completed_group_id]["status"] = "closed"
                    self.active_strategy_group_id = None
                    self.pm_baseline = {}
                self._save_state()
            else:
                self.positions = [position for position in self.positions if position not in current]
            return results, self.last_executions

    async def scheduler(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            week = now.strftime("%G-W%V")
            if self.settings.auto_open and self.is_open_window(now) and self.last_open_week != week:
                try:
                    plan = await self.prepare_trade_plan(TradePlanRequest(operation="open", strategy_mode=self.settings.strategy_mode))
                    await self.start_trade_task(TradeTaskRequest(plan_id=plan["plan_id"], request_id=f"scheduled-{week}",
                                                               confirm_live=self.settings.can_send_orders), scheduled=True)
                except Exception as exc:
                    self.log("ERROR", f"Scheduled open failed: {exc}")
            await asyncio.sleep(5)

    async def market_loop(self) -> None:
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self.refresh_chain(force=True)
            except Exception as exc:
                self.log("WARNING", f"Market refresh loop failed: {exc}")
            elapsed = asyncio.get_running_loop().time() - started
            await asyncio.sleep(max(0.1, self.settings.market_refresh_seconds - elapsed))

    async def load_account_health(self) -> AccountHealth:
        if not self.settings.bybit_api_key or not self.settings.bybit_api_secret:
            self.account_health = AccountHealth(available=False, message="Live account credentials are not configured")
            return self.account_health
        try:
            account, wallet = await asyncio.gather(self.client.account_info(), self.client.wallet_balance())
            def number(name: str) -> float | None:
                value = wallet.get(name, "")
                return float(value) if value not in (None, "") else None
            def rate(name: str) -> float | None:
                value = wallet.get(name, "")
                return float(value) if value not in (None, "") else None
            health_data = {"available_balance_usd": number("totalAvailableBalance"), "margin_balance_usd": number("totalMarginBalance"), "total_equity_usd": number("totalEquity"), "wallet_balance_usd": number("totalWalletBalance"), "initial_margin_usd": number("totalInitialMargin"), "maintenance_margin_usd": number("totalMaintenanceMargin"), "initial_margin_rate": rate("accountIMRate"), "maintenance_margin_rate": rate("accountMMRate"), "margin_mode": account.get("marginMode"), "updated_at": datetime.now(timezone.utc), "available": True}
            if account.get("marginMode") == "PORTFOLIO_MARGIN":
                try:
                    metrics = self._portfolio_margin_metrics(await self.client.portfolio_margin("BTC"))
                    baseline_account_im = self.pm_baseline.get("account_im")
                    baseline_account_mm = self.pm_baseline.get("account_mm")
                    account_im = metrics.get("account_im")
                    account_mm = metrics.get("account_mm")
                    health_data.update({"portfolio_margin_available": True, "pm_account_initial_margin_usd": account_im, "pm_account_maintenance_margin_usd": account_mm, "pm_asset_initial_margin_usd": metrics.get("asset_im"), "pm_asset_maintenance_margin_usd": metrics.get("asset_mm"), "pm_incremental_initial_margin_usd": account_im - baseline_account_im if account_im is not None and baseline_account_im is not None else None, "pm_incremental_maintenance_margin_usd": account_mm - baseline_account_mm if account_mm is not None and baseline_account_mm is not None else None, "pm_contingency_usd": metrics.get("contingency"), "pm_max_loss_price_move": metrics.get("max_loss_price_move"), "pm_max_loss_iv_shock": metrics.get("max_loss_iv_shock"), "pm_baseline_at": self.pm_baseline.get("captured_at"), "pm_baseline_context": self.pm_baseline.get("context")})
                except Exception as exc:
                    health_data.update({"portfolio_margin_message": str(exc)})
                    self.log("WARNING", f"Could not load detailed portfolio margin: {exc}")
            self.account_health = AccountHealth(**health_data)
        except Exception as exc:
            self.account_health = AccountHealth(available=False, message=str(exc))
            self.log("WARNING", f"Could not load account health: {exc}")
        return self.account_health
