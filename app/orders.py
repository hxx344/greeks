import asyncio
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR, ROUND_CEILING
from math import isclose, isfinite

from .order_activity import EXCHANGE_STATUSES


TERMINAL_STATUSES = {"Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated"}


class OrderExecutor:
    """Submit once and reconcile by order identity, never by account position."""

    def __init__(self, client, settings, log):
        self.client = client
        self.settings = settings
        self.log = log

    @staticmethod
    def _observe(observer, phase: str, **details) -> None:
        # Presentation failures must never enter execution/cancellation handling.
        if observer is not None:
            try:
                observer({"phase": phase, **details})
            except Exception:
                pass

    def _observe_snapshot(self, observer, order: dict | None, outcome: dict) -> None:
        try:
            if order is None or order.get("orderStatus") not in EXCHANGE_STATUSES:
                self._observe(observer, "unknown")
                return
            # This runs only after snapshot validates side and cumulative size.
            # A submission/amendment acknowledgement is not a price observation.
            try:
                raw_price = order.get("price", "nan")
                price = float(raw_price)
                price = price if not isinstance(raw_price, bool) and isfinite(price) and price > 0 else None
            except Exception:
                price = None
            self._observe(observer, "terminal" if outcome["terminal"] else "working", confirmed=True,
                          exchange_status=order["orderStatus"], confirmed_price=price, order_id=outcome.get("orderId"))
        except Exception:
            self._observe(observer, "unknown")

    @staticmethod
    def snapshot(order: dict | None, qty: float, side: str, previous: dict) -> dict:
        if order is None:
            return previous
        filled = float(order.get("cumExecQty", "nan"))
        requested = float(order.get("qty", "nan"))
        if (order.get("side") != side or not isfinite(requested) or not isclose(requested, qty, rel_tol=0, abs_tol=1e-9)
                or not isfinite(filled) or filled < previous["filledQty"] - 1e-9 or filled < 0 or filled > qty + 1e-9):
            raise ValueError("Order reconciliation returned inconsistent quantities or side")
        terminal = order.get("orderStatus") in TERMINAL_STATUSES
        full = isclose(filled, qty, rel_tol=0, abs_tol=1e-9)
        if order.get("orderStatus") == "Filled" and not full:
            raise ValueError("Filled order has an incomplete cumulative quantity")
        status = ("filled" if full else "partial" if filled > 0 else "timeout_cancelled") if terminal else "unknown"
        return {**previous, "orderId": order.get("orderId", previous.get("orderId", "")),
                "status": status, "terminal": terminal, "filledQty": min(qty, filled)}

    async def reconcile(self, symbol: str, side: str, qty: float, link: str, previous: dict, cancel: bool = True, observer=None, record=None) -> dict:
        outcome = {**previous, "status": "unknown", "terminal": False}
        if cancel:
            self._observe(observer, "cancelling")
            try:
                await self.client.cancel_order(symbol, link)
            except Exception as exc:
                self.log("WARNING", f"Cancellation not acknowledged for {link}: {exc}")
        for attempt in range(self.settings.failed_leg_position_checks):
            self._observe(observer, "reconciling")
            try:
                order = await self.client.order(symbol, link)
                outcome = self.snapshot(order, qty, side, outcome)
            except Exception as exc:
                self._observe(observer, "unknown")
                self.log("WARNING", f"Could not reconcile order {link}: {exc}")
            else:
                self._observe_snapshot(observer, order, outcome)
                if order is not None and record is not None:
                    record(outcome)
                if outcome["terminal"]:
                    return outcome
            if attempt + 1 < self.settings.failed_leg_position_checks:
                await asyncio.sleep(self.settings.failed_leg_position_check_interval_seconds)
        outcome["message"] = "Order state is unknown; automatic retry is blocked"
        self._observe(observer, "unknown")
        self.log("ERROR", f"Order {link} remains unresolved; automatic retry is blocked")
        return outcome

    async def execute(self, instrument, side: str, qty: float, link: str, record, reduce_only: bool = False, market: bool = False, check_price=None, observer=None) -> dict:
        outcome = {"orderId": "", "orderLinkId": link, "status": "unknown", "terminal": False, "filledQty": 0.0}
        attempted = False
        last_price = None
        deadline = asyncio.get_running_loop().time() + self.settings.bbo_order_timeout_seconds
        try:
            if market:
                latest = (await self.client.tickers(symbol=instrument.symbol) or [{}])[0]
                price = float(latest.get("ask1Price" if side == "Buy" else "bid1Price", 0) or 0)
                tick = Decimal(str(instrument.price_tick))
                if not isfinite(price) or price <= 0 or not tick.is_finite() or tick <= 0:
                    raise ValueError("No usable price for bounded IOC fallback")
                rounding = ROUND_FLOOR if side == "Buy" else ROUND_CEILING
                price = float((Decimal(str(price)) / tick).quantize(Decimal("1"), rounding=rounding) * tick)
                if price <= 0:
                    raise ValueError("IOC price is below the minimum tick")
                if check_price:
                    check_price(price)
                attempted = True
                self._observe(observer, "submitting", requested_price=price)
                response = await self.client.place_ioc_order(instrument.symbol, side, qty, price, link, reduce_only)
                outcome["orderId"] = response.get("orderId", "")
                self._observe(observer, "reconciling", order_id=outcome["orderId"])
                outcome = await self.reconcile(instrument.symbol, side, qty, link, outcome, cancel=False, observer=observer, record=record)
                if not outcome["terminal"]:
                    outcome = await self.reconcile(instrument.symbol, side, qty, link, outcome, observer=observer, record=record)
            else:
                while asyncio.get_running_loop().time() < deadline:
                    if check_price:
                        check_price(None)
                    if attempted:
                        self._observe(observer, "reconciling")
                        order = await self.client.order(instrument.symbol, link)
                        outcome = self.snapshot(order, qty, side, outcome)
                        self._observe_snapshot(observer, order, outcome)
                        record(outcome)
                        if outcome["terminal"]:
                            break
                    latest = (await self.client.tickers(symbol=instrument.symbol) or [{}])[0]
                    price = float(latest.get("bid1Price" if side == "Buy" else "ask1Price", 0) or 0)
                    if isfinite(price) and price > 0:
                        tick = Decimal(str(instrument.price_tick))
                        if not tick.is_finite() or tick <= 0:
                            raise ValueError("Invalid instrument price tick")
                        price = float((Decimal(str(price)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick)
                        if price <= 0:
                            raise ValueError("BBO price is below the minimum tick")
                        if check_price:
                            check_price(price)
                        if not attempted:
                            # Set before awaiting: a lost response can hide an accepted order.
                            attempted = True
                            self._observe(observer, "submitting", requested_price=price)
                            response = await self.client.place_limit_order(instrument.symbol, side, qty, price, link, reduce_only)
                            outcome["orderId"] = response.get("orderId", "")
                            self._observe(observer, "reconciling", order_id=outcome["orderId"])
                            last_price = price
                        elif price != last_price:
                            self._observe(observer, "amending", requested_price=price)
                            await self.client.amend_order(instrument.symbol, link, price)
                            self._observe(observer, "reconciling")
                            last_price = price
                    await asyncio.sleep(self.settings.bbo_poll_seconds)
                if attempted and not outcome["terminal"]:
                    outcome = await self.reconcile(instrument.symbol, side, qty, link, outcome, observer=observer, record=record)
                elif not attempted:
                    outcome.update(status="not_submitted", terminal=True, message="No usable BBO price; no order was submitted")
        except asyncio.CancelledError:
            if attempted:
                outcome = await self.reconcile(instrument.symbol, side, qty, link, outcome, observer=observer, record=record)
            else:
                outcome.update(status="not_submitted", terminal=True)
            self._observe(observer, "terminal" if outcome["terminal"] else "unknown")
            record(outcome)
            raise
        except Exception as exc:
            self.log("ERROR", f"Order execution interrupted for {link}: {exc}")
            if attempted:
                outcome = await self.reconcile(instrument.symbol, side, qty, link, outcome, observer=observer, record=record)
            else:
                outcome.update(status="not_submitted", terminal=True)
            outcome["message"] = str(exc)
        self._observe(observer, "terminal" if outcome["terminal"] else "unknown")
        record(outcome)
        return outcome
