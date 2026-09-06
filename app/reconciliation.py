import asyncio
from datetime import datetime, timezone
from math import isfinite

from .models import Position
from .orders import OrderExecutor


class ReconciliationMixin:
    """Order recovery and evidence-based retirement of tracked positions."""

    @staticmethod
    def _parse_positions(raw: list[dict]) -> list[Position]:
        if not isinstance(raw, list):
            raise ValueError("Invalid exchange positions response")
        positions = []
        seen = set()
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("Invalid exchange position")
            symbol, side = item.get("symbol"), item.get("side")
            size = float(item.get("size", "nan"))
            if not isinstance(symbol, str) or not symbol or not isfinite(size) or size < 0:
                raise ValueError("Invalid exchange position identity or size")
            if size == 0:
                continue
            if side not in {"Buy", "Sell"} or (symbol, side) in seen:
                raise ValueError("Invalid or duplicate exchange position side")
            seen.add((symbol, side))
            prices = [float(item.get(key, 0) or 0) for key in ("avgPrice", "markPrice", "unrealisedPnl")]
            if not all(isfinite(value) for value in prices):
                raise ValueError("Invalid exchange position prices")
            positions.append(Position(symbol=symbol, side=side, size=size, avg_price=prices[0],
                                      mark_price=prices[1], unrealised_pnl=prices[2], source="bybit"))
        return positions

    async def _retire_closed_positions(self, positions: list[Position]) -> None:
        if not self.active_strategy_symbols or self._rfq_unresolved() or any(not entry.get("terminal") for entry in self.order_journal.values()):
            return
        group_id = self.active_strategy_group_id
        group = self.execution_groups.get(group_id, {})
        present = {(position.symbol, position.side) for position in positions}
        absent = {key: qty for key, qty in self.active_strategy_sizes.items() if tuple(key.rsplit("|", 1)) not in present}
        if not absent:
            return
        started = group.get("created_at")
        if not started:
            raise ValueError("Missing opening time; tracked positions require verified historical recovery")
        since = datetime.fromisoformat(started)
        if since.tzinfo is None:
            raise ValueError("Opening time has no timezone")
        now = datetime.now(timezone.utc)
        # Closed-option history is limited to six months. Never infer a close
        # from absence, an expired symbol, or an unsupported history window.
        if not 0 <= (now - since).total_seconds() <= 180 * 86400:
            raise ValueError("Opening time is outside automatic position recovery history")
        start_ms, end_ms = int(since.timestamp() * 1000), int(now.timestamp() * 1000)
        evidence = {}
        for key, qty in absent.items():
            symbol, side = key.rsplit("|", 1)
            opening_qty = sum(entry.get("filledQty", 0) for link, entry in self.order_journal.items()
                              if self.execution_group_links.get(link) == group_id and entry["symbol"] == symbol
                              and entry["side"] == side and not entry["reduce_only"])
            required = max(qty, opening_qty)
            rows = await self.client.closed_option_positions(symbol, start_ms, end_ms)
            if not isinstance(rows, list):
                raise ValueError("Invalid closed position history")
            matched = []
            seen = set()
            total = 0.0
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("Invalid closed position history row")
                if row.get("symbol") != symbol or row.get("side") != side:
                    continue
                opened, closed, amount = (float(row.get(field, "nan")) for field in ("openTime", "closeTime", "qty"))
                if not all(isfinite(value) for value in (opened, closed, amount)) or amount <= 0:
                    raise ValueError("Invalid closed position history quantities or times")
                identity = (symbol, side, opened, closed)
                if identity in seen or not start_ms <= opened <= closed <= end_ms:
                    continue
                seen.add(identity)
                matched.append(dict(row))
                total += amount
            if total + 1e-9 >= required:
                evidence[key] = matched
        if not evidence:
            raise ValueError("Exchange positions are absent but matching closure evidence is not available yet")
        # Recheck the complete position list after history queries; a delayed
        # or transient empty snapshot alone must never retire a strategy.
        confirmed = self._parse_positions(await self.client.positions())
        self.positions = confirmed
        present = {(position.symbol, position.side) for position in confirmed}
        evidence = {key: rows for key, rows in evidence.items() if tuple(key.rsplit("|", 1)) not in present}
        for key, rows in evidence.items():
            self.active_strategy_sizes.pop(key, None)
            symbol = key.rsplit("|", 1)[0]
            if not any(tracked.rsplit("|", 1)[0] == symbol for tracked in self.active_strategy_sizes):
                self.active_strategy_symbols.discard(symbol)
            group.setdefault("closure_evidence", {})[key] = rows
        if evidence:
            if not self.active_strategy_symbols:
                group.update(status="closed", closed_at=now.isoformat(), close_reason="exchange_closed_position_history")
                self.active_strategy_group_id = None
                self.pm_baseline = {}
            self._save_state()
            self.log("INFO", f"Reconciled {len(evidence)} closed strategy legs using exchange history")
        if any(key not in evidence and tuple(key.rsplit("|", 1)) not in present for key in absent):
            raise ValueError("Some absent strategy legs still lack matching closure evidence")

    async def _sync_positions(self) -> list[Position]:
        # Caller owns self.lock from before the exchange read to state commit.
        positions = self._parse_positions(await self.client.positions())
        self.positions = positions
        if not self.state_error:
            self.reconciliation_error = None
            if len(self.active_strategy_symbols) < 4:
                self._track_filled_rfq(positions) or self._recover_tracked_open_positions(positions)
            try:
                await self._retire_closed_positions(positions)
            except Exception as exc:
                self.reconciliation_error = str(exc)
                self.log("WARNING", f"Position reconciliation incomplete: {exc}")
        return self.positions

    async def load_positions(self) -> list[Position]:
        if not self.settings.can_send_orders or self.lock.locked():
            return self.positions
        async with self.lock:
            if not self.state_error:
                try:
                    await self._reconcile_pending_orders()
                except ValueError as exc:
                    self.log("WARNING", str(exc))
            return await self._sync_positions()

    async def reconcile_once(self) -> None:
        if not self.settings.can_send_orders:
            return
        async with self.lock:
            self._require_trading_state()
            self.reconciliation_error = None
            errors = []
            # RFQ and single-leg failures must not prevent each other's recovery.
            try:
                await self._reconcile_pending_orders()
            except Exception as exc:
                errors.append(str(exc))
            if self._rfq_unresolved():
                try:
                    await self._refresh_rfq(include_quotes=False)
                    self._require_resolved_rfq()
                except Exception as exc:
                    errors.append(str(exc))
            try:
                await self._sync_positions()
            except Exception as exc:
                errors.append(str(exc))
            if self.reconciliation_error:
                errors.append(self.reconciliation_error)
            self.reconciliation_error = "; ".join(errors) or None
            if not errors:
                self.reconciliation_last_success = datetime.now(timezone.utc)

    async def reconciliation_loop(self) -> None:
        while True:
            try:
                await self.reconcile_once()
            except Exception as exc:
                self.reconciliation_error = str(exc)
                self.log("WARNING", f"Background reconciliation failed: {exc}")
            await asyncio.sleep(self.settings.reconciliation_seconds)

    def reconciliation_health(self) -> dict:
        pending = [entry for entry in self.order_journal.values() if not entry.get("terminal")]
        times = [entry.get("created_at") for entry in pending]
        rfq_pending = self._rfq_unresolved()
        if rfq_pending:
            times.append(self.rfq_state.get("execution_started_at") or self.rfq_state.get("created_at"))
        parsed = []
        for value in times:
            try:
                time = datetime.fromisoformat(value) if value else self.reconciliation_started_at
                if time.tzinfo is not None:
                    parsed.append(time)
            except (ValueError, TypeError):
                parsed.append(self.reconciliation_started_at)
        now = datetime.now(timezone.utc)
        return {"pending_orders": len(pending), "pending_rfq": rfq_pending,
                "oldest_pending_seconds": max(0.0, (now - min(parsed)).total_seconds()) if parsed else 0,
                "last_success_at": self.reconciliation_last_success, "error": self.reconciliation_error}

    async def _reconcile_pending_orders(self) -> None:
        executor = OrderExecutor(self.client, self.settings, self.log)
        async def reconcile(link, entry):
            outcome = await executor.reconcile(entry["symbol"], entry["side"], entry["qty"], link, entry)
            self._record_order(link, outcome)
        await asyncio.gather(*(reconcile(link, dict(entry)) for link, entry in list(self.order_journal.items()) if not entry.get("terminal")))
        if any(not entry.get("terminal") for entry in self.order_journal.values()):
            raise ValueError("Unresolved orders remain; verify exchange orders before trading again")

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
            if self.execution_groups.get(group_id, {}).get("order_tracking") or self.execution_groups.get(group_id, {}).get("status") == "closed":
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
