#!/usr/bin/env python3
"""
Offline unit tests for haproxy_runtime (no Docker / HAProxy needed).

A fake Runtime API emulates `show servers state` and `set server ... weight`
so validation, update ordering and rollback can be tested deterministically,
including failure modes that are hard to trigger against a real HAProxy.

Run:  python runtime/test_haproxy_runtime.py -v
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from haproxy_runtime import (  # noqa: E402
    HAProxyRuntime,
    RuntimeAPIError,
    WeightValidationError,
    validate_weights,
)

# The fake emulates the Milestone 2 topology; names are explicit so the test
# does not depend on the module defaults (which point at Milestone 3's
# "traffic_policy" backend).
NAMES = {"backend": "http_backends", "server_a": "backend-a", "server_b": "backend-b"}

HEADER = (
    "1\n# be_id be_name srv_id srv_name srv_addr srv_op_state srv_admin_state "
    "srv_uweight srv_iweight srv_time_since_last_change\n"
)


class FakeHAProxy:
    def __init__(self, a=90, b=10, fail_on=None):
        self.weights = {"backend-a": a, "backend-b": b}
        self.fail_on = fail_on or set()  # server names whose 'set' fails
        self.history = []                # (a, b) after every successful set
        self.commands = []

    def __call__(self, cmd: str) -> str:
        self.commands.append(cmd)
        parts = cmd.split()
        if parts[:3] == ["show", "servers", "state"]:
            rows = "".join(
                f"3 http_backends {i} {n} 10.0.0.{i} 2 0 {w} 50 1\n"
                for i, (n, w) in enumerate(self.weights.items(), 1)
            )
            return HEADER + rows + "\n"
        if parts[:2] == ["set", "server"] and parts[3] == "weight":
            server = parts[2].split("/", 1)[1]
            if server not in self.weights:
                return "No such server.\n\n"
            if server in self.fail_on:
                self.fail_on.discard(server)  # fail once, allow rollback
                return "Simulated failure.\n\n"
            self.weights[server] = int(parts[4])
            self.history.append((self.weights["backend-a"], self.weights["backend-b"]))
            return "\n"
        return "Unknown command.\n"


class ValidationTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(validate_weights(80, 20), (80, 20))
        self.assertEqual(validate_weights("100", "0"), (100, 0))
        self.assertEqual(validate_weights(0, 100), (0, 100))
        self.assertEqual(validate_weights(256, 1), (256, 1))

    def test_rejected(self):
        for a, b in [
            (-1, 10), (10, -5), ("-3", "4"),        # negative
            (0, 0), ("0", "0"),                     # zero total
            ("abc", 10), ("1.5", 1), ("1e2", 1),    # not integers
            ("50%", 50), ("", 10), (" ", 10),
            (None, 10), (10, None),                 # missing
            (257, 1), (True, 1),                    # out of range / bool
        ]:
            with self.subTest(a=a, b=b), self.assertRaises(WeightValidationError):
                validate_weights(a, b)


class SetWeightsTests(unittest.TestCase):
    def test_apply_and_verify(self):
        fake = FakeHAProxy(90, 10)
        rt = HAProxyRuntime(transport=fake, **NAMES)
        res = rt.set_weights(80, 20)
        self.assertEqual(fake.weights, {"backend-a": 80, "backend-b": 20})
        self.assertEqual(res.previous, {"backend-a": 90, "backend-b": 10})

    def test_total_never_zero_during_flip(self):
        fake = FakeHAProxy(100, 0)
        HAProxyRuntime(transport=fake, **NAMES).set_weights(0, 100)
        self.assertEqual(fake.history[0], (100, 100))  # increase applied first
        self.assertTrue(all(a + b > 0 for a, b in fake.history))
        fake = FakeHAProxy(0, 100)
        HAProxyRuntime(transport=fake, **NAMES).set_weights(100, 0)
        self.assertTrue(all(a + b > 0 for a, b in fake.history))

    def test_invalid_input_sends_nothing(self):
        fake = FakeHAProxy(90, 10)
        with self.assertRaises(WeightValidationError):
            HAProxyRuntime(transport=fake, **NAMES).set_weights(0, 0)
        self.assertEqual(fake.commands, [])
        self.assertEqual(fake.weights, {"backend-a": 90, "backend-b": 10})

    def test_rollback_when_second_update_fails(self):
        # 90:10 -> 50:50: B (increase) applied first, then A fails.
        fake = FakeHAProxy(90, 10, fail_on={"backend-a"})
        with self.assertRaises(RuntimeAPIError) as ctx:
            HAProxyRuntime(transport=fake, **NAMES).set_weights(50, 50)
        self.assertIn("restored", str(ctx.exception))
        self.assertEqual(fake.weights, {"backend-a": 90, "backend-b": 10})

    def test_unknown_server_changes_nothing(self):
        fake = FakeHAProxy(90, 10)
        rt = HAProxyRuntime(transport=fake, backend="http_backends", server_a="backend-a", server_b="does-not-exist")
        with self.assertRaises(RuntimeAPIError):
            rt.set_weights(50, 50)
        self.assertFalse(any(c.startswith("set ") for c in fake.commands))
        self.assertEqual(fake.weights, {"backend-a": 90, "backend-b": 10})


if __name__ == "__main__":
    unittest.main()
