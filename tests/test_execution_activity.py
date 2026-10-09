from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

from app.execution_activity import execution_dashboard


class ExecutionActivityTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 9, 21, 0, tzinfo=timezone.utc)
        self.call, self.put = "BTC-11OCT26-100000-C", "BTC-11OCT26-90000-P"
        self.groups = {"group": {"type": "open", "order_tracking": True, "task_version": 1,
                                  "strategy_mode": "short_strangle", "execution_status": "running",
                                  "accepted_at": (self.now - timedelta(seconds=12)).isoformat(),
                                  "legs": {self.call: {"side": "Sell", "qty": .1},
                                           self.put: {"side": "Sell", "qty": .1}}}}
        self.links, self.journal, self.activity, self.records = {}, {}, {}, []

    def order(self, link, symbol, qty, filled, *, terminal=True, kind="BBO", phase="working"):
        self.links[link] = "group"
        self.journal[link] = {"symbol": symbol, "side": "Sell", "qty": qty, "filledQty": filled,
                              "terminal": terminal, "status": "filled" if filled == qty else "partial",
                              "reduce_only": False, "execution_type": kind}
        self.activity[link] = {"phase": phase}

    def fill(self, identity, link, symbol, qty, price=100, fee=.1, currency="USDC"):
        record = {"exec_id": identity, "order_link_id": link, "symbol": symbol, "side": "Sell",
                  "exec_qty": qty, "exec_price": price, "exec_fee": fee, "fee_currency": currency}
        self.records.append(record)
        return record

    def cards(self, **kwargs):
        return execution_dashboard(self.groups, self.links, self.journal, self.activity, self.records,
                                   now=self.now, **kwargs)

    def test_ioc_fills_belong_to_the_original_target_and_duplicates_do_not_change_amounts(self):
        self.order("bbo", self.call, .1, .04)
        self.order("ioc", self.call, .06, .06, kind="IOC")
        self.order("put", self.put, .1, .05, terminal=False)
        original = self.fill("one", "bbo", self.call, .04, fee=.04)
        self.records.append(dict(original))
        self.fill("two", "ioc", self.call, .06, price=110, fee=.06)
        self.fill("three", "put", self.put, .05, price=120, fee=.05)
        card = self.cards()[0]
        self.assertEqual((card["completed_legs"], card["total_legs"]), (1, 2))
        self.assertAlmostEqual(card["progress_ratio"], .75)
        call = next(leg for leg in card["legs"] if leg["symbol"] == self.call)
        self.assertEqual((call["target_qty"], call["filled_qty"], call["remaining_qty"]), (.1, .1, 0))
        self.assertEqual(len(call["orders"]), 2)
        self.assertTrue(card["amounts_complete"])
        self.assertEqual((card["gross_amount"], card["fee_amount"], card["net_amount"]), (16.6, .15, 16.45))
        self.assertEqual(card["currency"], "USDC")
        self.assertEqual(card["elapsed_seconds"], 12)

    def test_missing_fill_details_and_mixed_fee_currency_never_become_zero_cost(self):
        self.order("one", self.call, .1, .03)
        card = self.cards()[0]
        self.assertFalse(card["amounts_complete"])
        self.assertIsNone(card["fee_amount"])
        self.fill("e", "one", self.call, .03, currency="BTC")
        card = self.cards()[0]
        self.assertFalse(card["amounts_complete"])
        self.assertIsNone(card["gross_amount"])
        self.assertIsNone(card["net_amount"])

    def test_conflicting_duplicate_fill_identity_is_not_silently_accepted(self):
        self.order("one", self.call, .1, .03)
        record = self.fill("e", "one", self.call, .03)
        self.records.append({**record, "exec_price": 200})
        self.assertFalse(self.cards()[0]["amounts_complete"])

    def test_cancelled_partial_group_does_not_look_complete_and_unknown_stays_active(self):
        self.order("one", self.call, .1, .03)
        self.order("two", self.put, .1, 0, terminal=False, phase="unknown")
        self.groups["group"]["execution_status"] = "stopped"
        card = self.cards()[0]
        self.assertEqual(card["execution_status"], "recovery_needed")
        self.assertEqual(card["completed_legs"], 0)
        self.assertTrue(card["has_unknown"])
        self.assertTrue(card["can_stop"])
        self.assertEqual(card["legs"][0]["remaining_qty"], .07)

    def test_legacy_closed_position_status_is_not_an_execution_status(self):
        self.order("one", self.call, .1, .1)
        self.order("two", self.put, .1, .1)
        group = self.groups["group"]
        group.pop("task_version")
        group.pop("execution_status")
        group["status"] = "closed"
        card = self.cards()[0]
        self.assertEqual(card["execution_status"], "completed")
        self.assertFalse(card["can_stop"])
        self.assertIsNone(card["elapsed_seconds"])

    def test_legacy_close_target_uses_original_order_size_not_original_plus_ioc(self):
        self.groups["group"] = {"type": "close", "order_tracking": True}
        self.order("one", self.call, .1, .04)
        self.order("two", self.call, .06, .06, kind="IOC")
        card = self.cards()[0]
        self.assertEqual(card["legs"][0]["target_qty"], .1)
        self.assertEqual(card["execution_status"], "completed")

    def test_overfill_keeps_actual_quantity_and_requires_recovery(self):
        self.order("one", self.call, .1, .1)
        self.order("two", self.call, .02, .02, kind="IOC")
        card = self.cards()[0]
        self.assertEqual(card["legs"][0]["filled_qty"], .12)
        self.assertFalse(card["legs"][0]["complete"])
        self.assertEqual(card["execution_status"], "recovery_needed")

    def test_snapshot_has_no_mutation_or_unbounded_history_but_keeps_every_active_group(self):
        self.order("one", self.call, .1, .03)
        self.groups["group"]["secret"] = "never-export"
        self.journal["one"]["raw_exchange_response"] = "never-export"
        for index in range(35):
            self.groups[f"history-{index}"] = {**deepcopy(self.groups["group"]), "execution_status": "stopped"}
            self.groups[f"active-{index}"] = {**deepcopy(self.groups["group"]), "execution_status": "accepted"}
        before = deepcopy((self.groups, self.links, self.journal, self.activity, self.records))
        cards = self.cards()
        self.assertEqual(len(cards), 36 + 30)
        self.assertEqual(before, (self.groups, self.links, self.journal, self.activity, self.records))
        self.assertNotIn("never-export", str(cards))
        self.assertEqual(len(self.cards(history_limit=None)), 71)

    def test_simulation_has_progress_but_no_fabricated_exchange_cash_amounts(self):
        self.groups["group"].update(live=False, execution_status="completed",
                                   simulated_fills={self.call: .1, self.put: .1})
        card = self.cards()[0]
        self.assertTrue(card["simulated"])
        self.assertEqual(card["progress_ratio"], 1)
        self.assertEqual(card["completed_legs"], 2)
        self.assertFalse(card["amounts_complete"])
        self.assertIsNone(card["net_amount"])


if __name__ == "__main__":
    unittest.main()
