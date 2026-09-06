import asyncio
import json
from datetime import datetime, timedelta, timezone
from math import isclose, isfinite
from pathlib import Path
from uuid import uuid4

from .bybit import BybitClient, BybitError
from .config import Settings
from .models import AccountHealth, CloseRequest, ExecutionRecord, LogEntry, OpenRequest, OrderResult, Position, RfqCancelRequest, RfqCreateRequest, RfqExecuteRequest, StrategyPreview
from .strategy import build_iron_condor
from .orders import OrderExecutor


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
        self.order_journal: dict[str, dict] = {}
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
            self.order_journal = state.get("order_journal") or {}
        except (FileNotFoundError, OSError, ValueError):
            self.last_open_week = None

    def _save_state(self) -> None:
        target = Path(self.settings.state_file)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(json.dumps({"last_open_week": self.last_open_week, "active_strategy_symbols": sorted(self.active_strategy_symbols), "active_strategy_sizes": self.active_strategy_sizes, "active_strategy_group_id": self.active_strategy_group_id, "rfq_state": self.rfq_state, "execution_groups": self.execution_groups, "execution_group_links": self.execution_group_links, "pm_baseline": self.pm_baseline, "order_journal": self.order_journal}), encoding="utf-8")
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
        qty = self.settings.leg_qty if quantity is None else quantity
        self.preview = build_iron_condor(self.chain, now, self.settings.target_dte_days, qty, multiplier, self.settings.estimated_taker_fee_rate, self.settings.portfolio_margin_buffer_pct, self.btc_price or 0.0, self.settings.margin_mode, self.settings.option_mm_factor, self.settings.option_max_im_factor, self.settings.option_min_im_factor, self.settings.option_liquidation_fee_rate, self.settings.option_fee_cap_pct)
        self.preview.source = self.chain_source
        self.preview.market_timestamp = self.chain_updated_at
        self.preview.btc_price = self.btc_price
        if self.preview.max_loss_usd > self.settings.max_risk_usd:
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
        metrics = (preview.max_loss_usd, preview.estimated_margin_usd)
        if any(not isfinite(value) or value > self.settings.max_risk_usd for value in metrics):
            raise ValueError("Risk limit exceeded; reduce quantity or raise MAX_RISK_USD")

    def _record_order(self, link: str, outcome: dict) -> None:
        entry = self.order_journal[link]
        previous_filled = float(entry.get("filledQty", 0))
        if float(outcome.get("filledQty", 0)) < previous_filled:
            outcome = {**outcome, "filledQty": previous_filled}
        if all(entry.get(key) == value for key, value in outcome.items()):
            return
        entry.update(outcome)
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
        instrument = next((item for item in self.chain if item.symbol == leg.symbol), None)
        if instrument is None:
            raise ValueError(f"Instrument disappeared from fresh market data: {leg.symbol}")
        if link in self.order_journal:
            raise ValueError("Order link has already been used; reconcile it instead of resubmitting")
        self.order_journal[link] = {"symbol": leg.symbol, "side": leg.side, "qty": qty,
                                    "reduce_only": reduce_only, "status": "unknown", "terminal": False, "filledQty": 0.0}
        self._save_state()
        executor = OrderExecutor(self.client, self.settings, self.log)
        return await executor.execute(instrument, leg.side, qty, link, lambda outcome: self._record_order(link, outcome), reduce_only, market)

    async def follow_bbo_order(self, leg, qty: float, order_link_id: str, reduce_only: bool = False) -> dict:
        return await self._execute_order(leg, qty, order_link_id, reduce_only)

    async def _reconcile_pending_orders(self) -> None:
        executor = OrderExecutor(self.client, self.settings, self.log)
        async def reconcile(link, entry):
            outcome = await executor.reconcile(entry["symbol"], entry["side"], entry["qty"], link, entry)
            self._record_order(link, outcome)
        await asyncio.gather(*(reconcile(link, dict(entry)) for link, entry in list(self.order_journal.items()) if not entry.get("terminal")))
        if any(not entry.get("terminal") for entry in self.order_journal.values()):
            raise ValueError("Unresolved orders remain; verify exchange orders before trading again")

    async def open_position(self, request: OpenRequest, scheduled: bool = False) -> list[OrderResult]:
        async with self.lock:
            if self.settings.can_trade_live and not request.confirm_live:
                raise ValueError("Live opening requires explicit confirmation")
            qty = request.quantity or self.settings.leg_qty
            preview = await self.make_preview(qty)
            live = bool(request.confirm_live and self.settings.can_trade_live)
            if live:
                await self._reconcile_pending_orders()
                if self.active_strategy_symbols:
                    raise ValueError("Close the tracked strategy before opening another")
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
            self._validate_risk(preview)
            if live:
                self._validate_market_snapshot()
                await self._capture_pm_baseline("scheduled_open" if scheduled else "manual_open")
                self._validate_market_snapshot()
                request_id = uuid4().hex[:12]
                order_links = [f"ic-{request_id}-{index}" for index, _ in enumerate(preview.legs)]
                self.execution_groups[request_id] = {"type": "open", "order_tracking": True, "created_at": datetime.now(timezone.utc).isoformat(), "legs": {leg.symbol: {"side": leg.side, "chain_price": (next(item for item in self.chain if item.symbol == leg.symbol).bid if leg.side == "Sell" else next(item for item in self.chain if item.symbol == leg.symbol).ask), "qty": qty} for leg in preview.legs}}
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
                    self.log("INFO", "All four live legs are confirmed filled")
            else:
                self.log("INFO", "All four legs were simulated")
            if scheduled:
                self.last_open_week = datetime.now(timezone.utc).strftime("%G-W%V")
                self._save_state()
            self.log("INFO", f"Iron Condor {'processed on Bybit' if live else 'simulated'} with {len(results)} legs")
            return results

    async def _market_fallback(self, legs, qty, links, results, group_id, all_links) -> None:
        executor = OrderExecutor(self.client, self.settings, self.log)
        await asyncio.sleep(self.settings.failed_leg_retry_delay_seconds)
        for leg, link, result in zip(legs, links, results):
            original = self.order_journal.get(link)
            if not original or not original.get("terminal") or original.get("status") not in {"partial", "timeout_cancelled"}:
                continue
            # Re-read the original order, even if cancellation was previously
            # confirmed. Neither account holdings nor a missing order proves it safe.
            confirmed = await executor.reconcile(leg.symbol, leg.side, qty, link, original, cancel=False)
            self._record_order(link, confirmed)
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

    async def create_rfq(self, request: RfqCreateRequest) -> dict:
        async with self.lock:
            return await self._create_rfq(request)

    async def _create_rfq(self, request: RfqCreateRequest) -> dict:
        if self.rfq_state.get("rfq_id") and self.rfq_state.get("status") not in {"Canceled", "Expired", "Filled", "Failed"}:
            raise ValueError("An RFQ is already active; resolve it before creating another")
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
        self._validate_market_snapshot()
        self._validate_risk(preview)
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
        async with self.lock:
            return await self._refresh_rfq()

    async def _refresh_rfq(self) -> dict:
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
        async with self.lock:
            return await self._execute_rfq(request)

    async def _execute_rfq(self, request: RfqExecuteRequest) -> dict:
        if not self.settings.can_trade_live:
            raise ValueError("Live trading is disabled")
        if not request.confirm_live:
            raise ValueError("RFQ execution requires explicit confirmation")
        if request.quote_side != "Sell":
            raise ValueError("This Iron Condor workflow only accepts the Sell quote direction")
        if request.rfq_id != self.rfq_state.get("rfq_id"):
            raise ValueError("RFQ is not the active inquiry")
        if self.rfq_state.get("selected_quote_id") or self.rfq_state.get("status") in {"Filled", "PendingFill", "Canceled", "Expired", "Failed", "ExecutionUnknown"}:
            raise ValueError("RFQ is no longer available for execution")
        await self._reconcile_pending_orders()
        if self.active_strategy_symbols:
            raise ValueError("Close the tracked strategy before opening another")
        await self.refresh_chain()
        self._validate_market_snapshot()
        self._validate_rfq_quote(request)
        await self._capture_pm_baseline("rfq_open")
        self._validate_market_snapshot()
        self._validate_rfq_quote(request)
        # Persist the intent before submitting: an ambiguous network failure
        # must not permit a second execution of the same inquiry.
        self.rfq_state.update({"status": "ExecutionUnknown", "selected_quote_id": request.quote_id, "selected_quote_side": request.quote_side})
        self._save_state()
        result = await self.client.execute_quote(request.rfq_id, request.quote_id, request.quote_side)
        self.rfq_state.update({"status": result.get("status", "PendingFill"), "selected_quote_id": request.quote_id, "selected_quote_side": request.quote_side, "updated_at": datetime.now(timezone.utc).isoformat()})
        self._save_state()
        self.log("INFO", f"RFQ quote execution submitted: {request.quote_id}")
        return {**self.rfq_state, "execution": result}

    def _validate_rfq_quote(self, request: RfqExecuteRequest) -> None:
        quote = next((item for item in self.rfq_state.get("quotes", []) if item.get("quoteId") == request.quote_id), None)
        if quote is None:
            raise ValueError("Quote is not available or has expired")
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        for deadline in (quote.get("expiresAt"), self.rfq_state.get("expires_at")):
            if deadline not in (None, ""):
                expiry_ms = float(deadline)
                if not isfinite(expiry_ms) or expiry_ms <= now_ms:
                    raise ValueError("Quote is not available or has expired")
        legs = self.rfq_state.get("legs") or []
        quoted_legs = quote.get("quoteSellList") or []
        requested = {leg.get("symbol"): leg for leg in legs}
        quoted = {leg.get("symbol"): leg for leg in quoted_legs}
        if len(legs) != 4 or len(requested) != 4 or len(quoted_legs) != 4 or set(requested) != set(quoted):
            raise ValueError("Quote must cover exactly the four requested legs")
        for symbol, leg in requested.items():
            quantity = float(quoted[symbol].get("qty", 0) or 0)
            price = float(quoted[symbol].get("price", 0) or 0)
            if not isfinite(quantity) or quantity <= 0 or not isclose(quantity, float(leg["qty"]), rel_tol=0, abs_tol=1e-9) or not isfinite(price) or price <= 0:
                raise ValueError("Quote has an invalid price or mismatched quantity")
        instruments = {item.symbol: item for item in self.chain if item.symbol in requested}
        if set(instruments) != set(requested):
            raise ValueError("RFQ instruments are missing from market data")
        roles = {(instruments[symbol].option_type, leg.get("side")): instruments[symbol].strike for symbol, leg in requested.items()}
        if set(roles) != {("Call", "Buy"), ("Call", "Sell"), ("Put", "Buy"), ("Put", "Sell")} or len({float(leg["qty"]) for leg in legs}) != 1:
            raise ValueError("RFQ must contain four balanced Iron Condor legs")
        if not roles[("Put", "Buy")] < roles[("Put", "Sell")] < roles[("Call", "Sell")] < roles[("Call", "Buy")]:
            raise ValueError("RFQ Iron Condor strikes are not ordered correctly")
        expiries = {item.expiry for item in instruments.values()}
        if len(expiries) != 1:
            raise ValueError("RFQ legs must resolve to one common expiry")
        self._validate_open_calendar(expiries.pop())
        # A piecewise-linear expiry payoff reaches its minimum at a strike
        # or an outer boundary. Evaluate the actual quoted legs and prices.
        credit = sum((1 if leg["side"] == "Sell" else -1) * float(quoted[symbol]["price"]) * float(leg["qty"]) for symbol, leg in requested.items())
        payoffs = []
        for spot in [0.0, *(item.strike for item in instruments.values())]:
            payoff = credit
            for symbol, leg in requested.items():
                item = instruments[symbol]
                intrinsic = max(spot - item.strike, 0) if item.option_type == "Call" else max(item.strike - spot, 0)
                payoff += (1 if leg["side"] == "Buy" else -1) * intrinsic * float(leg["qty"])
            payoffs.append(payoff)
        if max(0.0, -min(payoffs)) > self.settings.max_risk_usd:
            raise ValueError("RFQ quote exceeds the maximum loss limit")

    def _track_filled_rfq(self, positions: list[Position] | None = None) -> bool:
        if self.execution_groups.get(self.active_strategy_group_id, {}).get("order_tracking"):
            return False
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
        if self.execution_groups.get(self.active_strategy_group_id, {}).get("order_tracking"):
            return False
        position_map = {(position.symbol, position.side): position for position in positions if position.size > 0}
        candidates: list[tuple[str, str, list[dict]]] = []
        for group_id, group in self.execution_groups.items():
            if group.get("type") != "open" or group.get("status") == "closed" or group.get("order_tracking"):
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
            if self.execution_groups.get(group_id, {}).get("order_tracking"):
                continue
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
        async with self.lock:
            return await self._cancel_rfq(request)

    async def _cancel_rfq(self, request: RfqCancelRequest) -> dict:
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
            links = result.related_order_link_ids or [result.order_link_id or ""]
            executions = [item for link in set(links) for item in by_link.get(link, [])]
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
                raw_groups = await asyncio.gather(*[self.client.executions(order_link_id) for order_link_id in set(order_link_ids)], return_exceptions=True)
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
                records.append(ExecutionRecord(symbol=item.get("symbol", ""), side=item.get("side", ""), order_id=item.get("orderId", ""), order_link_id=order_link_id, exec_id=exec_id, exec_fee=float(item.get("execFee", 0) or 0), fee_currency=item.get("feeCurrency", ""), exec_price=exec_price, exec_qty=exec_qty, fee_rate=float(item.get("feeRate", 0) or 0) if item.get("feeRate") not in (None, "") else None, exec_time=datetime.fromtimestamp(int(item.get("execTime", 0)) / 1000, tz=timezone.utc), reduce_only=reduce_only, opening_group=group.get("opening_group"), execution_group=group_id, chain_price_at_create=chain_price, chain_price_diff=chain_diff))
            merged = {item.exec_id: item for item in self.last_executions}
            merged.update({item.exec_id: item for item in records})
            self.last_executions = sorted(merged.values(), key=lambda item: item.exec_time, reverse=True)[:100]
        except Exception as exc:
            self.log("WARNING", f"Could not load execution fee records: {exc}")
        return self.last_executions

    async def close_position(self, request: CloseRequest) -> tuple[list[OrderResult], list[ExecutionRecord]]:
        async with self.lock:
            if self.settings.can_trade_live and not request.confirm_live:
                raise ValueError("Live closing requires explicit confirmation")
            live = bool(request.confirm_live and self.settings.can_trade_live)
            if live:
                await self._reconcile_pending_orders()
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
                current = [position for position in self.positions if position.source == "demo"]
            if live and not self.active_strategy_symbols:
                raise ValueError("No tracked live Iron Condor legs found; refusing to close untracked positions")
            symbols = self.active_strategy_symbols if live else {leg.symbol for leg in preview.legs}
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
                self.execution_groups[close_group_id] = {"type": "close", "opening_group": self.active_strategy_group_id, "order_tracking": True}
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
            if not self.lock.locked() and any(not entry.get("terminal") for entry in self.order_journal.values()):
                async with self.lock:
                    try:
                        await self._reconcile_pending_orders()
                    except ValueError as exc:
                        self.log("WARNING", str(exc))
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
