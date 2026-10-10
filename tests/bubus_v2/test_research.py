from __future__ import annotations

import unittest

from bubus.research import compare_candles


def flat_candles(*, count: int = 120, price: str = "100") -> list[dict[str, object]]:
    return [
        {"timestamp": (index + 1) * 3_600_000, "o": price, "h": price,
         "l": price, "c": price, "v": "1000"}
        for index in range(count)
    ]


class ResearchSimulationTests(unittest.TestCase):
    def by_id(self, result: dict[str, object], strategy_id: str) -> dict[str, object]:
        strategies = result["results"]
        assert isinstance(strategies, list)
        return next(row for row in strategies if row["id"] == strategy_id)

    def test_cash_baseline_stays_flat_without_fills_or_exposure(self) -> None:
        result = compare_candles(flat_candles(), commission_bps=100, slippage_bps=50)

        cash = self.by_id(result, "do_nothing")
        self.assertEqual(cash["test_return_percent"], "0.00")
        self.assertEqual(cash["max_drawdown_percent"], "0.00")
        self.assertEqual(cash["fills"], 0)
        self.assertEqual(cash["fees_normalized"], "0E-8")
        self.assertEqual(cash["exposure_percent"], "0.00")

    def test_open_position_is_liquidated_at_test_end_and_exit_fee_is_charged(self) -> None:
        result = compare_candles(flat_candles(), commission_bps=100, slippage_bps=0)

        buy_hold = self.by_id(result, "buy_hold")
        self.assertEqual(buy_hold["fills"], 2)
        self.assertEqual(buy_hold["test_return_percent"], "-1.98")
        self.assertEqual(buy_hold["fees_normalized"], "0.01980198")

    def test_intrabar_drawdown_uses_adverse_high_then_low_path(self) -> None:
        candles = flat_candles()
        candles[84] = {"timestamp": 85 * 3_600_000, "o": "100", "h": "120",
                       "l": "90", "c": "110", "v": "1000"}

        result = compare_candles(candles, commission_bps=0, slippage_bps=0)

        buy_hold = self.by_id(result, "buy_hold")
        self.assertEqual(buy_hold["max_drawdown_percent"], "25.00")

    def test_trade_count_and_holding_duration_are_reported(self) -> None:
        result = compare_candles(flat_candles(), commission_bps=0, slippage_bps=0)

        buy_hold = self.by_id(result, "buy_hold")
        self.assertEqual(buy_hold["round_trips"], 1)
        self.assertEqual(buy_hold["average_holding_bars"], "36.00")
        self.assertEqual(buy_hold["median_holding_bars"], "36.00")

    def test_default_fee_assumption_is_the_current_level_one_taker_rate(self) -> None:
        result = compare_candles(flat_candles())

        self.assertEqual(result["commission_bps"], 50)

    def test_incomplete_hourly_series_is_rejected_instead_of_bridging_a_gap(self) -> None:
        candles = flat_candles()
        candles[85]["timestamp"] = int(candles[85]["timestamp"]) + 3_600_000

        with self.assertRaisesRegex(ValueError, "trous|continue"):
            compare_candles(candles, timeframe="1h")

    def test_explicit_evaluation_window_is_separate_from_prior_signal_context(self) -> None:
        candles = flat_candles()
        candles[84] = {"timestamp": 85 * 3_600_000, "o": "100", "h": "201",
                       "l": "100", "c": "200", "v": "1000"}
        candles[85] = {"timestamp": 86 * 3_600_000, "o": "200", "h": "201",
                       "l": "199", "c": "200", "v": "1000"}

        result = compare_candles(candles, commission_bps=0, slippage_bps=0,
                                 evaluation_start_ms=85 * 3_600_000,
                                 evaluation_end_ms=121 * 3_600_000)

        buy_hold = self.by_id(result, "buy_hold")
        self.assertEqual(result["training_bars"], 84)
        self.assertEqual(result["test_bars"], 36)
        self.assertEqual(buy_hold["test_return_percent"], "100.00")

    def test_historical_breakout_parameters_are_explicit_and_reported(self) -> None:
        result = compare_candles(flat_candles(), breakout_lookback=5,
                                 relative_volume_multiple="2.5")

        self.assertEqual(result["breakout_parameters"], {
            "lookback_bars": 5, "relative_volume_multiple": "2.5",
        })


if __name__ == "__main__":
    unittest.main()
