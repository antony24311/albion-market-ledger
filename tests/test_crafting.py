import unittest

from albion_tracker.crafting import (
    CraftingValidationError,
    build_refining_plan,
    build_transmutation_options,
    catalog,
)


class CraftingTests(unittest.TestCase):
    def test_multistage_71_leather_from_51(self):
        plan = build_refining_plan({
            "family": "leather",
            "target_tier": 7,
            "start_tier": 5,
            "enchantment": 1,
            "quantity": 100,
            "return_rate": 36.7,
        })
        self.assertEqual(
            [(item["name"], item["required_quantity"]) for item in plan["materials"]],
            [("5.1 皮革", 41), ("6.1 粗皮", 163), ("7.1 粗皮", 317)],
        )
        self.assertEqual([(step["output_name"], step["crafts"]) for step in plan["steps"]], [
            ("6.1 皮革", 64), ("7.1 皮革", 100),
        ])
        self.assertEqual(plan["materials"][0]["gross_quantity"], 64)

    def test_focus_and_station_fees_are_split_by_stage(self):
        plan = build_refining_plan({
            "family": "cloth", "target_tier": 7, "start_tier": 5, "enchantment": 1,
            "quantity": 100, "return_rate": 36.7, "focus_return_rate": 53.9,
            "use_focus": True, "available_focus": 503 * 50,
            "station_fees": {"T7_CLOTH_LEVEL1@1": 1736},
        })
        top = plan["steps"][-1]
        self.assertEqual(top["focus_crafts"], 50)
        self.assertEqual(top["normal_crafts"], 50)
        self.assertEqual(plan["focus_used"], 503 * 50)
        self.assertEqual(plan["total_station_cost"], 173600)

    def test_actual_focus_cost_can_override_base_cost(self):
        plan = build_refining_plan({
            "family": "metalbar", "target_tier": 6, "start_tier": 5, "enchantment": 2,
            "quantity": 100, "use_focus": True, "available_focus": 3000,
            "focus_costs": {"T6_METALBAR_LEVEL2@2": 30},
        })
        self.assertEqual(plan["steps"][0]["base_focus_cost_per_craft"], 503)
        self.assertEqual(plan["steps"][0]["focus_cost_per_craft"], 30)
        self.assertEqual(plan["steps"][0]["focus_crafts"], 100)

    def test_single_stage_keeps_gross_starting_material_for_readiness(self):
        plan = build_refining_plan({
            "family": "metalbar", "target_tier": 6, "start_tier": 5,
            "enchantment": 2, "quantity": 100, "return_rate": 36.7,
        })
        self.assertEqual([(item["name"], item["gross_quantity"], item["required_quantity"]) for item in plan["materials"]], [
            ("5.2 金屬錠", 100, 64), ("6.2 礦石", 400, 254),
        ])

    def test_transmutation_compares_both_routes(self):
        plan = build_transmutation_options({"family": "metalbar", "target_tier": 6, "enchantment": 1})
        self.assertEqual(plan["target_item"]["item_id"], "T6_ORE_LEVEL1@1")
        self.assertEqual([(route["id"], route["input_item"]["item_id"], route["base_silver_fee"]) for route in plan["routes"]], [
            ("tier", "T5_ORE_LEVEL1@1", 2500),
            ("enchantment", "T6_ORE", 3000),
        ])

    def test_catalog_has_auto_names_and_images(self):
        items = catalog()["items"]
        item = next(value for value in items if value["item_id"] == "T4_FIBER_LEVEL1@1")
        self.assertEqual(item["name"], "4.1 纖維")
        self.assertIn("render.albiononline.com", item["icon_url"])

    def test_invalid_chain_is_rejected(self):
        with self.assertRaises(CraftingValidationError):
            build_refining_plan({"target_tier": 5, "start_tier": 6})


if __name__ == "__main__":
    unittest.main()
