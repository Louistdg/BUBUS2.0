from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = PROJECT_ROOT / "experiments" / "trading-engine-01"
sys.path.insert(0, str(EXPERIMENT))
from scenario import reference_result  # noqa: E402

try:
    from benchmark import run_backtrader  # noqa: E402
    BACKTRADER_AVAILABLE = True
except ModuleNotFoundError as exc:
    if exc.name != "backtrader":
        raise
    run_backtrader = None
    BACKTRADER_AVAILABLE = False


class BacktraderBenchmarkTests(unittest.TestCase):
    def test_reference_scenario_is_explicit_and_deterministic(self) -> None:
        result = reference_result()
        self.assertEqual(result["ending_cash"], "10027.95")
        self.assertEqual(result["net_pnl"], "27.95")
        self.assertEqual([fill["side"] for fill in result["fills"]], ["BUY", "SELL"])

    @unittest.skipUnless(BACKTRADER_AVAILABLE, "Install the pinned prototype dependency in the experiment venv")
    def test_backtrader_executes_same_fixture_with_configured_fees(self) -> None:
        result = run_backtrader()
        self.assertEqual(result["engine"], "Backtrader")
        self.assertEqual(result["ending_cash"], "10027.95")
        self.assertEqual(result["net_pnl"], "27.95")
        self.assertEqual(result["fills"], [
            {"date": "2025-01-03", "side": "BUY", "quantity": 10, "price": "101.00", "commission": "1.01"},
            {"date": "2025-01-06", "side": "SELL", "quantity": 10, "price": "104.00", "commission": "1.04"},
        ])
        self.assertTrue(result["reference_matches"])

    @unittest.skipUnless(BACKTRADER_AVAILABLE, "Install the pinned prototype dependency in the experiment venv")
    def test_repeat_run_has_same_financial_result(self) -> None:
        first = run_backtrader()
        second = run_backtrader()
        for key in ("ending_cash", "net_pnl", "fills", "reference_matches"):
            self.assertEqual(first[key], second[key])


if __name__ == "__main__":
    unittest.main()
