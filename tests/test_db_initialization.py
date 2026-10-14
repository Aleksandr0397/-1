import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

from db_initialization import run_once


class InitializationTests(unittest.TestCase):
    def setUp(self):
        self.identity = "postgresql://example/" + str(uuid.uuid4())

    def test_success_is_cached_and_operation_return_is_not_exposed(self):
        operation = Mock(return_value={"internal": "initialization result"})
        self.assertIsNone(run_once(self.identity, "accounts", operation))
        self.assertIsNone(run_once(self.identity, "accounts", operation))
        operation.assert_called_once_with()

    def test_failure_propagates_and_next_call_retries(self):
        operation = Mock(side_effect=[RuntimeError("commit failed"), None])
        with self.assertRaisesRegex(RuntimeError, "commit failed"):
            run_once(self.identity, "catalog", operation)
        self.assertIsNone(run_once(self.identity, "catalog", operation))
        self.assertIsNone(run_once(self.identity, "catalog", operation))
        self.assertEqual(operation.call_count, 2)

    def test_concurrent_calls_initialize_exactly_once(self):
        callers = threading.Barrier(8)
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def operation():
            calls.append("initialized")
            entered.set()
            if not release.wait(5):
                raise TimeoutError("initialization was not released")

        def initialize():
            callers.wait(timeout=5)
            return run_once(self.identity, "schema", operation)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(initialize) for _ in range(8)]
            try:
                self.assertTrue(entered.wait(5))
            finally:
                release.set()
            self.assertEqual([future.result(timeout=5) for future in futures], [None] * 8)
        self.assertEqual(calls, ["initialized"])

    def test_identity_and_group_each_have_independent_completion(self):
        operation = Mock()
        keys = [(self.identity, "accounts"), (self.identity, "catalog"),
                (self.identity + "-other", "accounts")]
        for identity, group in keys + keys:
            run_once(identity, group, operation)
        self.assertEqual(operation.call_count, len(keys))

    def test_different_keys_are_not_blocked_by_an_initializing_key(self):
        entered = threading.Event()
        release = threading.Event()

        def blocked_operation():
            entered.set()
            if not release.wait(5):
                raise TimeoutError("initialization was not released")

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(run_once, self.identity, "accounts", blocked_operation)
            try:
                self.assertTrue(entered.wait(5))
                other = Mock()
                second = executor.submit(run_once, self.identity, "catalog", other)
                self.assertIsNone(second.result(timeout=3))
                other.assert_called_once_with()
            finally:
                release.set()
            self.assertIsNone(first.result(timeout=5))

    def test_key_parts_cannot_alias_through_embedded_separators(self):
        operation = Mock()
        run_once(self.identity + "\0catalog", "schema", operation)
        run_once(self.identity, "catalog\0schema", operation)
        self.assertEqual(operation.call_count, 2)


if __name__ == "__main__":
    unittest.main()
