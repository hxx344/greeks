import asyncio
from datetime import datetime, timezone
from math import isclose, isfinite
from uuid import uuid4

from .models import Position, RfqCreateRequest, RfqExecuteRequest, RfqCancelRequest


class RfqMixin:
    """RFQ lifecycle; public mutations share the engine transaction lock."""

    def _rfq_unresolved(self) -> bool:
        state = self.rfq_state
        if state.get("status") in {"ExecutionUnknown", "PendingFill", "CreationUnknown", "CancelUnknown"}:
            return True
        if state.get("status") == "Filled":
            return not state.get("tracking_applied", False)
        return bool(state.get("selected_quote_id") and not state.get("execution_resolved", False))

    def _require_resolved_rfq(self) -> None:
        if self._rfq_unresolved():
            raise ValueError("Unresolved RFQ remains; wait for exchange reconciliation before trading")

    async def create_rfq(self, request: RfqCreateRequest) -> dict:
        async with self.lock:
            return await self._create_rfq(request)

    async def _create_rfq(self, request: RfqCreateRequest) -> dict:
        self._require_trading_state()
        self._require_resolved_rfq()
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
        self.rfq_state = {"rfq_id": "", "rfq_link_id": rfq_link_id, "strategy_type": strategy_type,
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
        return self.rfq_state

    async def refresh_rfq(self) -> dict:
        async with self.lock:
            return await self._refresh_rfq()

    async def _refresh_rfq(self, include_quotes: bool = True) -> dict:
        self._require_trading_state()
        if not self.rfq_state.get("rfq_id") and not self.rfq_state.get("rfq_link_id"):
            return self.rfq_state
        rfq_id = self.rfq_state.get("rfq_id")
        link = self.rfq_state.get("rfq_link_id")
        def match(rows):
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Invalid RFQ response")
            return next((row for row in rows if (row.get("rfqId") == rfq_id if rfq_id else row.get("rfqLinkId") == link)), None)
        rows = await self.client.rfq_realtime(rfq_id) if rfq_id else await self.client.rfq_realtime(rfq_link_id=link)
        item = match(rows)
        if item is None:
            item = match(await self.client.rfq_history(rfq_id, rfq_link_id=link))
        if item is None:
            if self._rfq_unresolved():
                raise ValueError("RFQ remains unknown in exchange realtime and history")
            return self.rfq_state
        status = item.get("status")
        if status not in {"Active", "PendingFill", "Canceled", "Expired", "Filled", "Failed"}:
            raise ValueError("Unknown exchange RFQ status")
        if not isinstance(item.get("rfqId"), str) or not item["rfqId"]:
            raise ValueError("RFQ response has no inquiry identity")
        # Terminal observations cannot regress when the exchange serves lagging data.
        if self.rfq_state.get("execution_resolved") and status not in {"Filled", "Canceled", "Expired", "Failed"}:
            return self.rfq_state
        self.rfq_state.update({"rfq_id": item["rfqId"], "status": status,
                               "expires_at": item.get("expiresAt", self.rfq_state.get("expires_at"))})
        if not self.rfq_state.get("created_at") and item.get("createdAt"):
            self.rfq_state["created_at"] = datetime.fromtimestamp(float(item["createdAt"]) / 1000, timezone.utc).isoformat()
        if status in {"Filled", "Canceled", "Expired", "Failed"}:
            self.rfq_state["execution_resolved"] = True
        if self.rfq_state.get("status") == "Filled":
            self._track_filled_rfq()
        self.rfq_state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._save_state()
        if include_quotes and status == "Active":
            self.rfq_state["quotes"] = await self.client.quote_realtime(self.rfq_state["rfq_id"])
            self._save_state()
        return self.rfq_state

    async def execute_rfq(self, request: RfqExecuteRequest) -> dict:
        async with self.lock:
            return await self._execute_rfq(request)

    async def _execute_rfq(self, request: RfqExecuteRequest) -> dict:
        self._require_trading_state()
        if not self.settings.can_send_orders:
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
        self._require_trading_state()
        group_id = f"rfq:{self.rfq_state.get('rfq_id', '')}"
        started = self.rfq_state.get("execution_started_at") or self.rfq_state.get("created_at")
        if started and self.active_strategy_group_id == group_id and group_id not in self.execution_groups:
            self.execution_groups[group_id] = {"type": "open", "created_at": started}
            self._save_state()
        if group_id in self.execution_groups and "legs" not in self.execution_groups[group_id]:
            legs = self.rfq_state.get("legs") or []
            if len(legs) == 4 and all(leg.get("symbol") and leg.get("side") in {"Buy", "Sell"} and float(leg.get("qty", 0)) > 0 for leg in legs):
                self.execution_groups[group_id]["legs"] = {leg["symbol"]: {"side": leg["side"], "qty": float(leg["qty"])} for leg in legs}
                self._save_state()
        if self.rfq_state.get("tracking_applied"):
            return False
        # Migrate existing tracking without restoring the original quantity
        # after a partial or complete close, including older state files.
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
        legs = self.rfq_state.get("legs") or []
        execution_submitted = self.rfq_state.get("status") == "Filled"
        if not execution_submitted or len(legs) != 4 or len({leg.get("symbol") for leg in legs}) != 4:
            return False
        sizes: dict[str, float] = {}
        for leg in legs:
            symbol = str(leg.get("symbol", ""))
            side = str(leg.get("side", ""))
            requested_qty = float(leg.get("qty", 0) or 0)
            if not symbol or side not in {"Buy", "Sell"} or requested_qty <= 0:
                return False
            # Filled RFQ is the evidence. Account positions can lag or already
            # reflect an external close and must not replace the confirmed size.
            sizes[f"{symbol}|{side}"] = requested_qty
        self.active_strategy_symbols = {str(leg["symbol"]) for leg in legs}
        self.active_strategy_sizes = sizes
        self.active_strategy_group_id = f"rfq:{self.rfq_state.get('rfq_id', '')}"
        self.execution_groups.setdefault(group_id, {"type": "open", "created_at": self.rfq_state.get("execution_started_at") or self.rfq_state.get("created_at")})
        self.execution_groups[group_id].setdefault("legs", {leg["symbol"]: {"side": leg["side"], "qty": float(leg["qty"])} for leg in legs})
        self.rfq_state["tracking_applied"] = True
        self._save_state()
        return True

    async def cancel_rfq(self, request: RfqCancelRequest) -> dict:
        async with self.lock:
            return await self._cancel_rfq(request)

    async def _cancel_rfq(self, request: RfqCancelRequest) -> dict:
        self._require_trading_state()
        if request.rfq_id != self.rfq_state.get("rfq_id"):
            raise ValueError("RFQ is not the active inquiry")
        self._require_resolved_rfq()
        if self.rfq_state.get("status") != "Active":
            raise ValueError("Only an active, unexecuted RFQ may be canceled")
        self.rfq_state.update(status="CancelUnknown", updated_at=datetime.now(timezone.utc).isoformat())
        self._save_state()
        result = await self.client.cancel_rfq(request.rfq_id)
        await self._refresh_rfq(include_quotes=False)
        self.log("INFO", f"RFQ cancellation requested: {request.rfq_id}")
        return {**self.rfq_state, "cancellation": result}
