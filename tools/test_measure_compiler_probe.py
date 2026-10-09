#!/usr/bin/env python3
"""Contract tests for execution and measurement-runner outcome classes."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_compiler_probe as measurement


def test_inner_timeout_is_not_runner_success():
    report = {
        "native_transport_outcome": "TIMEOUT",
        "transport_failure": {"stage": "retained_session_admission", "status": "TIMEOUT"},
    }
    runner = measurement._measurement_runner_status(timed_out=False, returncode=0)
    assert runner == "SUCCESS"
    assert measurement._operation_classification(report, runner) == "TIMEOUT"
    assert measurement._operation_outcome(report) == "TIMEOUT"


def test_outer_runner_statuses_are_preserved():
    assert measurement._measurement_runner_status(timed_out=True, returncode=130) == "TIMEOUT"
    assert measurement._measurement_runner_status(timed_out=False, returncode=-9) == "INTERRUPTED"
    assert measurement._measurement_runner_status(timed_out=False, returncode=2) == "FAILURE"
    assert measurement._operation_classification(None, "TIMEOUT") == "TIMEOUT"
    assert measurement._operation_classification(None, "INTERRUPTED") == "INTERRUPTED"
    assert measurement._operation_classification(None, "FAILURE") == "FAILURE"


def test_budget_and_protocol_failures_are_distinct():
    budget = {"native_request_status": "budget_exhausted"}
    invalid = {"native_request_status": "invalid_request"}
    assert measurement._operation_outcome(budget) == "RESOURCE_EXHAUSTED"
    assert measurement._operation_classification(budget, "SUCCESS") == "RESOURCE_EXHAUSTED"
    assert measurement._operation_classification(invalid, "SUCCESS") == "FAILURE"


def test_child_interruption_and_unknown_remain_distinct():
    interrupted = {
        "native_transport_outcome": "INTERRUPTED",
        "transport_failure": {"status": "INTERRUPTED", "return_code": -15},
    }
    unknown = {
        "native_transport_outcome": "UNKNOWN",
        "transport_failure": {"status": "UNKNOWN", "return_code": -9},
    }
    assert measurement._operation_classification(interrupted, "SUCCESS") == "INTERRUPTED"
    assert measurement._operation_classification(unknown, "SUCCESS") == "UNKNOWN"
    assert measurement._operation_classification(None, "SUCCESS") == "UNKNOWN"


def test_execution_success_is_separate_from_semantic_rejection():
    returned = {
        "native_request_status": "returned",
        "native_project_valid": False,
        "native_project_ssa_valid": None,
    }
    assert measurement._operation_classification(returned, "SUCCESS") == "SUCCESS"
    assert measurement._semantic_result(returned) == "SEMANTIC_REJECTION"


def test_stage0_oracle_completion_is_not_native_compiler_evidence():
    report = {
        "kind": "stage0-project-oracle-frontier",
        "stage0_oracle": {"status": "valid", "valid": True},
    }
    assert measurement._operation_classification(report, "SUCCESS") == "SUCCESS"
    assert measurement._semantic_result(report) == "UNKNOWN_NO_NATIVE_COMPILER_RESULT"


if __name__ == "__main__":
    tests = [
        test_inner_timeout_is_not_runner_success,
        test_outer_runner_statuses_are_preserved,
        test_budget_and_protocol_failures_are_distinct,
        test_child_interruption_and_unknown_remain_distinct,
        test_execution_success_is_separate_from_semantic_rejection,
        test_stage0_oracle_completion_is_not_native_compiler_evidence,
    ]
    for test in tests:
        test()
    print(f"measure_compiler_probe: {len(tests)} checks passed")
