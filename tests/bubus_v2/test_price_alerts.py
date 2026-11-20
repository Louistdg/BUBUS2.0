import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bubus.price_alerts import PriceAlerts


SUBSCRIPTION = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/test-subscription",
    "keys": {"p256dh": "A" * 87, "auth": "A" * 22},
}


class PriceAlertTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.price, self.stamp, self.now = "10", 100, 100
        self.messages = []
        self.alerts = self.open_store()

    def open_store(self):
        return PriceAlerts(Path(self.directory.name), quote_source=lambda: {
            "market": [{"symbol": "BTC", "currency": "EUR", "price": self.price, "stamp": self.stamp}]
        }, sender=lambda _, message: self.messages.append(message), clock=lambda: self.now)

    def rule(self):
        return dict(subscription=SUBSCRIPTION, symbol="BTC", currency="EUR",
                    direction="above", threshold="12")

    def test_crossing_survives_restart_and_sends_once_across_concurrent_polls(self):
        self.alerts.add(self.rule())
        self.alerts.add(self.rule())
        reopened = self.open_store()
        self.assertEqual(reopened.public_key, self.alerts.public_key)
        self.assertEqual(len(reopened.list()["rules"]), 1)
        self.price = "12"
        reopened.poll()
        self.assertEqual(self.messages, [])
        self.stamp += 1
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: reopened.poll(), range(8)))
        self.open_store().poll()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(reopened.list()["rules"][0]["state"], "sent")

    def test_price_already_beyond_threshold_needs_a_new_crossing(self):
        self.price = "13"
        self.alerts.add(self.rule())
        self.stamp += 1
        self.alerts.poll()
        self.assertEqual(self.messages, [])
        self.price, self.stamp = "11", 103
        self.alerts.poll()
        self.price, self.stamp = "14", 104
        self.alerts.poll()
        self.assertEqual(len(self.messages), 1)

    def test_delayed_test_does_not_require_live_market_data(self):
        self.alerts.add(dict(subscription=SUBSCRIPTION), test=True)
        self.alerts.add(dict(subscription=SUBSCRIPTION), test=True)
        self.now += 24
        self.alerts.poll()
        self.assertFalse(self.messages)
        self.now += 6
        for number in range(1, 4):
            self.alerts.poll()
            self.alerts.poll()
            self.assertEqual(len(self.messages), number)
            self.assertEqual(self.messages[-1]["title"], f"BUBUS · Test {number}/3")
            self.now += 35
        self.assertEqual(len({msg['tag'] for msg in self.messages}), 3)

    def test_rejects_untrusted_push_endpoint(self):
        invalid = {**SUBSCRIPTION, "endpoint":"https://fcm.googleapis.com.invalid/fcm/send/test-subscription"}
        with self.assertRaises(ValueError):
            self.alerts.add(dict(subscription=invalid), test=True)
