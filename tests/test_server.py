import json
import tempfile
import unittest
import urllib.request
from pathlib import Path

from albion_tracker.db import Database
from albion_tracker.server import serve_in_thread


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database = Database(Path(self.temp.name) / "test.sqlite3")
        web_root = Path(self.temp.name) / "web"
        web_root.mkdir()
        (web_root / "index.html").write_text("ok", encoding="utf-8")
        try:
            self.server, self.thread = serve_in_thread(database, "127.0.0.1", 0, web_root)
        except PermissionError as error:
            self.temp.cleanup()
            self.skipTest(f"執行環境不允許建立本機測試 socket：{error}")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, payload=None, method=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + path,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())

    def test_health_and_purchase_ingest(self):
        status, health = self.request("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["ok"])

        event = {
            "type": "purchase",
            "source_event_id": "http-1",
            "item_id": "T5_CAPE",
            "quantity": 2,
            "unit_price": 900,
        }
        _, result = self.request("/api/events", event)
        self.assertTrue(result["inserted"])
        _, purchases = self.request("/api/purchases")
        self.assertEqual(purchases["items"][0]["total_price"], 1800)
        _, prices = self.request("/api/ledger/prices?ids=T5_CAPE")
        self.assertEqual(prices["items"][0]["buy_unit_price"], 900)

    def test_sale_snapshots_and_project_api(self):
        sale = {
            "type": "transaction",
            "source_event_id": "sale-http-1",
            "traded_at": "2026-08-14T12:01:00Z",
            "direction": "sell",
            "transaction_kind": "order",
            "item_id": "T1_HIDE",
            "quantity": 2,
            "unit_price": 12,
            "location_id": "1002",
        }
        _, created = self.request("/api/events", sale)
        _, transactions = self.request("/api/transactions")
        self.assertEqual(transactions["items"][0]["direction"], "sell")
        self.assertEqual(transactions["items"][0]["location_name"], "林姆赫斯特 (Lymhurst)")
        _, snapshots = self.request("/api/snapshots")
        self.assertEqual(snapshots["items"][0]["revenue"], 24)

        status, project = self.request(
            "/api/projects",
            {
                "name": "HTTP 專案",
                "selections": [{"transaction_id": created["id"], "quantity": 1}],
            },
        )
        self.assertEqual(status, 201)
        _, projects = self.request("/api/projects")
        self.assertEqual(projects["items"][0]["id"], project["id"])
        self.assertEqual(projects["items"][0]["revenue"], 12)

    def test_crafting_catalog_and_multistage_plan_api(self):
        status, catalog = self.request("/api/crafting/catalog")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["item_id"] == "T7_LEATHER_LEVEL1@1" for item in catalog["items"]))

        status, plan = self.request("/api/crafting/plan", {
            "family": "leather", "target_tier": 7, "start_tier": 5,
            "enchantment": 1, "quantity": 100, "return_rate": 36.7,
        })
        self.assertEqual(status, 200)
        self.assertEqual([item["required_quantity"] for item in plan["materials"]], [41, 163, 317])

        status, comparison = self.request("/api/crafting/transmutation", {
            "family": "metalbar", "target_tier": 6, "enchantment": 1,
        })
        self.assertEqual(status, 200)
        self.assertEqual([route["id"] for route in comparison["routes"]], ["tier", "enchantment"])

    def test_mail_cutoff_setting_and_clear_ledger_api(self):
        status, saved = self.request("/api/settings", {
            "mail_import_after": "2026-08-15T00:00:00Z",
        }, method="PUT")
        self.assertEqual(status, 200)
        self.assertEqual(saved["settings"]["mail_import_after"], "2026-08-15T00:00:00Z")
        self.request("/api/transactions", {
            "direction": "buy", "transaction_kind": "instant", "item_id": "T4_BAG",
            "quantity": 1, "unit_price": 10,
        })
        status, cleared = self.request("/api/ledger", {
            "confirmation": "CLEAR_LEDGER",
        }, method="DELETE")
        self.assertEqual(status, 200)
        self.assertEqual(cleared["deleted"]["transactions"], 1)
        _, transactions = self.request("/api/transactions")
        self.assertEqual(transactions["items"], [])


if __name__ == "__main__":
    unittest.main()
