import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from math import isclose, isfinite
from uuid import uuid4

from .models import Position, RfqCreateRequest, RfqExecuteRequest, RfqCancelRequest
from .risk import leg_count, maximum_loss, strategy_mode as validate_mode, validate_structure


RFQ_TERMINAL_STATUSES = {"Canceled", "Expired", "Filled", "Failed"}


class RfqMixin:
    """RFQ lifecycle; public mutations share the engine transaction lock."""

    def _rfq_unresolved(self, state: dict | None = None) -> bool:
        state = self.rfq_state if state is None else state
        if state.get("status") in {"ExecutionUnknown", "PendingFill", "CreationUnknown", "CancelUnknown"}:
            return True
        if state.get("status") == "Filled":
            return not state.get("tracking_applied", False)
        return bool(state.get("selected_quote_id") and not state.get("execution_resolved", False))

    def _require_resolved_rfq(self) -> None:
        if self._rfq_unresolved():
            raise ValueError("Unresolved RFQ remains; wait for exchange reconciliation before trading")

    async def create_rfq(self, request: RfqCreateRequest) -> dict:
        self._admit_trading_operation()
        try:
            async with self.lock:
                return await self._create_rfq(request)
        finally:
            self._trade_admitting = False

    async def _create_rfq(self, request: RfqCreateRequest) -> dict:
        self._require_trading_state()
        self._require_resolved_rfq()
        if self.rfq_state.get("rfq_id") and self.rfq_state.get("status") not in {"Canceled", "Expired", "Filled", "Failed"}:
            raise ValueError("An RFQ is already active; resolve it before creating another")
        if not self.settings.bybit_api_key or not self.settings.bybit_api_secret:
            raise ValueError("Bybit API credentials are not configured")
        counterparties = list(request.counterparties)
        mode = validate_mode(request.strategy_mode or self.settings.strategy_mode)

        async def load_config():
            try:
                return await self.client.rfq_config()
            except Exception as exc:
                if not counterparties:
                    raise
                self.log("WARNING", f"Could not load RFQ config; using specified counterparties: {exc}")
                return {}

        reads = [asyncio.create_task(load_config()),
                 asyncio.create_task(self.make_preview(request.quantity or self.settings.leg_qty, strategy_mode=mode))]
        try:
            rfq_config, preview = await asyncio.gather(*reads)
        except BaseException:
            # A failed or canceled request must leave no preview worker behind
            # after the transaction lock is released.
            for read in reads:
                read.cancel()
            await asyncio.gather(*reads, return_exceptions=True)
            raise
        strategy_type = "custom"
        if mode == "iron_condor":
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
        self._require_trading_state()
        self._require_resolved_rfq()
        if preview.strategy_mode != mode:
            raise ValueError("Preview strategy mode does not match the RFQ request")
        self._validate_market_snapshot()
        self._validate_risk(preview)
        qty = request.quantity or self.settings.leg_qty
        if any(not isclose(leg.qty, qty, rel_tol=0, abs_tol=1e-9) for leg in preview.legs):
            raise ValueError("Preview quantity does not match the RFQ request")
        legs = [{"category": "option", "symbol": leg.symbol, "side": leg.side, "qty": str(qty)} for leg in preview.legs]
        risk = self._initial_execution_risk(preview)
        instruments = {item.symbol: item for item in self.chain}
        for leg in preview.legs:
            self._validate_leg_quantity(instruments[leg.symbol], qty)
        # Bybit RFQ link IDs allow letters and numbers only.
        rfq_link_id = f"icrfq{uuid4().hex[:16]}"
        self.rfq_state = {"rfq_id": "", "rfq_link_id": rfq_link_id, "strategy_type": strategy_type, **risk,
                          "status": "CreationUnknown", "counterparties": counterparties, "legs": legs,
                          "quotes": [], "created_at": datetime.now(timezone.utc).isoformat()}
        self._save_state()
        result = await self.client.create_rfq(counterparties, legs, rfq_link_id, strategy_type)
        if result.get("rfqLinkId") not in (None, "", rfq_link_id):
            raise ValueError("RFQ creation returned a different inquiry link; reconciliation required")
        if not isinstance(result.get("rfqId"), str) or not result["rfqId"]:
            raise ValueError("RFQ creation response has no inquiry identity; reconciliation required")
        self.rfq_state.update({"rfq_id": result["rfqId"], "status": result.get("status", "Active"),
                               "expires_at": result.get("expiresAt"), "updated_at": datetime.now(timezone.utc).isoformat()})
        self._save_state()
        self.log("INFO", f"RFQ created: {self.rfq_state['rfq_id']}")
        return deepcopy(self.rfq_state)

    async def refresh_rfq(self) -> dict:
        if self.lock.locked() or self._trade_admitting or getattr(self, "_rfq_refreshing", False):
            return deepcopy(self.rfq_state)
        async with self.lock:
            self._require_trading_state()
            snapshot = deepcopy(self.rfq_state)
        if not self._rfq_needs_refresh(snapshot):
            return snapshot
        # Only public polls share this claim. Reconciliation already owns the
        # engine lock and must never wait for a poll that needs it to apply.
        self._rfq_refreshing = True
        try:
            observation = await self._fetch_rfq(snapshot)
            if self.lock.locked() or self._trade_admitting:
                return deepcopy(self.rfq_state)
            async with self.lock:
                self._require_trading_state()
                if self.rfq_state != snapshot:
                    return deepcopy(self.rfq_state)
                return self._apply_rfq_observation(*observation)
        finally:
            self._rfq_refreshing = False

    async def _refresh_rfq(self, include_quotes: bool = True) -> dict:
        """Refresh while the caller owns the engine lock (reconciliation)."""
        self._require_trading_state()
        snapshot = deepcopy(self.rfq_state)
        if not self._rfq_needs_refresh(snapshot):
            return snapshot
        observation = await self._fetch_rfq(snapshot, include_quotes=include_quotes)
        return self._apply_rfq_observation(*observation)

    def _rfq_needs_refresh(self, state: dict) -> bool:
        if not state.get("rfq_id") and not state.get("rfq_link_id"):
            return False
        return state.get("status") not in RFQ_TERMINAL_STATUSES or self._rfq_unresolved(state)

    async def _fetch_rfq(self, snapshot: dict, include_quotes: bool = True) -> tuple[dict | None, list | None]:
        """Read an isolated inquiry snapshot without mutating engine state."""
        rfq_id = snapshot.get("rfq_id")
        link = snapshot.get("rfq_link_id")

        def match(rows):
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Invalid RFQ response")
            return next((row for row in rows if (row.get("rfqId") == rfq_id if rfq_id else row.get("rfqLinkId") == link)), None)

        rows = await self.client.rfq_realtime(rfq_id) if rfq_id else await self.client.rfq_realtime(rfq_link_id=link)
        item = match(rows)
        if item is None:
            item = match(await self.client.rfq_history(rfq_id, rfq_link_id=link))
        elif snapshot.get("status") == "CancelUnknown" and item.get("status") == "Active":
            # The realtime endpoint can lag a cancellation. One bounded history
            # lookup may confirm it, but an Active response never clears intent.
            historical = match(await self.client.rfq_history(rfq_id, rfq_link_id=link))
            if historical and historical.get("status") in RFQ_TERMINAL_STATUSES:
                item = historical
        if item is None:
            if self._rfq_unresolved(snapshot):
                raise ValueError("RFQ remains unknown in exchange realtime and history")
            return None, None
        status = item.get("status")
        if status not in {"Active", "PendingFill", *RFQ_TERMINAL_STATUSES}:
            raise ValueError("Unknown exchange RFQ status")
        if not isinstance(item.get("rfqId"), str) or not item["rfqId"]:
            raise ValueError("RFQ response has no inquiry identity")
        quotes = None
        if include_quotes and status == "Active" and snapshot.get("status") != "CancelUnknown" and not snapshot.get("execution_resolved"):
            quotes = await self.client.quote_realtime(item["rfqId"])
        return item, quotes

    def _apply_rfq_observation(self, item: dict | None, quotes: list | None) -> dict:
        if item is None:
            return deepcopy(self.rfq_state)
        status = item["status"]
        # Terminal observations cannot regress when the exchange serves lagging data.
        if self.rfq_state.get("execution_resolved") and status not in RFQ_TERMINAL_STATUSES:
            return deepcopy(self.rfq_state)
        # Keep the durable cancellation intent through every nonterminal
        # observation, including PendingFill followed by a lagging Active.
        if self.rfq_state.get("status") == "CancelUnknown" and status not in RFQ_TERMINAL_STATUSES:
            return deepcopy(self.rfq_state)
        self.rfq_state.update({"rfq_id": item["rfqId"], "status": status,
                               "expires_at": item.get("expiresAt", self.rfq_state.get("expires_at"))})
        if not self.rfq_state.get("created_at") and item.get("createdAt"):
            self.rfq_state["created_at"] = datetime.fromtimestamp(float(item["createdAt"]) / 1000, timezone.utc).isoformat()
        if status in RFQ_TERMINAL_STATUSES:
            self.rfq_state["execution_resolved"] = True
        if self.rfq_state.get("status") == "Filled":
            self._track_filled_rfq()
        self.rfq_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        if quotes is not None:
            self.rfq_state["quotes"] = deepcopy(quotes)
        self._save_state()
        return deepcopy(self.rfq_state)

    async def execute_rfq(self, request: RfqExecuteRequest) -> dict:
        self._admit_trading_operation()
        try:
            async with self.lock:
                return await self._execute_rfq(request)
        finally:
            self._trade_admitting = False

    async def _execute_rfq(self, request: RfqExecuteRequest) -> dict:
        self._require_trading_state()
        if not self.settings.can_send_orders:
            raise ValueError("Live trading is disabled")
        if not request.confirm_live:
            raise ValueError("RFQ execution requires explicit confirmation")
        if request.quote_side != "Sell":
            raise ValueError("This strategy workflow only accepts the Sell quote direction")
        if request.rfq_id != self.rfq_state.get("rfq_id"):
            raise ValueError("RFQ is not the active inquiry")
        if self.rfq_state.get("selected_quote_id") or self.rfq_state.get("status") in {"Filled", "PendingFill", "Canceled", "Expired", "Failed", "ExecutionUnknown"}:
            raise ValueError("RFQ is no longer available for execution")
        await self._reconcile_pending_orders()
        self._require_resolved_rfq()
        if self.active_strategy_symbols:
            await self._sync_positions()
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
        self.rfq_state.update({"status": "ExecutionUnknown", "execution_resolved": False,
                               "execution_started_at": datetime.now(timezone.utc).isoformat(),
                               "selected_quote_id": request.quote_id, "selected_quote_side": request.quote_side})
        self._save_state()
        result = await self.client.execute_quote(request.rfq_id, request.quote_id, request.quote_side)
        if result.get("rfqId", request.rfq_id) != request.rfq_id or result.get("quoteId", request.quote_id) != request.quote_id:
            raise ValueError("RFQ execution returned a different identity; reconciliation required")
        self.rfq_state.update({"status": result.get("status", "PendingFill"), "selected_quote_id": request.quote_id, "selected_quote_side": request.quote_side, "updated_at": datetime.now(timezone.utc).isoformat()})
        if result.get("status") == "Failed":
            self.rfq_state["execution_resolved"] = True
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
        mode = validate_mode(self.rfq_state.get("strategy_mode", "iron_condor"))
        count = leg_count(mode)
        if len(legs) != count or len(requested) != count or len(quoted_legs) != count or set(requested) != set(quoted):
            raise ValueError(f"Quote must cover exactly the {'four' if count == 4 else 'two'} requested legs")
        for symbol, leg in requested.items():
            quantity = float(quoted[symbol].get("qty", 0) or 0)
            price = float(quoted[symbol].get("price", 0) or 0)
            if not isfinite(quantity) or quantity <= 0 or not isclose(quantity, float(leg["qty"]), rel_tol=0, abs_tol=1e-9) or not isfinite(price) or price <= 0:
                raise ValueError("Quote has an invalid price or mismatched quantity")
        instruments = {item.symbol: item for item in self.chain if item.symbol in requested}
        if set(instruments) != set(requested):
            raise ValueError("RFQ instruments are missing from market data")
        bounds = {symbol: {"side": leg.get("side"), "option_type": instruments[symbol].option_type,
                           "strike": instruments[symbol].strike, "qty": float(leg["qty"]),
                           "expiry": instruments[symbol].expiry.isoformat(), "mark_price": instruments[symbol].mark_price,
                           "price_bound": float(quoted[symbol]["price"])} for symbol, leg in requested.items()}
        validate_structure(bounds, mode, require_expiry=True)
        for symbol, leg in bounds.items():
            self._validate_leg_quantity(instruments[symbol], leg["qty"])
        expiries = {item.expiry for item in instruments.values()}
        if len(expiries) != 1:
            raise ValueError("RFQ legs must resolve to one common expiry")
        self._validate_open_calendar(expiries.pop())
        if mode == "short_strangle":
            saved = self.rfq_state.get("risk_legs") or {}
            if set(saved) != set(bounds) or any(saved[symbol].get(key) != leg[key]
                                               for symbol, leg in bounds.items() for key in ("side", "option_type", "strike", "qty", "expiry")):
                raise ValueError("RFQ structure does not match its saved short-strangle risk bounds")
            self.rfq_state.update(self._proposed_execution_risk(self.rfq_state, {symbol: leg["price_bound"] for symbol, leg in bounds.items()}))
        else:
            risk = maximum_loss(bounds)
            if risk > self.settings.max_risk_usd:
                raise ValueError("RFQ quote exceeds the maximum loss limit")
            self.rfq_state.update(risk_legs=bounds, risk_reserved_usd=risk)

    def _track_filled_rfq(self, positions: list[Position] | None = None) -> bool:
        self._require_trading_state()
        group_id = f"rfq:{self.rfq_state.get('rfq_id', '')}"
        mode = validate_mode(self.rfq_state.get("strategy_mode", "iron_condor"))
        started = self.rfq_state.get("execution_started_at") or self.rfq_state.get("created_at")
        try:
            _, legs = self._stored_strategy_legs(self.rfq_state)
        except (ValueError, TypeError, KeyError):
            legs = None
        metadata = {"strategy_mode": mode}
        for key in ("risk_legs", "risk_context", "risk_budget_usd", "risk_reserved_usd"):
            if key in self.rfq_state:
                metadata[key] = deepcopy(self.rfq_state[key])
        if started and self.active_strategy_group_id == group_id and group_id not in self.execution_groups:
            self.execution_groups[group_id] = {"type": "open", "created_at": started, **metadata}
            self._save_state()
        if group_id in self.execution_groups and "legs" not in self.execution_groups[group_id] and legs:
            self.execution_groups[group_id].update(legs=legs, **metadata)
            self._save_state()
        if self.rfq_state.get("tracking_applied"):
            return False
        # Preserve any partial/complete close already reflected by the journal.
        already_tracked = self.active_strategy_group_id == group_id or any(
            group.get("type") == "close" and group.get("opening_group") == group_id
            for group in self.execution_groups.values()
        )
        if already_tracked:
            self.rfq_state["tracking_applied"] = True
            self._save_state()
            return False
        if self.active_strategy_symbols and self.active_strategy_group_id != group_id:
            return False
        if self.rfq_state.get("status") != "Filled" or not legs:
            return False
        # Filled RFQ plus the complete saved structure is the evidence. Account
        # positions can lag and residual legs cannot identify a strategy mode.
        self.active_strategy_symbols = set(legs)
        self.active_strategy_sizes = {f"{symbol}|{leg['side']}": leg["qty"] for symbol, leg in legs.items()}
        self.active_strategy_group_id = group_id
        group = self.execution_groups.setdefault(group_id, {"type": "open", "created_at": started, **metadata})
        group.setdefault("strategy_mode", mode)
        group.setdefault("legs", legs)
        self.rfq_state["tracking_applied"] = True
        self._save_state()
        return True

    async def cancel_rfq(self, request: RfqCancelRequest) -> dict:
        if self.lock.locked() or self._trade_admitting:
            from .trade_tasks import TradeConflict
            raise TradeConflict("Another trading operation is active; retry cancellation after reconciliation")
        async with self.lock:
            return await self._cancel_rfq(request)

    async def _cancel_rfq(self, request: RfqCancelRequest) -> dict:
        self._require_trading_state()
        if request.rfq_id != self.rfq_state.get("rfq_id"):
            raise ValueError("RFQ is not the active inquiry")
        self._require_resolved_rfq()
        if self.rfq_state.get("status") != "Active" or self.rfq_state.get("selected_quote_id"):
            raise ValueError("Only an active, unexecuted RFQ may be canceled")
        self.rfq_state.update(status="CancelUnknown", updated_at=datetime.now(timezone.utc).isoformat())
        self._save_state()
        result = await self.client.cancel_rfq(request.rfq_id)
        self.log("INFO", f"RFQ cancellation requested: {request.rfq_id}")
        return deepcopy({**self.rfq_state, "cancellation": result})
