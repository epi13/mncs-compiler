#!/usr/bin/env python3
"""Focused transport-outcome classification contract checks."""
import signal
import unittest

from test_project import classify_probe_exit


class ProbeExitClassificationTests(unittest.TestCase):
    def test_allocator_failure_is_resource_exhaustion(self):
        status, reason = classify_probe_exit(
            -signal.SIGABRT,
            b"memory allocation of 1617951682 bytes failed\n",
        )
        self.assertEqual(status, "RESOURCE_EXHAUSTED")
        self.assertIn("allocator", reason)

    def test_sigkill_cause_remains_unknown(self):
        status, reason = classify_probe_exit(-signal.SIGKILL)
        self.assertEqual(status, "UNKNOWN")
        self.assertIn("cause is not established", reason)

    def test_unexplained_abort_remains_unknown(self):
        status, _ = classify_probe_exit(-signal.SIGABRT, b"assertion failed\n")
        self.assertEqual(status, "UNKNOWN")

    def test_external_termination_is_interrupted(self):
        status, _ = classify_probe_exit(-signal.SIGTERM)
        self.assertEqual(status, "INTERRUPTED")

    def test_nonzero_exit_is_failure(self):
        status, _ = classify_probe_exit(2)
        self.assertEqual(status, "FAILURE")

    def test_zero_exit_without_response_is_unknown(self):
        status, _ = classify_probe_exit(0)
        self.assertEqual(status, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
