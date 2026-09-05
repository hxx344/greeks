import asyncio
import json
from datetime import datetime, timedelta, timezone
from math import isclose
from pathlib import Path
from uuid import uuid4

from .bybit import BybitClient, BybitError
from .config import Settings
from .models import AccountHealth, CloseRequest, ExecutionRecord, LogEntry, OpenRequest, OrderResult, Position, RfqCancelRequest, RfqCreateRequest, RfqExecuteRequest, StrategyPreview
from .strategy import build_iron_condor


class TradingEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = BybitClient(settings.bybit_api_key, settings.bybit_api_secret, settings.bybit_testnet, settings.recv_window_ms)
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
        self._load_state()
        self.account_health = AccountHealth(available=False, message="Live account credentials are not configured")
        self.last_executions: list[ExecutionRecord] = []
        self.lock = asyncio.Lock()
        self.log("INFO", f"Engine started in {settings.environment} mode")

    def _load_state(self) -> None:
        try:
            state = json.loads(Path(self.settings.state_file).read_text(encoding="utf-8"))
            self.last_open_week = state.get("last_open_week")
            self.active_strategy_symbols = set(state.get("active_strategy_symbols", []))
            self.active_strategy_sizes = {key: float(value) for key, value in (state.get("active_strategy_sizes") or {}).items()}
            self.active_strategy_group_id = state.get("active_strategy_group_id")
            self.rfq_state = state.get("rfq_state") or {}
            self.execution_groups = state.get("execution_groups") or {}
            self.execution_group_links = state.get("execution_group_links") or {}
            self.pm_baseline = state.get("pm_baseline") or {}
        except (FileNotFoundError, OSError, ValueError):
            self.last_open_week = None

    def _save_state(self) -> None:
        target = Path(self.settings.state_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps({"last_open_week": self.last_open_week, "active_strategy_symbols": sorted(self.active_strategy_symbols), "active_strategy_sizes": self.active_strategy_sizes, "active_strategy_group_id": self.active_strategy_group_id, "rfq_state": self.rfq_state, "execution_groups": self.execution_groups, "execution_group_links": self.execution_group_links, "pm_baseline": self.pm_baseline}), encoding="utf-8")
        temp.replace(target)

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

    async def refresh_chain(self, force: bool = False) -> list:
        now = datetime.now(timezone.utc)
        if not force and self.chain and self.chain_updated_at and (now - self.chain_updated_at).total_seconds() < max(1, self.settings.market_refresh_seconds - 1):
            return self.chain
        async with self.refresh_lock:
            now = datetime.now(timezone.utc)
            if not force and self.chain and self.chain_updated_at and (now - self.chain_updated_at).total_seconds() < max(1, self.settings.market_refresh_seconds - 1):
                return self.chain
            try:
                instruments_stale = not self.raw_instruments or not self.instruments_updated_at or (now - self.instruments_updated_at).total_seconds() >= self.settings.instrument_refresh_seconds
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
                if len(valid_deltas) >= 8 and {item.option_type for item in valid_deltas} == {"Call", "Put"}:
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

    async def make_preview(self, quantity: float | None = None) -> StrategyPreview:
        await self.refresh_chain()
        multiplier = 1.0 if self.chain_source == "bybit" else 0.01
        now = datetime.now(timezone.utc)
        qty = quantity or self.settings.leg_qty
        self.preview = build_iron_condor(self.chain, now, self.settings.target_dte_days, qty, multiplier, self.settings.estimated_taker_fee_rate, self.settings.portfolio_margin_buffer_pct, self.btc_price or 0.0, self.settings.margin_mode, self.settings.option_mm_factor, self.settings.option_max_im_factor, self.settings.option_min_im_factor, self.settings.option_liquidation_fee_rate, self.settings.option_fee_cap_pct)
        self.preview.source = self.chain_source
        self.preview.market_timestamp = self.chain_updated_at
        self.preview.btc_price = self.btc_price
        if self.preview.max_loss_usd > self.settings.max_risk_usd:
            self.log("WARNING", f"Preview risk ${self.preview.max_loss_usd:.2f} exceeds limit ${self.settings.max_risk_usd:.2f}")
        else:
            self.log("INFO", f"Preview ready: credit ${self.preview.net_credit_usd:.2f}, max loss ${self.preview.max_loss_usd:.2f}")
        return self.preview

    async def follow_bbo_order(self, leg, qty: float, order_link_id: str, reduce_only: bool = False) -> dict:
        """Keep a limit order at the current BBO until filled or timeout."""
        instrument = next((item for item in self.chain if item.symbol == leg.symbol), None)
        if instrument is None:
            raise ValueError(f"Instrument disappeared from fresh market data: {leg.symbol}")
        response = None
        last_order_price: float | None = None
        remaining = qty
        deadline = asyncio.get_running_loop().time() + self.settings.bbo_order_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            latest = (await self.client.tickers(symbol=leg.symbol) or [{}])[0]
            bid = float(latest.get("bid1Price", 0) or 0)
            ask = float(latest.get("ask1Price", 0) or 0)
            price = bid if leg.side == "Buy" else ask
            if price <= 0:
                await asyncio.sleep(self.settings.bbo_poll_seconds)
                continue
            tick = instrument.price_tick or 0.01
            price = round(round(price / tick) * tick, 10)
            instrument.bid, instrument.ask, instrument.mark_price = bid, ask, float(latest.get("markPrice", instrument.mark_price) or instrument.mark_price)
            fills = await self.client.executions(order_link_id)
            filled = sum(float(item.get("execQty", 0) or 0) for item in fills)
            remaining = max(0.0, qty - filled)
            if remaining <= 1e-12:
                return {"orderId": response.get("orderId") if isinstance(response, dict) else "", "orderLinkId": order_link_id, "status": "filled"}
            if response is None:
                response = await self.client.place_limit_order(leg.symbol, leg.side, remaining, price, order_link_id, reduce_only)
                last_order_price = price
            elif last_order_price is None or abs(price - last_order_price) >= tick / 2:
                await self.client.amend_order(leg.symbol, order_link_id, price)
                last_order_price = price
            await asyncio.sleep(self.settings.bbo_poll_seconds)
        if response and remaining > 1e-12:
            try:
                await self.client.cancel_order(leg.symbol, order_link_id)
            except Exception as exc:
                self.log("ERROR", f"Could not cancel timed-out BBO order {order_link_id}: {exc}")
        # A fill can race with the cancel request. Reconcile one final time so
        # a partial position is never discarded from strategy tracking.
        try:
            final_fills = await self.client.executions(order_link_id)
            filled = sum(float(item.get("execQty", 0) or 0) for item in final_fills)
            if filled >= qty - 1e-12:
                return {"orderId": response.get("orderId") if isinstance(response, dict) else "", "orderLinkId": order_link_id, "status": "filled", "filledQty": qty}
            if filled > 1e-12:
                return {"orderId": response.get("orderId") if isinstance(response, dict) else "", "orderLinkId": order_link_id, "status": "partial", "filledQty": filled}
        except Exception as exc:
            self.log("WARNING", f"Could not reconcile timed-out order {order_link_id}: {exc}")
        return {"orderId": response.get("orderId") if isinstance(response, dict) else "", "orderLinkId": order_link_id, "status": "timeout_cancelled", "filledQty": 0.0}

    async def open_position(self, request: OpenRequest, scheduled: bool = False) -> list[OrderResult]:
        async with self.lock:
            qty = request.quantity or self.settings.leg_qty
            preview = await self.make_preview(qty)
            live = bool(request.confirm_live and self.settings.can_trade_live)
            if live:
                self._validate_open_calendar(preview.expiry)
            if live and scheduled and not self.is_open_window():
                raise ValueError(f"Live orders are only allowed during Friday {self.settings.open_hour_utc:02d}:{self.settings.open_minute_utc:02d} UTC window")
            if request.confirm_live and not self.settings.can_trade_live:
                self.log("WARNING", "Live confirmation received but live trading is disabled; using dry-run")
            if qty <= 0:
                raise ValueError("Quantity must be greater than zero")
            for leg in preview.legs:
                instrument = next((item for item in self.chain if item.symbol == leg.symbol), None)
                if instrument is None:
                    raise ValueError(f"Instrument disappeared from fresh market data: {leg.symbol}")
                if qty < instrument.min_qty or qty > instrument.max_qty:
                    raise ValueError(f"Quantity for {leg.symbol} must be between {instrument.min_qty} and {instrument.max_qty}")
                steps = round((qty - instrument.min_qty) / instrument.qty_step)
                if not isclose(instrument.min_qty + steps * instrument.qty_step, qty, rel_tol=0, abs_tol=1e-9):
                    raise ValueError(f"Quantity {qty} does not match Bybit step {instrument.qty_step} for {leg.symbol}")
                if live and (instrument.bid <= 0 or instrument.ask <= 0):
                    raise ValueError(f"No executable bid/ask for {leg.symbol}")
                spread_bps = (instrument.ask - instrument.bid) / instrument.mark_price * 10000 if instrument.mark_price > 0 else float("inf")
                if live and self.settings.max_spread_bps > 0 and spread_bps > self.settings.max_spread_bps:
                    raise ValueError(f"Spread for {leg.symbol} is {spread_bps:.0f} bps, above limit {self.settings.max_spread_bps:.0f} bps")
            if preview.estimated_margin_usd > self.settings.max_risk_usd:
                raise ValueError("Risk limit exceeded; reduce quantity or raise MAX_RISK_USD")
            if live and preview.source != "bybit":
                raise ValueError("Live orders require a fresh Bybit market snapshot")
            if live:
                await self._capture_pm_baseline("scheduled_open" if scheduled else "manual_open")
                request_id = uuid4().hex[:12]
                order_links = [f"ic-{request_id}-{index}" for index, _ in enumerate(preview.legs)]
                self.execution_groups[request_id] = {"type": "open", "created_at": datetime.now(timezone.utc).isoformat(), "legs": {leg.symbol: {"side": leg.side, "chain_price": (next(item for item in self.chain if item.symbol == leg.symbol).bid if leg.side == "Sell" else next(item for item in self.chain if item.symbol == leg.symbol).ask), "qty": qty} for leg in preview.legs}}
                for link in order_links:
                    self.execution_group_links[link] = request_id
                self._save_state()
                responses = await asyncio.gather(*[self.follow_bbo_order(leg, qty, order_links[index]) for index, leg in enumerate(preview.legs)], return_exceptions=True)
            else:
                order_links = [None] * len(preview.legs)
                responses = [None] * len(preview.legs)
            results = []
            successful_legs: list[tuple[object, float]] = []
            for leg, response, order_link_id in zip(preview.legs, responses, order_links):
                if isinstance(response, Exception) or (isinstance(response, dict) and response.get("status") == "timeout_cancelled"):
                    message = str(response) if isinstance(response, Exception) else "BBO order timed out and was cancelled"
                    results.append(OrderResult(symbol=leg.symbol, side=leg.side, qty=qty, status="error", message=message, order_link_id=order_link_id))
                    self.log("ERROR", f"Order failed for {leg.symbol}: {response}")
                else:
                    order_id = response.get("orderId") if isinstance(response, dict) else f"dry-{int(datetime.now().timestamp())}"
                    filled_qty = float(response.get("filledQty", qty) if isinstance(response, dict) else qty)
                    status = response.get("status") if isinstance(response, dict) else ("simulated" if not live else "submitted")
                    results.append(OrderResult(symbol=leg.symbol, side=leg.side, qty=filled_qty, status=status, order_id=order_id, order_link_id=order_link_id))
                    if filled_qty > 1e-12:
                        successful_legs.append((leg, filled_qty))
            if live and any(item.status == "error" for item in results) and self.settings.allow_market_fallback:
                # A request can time out after Bybit accepted it. Check positions
                # before retrying so an uncertain leg is never duplicated.
                failed_legs = [leg for leg, result in zip(preview.legs, results) if result.status == "error"]
                await asyncio.sleep(self.settings.failed_leg_retry_delay_seconds)
                retry_candidates = failed_legs
                for check_index in range(self.settings.failed_leg_position_checks):
                    try:
                        raw_positions = await self.client.positions()
                        position_sizes = {(item.get("symbol"), item.get("side")): abs(float(item.get("size", 0) or 0)) for item in raw_positions}
                        retry_candidates = [leg for leg in failed_legs if position_sizes.get((leg.symbol, leg.side), 0) < qty]
                        if not retry_candidates:
                            break
                    except Exception as exc:
                        self.log("WARNING", f"Position check {check_index + 1}/{self.settings.failed_leg_position_checks} failed: {exc}")
                    if check_index + 1 < self.settings.failed_leg_position_checks:
                        await asyncio.sleep(self.settings.failed_leg_position_check_interval_seconds)
                if retry_candidates:
                    market_id = uuid4().hex[:12]
                    retry_links = [f"ic-mkt-{market_id}-{index}" for index, _ in enumerate(retry_candidates)]
                    for link in retry_links:
                        self.execution_group_links[link] = request_id
                    self._save_state()
                    retry_responses = await asyncio.gather(*[self.client.place_market_order(leg.symbol, leg.side, qty, retry_links[index]) for index, leg in enumerate(retry_candidates)], return_exceptions=True)
                    for leg, response, retry_link in zip(retry_candidates, retry_responses, retry_links):
                        result = next(item for item in results if item.symbol == leg.symbol)
                        result.order_link_id = retry_link
                        if isinstance(response, Exception):
                            result.message = f"BBO timeout and market fallback failed: {response}"
                        else:
                            result.status = "market_submitted"
                            result.order_id = response.get("orderId") if isinstance(response, dict) else result.order_id
                            successful_legs.append((leg, qty))
                    self.log("WARNING", f"Submitted market fallback for {len(retry_candidates)} missing leg(s) after BBO timeout")
                else:
                    self.log("WARNING", "Failed leg(s) already existed in current positions; no duplicate retry sent")
            if live:
                self.last_executions = await self.load_recent_executions([item.order_link_id for item in results if item.order_link_id])
                self._attach_execution_details(results)
            if live and any(item.status == "partial" for item in results):
                self.log("ERROR", "One or more live limit orders partially filled before cancellation; verify the tracked quantities and complete the strategy manually.")
            elif live and any(item.status == "error" for item in results):
                self.log("ERROR", "One or more live limit orders failed; market fallback is disabled. Verify positions and handle missing legs manually.")
            elif live:
                self.log("INFO", "All four live legs were accepted through BBO limit orders; final fills are asynchronous on Bybit.")
            else:
                self.positions.extend([Position(symbol=leg.symbol, side=leg.side, size=filled_qty, avg_price=leg.mark_price, mark_price=leg.mark_price, unrealised_pnl=0, source="demo") for leg, filled_qty in successful_legs])
            if live and successful_legs:
                self.active_strategy_symbols = {leg.symbol for leg, _ in successful_legs}
                self.active_strategy_sizes = {f"{leg.symbol}|{leg.side}": filled_qty for leg, filled_qty in successful_legs}
                self.active_strategy_group_id = request_id
                self._save_state()
            if scheduled:
                self.last_open_week = datetime.now(timezone.utc).strftime("%G-W%V")
                self._save_state()
            self.log("INFO", f"Iron Condor {'submitted to Bybit' if live else 'simulated'} with {len(results)} market legs")
            return results

    async def create_rfq(self, request: RfqCreateRequest) -> dict:
        if not self.settings.bybit_api_key or not self.settings.bybit_api_secret:
            raise ValueError("Bybit API credentials are not configured")
        counterparties = list(request.counterparties)
        rfq_config = {}
        try:
            rfq_config = await self.client.rfq_config()
        except Exception as exc:
            if not counterparties:
                raise
            self.log("WARNING", f"Could not load RFQ config; using specified counterparties: {exc}")
        strategy_type = "custom"
        for item in rfq_config.get("strategyTypes") or []:
            name = str(item.get("strategyName", "") if isinstance(item, dict) else item)
            normalized = name.replace(" ", "").replace("_", "").lower()
            if "ironcondor" in normalized:
                strategy_type = name
                break
        if not counterparties:
            available = rfq_config.get("counterparties") or []
            counterparties = [item.get("deskCode") if isinstance(item, dict) else str(item) for item in available]
            counterparties = [item for item in counterparties if item]
            max_lp = int(rfq_config.get("maxLP") or len(counterparties) or 0)
            counterparties = counterparties[:max_lp] if max_lp else counterparties
        if not counterparties:
            raise ValueError("Bybit returned no available RFQ counterparties")
        preview = await self.make_preview(request.quantity or self.settings.leg_qty)
        if preview.source != "bybit":
            raise ValueError("RFQ requires a fresh Bybit market snapshot")
        qty = request.quantity or self.settings.leg_qty
        legs = [{"category": "option", "symbol": leg.symbol, "side": leg.side, "qty": str(qty)} for leg in preview.legs]
        # Bybit RFQ link IDs allow letters and numbers only.
        rfq_link_id = f"icrfq{uuid4().hex[:16]}"
        result = await self.client.create_rfq(counterparties, legs, rfq_link_id, strategy_type)
        self.rfq_state = {"rfq_id": result.get("rfqId", ""), "rfq_link_id": result.get("rfqLinkId", rfq_link_id), "strategy_type": strategy_type, "status": result.get("status", "Active"), "expires_at": result.get("expiresAt"), "counterparties": counterparties, "legs": legs, "quotes": [], "updated_at": datetime.now(timezone.utc).isoformat()}
        self._save_state()
        self.log("INFO", f"RFQ created: {self.rfq_state['rfq_id']}")
        return self.rfq_state

    async def refresh_rfq(self) -> dict:
        if not self.rfq_state.get("rfq_id"):
            return self.rfq_state
        rfq_id = self.rfq_state["rfq_id"]
        rfqs, quotes = await asyncio.gather(self.client.rfq_realtime(rfq_id), self.client.quote_realtime(rfq_id))
        if rfqs:
            self.rfq_state.update({"status": rfqs[0].get("status", self.rfq_state.get("status")), "expires_at": rfqs[0].get("expiresAt", self.rfq_state.get("expires_at"))})
        if self.rfq_state.get("status") == "Filled":
            self._track_filled_rfq()
        self.rfq_state["quotes"] = quotes
        self.rfq_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state()
        return self.rfq_state

    async def execute_rfq(self, request: RfqExecuteRequest) -> dict:
        if not request.confirm_live:
            raise ValueError("RFQ execution requires explicit confirmation")
        if request.quote_side != "Sell":
            raise ValueError("This Iron Condor workflow only accepts the Sell quote direction")
        if request.rfq_id != self.rfq_state.get("rfq_id"):
            raise ValueError("RFQ is not the active inquiry")
        quote = next((item for item in self.rfq_state.get("quotes", []) if item.get("quoteId") == request.quote_id), None)
        if quote is None:
            raise ValueError("Quote is not available or has expired")
        expiries = {item.expiry for leg in self.rfq_state.get("legs", []) for item in self.chain if item.symbol == leg.get("symbol")}
        if len(expiries) != 1:
            raise ValueError("RFQ legs must resolve to one common expiry")
        self._validate_open_calendar(expiries.pop())
        await self._capture_pm_baseline("rfq_open")
        result = await self.client.execute_quote(request.rfq_id, request.quote_id, request.quote_side)
        self.rfq_state.update({"status": result.get("status", "PendingFill"), "selected_quote_id": request.quote_id, "selected_quote_side": request.quote_side, "updated_at": datetime.now(timezone.utc).isoformat()})
        self._save_state()
        self.log("INFO", f"RFQ quote execution submitted: {request.quote_id}")
        return {**self.rfq_state, "execution": result}

    def _track_filled_rfq(self, positions: list[Position] | None = None) -> bool:
        legs = self.rfq_state.get("legs") or []
        execution_submitted = bool(self.rfq_state.get("selected_quote_id")) or self.rfq_state.get("status") == "Filled"
        if not execution_submitted or len(legs) != 4 or len({leg.get("symbol") for leg in legs}) != 4:
            return False
        position_map = {(position.symbol, position.side): position for position in (positions or []) if position.size > 0}
        if positions is not None and any((leg.get("symbol"), leg.get("side")) not in position_map for leg in legs):
            return False
        sizes: dict[str, float] = {}
        for leg in legs:
            symbol = str(leg.get("symbol", ""))
            side = str(leg.get("side", ""))
            requested_qty = float(leg.get("qty", 0) or 0)
            if not symbol or side not in {"Buy", "Sell"} or requested_qty <= 0:
                return False
            tracked_qty = min(requested_qty, position_map[(symbol, side)].size) if positions is not None else requested_qty
            sizes[f"{symbol}|{side}"] = tracked_qty
        self.active_strategy_symbols = {str(leg["symbol"]) for leg in legs}
        self.active_strategy_sizes = sizes
        self.active_strategy_group_id = f"rfq:{self.rfq_state.get('rfq_id', '')}"
        self._save_state()
        return True

    def _recover_tracked_open_positions(self, positions: list[Position]) -> bool:
        position_map = {(position.symbol, position.side): position for position in positions if position.size > 0}
        candidates: list[tuple[str, str, list[dict]]] = []
        for group_id, group in self.execution_groups.items():
            if group.get("type") != "open" or group.get("status") == "closed":
                continue
            legs = [{"symbol": symbol, **details} for symbol, details in (group.get("legs") or {}).items()]
            candidates.append((str(group.get("created_at", "")), group_id, legs))
        execution_groups: dict[str, dict[tuple[str, str], float]] = {}
        execution_times: dict[str, str] = {}
        for execution in self.last_executions:
            parts = execution.order_link_id.split("-")
            if execution.reduce_only or len(parts) < 3 or parts[0] != "ic" or parts[1] in {"close", "mkt"}:
                continue
            group_id = execution.execution_group or parts[1]
            key = (execution.symbol, execution.side)
            bucket = execution_groups.setdefault(group_id, {})
            bucket[key] = bucket.get(key, 0.0) + execution.exec_qty
            execution_times[group_id] = max(execution_times.get(group_id, ""), execution.exec_time.isoformat())
        for group_id, legs in execution_groups.items():
            candidates.append((execution_times.get(group_id, ""), group_id, [{"symbol": symbol, "side": side, "qty": qty} for (symbol, side), qty in legs.items()]))
        for _, group_id, legs in sorted(candidates, reverse=True):
            if len(legs) != 4 or len({leg.get("symbol") for leg in legs}) != 4:
                continue
            if any((leg.get("symbol"), leg.get("side")) not in position_map for leg in legs):
                continue
            sizes = {}
            for leg in legs:
                symbol, side = str(leg["symbol"]), str(leg["side"])
                requested_qty = float(leg.get("qty", 0) or 0)
                if requested_qty <= 0:
                    break
                sizes[f"{symbol}|{side}"] = min(requested_qty, position_map[(symbol, side)].size)
            if len(sizes) != 4:
                continue
            self.active_strategy_symbols = {str(leg["symbol"]) for leg in legs}
            self.active_strategy_sizes = sizes
            self.active_strategy_group_id = group_id
            self._save_state()
            return True
        return False

    async def cancel_rfq(self, request: RfqCancelRequest) -> dict:
        if request.rfq_id != self.rfq_state.get("rfq_id"):
            raise ValueError("RFQ is not the active inquiry")
        result = await self.client.cancel_rfq(request.rfq_id)
        canceled_id = request.rfq_id
        self.rfq_state = {"status": "Canceled", "canceled_rfq_id": canceled_id, "quotes": [], "updated_at": datetime.now(timezone.utc).isoformat()}
        self._save_state()
        self.log("INFO", f"RFQ canceled: {canceled_id}")
        return {**self.rfq_state, "cancellation": result}

    def _attach_execution_details(self, results: list[OrderResult]) -> None:
        by_link: dict[str, list[ExecutionRecord]] = {}
        for execution in self.last_executions:
            by_link.setdefault(execution.order_link_id, []).append(execution)
        for result in results:
            executions = by_link.get(result.order_link_id or "", [])
            if not executions:
                continue
            result.exec_fee = round(sum(item.exec_fee for item in executions), 8)
            result.fee_currency = executions[0].fee_currency
            result.exec_qty = round(sum(item.exec_qty for item in executions), 8)
            result.exec_price = round(sum(item.exec_price * item.exec_qty for item in executions) / result.exec_qty, 8) if result.exec_qty else None
            result.execution_id = executions[-1].exec_id
            result.exec_time = executions[-1].exec_time

    async def load_recent_executions(self, order_link_ids: list[str] | None = None) -> list[ExecutionRecord]:
        if not self.settings.bybit_api_key or not self.settings.bybit_api_secret:
            return self.last_executions
        try:
            if order_link_ids:
                raw_groups = await asyncio.gather(*[self.client.executions(order_link_id) for order_link_id in set(order_link_ids)], return_exceptions=True)
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
                baseline = (self.execution_groups.get(group_id or "") or {}).get("legs", {}).get(item.get("symbol", ""), {})
                exec_price = float(item.get("execPrice", 0) or 0)
                exec_qty = float(item.get("execQty", 0) or 0)
                chain_price = float(baseline.get("chain_price", 0) or 0) if baseline else None
                strategy_side = baseline.get("side")
                chain_diff = ((1 if strategy_side == "Sell" else -1) * (exec_price - chain_price) * exec_qty) if chain_price is not None and strategy_side else None
                records.append(ExecutionRecord(symbol=item.get("symbol", ""), side=item.get("side", ""), order_id=item.get("orderId", ""), order_link_id=order_link_id, exec_id=exec_id, exec_fee=float(item.get("execFee", 0) or 0), fee_currency=item.get("feeCurrency", ""), exec_price=exec_price, exec_qty=exec_qty, fee_rate=float(item.get("feeRate", 0) or 0) if item.get("feeRate") not in (None, "") else None, exec_time=datetime.fromtimestamp(int(item.get("execTime", 0)) / 1000, tz=timezone.utc), execution_group=group_id, chain_price_at_create=chain_price, chain_price_diff=chain_diff))
            self.last_executions = sorted(records, key=lambda item: item.exec_time, reverse=True)[:100]
        except Exception as exc:
            self.log("WARNING", f"Could not load execution fee records: {exc}")
        return self.last_executions

    async def close_position(self, request: CloseRequest) -> tuple[list[OrderResult], list[ExecutionRecord]]:
        async with self.lock:
            live = bool(request.confirm_live and self.settings.can_trade_live)
            if live:
                raw_positions = await self.client.positions()
                current = [Position(symbol=item.get("symbol", ""), side=item.get("side", ""), size=float(item.get("size", 0) or 0), avg_price=float(item.get("avgPrice", 0) or 0), mark_price=float(item.get("markPrice", 0) or 0), unrealised_pnl=float(item.get("unrealisedPnl", 0) or 0), source="bybit") for item in raw_positions if float(item.get("size", 0) or 0) > 0]
                if len(self.active_strategy_symbols) < 4:
                    recovered = self._track_filled_rfq(current)
                    if not recovered:
                        await self.load_recent_executions()
                        recovered = self._recover_tracked_open_positions(current)
                    if recovered:
                        self.log("INFO", "Recovered tracked Iron Condor legs from the opening task and current Bybit positions")
            else:
                preview = self.preview or await self.make_preview()
                current = list(self.positions)
            if live and not self.active_strategy_symbols:
                raise ValueError("No tracked live Iron Condor legs found; refusing to close untracked positions")
            symbols = self.active_strategy_symbols if live else (self.active_strategy_symbols or {leg.symbol for leg in preview.legs})
            if len(symbols) > 4:
                raise ValueError("Tracked strategy contains more than four symbols; refusing bulk close")
            current = [position for position in current if position.symbol in symbols and position.size > 0]
            if live:
                current = [position.model_copy(update={"size": min(position.size, self.active_strategy_sizes.get(f"{position.symbol}|{position.side}", 0.0))}) for position in current if self.active_strategy_sizes.get(f"{position.symbol}|{position.side}", 0.0) > 0]
            if not current:
                raise ValueError("No open Iron Condor legs found to close")
            close_group_id = uuid4().hex[:12]
            links = [f"ic-close-{close_group_id}-{index}" for index, _ in enumerate(current)]
            if live:
                close_legs = [type("CloseLeg", (), {"symbol": position.symbol, "side": "Sell" if position.side == "Buy" else "Buy"})() for position in current]
                responses = await asyncio.gather(*[self.follow_bbo_order(close_legs[index], position.size, links[index], reduce_only=True) for index, position in enumerate(current)], return_exceptions=True)
            else:
                responses = [None] * len(current)
            results = []
            for position, response, link in zip(current, responses, links):
                if isinstance(response, Exception) or (isinstance(response, dict) and response.get("status") == "timeout_cancelled"):
                    message = str(response) if isinstance(response, Exception) else "BBO close order timed out and was cancelled"
                    results.append(OrderResult(symbol=position.symbol, side="Sell" if position.side == "Buy" else "Buy", qty=position.size, status="error", message=message, order_link_id=link))
                else:
                    filled_qty = float(response.get("filledQty", position.size) if isinstance(response, dict) else position.size)
                    results.append(OrderResult(symbol=position.symbol, side="Sell" if position.side == "Buy" else "Buy", qty=filled_qty, status=response.get("status") if isinstance(response, dict) else ("submitted" if live else "simulated"), order_id=response.get("orderId") if isinstance(response, dict) else f"dry-close-{int(datetime.now().timestamp())}", order_link_id=link))
            if live:
                await asyncio.sleep(self.settings.failed_leg_retry_delay_seconds)
                self.last_executions = await self.load_recent_executions([item.order_link_id for item in results if item.order_link_id])
                for item in self.last_executions:
                    item.reduce_only = True
                self._attach_execution_details(results)
            else:
                self.positions = [position for position in self.positions if position not in current]
                self.last_executions = []
            if not any(item.status in {"error", "partial"} for item in results):
                completed_group_id = self.active_strategy_group_id
                self.active_strategy_symbols.difference_update(position.symbol for position in current)
                for position in current:
                    self.active_strategy_sizes.pop(f"{position.symbol}|{position.side}", None)
                if completed_group_id in self.execution_groups:
                    self.execution_groups[completed_group_id]["status"] = "closed"
                if not self.active_strategy_symbols:
                    self.active_strategy_group_id = None
                    self.pm_baseline = {}
                self._save_state()
            elif live:
                for position, result in zip(current, results):
                    if result.status == "partial":
                        key = f"{position.symbol}|{position.side}"
                        self.active_strategy_sizes[key] = max(0.0, self.active_strategy_sizes.get(key, position.size) - result.qty)
                self._save_state()
            return results, self.last_executions

    async def scheduler(self) -> None:
        while True:
            now = datetime.now(timezone.utc)
            week = now.strftime("%G-W%V")
            if self.settings.auto_open and self.is_open_window(now) and self.last_open_week != week:
                try:
                    await self.make_preview()
                    await self.open_position(OpenRequest(confirm_live=self.settings.can_trade_live), scheduled=True)
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

    async def load_positions(self) -> list[Position]:
        if self.settings.can_trade_live:
            try:
                raw = await self.client.positions()
                self.positions = [Position(symbol=item.get("symbol", ""), side=item.get("side", ""), size=float(item.get("size", 0)), avg_price=float(item.get("avgPrice", 0) or 0), mark_price=float(item.get("markPrice", 0) or 0), unrealised_pnl=float(item.get("unrealisedPnl", 0) or 0), source="bybit") for item in raw if float(item.get("size", 0) or 0) > 0]
                if len(self.active_strategy_symbols) < 4 and (self._track_filled_rfq(self.positions) or self._recover_tracked_open_positions(self.positions)):
                    self.log("INFO", "Recovered tracked Iron Condor legs from the opening task and current Bybit positions")
            except BybitError as exc:
                self.log("WARNING", f"Could not load private positions: {exc}")
        return self.positions

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
