import unittest
from decimal import Decimal

from scripts.yuexin_t_paper import simulate
from scripts.check_research_coverage import audit


def bar(day, price, signal):
    return {"session": day, "open": str(price), "high": str(price),
            "low": str(price), "close": str(price), "volume": "100000",
            "signal": signal}


class YuexinPaperTest(unittest.TestCase):
    def test_reverse_t_profit_and_inventory(self):
        result = simulate([bar("2026-10-16", 40, "sell"),
                           bar("2026-10-19", 35, "buyback")])
        self.assertEqual(result["summary"]["final_shares"], 1500)
        self.assertEqual(result["summary"]["unrecovered_shares"], 0)
        self.assertEqual(result["summary"]["fills"], 2)
        self.assertGreater(Decimal(result["summary"]["final_relative_nav"]), 0)

    def test_cannot_buy_without_prior_sale(self):
        result = simulate([bar("2026-10-16", 35, "buyback")])
        self.assertEqual(result["summary"]["fills"], 0)

    def test_missing_yuexin_flagged(self):
        report = audit({"watchlist": ["300136", "301297"], "deep_analysis_count": 10})
        self.assertEqual(report["missing"], ["301660"])


if __name__ == "__main__":
    unittest.main()
