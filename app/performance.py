"""Persistent exchange fills and conservative, per-opening strategy attribution.

Closed position totalPnl is already net of fees:
https://bybit-exchange.github.io/docs/v5/position/close-position
"""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .models import ExecutionRecord


ZERO = Decimal(0)
EPS = Decimal("0.00000001")


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Non-finite performance value")
    return result


def stamp(value):
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Performance timestamp needs timezone")
    return parsed


def amount(value):
    return float(value.quantize(EPS)) if value is not None else None


def settlement_currency(symbol):
    return "USDT" if symbol.endswith("-USDT") else "USDC"


def build_performance(groups, executions, links, journal, positions=(), positions_available=False):
    """Only uniquely attributable, completely reconciled round trips enter the curve."""
    buckets = {}
    for key, group in groups.items():
        if group.get("type") == "open":
            buckets[key] = dict(id=key, meta=group, lots=[], closes=[], issues=[], open_fee=ZERO, close_fee=ZERO,
                                premium=ZERO, realized=ZERO, opened=None, closed=None, currencies=set())
    ordered = sorted({item.exec_id: item for item in executions}.values(), key=lambda item: (item.exec_time, item.exec_id))
    unassigned = 0
    for execution in ordered:
        group_id = links.get(execution.order_link_id) or execution.execution_group
        if not group_id and execution.order_link_id.startswith("ic-"):
            parts = execution.order_link_id.split("-")
            if len(parts) >= 3 and parts[1] not in {"close", "mkt"}:
                group_id = parts[1]
        meta = groups.get(group_id, {})
        is_close = execution.reduce_only or meta.get("type") == "close" or execution.order_link_id.startswith("ic-close-") or (execution.closed_size or 0) > 0
        if not is_close and not group_id:
            continue  # Account transfers and unrelated trades are not strategy returns.
        if execution.exec_type not in {"Trade", "BlockTrade"}:
            continue  # Delivery is taken from verified closed-position evidence.
        try:
            qty, price, fee = number(execution.exec_qty), number(execution.exec_price), number(execution.exec_fee)
            if qty <= 0 or price < 0 or execution.side not in {"Buy", "Sell"} or not execution.symbol.startswith("BTC-"):
                raise ValueError("Invalid execution")
            if execution.exec_time.tzinfo is None:
                raise ValueError("Missing execution timezone")
        except (ValueError, InvalidOperation):
            if group_id in buckets:
                buckets[group_id]["issues"].append("成交数据不完整")
            continue
        currency = settlement_currency(execution.symbol)
        currency_ok = execution.fee_currency == currency or (not execution.fee_currency and fee == 0)
        if not is_close:
            bucket = buckets.setdefault(group_id, dict(id=group_id, meta={}, lots=[], closes=[], issues=[], open_fee=ZERO,
                                                        close_fee=ZERO, premium=ZERO, realized=ZERO, opened=None, closed=None, currencies=set()))
            if not currency_ok:
                bucket["issues"].append("手续费币种无法换算")
            bucket["currencies"].add(currency)
            bucket["lots"].append(dict(execution=execution, remaining=qty, qty=qty, price=price, fee=fee))
            bucket["open_fee"] += fee
            bucket["premium"] += (1 if execution.side == "Sell" else -1) * qty * price
            bucket["opened"] = min(bucket["opened"] or execution.exec_time, execution.exec_time)
            continue
        opening = meta.get("opening_group") or execution.opening_group
        if not opening:
            # A matching symbol alone cannot prove that an external/manual close
            # belongs to this strategy rather than another account position.
            unassigned += 1
            continue
        candidates = [b for key, b in buckets.items() if (not opening or key == opening) and any(
            lot["remaining"] > EPS and lot["execution"].symbol == execution.symbol and lot["execution"].side != execution.side
            for lot in b["lots"])]
        if len(candidates) != 1:
            if opening in buckets:
                buckets[opening]["issues"].append("平仓成交缺少对应开仓记录")
            unassigned += 1
            continue
        bucket = candidates[0]
        lots = [lot for lot in bucket["lots"] if lot["execution"].symbol == execution.symbol and lot["execution"].side != execution.side and lot["remaining"] > EPS]
        if sum((lot["remaining"] for lot in lots), ZERO) + EPS < qty or (execution.closed_size is not None and abs(number(execution.closed_size) - qty) > EPS):
            bucket["issues"].append("平仓数量与开仓记录不符")
            continue
        if not currency_ok:
            bucket["issues"].append("手续费币种无法换算")
        remaining = qty
        pnl = -fee
        for lot in lots:
            matched = min(remaining, lot["remaining"])
            pnl += (1 if execution.side == "Sell" else -1) * (price - lot["price"]) * matched - lot["fee"] * matched / lot["qty"]
            lot["remaining"] -= matched
            remaining -= matched
            if remaining <= EPS:
                break
        bucket["realized"] += pnl
        bucket["close_fee"] += fee
        bucket["closes"].append(execution)
        bucket["closed"] = execution.exec_time

    rows, evidence_owners = [], {}
    for key, group in groups.items():
        for evidence in group.get("closure_evidence", {}).values():
            for row in evidence:
                identity = (row.get("symbol"), row.get("side"), row.get("openTime"), row.get("closeTime"))
                evidence_owners.setdefault(identity, set()).add(key)
    for key, bucket in buckets.items():
        meta, lots = bucket["meta"], bucket["lots"]
        actual = {}
        for lot in lots:
            leg = (lot["execution"].symbol, lot["execution"].side)
            actual[leg] = actual.get(leg, ZERO) + lot["qty"]
        expected = {}
        for link, entry in journal.items():
            if links.get(link) == key and not entry["reduce_only"]:
                leg = (entry["symbol"], entry["side"])
                expected[leg] = expected.get(leg, ZERO) + number(entry.get("filledQty", 0))
                if not entry.get("terminal"):
                    bucket["issues"].append("开仓成交仍待确认")
        if not expected and meta.get("legs"):
            expected = {(symbol, leg["side"]): number(leg["qty"]) for symbol, leg in meta["legs"].items() if "qty" in leg and "side" in leg}
        for leg, qty in expected.items():
            if abs(actual.get(leg, ZERO) - qty) > EPS:
                bucket["issues"].append("开仓成交历史尚未补齐")
        closed = bool(lots) and all(lot["remaining"] <= EPS for lot in lots)
        source, delivery_fee = "executions", ZERO
        # Exchange closure evidence replaces, never adds to, fill-based P&L.
        evidence = meta.get("closure_evidence", {})
        targets = expected or actual or {(symbol, leg["side"]): number(leg["qty"]) for symbol, leg in meta.get("legs", {}).items() if "qty" in leg and "side" in leg}
        if evidence and targets:
            try:
                net, opening_fee, closing_fee, delivery, premium = ZERO, ZERO, ZERO, ZERO, ZERO
                opened_times, closed_times, currencies, seen = [], [], set(), set()
                for (symbol, side), qty in targets.items():
                    if qty <= EPS:
                        continue
                    rows_for_leg = evidence.get(f"{symbol}|{side}", [])
                    total = ZERO
                    for row in rows_for_leg:
                        identity = (row.get("symbol"), row.get("side"), row.get("openTime"), row.get("closeTime"))
                        if identity in seen:
                            continue
                        seen.add(identity)
                        if row.get("symbol") != symbol or row.get("side") != side or evidence_owners.get(identity) != {key}:
                            raise ValueError("Ambiguous closure evidence")
                        opened_at = datetime.fromtimestamp(int(row["openTime"]) / 1000, timezone.utc)
                        closed_at = datetime.fromtimestamp(int(row["closeTime"]) / 1000, timezone.utc)
                        if closed_at < opened_at or (meta.get("created_at") and opened_at < stamp(meta["created_at"])):
                            raise ValueError("Invalid closure times")
                        total += number(row["qty"])
                        net += number(row["totalPnl"])
                        opening_fee += number(row["totalOpenFee"])
                        closing_fee += number(row["totalCloseFee"])
                        delivery += number(row["deliveryFee"])
                        premium += (1 if side == "Sell" else -1) * number(row["avgEntryPrice"]) * number(row["qty"])
                        opened_times.append(opened_at)
                        closed_times.append(closed_at)
                        currencies.add(settlement_currency(symbol))
                    if abs(total - qty) > EPS:
                        raise ValueError("Closure quantity mismatch")
                if not closed_times:
                    raise ValueError("No closure amounts")
                bucket.update(realized=net, open_fee=opening_fee, close_fee=closing_fee, premium=premium,
                              opened=min(opened_times), closed=max(closed_times), currencies=currencies, issues=[])
                closed, source, delivery_fee = True, "exchange_closed_positions", delivery
            except (ValueError, KeyError, TypeError, InvalidOperation, OverflowError):
                if not closed:
                    bucket["issues"].append("交割或平仓收益待核对")
        if meta.get("status") == "closed" and not closed:
            bucket["issues"].append("已结束组合的成交或交割记录不完整")
        if not lots and source == "executions":
            bucket["issues"].append("等待实际开仓成交记录")
        if len(bucket["currencies"]) != 1:
            bucket["issues"].append("结算币种待核对")
        if any(links.get(link) in groups and groups[links[link]].get("opening_group") == key and not entry.get("terminal") for link, entry in journal.items()):
            bucket["issues"].append("平仓成交仍待确认")
        issues = list(dict.fromkeys(bucket["issues"]))
        floating = None
        if positions_available and not closed and lots and not issues:
            residuals = {}
            for lot in lots:
                leg = (lot["execution"].symbol, lot["execution"].side)
                residuals[leg] = residuals.get(leg, ZERO) + lot["remaining"]
            total_float = ZERO
            for (symbol, side), qty in residuals.items():
                if qty <= EPS:
                    continue
                matches = [p for p in positions if p.source == "bybit" and p.symbol == symbol and p.side == side and abs(number(p.size) - qty) <= EPS]
                owners = [b for b in buckets.values() if any(l["remaining"] > EPS and l["execution"].symbol == symbol and l["execution"].side == side for l in b["lots"])]
                if len(matches) != 1 or len(owners) != 1:
                    break
                # Use this combination's cost, not an unrelated account-wide average.
                total_float += sum(((1 if side == "Buy" else -1) * (number(matches[0].mark_price) - lot["price"]) * lot["remaining"] for lot in lots if lot["execution"].symbol == symbol and lot["execution"].side == side), ZERO)
            else:
                floating = amount(total_float)
        currency = next(iter(bucket["currencies"])) if len(bucket["currencies"]) == 1 else "待确认"
        rows.append(dict(id=key, opened_at=(bucket["opened"].isoformat() if bucket["opened"] else meta.get("created_at")),
                         closed_at=bucket["closed"].isoformat() if closed and bucket["closed"] else None,
                         currency=currency, status="pending" if issues else "closed" if closed else "open", issues=issues,
                         leg_count=len(actual or targets), open_premium=amount(bucket["premium"]) if lots or source != "executions" else None,
                         open_fee=amount(bucket["open_fee"]) if lots or source != "executions" else None,
                         close_fee=amount(bucket["close_fee"]) if lots or source != "executions" else None, delivery_fee=amount(delivery_fee),
                         net_pnl=None if issues else amount(bucket["realized"]), floating_pnl=floating,
                         remaining_qty=amount(sum((lot["remaining"] for lot in lots), ZERO)) if not closed else 0,
                         source=source, fills=[item.model_dump(mode="json") for item in [*(lot["execution"] for lot in lots), *bucket["closes"]]]))
    rows.sort(key=lambda row: (row["opened_at"] or "", row["id"]), reverse=True)
    series = []
    for currency in sorted({row["currency"] for row in rows if row["status"] == "closed"}):
        completed = sorted((row for row in rows if row["status"] == "closed" and row["currency"] == currency), key=lambda row: (row["closed_at"], row["id"]))
        total, peak, drawdown = ZERO, ZERO, ZERO
        points = []
        for row in completed:
            total += number(row["net_pnl"])
            peak = max(peak, total)
            drawdown = max(drawdown, peak - total)
            points.append(dict(time=row["closed_at"], group_id=row["id"], pnl=row["net_pnl"], cumulative=amount(total)))
        series.append(dict(currency=currency, points=points, total_pnl=amount(total), closed_count=len(completed),
                           wins=sum(row["net_pnl"] > 0 for row in completed), max_drawdown=amount(drawdown)))
    return dict(groups=rows, series=series, unassigned_closes=unassigned)


class PerformanceMixin:
    def _archive_performance(self, records):
        changed = False
        for record in records:
            previous = self.performance_executions.get(record.exec_id)
            if previous:
                record = record.model_copy(update={field: getattr(previous, field) for field in (
                    "execution_group", "opening_group", "closed_size", "chain_price_at_create", "chain_price_diff") if getattr(record, field) is None})
            if record.exec_id and self.performance_executions.get(record.exec_id) != record:
                self.performance_executions[record.exec_id] = record
                changed = True
        if changed and not self.state_error:
            self._save_state()

    async def sync_performance(self):
        if self.performance_lock.locked():
            return
        if not self.settings.bybit_api_key or not self.settings.bybit_api_secret or self.state_error:
            return
        async with self.performance_lock:
            try:
                now = datetime.now(timezone.utc)
                now_ms = int(now.timestamp() * 1000)
                if self.performance_cursor_ms is None:
                    dates = [stamp(group["created_at"]) for group in self.execution_groups.values() if group.get("created_at")]
                    dates.extend(item.exec_time for item in self.performance_executions.values())
                    start = max(now - timedelta(days=729), min(dates) - timedelta(days=1) if dates else now - timedelta(days=7))
                    self.performance_start_ms = int(start.timestamp() * 1000)
                    self.performance_cursor_ms = self.performance_start_ms
                # One paginated week per refresh; overlap catches late fills without duplication.
                overlap_ms = 86400000 if now_ms - self.performance_cursor_ms < 120000 else 60000
                start_ms = max(self.performance_start_ms, self.performance_cursor_ms - overlap_ms)
                end_ms = min(now_ms, start_ms + 7 * 86400000 - 1)
                raw = await self.client.execution_history(start_ms, end_ms)
                records = []
                for row in raw:
                    qty, price, fee = (number(row[field]) for field in ("execQty", "execPrice", "execFee"))
                    if not row.get("execId") or qty <= 0 or price < 0 or row.get("side") not in {"Buy", "Sell"}:
                        raise ValueError("Invalid execution history")
                    records.append(ExecutionRecord(symbol=row["symbol"], side=row["side"], order_id=row.get("orderId", ""),
                        order_link_id=row.get("orderLinkId", ""), exec_id=row["execId"], exec_fee=float(fee), fee_currency=row.get("feeCurrency", ""),
                        exec_price=float(price), exec_qty=float(qty), exec_time=datetime.fromtimestamp(int(row["execTime"]) / 1000, timezone.utc),
                        closed_size=float(number(row["closedSize"])) if row.get("closedSize") not in (None, "") else None, exec_type=row.get("execType") or "Trade"))
                self._archive_performance(records)
                self.performance_cursor_ms = end_ms
                self.performance_updated_at = now.isoformat()
                self.performance_error = None
                self._save_state()
            except Exception as exc:
                self.performance_error = "成交历史同步失败，已保留本地台账，下次刷新重试"
                self.log("WARNING", f"Performance history sync failed: {type(exc).__name__}")

    async def performance_loop(self):
        while True:
            await self.sync_performance()
            await asyncio.sleep(30)

    def performance_report(self, positions_available=False, positions=None):
        report = build_performance(self.execution_groups, self.performance_executions.values(), self.execution_group_links,
                                   self.order_journal, self.positions if positions is None else positions, positions_available)
        return {**report, "network": "testnet" if self.settings.private_testnet else "mainnet", "updated_at": self.performance_updated_at,
                "history_start_ms": self.performance_start_ms, "history_cursor_ms": self.performance_cursor_ms,
                "syncing": self.performance_cursor_ms is not None and self.performance_cursor_ms < int(datetime.now(timezone.utc).timestamp() * 1000) - 120000,
                "error": self.state_error or self.performance_error, "credentials_available": bool(self.settings.bybit_api_key and self.settings.bybit_api_secret)}
