import unittest
from unittest.mock import patch

from albion_tracker.market import fetch_market_prices


class MarketTests(unittest.TestCase):
    @patch("albion_tracker.market._request_json")
    def test_history_average_fills_missing_current_price(self, request_json):
        request_json.side_effect = [
            [{"item_id": "T6_ORE_LEVEL1@1", "city": "Martlock", "quality": 1,
              "sell_price_min": 0, "buy_price_max": 0}],
            [{"item_id": "T6_ORE_LEVEL1@1", "location": "Martlock", "quality": 1, "data": [
                {"item_count": 2, "avg_price": 100, "timestamp": "2026-08-13T00:00:00"},
                {"item_count": 1, "avg_price": 130, "timestamp": "2026-08-14T00:00:00"},
            ]}],
        ]
        price = fetch_market_prices(["T6_ORE_LEVEL1@1"], "east", "Martlock")[0]
        self.assertEqual(price["history_avg_price"], 110)
        self.assertEqual(price["history_sample_days"], 2)


if __name__ == "__main__":
    unittest.main()
