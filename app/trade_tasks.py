"""Confirmed plans and durable, cooperatively stopped execution tasks."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from math import isclose, isfinite
from types import SimpleNamespace
from uuid import uuid4

from .models import OrderResult, Position, TradePlanRequest, TradeTaskRequest


ACTIVE_TASK_STATUSES = {"accepted", "running", "stopping", "recovering", "recovery_needed"}
TERMINAL_TASK_STATUSES = {"completed", "partial", "stopped", "failed"}
PLAN_TTL_SECONDS = 30


class TradeConflict(ValueError):
    """The confirmed plan or execution admission is no longer valid."""


class TradeStopped(ValueError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


class TradeTaskMixin:
    def _initialize_trade_tasks(self, *, persist=False):
        self.trade_plans = {}
        self.trade_workers = {}
        self.trade_stop_events = {}
        self._trade_admitting = False
        self._trade_shutdown = False
        changed = False
        for group_id, group in self.execution_groups.items():
            if group.get("task_version") == 1 and group.get("execution_status") in ACTIVE_TASK_STATUSES:
                group.update(execution_status="recovering", stop_requested=True,
                             stop_requested_at=group.get("stop_requested_at") or utcnow().isoformat())
                event = self.trade_stop_events.setdefault(group_id, asyncio.Event())
                event.set()
                changed = True
        if changed and persist and not self.state_error:
            self._save_state()

    def trading_operation_active(self):
        return bool(self._trade_admitting or self.lock.locked() or self._trade_shutdown
                    or any(group.get("task_version") == 1 and group.get("execution_status") in ACTIVE_TASK_STATUSES
                           for group in self.execution_groups.values())
                    or any(not entry.get("terminal") for entry in self.order_journal.values())
                    or self._rfq_unresolved())

    def _admit_trading_operation(self):
        self._require_idle_trading_operation()
        # No await separates the check from this claim.
        self._trade_admitting = True

    def _require_idle_trading_operation(self):
        self._require_trading_state()
        if self._rfq_unresolved():
            raise TradeConflict("Unresolved RFQ remains; wait for exchange reconciliation before trading")
        if any(not entry.get("terminal") for entry in self.order_journal.values()):
            raise TradeConflict("Unresolved orders remain; verify exchange orders before trading again")
        if self.trading_operation_active():
            raise TradeConflict("Another trading operation is active or requires reconciliation")

    @staticmethod
    def _task_response(group_id, group):
        return {"execution_id": group_id, "execution_status": group["execution_status"],
                "operation": group["type"], "plan_id": group["plan_id"]}

    @staticmethod
    def _leg_identity(legs):
        return sorted((leg["symbol"], leg["side"], leg["qty"], leg["expiry"], leg["strike"], leg["option_type"])
                      for leg in legs)

    def _plan_leg(self, symbol, side, qty, *, check_spread=False):
        item = next((item for item in self.chain if item.symbol == symbol), None)
        if item is None:
            raise TradeConflict("A confirmed instrument is no longer available")
        self._validate_leg_quantity(item, qty)
        price = item.bid if side == "Buy" else item.ask
        if not isfinite(price) or price <= 0 or not isfinite(item.strike) or item.strike <= 0:
            raise ValueError(f"No usable BBO for {symbol}")
        tick = Decimal(str(item.price_tick))
        if not tick.is_finite() or tick <= 0:
            raise ValueError("Invalid instrument price tick")
        price = float((Decimal(str(price)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick)
        if price <= 0:
            raise ValueError("BBO price is below the minimum tick")
        if (self.settings.can_send_orders and (item.bid <= 0 or item.ask <= 0 or not isfinite(item.bid) or not isfinite(item.ask))):
            raise ValueError(f"No executable bid/ask for {symbol}")
        spread = (item.ask - item.bid) / item.mark_price * 10000 if item.mark_price > 0 else float("inf")
        if check_spread and self.settings.can_send_orders and self.settings.max_spread_bps > 0 and spread > self.settings.max_spread_bps:
            raise ValueError(f"Spread for {symbol} exceeds configured limit")
        return {"symbol": symbol, "side": side, "qty": qty, "expiry": item.expiry.isoformat(),
                "strike": item.strike, "option_type": item.option_type, "reference_price": price}

    def _fee_context(self):
        index = self.btc_price or 0.0
        values = (index, self.settings.estimated_taker_fee_rate, self.settings.option_fee_cap_pct)
        if any(not isfinite(value) or value < 0 for value in values):
            raise ValueError("Invalid fee estimate inputs")
        if self.settings.can_send_orders and index <= 0:
            raise ValueError("A current BTC index price is required for fee estimates")
        return {"index_price": index, "fee_rate": values[1], "fee_cap_pct": values[2],
                "multiplier": 1.0 if self.chain_source == "bybit" else 0.01}

    @staticmethod
    def _net_estimate(legs, context):
        gross = sum((1 if leg["side"] == "Sell" else -1) * leg["price_bound"] * leg["qty"] for leg in legs.values()) * context["multiplier"]
        fee = sum(min(context["fee_rate"] * context["index_price"], context["fee_cap_pct"] * leg.get("fee_price_ceiling", leg["price_bound"])) * leg["qty"] for leg in legs.values())
        if not all(isfinite(value) for value in (gross, fee, gross - fee)):
            raise ValueError("Non-finite combination estimate")
        return gross, fee, gross - fee

    async def _close_plan_snapshot(self):
        live = self.settings.can_send_orders
        if live:
            self._require_resolved_rfq()
            current = await self._sync_positions()
            if self._tracking_needs_recovery():
                if not self._track_filled_rfq(current):
                    await self.load_recent_executions()
                    self._recover_tracked_open_positions(current)
        else:
            current = [item for item in self.positions if item.source == "demo"]
        tracked = dict(self.active_strategy_sizes)
        symbols = self.active_strategy_symbols if live else {item.symbol for item in current}
        selected = []
        exchange = []
        for item in current:
            if item.symbol not in symbols or item.size <= 0:
                continue
            qty = min(item.size, tracked.get(f"{item.symbol}|{item.side}", 0)) if live else item.size
            if qty > 0:
                selected.append(self._plan_leg(item.symbol, "Sell" if item.side == "Buy" else "Buy", qty))
                exchange.append((item.symbol, item.side, item.size))
        if not selected:
            raise ValueError("No tracked strategy legs found to close")
        if live:
            mode = self.execution_groups.get(self.active_strategy_group_id, {}).get("strategy_mode", "iron_condor")
        else:
            simulated = [group for group in self.execution_groups.values() if group.get("simulated") and group.get("type") == "open"]
            mode = simulated[-1].get("strategy_mode", "iron_condor") if simulated else "iron_condor"
        if len(selected) > (4 if mode == "iron_condor" else 2):
            raise ValueError("Tracked strategy contains too many legs")
        fingerprint = {"opening_group": self.active_strategy_group_id, "tracked": sorted(tracked.items()), "exchange": sorted(exchange)}
        return selected, mode, fingerprint

    async def prepare_trade_plan(self, request: TradePlanRequest):
        self._admit_trading_operation()
        try:
            async with self.lock:
                if request.operation == "open":
                    if self.settings.can_send_orders:
                        self._require_resolved_rfq()
                        if self.active_strategy_symbols:
                            await self._sync_positions()
                        if self.active_strategy_symbols:
                            raise TradeConflict("Close the tracked strategy before opening another")
                    preview = await self.make_preview(request.quantity, strategy_mode=request.strategy_mode)
                    self._validate_risk(preview)
                    legs = [self._plan_leg(leg.symbol, leg.side, leg.qty, check_spread=True) for leg in preview.legs]
                    if self.settings.can_send_orders:
                        self._validate_open_calendar(preview.expiry)
                    if any(leg["expiry"] != preview.expiry.isoformat() for leg in legs):
                        raise TradeConflict("Preview expiry no longer matches its instruments")
                    mode, margin, unbounded = preview.strategy_mode, preview.estimated_margin_usd, preview.unbounded_loss
                    close_fingerprint = None
                else:
                    await self.refresh_chain()
                    legs, mode, close_fingerprint = await self._close_plan_snapshot()
                    margin, unbounded = 0.0, mode == "short_strangle"
                if self.settings.can_send_orders:
                    self._validate_market_snapshot()
                context = self._fee_context()
                bounds = {leg["symbol"]: {**leg, "price_bound": leg["reference_price"]} for leg in legs}
                gross, fee, net = self._net_estimate(bounds, context)
                now = utcnow()
                plan = {"plan_id": uuid4().hex, "operation": request.operation, "environment": self.settings.environment,
                        "strategy_mode": mode, "created_at": now.isoformat(), "expires_at": (now + timedelta(seconds=PLAN_TTL_SECONDS)).isoformat(),
                        "legs": legs, "estimated_gross_usd": gross, "estimated_fee_usd": fee, "estimated_net_usd": net,
                        "estimated_margin_usd": margin, "unbounded_loss": unbounded,
                        "min_net_income_usd": net if request.operation == "open" else None,
                        "max_net_cost_usd": -net if request.operation == "close" else None}
                self.trade_plans = {key: value for key, value in self.trade_plans.items() if datetime.fromisoformat(value["plan"]["expires_at"]) > now}
                self.trade_plans[plan["plan_id"]] = {"plan": deepcopy(plan), "fee_context": context,
                                                    "close_fingerprint": close_fingerprint, "live": self.settings.can_send_orders,
                                                    "private_testnet": self.settings.private_testnet,
                                                    "instrument_fingerprint": self._plan_instrument_fingerprint(legs)}
                return plan
        finally:
            self._trade_admitting = False

    def _validate_plan_age(self, saved):
        plan = saved["plan"]
        if (utcnow() >= datetime.fromisoformat(plan["expires_at"]) or plan["environment"] != self.settings.environment
                or saved["private_testnet"] != self.settings.private_testnet or saved["live"] != self.settings.can_send_orders):
            raise TradeConflict("The trade plan expired or its environment changed; prepare a new plan")

    def _plan_instrument_fingerprint(self, legs):
        instruments = {item.symbol: item for item in self.chain}
        result = {}
        for leg in legs:
            item = instruments.get(leg["symbol"])
            if item is None:
                raise TradeConflict("A confirmed instrument is no longer available")
            result[item.symbol] = (item.expiry.isoformat(), item.strike, item.option_type,
                                   item.min_qty, item.max_qty, item.qty_step, item.price_tick)
        return result

    def _validate_plan_metadata(self, saved):
        if self._plan_instrument_fingerprint(saved["plan"]["legs"]) != saved["instrument_fingerprint"]:
            raise TradeConflict("Confirmed instrument metadata changed; prepare a new plan")

    async def start_trade_task(self, request: TradeTaskRequest, *, scheduled=False):
        fingerprint = request.model_dump()
        for group_id, group in self.execution_groups.items():
            if group.get("task_version") == 1 and group.get("request_id") == request.request_id:
                if group.get("request_fingerprint") != fingerprint:
                    raise TradeConflict("This request ID was already used with different parameters")
                return self._task_response(group_id, group)
        if any(group.get("task_version") == 1 and group.get("plan_id") == request.plan_id for group in self.execution_groups.values()):
            raise TradeConflict("This trade plan already has an execution; use its original request ID to retry")
        self._admit_trading_operation()
        try:
            async with self.lock:
                saved = self.trade_plans.get(request.plan_id)
                if saved is None:
                    raise TradeConflict("The trade plan is unavailable; prepare a new plan")
                self._validate_plan_age(saved)
                self._validate_plan_metadata(saved)
                plan = saved["plan"]
                if saved["live"] and not request.confirm_live:
                    raise ValueError("Starting exchange orders requires explicit confirmation")
                self._require_resolved_rfq()
                risk = {}
                if plan["operation"] == "open":
                    if saved["live"] and self.active_strategy_symbols:
                        await self._sync_positions()
                        if self.active_strategy_symbols:
                            raise TradeConflict("The tracked position changed; prepare a new plan")
                    try:
                        preview = await self.make_preview(plan["legs"][0]["qty"], strategy_mode=plan["strategy_mode"])
                    except ValueError as exc:
                        raise TradeConflict("The confirmed strategy is no longer available; prepare a new plan") from exc
                    self._validate_plan_metadata(saved)
                    candidate = [self._plan_leg(leg.symbol, leg.side, leg.qty, check_spread=True) for leg in preview.legs]
                    if preview.strategy_mode != plan["strategy_mode"] or self._leg_identity(candidate) != self._leg_identity(plan["legs"]):
                        raise TradeConflict("Selected instruments changed; prepare and confirm a new plan")
                    self._validate_risk(preview)
                    if saved["live"]:
                        self._validate_open_calendar(preview.expiry)
                        await self._capture_pm_baseline("scheduled_open" if scheduled else "manual_open")
                        self._validate_plan_metadata(saved)
                        risk = self._initial_execution_risk(preview)
                else:
                    await self.refresh_chain()
                    self._validate_plan_metadata(saved)
                    try:
                        candidate, mode, close_fingerprint = await self._close_plan_snapshot()
                    except ValueError as exc:
                        raise TradeConflict("The tracked close position changed; prepare a new plan") from exc
                    if (mode != plan["strategy_mode"] or close_fingerprint != saved["close_fingerprint"]
                            or self._leg_identity(candidate) != self._leg_identity(plan["legs"])):
                        raise TradeConflict("Tracked or exchange position quantities changed; prepare a new close plan")
                # Refreshes may have consumed the TTL or changed shared metadata.
                self._validate_plan_age(saved)
                self._validate_plan_metadata(saved)
                if plan["operation"] == "open":
                    # The market loop may update deltas during the margin await.
                    # Compare synchronously after the last await, then execute
                    # only the original confirmed legs saved in the plan.
                    try:
                        final_preview = self._build_preview_from_chain(plan["legs"][0]["qty"], plan["strategy_mode"])
                        final_candidate = [self._plan_leg(leg.symbol, leg.side, leg.qty, check_spread=True) for leg in final_preview.legs]
                    except ValueError as exc:
                        raise TradeConflict("The confirmed strategy is no longer available; prepare a new plan") from exc
                    if self._leg_identity(final_candidate) != self._leg_identity(plan["legs"]):
                        raise TradeConflict("Selected instruments changed; prepare and confirm a new plan")
                if scheduled and not self.is_open_window():
                    raise TradeConflict("The scheduled opening window ended")
                for leg in plan["legs"]:
                    current = self._plan_leg(leg["symbol"], leg["side"], leg["qty"], check_spread=plan["operation"] == "open")
                    if self._leg_identity([current]) != self._leg_identity([leg]):
                        raise TradeConflict("Confirmed instrument metadata changed; prepare a new plan")
                if saved["live"]:
                    self._validate_market_snapshot()
                group_id = uuid4().hex[:16]
                accepted = utcnow().isoformat()
                bounds = {leg["symbol"]: {**leg, "price_bound": leg["reference_price"]} for leg in plan["legs"]}
                minimum = request.min_net_income_usd if request.min_net_income_usd is not None else plan["min_net_income_usd"]
                maximum = request.max_net_cost_usd if request.max_net_cost_usd is not None else plan["max_net_cost_usd"]
                if (plan["operation"] == "open" and request.max_net_cost_usd is not None
                        or plan["operation"] == "close" and request.min_net_income_usd is not None):
                    raise ValueError("Net price constraint does not match the plan operation")
                group = {"type": plan["operation"], "task_version": 1, "execution_status": "accepted", "plan_id": request.plan_id,
                         "request_id": request.request_id, "request_fingerprint": fingerprint, "accepted_at": accepted, "created_at": accepted,
                         "started_at": None, "finished_at": None, "stop_requested_at": None, "stop_requested": False,
                         "strategy_mode": plan["strategy_mode"], "environment": plan["environment"], "live": saved["live"],
                         "legs": {leg["symbol"]: {**leg, "chain_price": leg["reference_price"]} for leg in plan["legs"]},
                         "net_price_legs": bounds, "fee_context": saved["fee_context"],
                         "min_net_income_usd": minimum, "max_net_cost_usd": maximum, "scheduled": scheduled,
                         "order_tracking": True, **risk}
                if plan["operation"] == "close":
                    group["opening_group"] = self.active_strategy_group_id
                self.execution_groups[group_id] = group
                self.trade_stop_events[group_id] = asyncio.Event()
                self._save_state()  # Durable admission precedes background dispatch.
                if scheduled:
                    self.last_open_week = utcnow().strftime("%G-W%V")
                    self._save_state()
                self.trade_workers[group_id] = asyncio.create_task(self._run_trade_task(group_id))
                return self._task_response(group_id, group)
        finally:
            self._trade_admitting = False

    def stop_trade_task(self, execution_id):
        group = self.execution_groups.get(execution_id)
        if not group or group.get("task_version") != 1:
            raise ValueError("Execution task not found")
        if group["execution_status"] in ACTIVE_TASK_STATUSES:
            group.update(stop_requested=True, stop_requested_at=group.get("stop_requested_at") or utcnow().isoformat())
            if group["execution_status"] not in {"recovering", "recovery_needed"}:
                group["execution_status"] = "stopping"
            self.trade_stop_events.setdefault(execution_id, asyncio.Event()).set()
            try:
                self._save_state()
            except ValueError as exc:
                # A storage failure must never disable in-memory cancellation.
                group["error"] = f"Stop requested in memory; persistence failed: {exc}"
                self.log("ERROR", group["error"])
        return self._task_response(execution_id, group)

    def _check_trade_stop(self, group_id):
        group = self.execution_groups.get(group_id, {})
        if group.get("stop_requested") or self._trade_shutdown:
            raise TradeStopped("Execution stop requested; no additional orders or amendments are allowed")

    async def _trade_delay(self, group_id, seconds):
        event = self.trade_stop_events.get(group_id)
        if event is None:
            await asyncio.sleep(seconds)
            return
        self._check_trade_stop(group_id)
        try:
            await asyncio.wait_for(event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._check_trade_stop(group_id)

    def _reserve_task_price(self, group, symbol, price):
        if group.get("net_price_blocked"):
            raise ValueError("Combination net price limit was exceeded")
        if price is None:
            return
        proposed = deepcopy(group["net_price_legs"])
        leg = proposed[symbol]
        if not isfinite(price) or price <= 0:
            raise ValueError("Invalid execution price")
        leg["price_bound"] = max(price, leg["price_bound"]) if leg["side"] == "Buy" else min(price, leg["price_bound"])
        leg["fee_price_ceiling"] = max(price, leg.get("fee_price_ceiling", leg["reference_price"]))
        context = dict(group["fee_context"])
        current = self._fee_context()
        for key in ("index_price", "fee_rate", "fee_cap_pct"):
            context[key] = max(context[key], current[key])
        gross, fee, net = self._net_estimate(proposed, context)
        breached = net < group["min_net_income_usd"] - 1e-9 if group["type"] == "open" else -net > group["max_net_cost_usd"] + 1e-9
        if breached:
            group["net_price_blocked"] = True
            self._save_state()
            raise ValueError("Combination price exceeds the confirmed net limit including estimated fees")
        group.update(net_price_legs=proposed, fee_context=context, reserved_gross_usd=gross, reserved_fee_usd=fee, reserved_net_usd=net)
        self._save_state()

    def _finish_trade_task(self, group_id, error=None, *, persist=True):
        group = self.execution_groups[group_id]
        entries = [entry for link, entry in self.order_journal.items() if self.execution_group_links.get(link) == group_id]
        if any(not entry.get("terminal") for entry in entries):
            group["execution_status"] = "recovery_needed"
        else:
            filled = {symbol: sum(entry.get("filledQty", 0) for entry in entries if entry["symbol"] == symbol) for symbol in group["legs"]}
            complete = all(isclose(filled[symbol], leg["qty"], rel_tol=0, abs_tol=1e-9) for symbol, leg in group["legs"].items())
            if complete or group.get("simulated"):
                status = "completed"
            elif any(value > 0 for value in filled.values()):
                status = "partial"
            elif group.get("stop_requested"):
                status = "stopped"
            else:
                status = "failed"
            group.update(execution_status=status, finished_at=utcnow().isoformat())
            if complete and group["type"] == "close" and not self.active_strategy_symbols:
                opening = self.execution_groups.get(group.get("opening_group"))
                if opening is not None:
                    opening["status"] = "closed"
                self.active_strategy_group_id = None
                self.pm_baseline = {}
        if error:
            group["error"] = str(error)
        if persist:
            self._save_state()

    async def _run_trade_task(self, group_id):
        group = self.execution_groups[group_id]
        error = None
        sampler = None
        sampler_done = asyncio.Event()
        try:
            async with self.lock:
                try:
                    self._check_trade_stop(group_id)
                    group.update(execution_status="running", started_at=utcnow().isoformat())
                    self._save_state()
                    if group.get("scheduled") and not self.is_open_window():
                        raise TradeConflict("The scheduled opening window ended before dispatch")
                    legs = [SimpleNamespace(**leg) for leg in group["legs"].values()]
                    if not group["live"]:
                        if group["type"] == "open":
                            for leg in legs:
                                self.positions.append(Position(symbol=leg.symbol, side=leg.side, size=leg.qty, avg_price=leg.reference_price,
                                                               mark_price=leg.reference_price, unrealised_pnl=0, source="demo"))
                        else:
                            self.positions = [item for item in self.positions if item.symbol not in group["legs"] or item.source != "demo"]
                        group["simulated"] = True
                        group["simulated_fills"] = {leg.symbol: leg.qty for leg in legs}
                    else:
                        self._validate_market_snapshot()
                        links = [f"ic-{'close-' if group['type'] == 'close' else ''}{group_id}-{index}" for index in range(len(legs))]
                        for link in links:
                            self.execution_group_links[link] = group_id
                        self._save_state()
                        sampler = asyncio.create_task(self._sample_trade_executions(group_id, sampler_done))
                        responses = await asyncio.gather(*(self.follow_bbo_order(leg, leg.qty, link, reduce_only=group["type"] == "close")
                                                           for leg, link in zip(legs, links)), return_exceptions=True)
                        errors = [str(response) for response in responses if isinstance(response, BaseException)]
                        if errors:
                            error = "; ".join(errors)
                        if group["type"] == "open" and self.settings.allow_market_fallback and not group.get("stop_requested"):
                            results = [OrderResult(symbol=leg.symbol, side=leg.side, qty=0, status="unknown") for leg in legs]
                            await self._market_fallback(legs, legs[0].qty, links, results, group_id, list(links))
                        try:
                            await self.load_recent_executions([link for link, owner in self.execution_group_links.items() if owner == group_id])
                        except Exception as exc:
                            self.log("WARNING", f"Execution detail refresh failed: {exc}")
                except Exception as exc:
                    error = str(exc)
                    self.log("WARNING", f"Trade task {group_id}: {exc}")
                finally:
                    sampler_done.set()
                    if sampler is not None:
                        await sampler
                    self._finish_trade_task(group_id, error)
        except Exception as exc:
            group.update(execution_status="recovery_needed", error=str(exc))
            self.log("ERROR", f"Trade task state could not be finalized: {exc}")
        finally:
            self.trade_workers.pop(group_id, None)

    async def _sample_trade_executions(self, group_id, done):
        while not done.is_set():
            try:
                await asyncio.wait_for(done.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                links = [link for link, owner in self.execution_group_links.items() if owner == group_id]
                if links:
                    try:
                        await self.load_recent_executions(links)
                    except Exception as exc:
                        self.log("WARNING", f"Trade execution detail refresh failed: {exc}")

    def _finish_recovered_trade_tasks(self, *, persist=True):
        for group_id, group in self.execution_groups.items():
            if (group.get("task_version") == 1 and group.get("execution_status") in {"recovering", "recovery_needed"}
                    and group_id not in self.trade_workers):
                self._finish_trade_task(group_id, persist=persist)

    async def shutdown_trade_tasks(self):
        self._trade_shutdown = True
        for group_id, group in list(self.execution_groups.items()):
            if group.get("task_version") == 1 and group.get("execution_status") in ACTIVE_TASK_STATUSES:
                self.stop_trade_task(group_id)
        if self.trade_workers:
            await asyncio.gather(*list(self.trade_workers.values()), return_exceptions=True)
