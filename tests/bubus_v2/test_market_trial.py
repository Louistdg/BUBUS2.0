import tempfile
import unittest
from pathlib import Path

from bubus.market_trial import DURATION, STEP, MarketTrial


class MarketTrialTests(unittest.TestCase):
    def test_restart_coverage_distinct_quotes_and_end(self):
        clock = [1_800_000_000]
        initial = clock[0]
        reports = []
        with tempfile.TemporaryDirectory() as tmp:
            def crypto():
                return [dict(id="bitcoin", symbol="BTC", name="Bitcoin",
                             provider_timestamp=str(clock[0]), price_usd=100 + (clock[0] - initial) / STEP)]

            def stock():
                return [dict(symbol="AAPL", name="Apple", currency="USD", stamp=initial, price=100)]

            def explain(data):
                reports.append(data)
                return "Synthèse."

            args = (Path(tmp), {"crypto": crypto, "actions": stock}, explain, lambda: clock[0])
            trial = MarketTrial(*args)
            trial.tick()
            trial.tick()
            self.assertEqual(trial.status()["coverage"], {"crypto": 1, "actions": 1})
            clock[0] += 3600
            resumed = MarketTrial(*args)
            resumed.tick()
            resumed.tick()
            result = resumed.status()
            self.assertEqual(result["started"], initial)
            self.assertEqual(result["ranking"]["crypto"][0]["change"], 6)
            self.assertEqual(result["ranking"]["actions"], [])
            self.assertEqual(result["report"]["note"], "Synthèse.")
            self.assertEqual(len(reports), 1)
            clock[0] = initial + DURATION
            self.assertTrue(resumed.tick())
            self.assertTrue(resumed.tick())
            self.assertEqual(len(reports), 2)
            self.assertEqual(resumed.status()["report"]["hour"], 24)
