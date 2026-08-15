import tempfile
import unittest
from pathlib import Path

from albion_tracker.db import Database, ValidationError


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "test.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def purchase(self, event_id="event-1"):
        return {
            "type": "purchase",
            "source_event_id": event_id,
            "purchased_at": "2026-08-14T10:00:00Z",
            "item_id": "T4_BAG",
            "quantity": 3,
            "unit_price": 1250,
            "total_price": 3750,
            "location_id": "3005",
            "source": "test",
        }

    def test_insert_and_deduplicate_purchase(self):
        inserted, row_id = self.database.insert_purchase(self.purchase())
        duplicate, duplicate_id = self.database.insert_purchase(self.purchase())
        self.assertTrue(inserted)
        self.assertFalse(duplicate)
        self.assertEqual(row_id, duplicate_id)
        self.assertEqual(self.database.summary()["totals"]["spent"], 3750)

    def test_total_must_match_quantity_times_price(self):
        event = self.purchase()
        event["total_price"] = 1
        with self.assertRaises(ValidationError):
            self.database.insert_purchase(event)

    def test_status_online_is_computed(self):
        self.database.update_status(
            {
                "client_id": "test-client",
                "state": "connected",
                "packets_seen": 42,
                "captured_at": "2026-08-14T10:00:00Z",
            }
        )
        status = self.database.summary()["capture"][0]
        self.assertEqual(status["packets_seen"], 42)
        self.assertFalse(status["online"])

    def test_sale_snapshot_and_partial_project_totals(self):
        self.database.insert_purchase(self.purchase())
        self.database.insert_transaction(
            {
                "type": "transaction",
                "source_event_id": "sale-1",
                "traded_at": "2026-08-14T10:02:59Z",
                "direction": "sell",
                "transaction_kind": "order",
                "item_id": "T4_BAG",
                "quantity": 2,
                "unit_price": 2000,
                "location_id": "1002",
                "source": "sell_order_mail",
            }
        )
        totals = self.database.summary()["totals"]
        self.assertEqual(totals["spent"], 3750)
        self.assertEqual(totals["revenue"], 4000)
        snapshots = self.database.list_snapshots()
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["transaction_count"], 2)
        self.assertEqual(len(snapshots[0]["items"]), 2)
        self.assertEqual(sum(item["total_price"] for item in snapshots[0]["items"]), 7750)

        sale = self.database.list_transactions(direction="sell")[0]
        project_id = self.database.create_project(
            "測試專案", [{"transaction_id": sale["id"], "quantity": 1}]
        )
        project = self.database.list_projects()[0]
        self.assertEqual(project["id"], project_id)
        self.assertEqual(project["revenue"], 2000)
        self.assertEqual(project["items"][0]["quantity"], 1)

    def test_legacy_instant_price_is_corrected_during_migration(self):
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO purchases(
                    source_event_id, purchased_at, captured_at, item_id, quantity,
                    unit_price, total_price, location_id, source, raw_event
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "legacy-1", "2026-08-14T11:00:00Z", "2026-08-14T11:00:00Z",
                    "T1_HIDE", 1, 100000, 100000, "1002", "auction_buy_offer", "{}",
                ),
            )
        Database(self.database.path)
        transaction = self.database.list_transactions()[0]
        self.assertEqual(transaction["unit_price"], 10)
        self.assertEqual(transaction["total_price"], 10)
        self.assertEqual(transaction["location_name"], "林姆赫斯特 (Lymhurst)")
        self.assertEqual(transaction["item_name"], "零碎獸皮")

    def test_all_projects_share_one_allocation_limit(self):
        self.database.insert_purchase(self.purchase())
        transaction = self.database.list_transactions()[0]
        self.database.create_project("第一案", [{"transaction_id": transaction["id"], "quantity": 2}])
        with self.assertRaisesRegex(ValidationError, "只剩 1 件"):
            self.database.create_project("第二案", [{"transaction_id": transaction["id"], "quantity": 2}])
        self.database.create_project("第二案", [{"transaction_id": transaction["id"], "quantity": 1}])
        transaction = self.database.list_transactions()[0]
        self.assertEqual(transaction["allocated_quantity"], 3)
        self.assertEqual(transaction["available_quantity"], 0)

    def test_manufacturing_output_and_realized_net_profit(self):
        material = self.purchase()
        material.update({"quantity": 100, "unit_price": 10, "total_price": 1000, "item_id": "T1_HIDE"})
        _, material_id = self.database.insert_purchase(material)
        _, sale_id = self.database.insert_transaction({
            "type": "transaction", "source_event_id": "leather-sale", "direction": "sell",
            "transaction_kind": "order", "item_id": "T2_LEATHER", "quantity": 100,
            "unit_price": 20, "traded_at": "2026-08-14T11:00:00Z",
        })
        self.database.create_project("粗皮加工", [
            {"transaction_id": material_id, "quantity": 100},
            {"transaction_id": sale_id, "quantity": 100},
        ], {
            "project_type": "manufacturing", "input_item_id": "T1_HIDE",
            "output_item_id": "T2_LEATHER", "return_rate": 36.7,
            "focus_return_rate": 53.9, "use_focus": False, "material_per_unit": 1,
            "output_per_craft": 1, "extra_cost": 100, "sale_fee_rate": 5,
            "target_sale_price": 20,
        })
        project = self.database.list_projects()[0]
        self.assertEqual(project["expected_output"], 157)
        self.assertEqual(project["unsold_output_quantity"], 57)
        self.assertEqual(project["fees"], 100)
        self.assertEqual(project["net_profit"], 800)

    def test_transaction_can_be_edited_archived_and_soft_deleted(self):
        _, transaction_id = self.database.insert_purchase(self.purchase())
        updated = self.database.update_transaction(transaction_id, {"unit_price": 1000, "status": "sold"})
        self.assertEqual(updated["total_price"], 3000)
        self.assertEqual(self.database.list_transactions(), [])
        self.assertEqual(self.database.list_transactions(include_sold=True)[0]["status"], "sold")
        self.assertTrue(self.database.delete_transaction(transaction_id))
        self.assertEqual(self.database.summary()["totals"]["transactions"], 0)

    def test_mail_cross_validation_and_exact_order_total(self):
        self.database.upsert_mail_metadata({
            "mail_id": 991, "mail_type": "MARKETPLACE_SELLORDER_FINISHED_SUMMARY",
            "mail_received": 1786752000, "location_id": "1002",
        })
        inserted, transaction_id = self.database.insert_transaction({
            "type": "transaction", "source_event_id": "mail:991", "mail_id": 991,
            "mail_type": "MARKETPLACE_SELLORDER_FINISHED_SUMMARY", "direction": "sell",
            "transaction_kind": "order", "item_id": "T2_LEATHER", "quantity": 3,
            "unit_price": 34, "total_price": 101, "source": "sell_order_mail",
        })
        self.assertTrue(inserted)
        transaction = self.database.list_transactions(direction="sell")[0]
        self.assertEqual(transaction["id"], transaction_id)
        self.assertEqual(transaction["total_price"], 101)
        self.assertEqual(transaction["sales_tax"], 4)
        self.assertEqual(transaction["setup_fee"], 3)
        self.assertEqual(transaction["net_total"], 94)
        mail = self.database.list_market_mails()[0]
        self.assertEqual(mail["state"], "completed")
        self.assertEqual(mail["transaction_id"], transaction_id)

    def test_multi_material_focus_shortage_calculator(self):
        material = self.purchase()
        material.update({"quantity": 200, "unit_price": 10, "total_price": 2000, "item_id": "T1_HIDE"})
        _, transaction_id = self.database.insert_purchase(material)
        self.database.create_project("多材料皮革", [{"transaction_id": transaction_id, "quantity": 100}], {
            "project_type": "manufacturing", "target_output": 100, "output_per_craft": 1,
            "return_rate": 36.7, "focus_return_rate": 53.9, "use_focus": True,
            "available_focus": 50, "focus_cost_per_craft": 10,
            "planning_mode": "expected",
            "materials": [
                {"item_id": "T1_HIDE", "quantity_per_craft": 1},
                {"item_id": "T1_ROCK", "quantity_per_craft": 2},
            ],
        })
        project = self.database.list_projects()[0]
        self.assertEqual(project["focus_crafts"], 5)
        self.assertEqual(project["normal_crafts"], 95)
        self.assertEqual(project["focus_shortage"], 950)
        self.assertEqual([x["required_quantity"] for x in project["required_materials"]], [63, 125])
        first = project["required_materials"][0]
        self.assertGreater(first["p95_quantity"], first["expected_quantity"])
        self.assertGreater(first["p99_quantity"], first["p95_quantity"])
        self.assertEqual(first["guaranteed_quantity"], 100)

    def test_multistage_plan_can_be_saved_before_transactions_exist(self):
        planner_state = {
            "family": "leather", "target_tier": 7, "start_tier": 5, "enchantment": 1,
            "quantity": 100, "return_rate": 36.7, "focus_return_rate": 53.9,
            "prices": {"T5_LEATHER_LEVEL1@1": 2000, "T6_HIDE_LEVEL1@1": 3500, "T7_HIDE_LEVEL1@1": 9000},
            "station_fees": {"T7_LEATHER_LEVEL1@1": 100},
        }
        project_id = self.database.create_project("7.1 皮革規劃", [], {
            "project_type": "manufacturing", "output_item_id": "T7_LEATHER_LEVEL1@1",
            "output_item_name": "7.1 皮革", "target_output": 100,
            "return_rate": 36.7, "focus_return_rate": 53.9, "material_per_unit": 1,
            "output_per_craft": 1, "extra_cost": 0, "sale_fee_rate": 4,
            "target_sale_price": 40000, "planner_state": planner_state,
        })
        project = self.database.list_projects()[0]
        self.assertEqual(project["id"], project_id)
        self.assertEqual(project["selection_count"], 0)
        self.assertEqual([item["required_quantity"] for item in project["required_materials"]], [41, 163, 317])
        self.assertEqual(project["planned_station_cost"], 10000)
        self.assertEqual(project["expected_output"], 100)
        self.assertGreater(project["projected_profit"], 0)

    def test_project_readiness_combines_allocated_and_manual_inventory(self):
        material = self.purchase()
        material.update({"quantity": 60, "unit_price": 100, "total_price": 6000,
                         "item_id": "T5_METALBAR_LEVEL2@2"})
        _, transaction_id = self.database.insert_purchase(material)
        planner_state = {
            "family": "metalbar", "target_tier": 6, "start_tier": 5, "enchantment": 2,
            "quantity": 100, "return_rate": 36.7, "focus_return_rate": 53.9,
            "inventory": {"T5_METALBAR_LEVEL2@2": 25},
        }
        self.database.create_project("6.2 金屬錠", [{"transaction_id": transaction_id, "quantity": 60}], {
            "project_type": "manufacturing", "output_item_id": "T6_METALBAR_LEVEL2@2",
            "output_item_name": "6.2 金屬錠", "target_output": 100,
            "return_rate": 36.7, "focus_return_rate": 53.9, "material_per_unit": 1,
            "output_per_craft": 1, "extra_cost": 0, "sale_fee_rate": 4,
            "target_sale_price": 0, "planner_state": planner_state,
        })
        item = self.database.list_projects()[0]["required_materials"][0]
        self.assertEqual(item["ready_required_quantity"], 100)
        self.assertEqual(item["allocated_quantity"], 60)
        self.assertEqual(item["inventory_quantity"], 25)
        self.assertEqual(item["shortage"], 15)

    def test_private_trade_accepts_exact_total_and_calculated_unit_price(self):
        _, transaction_id = self.database.insert_transaction({
            "type": "transaction", "source_event_id": "private-1", "source": "private_trade",
            "direction": "buy", "transaction_kind": "instant", "item_id": "T6_ORE_LEVEL2@2",
            "quantity": 3, "unit_price": 33_333_333, "total_price": 100_000_000,
        })
        transaction = next(item for item in self.database.list_transactions() if item["id"] == transaction_id)
        self.assertEqual(transaction["unit_price"], 33_333_333)
        self.assertEqual(transaction["total_price"], 100_000_000)

    def test_ledger_and_project_costs_use_quantity_weighted_orders(self):
        first = self.purchase("weighted-1")
        first.update({"item_id": "T6_ORE_LEVEL2@2", "quantity": 10, "unit_price": 100, "total_price": 1000})
        second = self.purchase("weighted-2")
        second.update({"item_id": "T6_ORE_LEVEL2@2", "quantity": 10, "unit_price": 200, "total_price": 2000})
        _, first_id = self.database.insert_purchase(first)
        _, second_id = self.database.insert_purchase(second)
        estimate = self.database.estimate_item_prices(["T6_ORE_LEVEL2@2"])["T6_ORE_LEVEL2@2"]
        self.assertEqual(estimate["buy_unit_price"], 150)
        self.database.create_project("加權成本", [
            {"transaction_id": first_id, "quantity": 4},
            {"transaction_id": second_id, "quantity": 6},
        ], {
            "project_type": "manufacturing", "input_item_id": "T6_ORE_LEVEL2@2",
            "output_item_id": "T6_METALBAR_LEVEL2@2", "output_item_name": "6.2 金屬錠",
            "return_rate": 0, "focus_return_rate": 0, "material_per_unit": 1,
            "output_per_craft": 1, "extra_cost": 0, "sale_fee_rate": 0,
            "target_sale_price": 0, "target_output": 10, "available_focus": 0,
            "focus_cost_per_craft": 0, "planning_mode": "expected",
            "materials": [{"item_id": "T6_ORE_LEVEL2@2", "quantity_per_craft": 1}],
        })
        self.assertEqual(self.database.list_projects()[0]["spent"], 1600)

    def test_zero_fill_expired_order_is_not_a_transaction(self):
        self.database.upsert_mail_metadata({
            "mail_id": 992, "mail_type": "MARKETPLACE_SELLORDER_EXPIRED_SUMMARY",
            "mail_received": 1786752000, "location_id": "1002",
        })
        self.database.resolve_market_mail({
            "mail_id": 992, "mail_type": "MARKETPLACE_SELLORDER_EXPIRED_SUMMARY",
            "mail_state": "no_trade", "raw_params": "0|39|0|T7_JOURNAL_HUNTER_FULL|",
        })
        self.assertEqual(self.database.summary()["totals"]["transactions"], 0)
        mail = self.database.list_market_mails()[0]
        self.assertEqual(mail["state"], "no_trade")
        self.assertIsNone(mail["transaction_id"])


if __name__ == "__main__":
    unittest.main()
