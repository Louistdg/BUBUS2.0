from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from invest_ai.core.orchestration import OrchestrationStore
from invest_ai.core.risk_policy import RiskPolicyRegistry, RiskPolicyUnavailable
from invest_ai.risk import AssetType, RiskPolicy, Side


def _policy() -> RiskPolicy:
    return RiskPolicy(
        version="candidate-v1", allowed_instruments=frozenset({"BTC_USD"}),
        allowed_sides=frozenset({Side.BUY}), allowed_asset_types=frozenset({AssetType.CRYPTO}),
        max_order_notional=Decimal("10"), max_position_notional=Decimal("10"),
        max_concentration_ratio=Decimal("0.5"), max_sector_concentration_ratio=Decimal("0.5"),
        max_gross_exposure=Decimal("10"), min_cash_balance=Decimal("0"),
        max_loss_per_trade=Decimal("2"), max_daily_loss=Decimal("5"),
        max_drawdown_ratio=Decimal("0.2"), max_leverage=Decimal("1"),
        max_market_data_age_seconds=60, max_portfolio_age_seconds=60,
        max_orders_per_window=3, frequency_window_seconds=60,
        fee_rate=Decimal("0.001"), slippage_buffer_rate=Decimal("0.002"),
    )


class RiskPolicyPasskeyBoundaryTests(unittest.TestCase):
    def test_pending_candidate_and_missing_receipt_never_activate_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrchestrationStore(Path(directory) / "runtime.sqlite3")
            registry = RiskPolicyRegistry(store)
            now = datetime.now(timezone.utc)
            revision = registry.register_candidate(
                _policy(), effective_at=now, recorded_at=now, reason="boundary test",
            )

            candidate = registry.candidate(revision.revision_id)
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate[0].approval_status, "PENDING")
            self.assertIsNone(registry.approval_receipt(revision.revision_id))
            with self.assertRaises(RiskPolicyUnavailable):
                registry.active_policy(now=now)


if __name__ == "__main__":
    unittest.main()
