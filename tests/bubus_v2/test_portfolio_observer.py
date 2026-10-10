from __future__ import annotations

import threading
import time
import unittest
from datetime import datetime, timezone

from bubus.portfolio_observer import ReadOnlyPortfolioObserver


class ReadOnlyPortfolioObserverTests(unittest.TestCase):
    def test_scheduler_polls_at_the_next_utc_midnight_to_capture_a_daily_opening(self):
        before_midnight = datetime(2026, 10, 10, 23, 59, 24, tzinfo=timezone.utc)
        ordinary_time = datetime(2026, 10, 10, 15, 20, tzinfo=timezone.utc)
        self.assertEqual(ReadOnlyPortfolioObserver._next_delay(before_midnight, 60), 36)
        self.assertEqual(ReadOnlyPortfolioObserver._next_delay(ordinary_time, 60), 60)

    def test_observer_is_stopped_until_explicit_start_and_stops_without_an_extra_cycle(self):
        collected = []
        second_cycle = threading.Event()

        def collect():
            collected.append(time.monotonic())
            if len(collected) >= 2:
                second_cycle.set()

        observer = ReadOnlyPortfolioObserver(collect, interval_seconds=0.05)
        self.assertFalse(observer.state()["active"])
        time.sleep(0.08)
        self.assertEqual(collected, [])

        observer.start()
        self.assertTrue(second_cycle.wait(1))
        observer.stop()
        count_at_stop = len(collected)
        time.sleep(0.08)

        self.assertGreaterEqual(count_at_stop, 2)
        self.assertEqual(len(collected), count_at_stop)
        state = observer.state()
        self.assertFalse(state["active"])
        self.assertEqual(state["status"], "STOPPED")
        self.assertEqual(state["completed_cycles"], count_at_stop)

    def test_collection_errors_are_redacted_and_do_not_kill_the_observer(self):
        failed = threading.Event()

        def collect():
            failed.set()
            raise RuntimeError("secret API details must not escape")

        observer = ReadOnlyPortfolioObserver(collect, interval_seconds=5)
        observer.start()
        try:
            self.assertTrue(failed.wait(1))
            state = observer.state()
            self.assertTrue(state["active"])
            self.assertEqual(state["last_error_code"], "COLLECTION_FAILED")
            self.assertNotIn("secret API details", repr(state))
            self.assertEqual(state["completed_cycles"], 1)
        finally:
            observer.stop()

    def test_repeated_start_does_not_create_a_second_worker(self):
        collected = []
        observer = ReadOnlyPortfolioObserver(lambda: collected.append(1), interval_seconds=0.05)
        observer.start()
        self.assertFalse(observer.start())
        try:
            deadline = time.monotonic() + 1
            while not collected and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(collected)
            self.assertEqual(observer.state()["worker_generation"], 1)
        finally:
            observer.stop()

    def test_unbounded_stop_waits_for_the_active_collection_to_finish(self):
        entered = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def collect():
            entered.set()
            release.wait(2)
            finished.set()

        observer = ReadOnlyPortfolioObserver(collect, interval_seconds=5)
        observer.start()
        self.assertTrue(entered.wait(1))
        stopper = threading.Thread(target=lambda: observer.stop(timeout=None))
        stopper.start()
        time.sleep(0.03)
        self.assertTrue(stopper.is_alive())
        release.set()
        stopper.join(1)
        self.assertFalse(stopper.is_alive())
        self.assertTrue(finished.is_set())
        self.assertFalse(observer.state()["active"])

    def test_stop_and_persistence_admission_are_atomic(self):
        entered = threading.Event()
        release = threading.Event()
        writes = []

        def collect():
            entered.set()
            release.wait(2)

        observer = ReadOnlyPortfolioObserver(collect, interval_seconds=5)
        observer.start()
        self.assertTrue(entered.wait(1))
        action_entered = threading.Event()
        action_release = threading.Event()

        def admitted_write():
            action_entered.set()
            action_release.wait(2)
            writes.append("before-stop")

        action_thread = threading.Thread(target=lambda: observer.run_if_active(admitted_write))
        action_thread.start()
        self.assertTrue(action_entered.wait(1))
        stopped = threading.Event()
        stopper = threading.Thread(target=lambda: (observer.stop(timeout=None), stopped.set()))
        stopper.start()
        time.sleep(0.03)
        self.assertFalse(stopped.is_set())
        action_release.set()
        release.set()
        action_thread.join(1)
        self.assertTrue(stopped.wait(1))
        stopper.join(1)
        self.assertEqual(writes, ["before-stop"])
        self.assertFalse(observer.run_if_active(lambda: writes.append("after-stop")))
        self.assertEqual(writes, ["before-stop"])


if __name__ == "__main__":
    unittest.main()
