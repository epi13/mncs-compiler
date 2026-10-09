#!/usr/bin/env python3
"""Focused transport-outcome classification contract checks."""
import signal
import unittest

from test_project import classify_probe_exit
from probe_compiler_module_frontier import _target_execution_started


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


class TargetExecutionEvidenceTests(unittest.TestCase):
    def test_admission_timeout_does_not_claim_target_started(self):
        phases = {"retained_session_admission": 60_000_000_000}
        self.assertIs(_target_execution_started(phases, {}), False)

    def test_returned_target_response_confirms_execution(self):
        phases = {"native_request_transport_and_execution": 10_000_000}
        self.assertIs(_target_execution_started(phases, {"status": "returned"}), True)

    def test_dispatched_target_timeout_keeps_execution_unknown(self):
        phases = {"native_request_transport_and_execution": 60_000_000_000}
        self.assertIs(_target_execution_started(phases, {}), None)

    def test_admission_only_run_does_not_claim_target_started(self):
        phases = {"retained_session_admission": 10_000_000,
                  "record_type_identity_admission": 10_000_000}
        self.assertIs(_target_execution_started(phases, {"status": "not_run"}), False)


if __name__ == "__main__":
    unittest.main()
