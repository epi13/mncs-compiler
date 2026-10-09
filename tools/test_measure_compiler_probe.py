#!/usr/bin/env python3
"""Contract tests for execution and measurement-runner outcome classes."""

import json
import signal
import sys
import tempfile
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


def test_body_runtime_progress_events_are_read_from_nested_probe_stderr():
    event = {
        "schema_version": "mncs.language.body-runtime-progress/1",
        "step_scope": "current_function_frame",
        "function_identity": "mncs:0.2:function:mncs.compiler.project.v1::compile_project_target",
        "frame_steps": 250_000,
        "step_budget": 8_000_000,
        "elapsed_ns": 12_345_000_000,
    }
    report = {
        "probe_stderr": {
            "tail": ["mncs-body-runtime-progress " + json.dumps(event)],
        },
    }
    stderr = "\n".join(measurement._nested_probe_stderr_lines(report, "tail"))
    events, errors = measurement._body_runtime_progress_events(stderr)

    assert not errors
    assert len(events) == 1
    assert events[0]["step_scope"] == "current_function_frame"
    assert events[0]["frame_steps"] == 250_000


def test_nested_probe_timing_lines_are_available():
    report = {"probe_stderr": {"mncs_timings": ["mncs-timing stage=ssa elapsed_ms=17"]}}
    lines = measurement._nested_probe_stderr_lines(report, "mncs_timings")
    assert lines == ["mncs-timing stage=ssa elapsed_ms=17"]


def test_explicit_stage0_oracle_skip_is_forwarded_and_recordable():
    env = {}
    assert measurement._configure_stage0_oracle_skip(env, requested=True) is True
    assert env["MNCS_PROBE_SKIP_STAGE0_ORACLE"] == "1"

    inherited = {"MNCS_PROBE_SKIP_STAGE0_ORACLE": "1"}
    assert measurement._configure_stage0_oracle_skip(inherited, requested=False) is True

    oracle_required = {"MNCS_PROBE_SKIP_STAGE0_ORACLE": "1"}
    assert measurement._configure_stage0_oracle_skip(
        oracle_required, requested=False, oracle_required=True
    ) is False
    assert "MNCS_PROBE_SKIP_STAGE0_ORACLE" not in oracle_required


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


def test_ssa_runtime_progress_events_preserve_request_scope_and_location():
    event = {
        "schema_version": "mncs.language.ssa-runtime-progress/1",
        "step_scope": "top_level_request",
        "steps": 250_000,
        "step_budget": 8_000_000,
        "module": "mncs.compiler.project.v1",
        "function": "compile_project_target",
        "function_identity": "mncs:0.2:function:mncs.compiler.project.v1::compile_project_target",
        "block_identity": "mncs:0.2:block:compile_project_target::entry",
        "call_depth": 0,
    }
    report = {
        "probe_stderr": {
            "tail": ["mncs-ssa-runtime-progress " + json.dumps(event)],
        },
    }
    stderr = "\n".join(measurement._nested_probe_stderr_lines(report, "tail"))
    events, errors = measurement._ssa_runtime_progress_events(stderr)

    assert not errors
    assert len(events) == 1
    assert events[0]["step_scope"] == "top_level_request"
    assert events[0]["steps"] == 250_000
    assert events[0]["function"] == "compile_project_target"


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


def test_probe_child_rss_cap_is_scoped_and_classified_as_resource_exhausted():
    records = {
        100: {"cmdline": "python3 measure_compiler_probe.py", "max_rss_kib": 4_000_000},
        101: {"cmdline": "/tmp/mncs-compiler-stage0-probe", "max_rss_kib": 1_945_600},
    }

    assert measurement._probe_rss_peak_kib(records) == 1_945_600
    assert measurement._rss_cap_state(records, 1900) == (True, 1_945_600)
    assert measurement._rss_cap_state(records, 1901) == (False, 1_945_600)
    assert measurement._rss_cap_state({}, 1900) == (False, None)
    runner = measurement._measurement_runner_status(
        timed_out=False, returncode=-2, resource_cap_triggered=True
    )
    assert runner == "RESOURCE_EXHAUSTED"
    assert measurement._operation_classification(None, runner) == "RESOURCE_EXHAUSTED"


def test_probe_child_samples_keep_process_elapsed_resource_series(monkeypatch):
    sample = {
        "pid": 101,
        "parent_pid": 100,
        "process_group_id": 101,
        "session_id": 101,
        "start_time_ticks": 1200,
        "executable_path": "/tmp/mncs-compiler-stage0-probe",
        "comm": "mncs-compiler-stage0-probe",
        "cmdline": "/tmp/mncs-compiler-stage0-probe",
        "rss_kib": 4096,
        "hwm_kib": 4352,
        "swap_kib": 0,
        "fd_count": 5,
        "cpu_seconds": 0.31,
        "cpu_user_seconds": 0.29,
        "cpu_system_seconds": 0.02,
        "io": {"rchar": 8192, "read_bytes": 0, "write_bytes": 16},
    }
    monkeypatch.setattr(measurement, "_proc_snapshot", lambda pid: sample)
    monkeypatch.setattr(measurement.time, "monotonic", lambda: 12.5)
    monkeypatch.setattr(measurement, "TICKS_PER_SECOND", 100)
    records = {}

    measurement._record_sample(records, 101)
    summary = measurement._probe_resource_summary(records)

    assert summary["probe_samples"] == [{
        "pid": 101,
        "start_time_ticks": 1200,
        "elapsed_ms": 500.0,
        "rss_kib": 4096,
        "hwm_kib": 4352,
        "swap_kib": 0,
        "fd_count": 5,
        "cpu_seconds": 0.31,
        "cpu_user_seconds": 0.29,
        "cpu_system_seconds": 0.02,
        "io": {"rchar": 8192, "read_bytes": 0, "write_bytes": 16},
    }]
    assert "CLOCK_MONOTONIC" in summary["probe_sample_clock"]


def test_probe_io_summary_keeps_read_and_write_bytes_separate():
    records = {
        100: {
            "pid": 100,
            "cmdline": "python3 measure_compiler_probe.py",
            "last_io": {"rchar": 900, "wchar": 800, "read_bytes": 700, "write_bytes": 600},
        },
        101: {
            "pid": 101,
            "cmdline": "/tmp/mncs-compiler-stage0-probe",
            "start_time_ticks": 1234,
            "last_cpu_seconds": 3.0,
            "last_cpu_user_seconds": 2.8,
            "last_cpu_system_seconds": 0.2,
            "last_io": {
                "rchar": 11,
                "wchar": 17,
                "read_bytes": 3,
                "write_bytes": 5,
                "cancelled_write_bytes": 2,
            },
            "resource_samples": [],
            "max_rss_kib": 100,
            "max_hwm_kib": 110,
            "max_fd_count": 4,
        },
    }

    summary = measurement._probe_resource_summary(records)

    assert summary["probe_io_rchar_bytes"] == 11
    assert summary["probe_io_wchar_bytes"] == 17
    assert summary["probe_io_read_bytes"] == 3
    assert summary["probe_io_write_bytes"] == 5
    assert summary["probe_io_cancelled_write_bytes"] == 2


def test_probe_io_summary_uses_last_valid_sample_after_process_exit():
    values = {
        "rchar": 11,
        "wchar": 17,
        "read_bytes": 3,
        "write_bytes": 5,
        "cancelled_write_bytes": 2,
    }
    records = {
        101: {
            "pid": 101,
            "start_time_ticks": 1234,
            "cmdline": "/tmp/mncs-compiler-stage0-probe",
            "last_io": {},
            "max_rss_kib": 100,
            "max_hwm_kib": 110,
            "max_fd_count": 4,
            "last_cpu_seconds": 1.0,
            "last_cpu_user_seconds": 0.9,
            "last_cpu_system_seconds": 0.1,
            "resource_samples": [
                {"elapsed_ms": 10, "io": values},
                {"elapsed_ms": 11},
            ],
        },
    }

    summary = measurement._probe_resource_summary(records)

    assert summary["probe_io_rchar_bytes"] == 11
    assert summary["probe_io_wchar_bytes"] == 17
    assert summary["probe_io_read_bytes"] == 3
    assert summary["probe_io_write_bytes"] == 5
    assert summary["probe_io_cancelled_write_bytes"] == 2


def test_only_revalidated_isolated_probe_group_is_signaled(monkeypatch):
    records = {
        101: {
            "pid": 101,
            "cmdline": "/tmp/mncs-compiler-stage0-probe",
            "process_group_id": 101,
            "session_id": 101,
            "start_time_ticks": 1234,
            "executable_path": "/tmp/mncs-compiler-stage0-probe",
        },
        102: {
            "pid": 102,
            "cmdline": "/tmp/mncs-compiler-stage0-probe",
            "process_group_id": 102,
            "session_id": 102,
            "start_time_ticks": 2345,
            "executable_path": "/tmp/mncs-compiler-stage0-probe",
        },
    }
    current = {
        101: {
            "start_time_ticks": 1234,
            "executable_path": "/tmp/mncs-compiler-stage0-probe",
            "process_group_id": 101,
            "session_id": 101,
        },
        102: {
            "start_time_ticks": 9999,
            "executable_path": "/tmp/mncs-compiler-stage0-probe",
            "process_group_id": 102,
            "session_id": 102,
        },
    }
    sent = []
    monkeypatch.setattr(measurement, "_proc_snapshot", lambda pid: current.get(pid))
    monkeypatch.setattr(measurement.os, "killpg", lambda pgid, sig: sent.append((pgid, sig)))

    result = measurement._signal_verified_probe_children(records, signal.SIGINT)

    assert sent == [(101, signal.SIGINT)]
    assert result[0]["outcome"] == "sent"
    assert result[1]["outcome"] == "identity_changed_or_not_isolated"


def test_phase_trace_keeps_valid_partial_events_and_hashes_raw_bytes(tmp_path):
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"event":"probe_start"}\nnot-json\n{"event":"phase"}\n')

    events, errors, digest = measurement._read_phase_trace(trace)

    assert [event["event"] for event in events] == ["probe_start", "phase"]
    assert len(errors) == 1
    assert digest is not None and len(digest) == 64


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


def test_program_cache_preparation_is_admission_only_and_not_semantic_evidence():
    report = {
        "kind": "compiler-program-cache-preparation",
        "native_request_status": "not_run",
        "probe_internal_trace": {
            "events": [
                {
                    "event": "phase",
                    "phase": "artifact_cache_gzip_write",
                    "cache_file_suffix": ".program.json.gz",
                    "success": True,
                },
            ],
        },
    }

    assert measurement._operation_outcome(report) == "ADMISSION_ONLY_SUCCESS"
    assert measurement._operation_classification(report, "SUCCESS") == "SUCCESS"
    assert measurement._semantic_result(report) == "NOT_RUN_CACHE_PREPARATION"

    unknown = {"kind": "compiler-program-cache-preparation", "probe_internal_trace": {"events": []}}
    assert measurement._operation_outcome(unknown) == "UNKNOWN_CACHE_PREPARATION"


def test_backend_admission_only_requires_cache_publication_and_retained_admission():
    report = {
        "kind": "compiler-backend-admission-only",
        "native_request_status": "not_run",
        "probe_internal_trace": {
            "events": [
                {
                    "event": "phase",
                    "phase": "artifact_cache_gzip_write",
                    "cache_file_suffix": ".json.gz",
                    "success": True,
                },
                {
                    "event": "phase",
                    "phase": "retained_session_admission",
                    "admitted": True,
                },
            ],
        },
    }

    assert measurement._operation_outcome(report) == "ADMISSION_ONLY_SUCCESS"
    assert measurement._operation_classification(report, "SUCCESS") == "SUCCESS"
    assert measurement._semantic_result(report) == "NOT_RUN_BACKEND_ADMISSION_ONLY"

    incomplete = {
        **report,
        "probe_internal_trace": {"events": report["probe_internal_trace"]["events"][:1]},
    }
    assert measurement._operation_outcome(incomplete) == "UNKNOWN_BACKEND_ADMISSION"


if __name__ == "__main__":
    class _ManualMonkeyPatch:
        def __init__(self):
            self._changes = []

        def setattr(self, target, name, value):
            self._changes.append((target, name, getattr(target, name)))
            setattr(target, name, value)

        def undo(self):
            for target, name, original in reversed(self._changes):
                setattr(target, name, original)

    tests = [
        test_body_profiles_are_read_from_nested_probe_stderr,
        test_nested_probe_timing_lines_are_available,
        test_explicit_stage0_oracle_skip_is_forwarded_and_recordable,
        test_ssa_and_artifact_profile_events_are_structured,
        test_inner_timeout_is_not_runner_success,
        test_outer_runner_statuses_are_preserved,
        test_probe_child_rss_cap_is_scoped_and_classified_as_resource_exhausted,
        test_probe_child_samples_keep_process_elapsed_resource_series,
        test_only_revalidated_isolated_probe_group_is_signaled,
        test_phase_trace_keeps_valid_partial_events_and_hashes_raw_bytes,
        test_budget_and_protocol_failures_are_distinct,
        test_child_interruption_and_unknown_remain_distinct,
        test_execution_success_is_separate_from_semantic_rejection,
        test_program_cache_preparation_is_admission_only_and_not_semantic_evidence,
        test_stage0_oracle_completion_is_not_native_compiler_evidence,
    ]
    for test in tests:
        patch = _ManualMonkeyPatch()
        temporary_directories = []
        arguments = {}
        try:
            for name in test.__code__.co_varnames[:test.__code__.co_argcount]:
                if name == "monkeypatch":
                    arguments[name] = patch
                elif name == "tmp_path":
                    temporary = tempfile.TemporaryDirectory()
                    temporary_directories.append(temporary)
                    arguments[name] = Path(temporary.name)
                else:
                    raise TypeError(f"unsupported test fixture: {name}")
            test(**arguments)
        finally:
            patch.undo()
            for temporary in temporary_directories:
                temporary.cleanup()
    print(f"measure_compiler_probe: {len(tests)} checks passed")
