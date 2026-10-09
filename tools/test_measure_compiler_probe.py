#!/usr/bin/env python3
"""Contract tests for execution and measurement-runner outcome classes."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure_compiler_probe as measurement


def test_body_profiles_are_read_from_nested_probe_stderr():
    profile = {
        "schema_version": "mncs.language.body-runtime-profile/1",
        "status": "budget_exhausted",
        "steps": 50,
        "profiled_steps": 50,
        "step_count_matches_result": True,
        "functions": {
            "mncs:0.2:function:mncs.compiler.source.v1::validate_page_step": {
                "calls": 2,
                "steps": 18,
                "inclusive_ns": 1_400_000,
                "exclusive_ns": 1_200_000,
            },
            "mncs:0.2:function:mncs.compiler.lexer.v1::next_token_global": {
                "calls": 1,
                "steps": 7,
                "inclusive_ns": 800_000,
                "exclusive_ns": 500_000,
            },
        },
    }
    report = {
        "probe_stderr": {
            "tail": ["mncs-body-runtime-profile " + json.dumps(profile)],
        },
    }
    stderr = "\n".join(measurement._nested_probe_stderr_lines(report, "tail"))
    profiles, errors = measurement._body_runtime_profiles(stderr)
    modules, hot_functions = measurement._body_profile_summaries(profiles)

    assert not errors
    assert len(profiles) == 1
    assert profiles[0]["step_count_matches_result"] is True
    assert {row["module"] for row in modules} == {
        "mncs.compiler.source.v1",
        "mncs.compiler.lexer.v1",
    }
    assert hot_functions[0]["identity"].endswith("source.v1::validate_page_step")


def test_nested_probe_timing_lines_are_available():
    report = {"probe_stderr": {"mncs_timings": ["mncs-timing stage=ssa elapsed_ms=17"]}}
    lines = measurement._nested_probe_stderr_lines(report, "mncs_timings")
    assert lines == ["mncs-timing stage=ssa elapsed_ms=17"]


def test_ssa_and_artifact_profile_events_are_structured():
    runtime = {
        "status": "BudgetExhausted",
        "steps": 50_000,
        "execution_frames": 2_190,
        "frame_setup_ns": 18_000_000_000,
    }
    report = {
        "probe_stderr": {
            "tail": [
                "mncs-runtime-profile " + json.dumps(runtime),
                "mncs-ssa-profile phase=module_validation elapsed_ns=123",
                "mncs-artifact-profile phase=content_identity elapsed_ns=456",
            ],
        },
    }
    stderr = "\n".join(measurement._nested_probe_stderr_lines(report, "tail"))
    runtime_profiles, errors = measurement._ssa_runtime_profiles(stderr)

    assert not errors
    assert runtime_profiles[0]["steps"] == 50_000
    assert runtime_profiles[0]["frame_setup_ns"] == 18_000_000_000
    assert measurement._named_profile_phases(stderr, "mncs-ssa-profile") == [
        {
            "phase": "module_validation",
            "elapsed_ns": 123,
            "scope": "mncs-ssa-profile",
        },
    ]
    assert measurement._named_profile_phases(stderr, "mncs-artifact-profile") == [
        {
            "phase": "content_identity",
            "elapsed_ns": 456,
            "scope": "mncs-artifact-profile",
        },
    ]


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
        test_body_profiles_are_read_from_nested_probe_stderr,
        test_nested_probe_timing_lines_are_available,
        test_ssa_and_artifact_profile_events_are_structured,
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
