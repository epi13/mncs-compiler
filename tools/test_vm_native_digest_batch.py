#!/usr/bin/env python3
"""Run the real MNCS Test digest and suite operations in one VM workflow.

The pinned Gate A fixture supplies Stage-0 oracle outputs for the current
digest source snapshot. This test compiles the digest and schema callables
alongside suite and assertion harness roots into one /5 artifact, checks a
fresh-process cache hit, and runs it through bounded batch and retained VM
sessions.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time

import test_vm_test_digest_app as digest_app

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(os.environ.get(
    "MNCS_DIGEST_BATCH_PROBE_BIN",
    ROOT / "tools/stage0-probe/target/debug/mncs-compiler-stage0-probe",
))
VM_BIN = Path(os.environ.get(
    "MNCS_DIGEST_BATCH_VM_BIN",
    digest_app.VM_ROOT / "target/debug/mncs-vm",
))
CACHE_DIR = ROOT / ".build/native-app-readiness/digest-batch-cache" / (
    f"run-{os.getpid()}-{time.time_ns()}"
)
EVIDENCE_PATH = ROOT / os.environ.get(
    "MNCS_NATIVE_TEST_WORKFLOW_EVIDENCE_PATH",
    "evidence/native-app-readiness-native-test-workflow-"
    f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.getpid()}.json",
)
MODULE = "mncs.test.digest"
SUITE_FIXTURE_FUNCTION = "native_suite_fixture"
SUITE_EDGE_FIXTURE_FUNCTION = "native_suite_edge_fixture"
SUITE_FIXTURE_SOURCE = """

// Harness-only root that exercises the real Test suite's generic reducer.
fn native_suite_fixture() -> (result: SuiteSummary) {
    let passed: TestResult = pass();
    let failed: TestResult = from_assertion(equals_i64(7, 9, 7001));
    let skipped: TestResult = skip(7002);
    let unsupported_result: TestResult = unsupported(7003);
    let tests: [TestResult; 4] = [passed, failed, skipped, unsupported_result];
    return summarize<4>(tests);
}

record NativeSuiteEdgeReport {
    summary: SuiteSummary,
    suite_projection: SuiteProjection,
    passing_bytes: TestResult,
    failing_bytes: TestResult,
    failed_assertion_projection: TestProjection,
    unsupported_projection: TestProjection
}

// Drive the real generic assertion and suite implementations through every
// failure classification used by mncs-test. The fixed records are harness
// inputs; summary and classification remain native MNCS results.
fn native_suite_edge_fixture() -> (result: NativeSuiteEdgeReport) {
    let passing_bytes: TestResult = from_assertion(
        equals_bytes<4>([1, 2, 3, 4], [1, 2, 3, 4], 4101));
    let failing_bytes: TestResult = from_assertion(
        equals_bytes<4>([1, 2, 3, 4], [1, 2, 3, 5], 4102));
    let setup_failure: TestResult = TestResult {
        verdict: Verdict.FAIL,
        verdict_code: 1,
        failure_kind: FailureKind.Setup,
        failure_code: 4103,
        assertions: 0,
        failures: 1,
        expected: 0,
        actual: 0,
        assertion_code: 4103
    };
    let compile_failure: TestResult = TestResult {
        verdict: Verdict.FAIL,
        verdict_code: 1,
        failure_kind: FailureKind.Compile,
        failure_code: 4104,
        assertions: 0,
        failures: 1,
        expected: 0,
        actual: 0,
        assertion_code: 4104
    };
    let runtime_failure: TestResult = TestResult {
        verdict: Verdict.FAIL,
        verdict_code: 1,
        failure_kind: FailureKind.Runtime,
        failure_code: 4105,
        assertions: 0,
        failures: 1,
        expected: 0,
        actual: 0,
        assertion_code: 4105
    };
    let timeout_failure: TestResult = TestResult {
        verdict: Verdict.FAIL,
        verdict_code: 1,
        failure_kind: FailureKind.Timeout,
        failure_code: 4106,
        assertions: 0,
        failures: 1,
        expected: 0,
        actual: 0,
        assertion_code: 4106
    };
    let unsupported_failure: TestResult = TestResult {
        verdict: Verdict.FAIL,
        verdict_code: 1,
        failure_kind: FailureKind.Unsupported,
        failure_code: 4107,
        assertions: 0,
        failures: 1,
        expected: 0,
        actual: 0,
        assertion_code: 4107
    };
    let infrastructure_failure: TestResult = TestResult {
        verdict: Verdict.FAIL,
        verdict_code: 1,
        failure_kind: FailureKind.Infrastructure,
        failure_code: 4108,
        assertions: 0,
        failures: 1,
        expected: 0,
        actual: 0,
        assertion_code: 4108
    };
    let unsupported_result: TestResult = unsupported(4110);
    let tests: [TestResult; 10] = [
        passing_bytes,
        failing_bytes,
        setup_failure,
        compile_failure,
        runtime_failure,
        timeout_failure,
        unsupported_failure,
        infrastructure_failure,
        skip(4109),
        unsupported_result
    ];
    let summary: SuiteSummary = summarize<10>(tests);
    return NativeSuiteEdgeReport {
        summary: summary,
        suite_projection: project(summary),
        passing_bytes: passing_bytes,
        failing_bytes: failing_bytes,
        failed_assertion_projection: project_test(failing_bytes),
        unsupported_projection: project_test(unsupported_result)
    };
}
"""
ROOTS = [
    {"module": MODULE, "function": "build_digest"},
    {"module": MODULE, "function": "request_schema"},
    {"module": MODULE, "function": "result_schema"},
    {"module": MODULE, "function": SUITE_FIXTURE_FUNCTION},
    {"module": MODULE, "function": SUITE_EDGE_FIXTURE_FUNCTION},
]


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def cpu_times(stderr: str) -> dict:
    measurements = {}
    for label, field in (("User time (seconds)", "user_cpu_seconds"),
                         ("System time (seconds)", "system_cpu_seconds")):
        match = re.search(rf"^\s*{re.escape(label)}: ([0-9]+(?:\.[0-9]+)?)$",
                          stderr, re.MULTILINE)
        measurements[field] = float(match.group(1)) if match else None
    user = measurements["user_cpu_seconds"]
    system = measurements["system_cpu_seconds"]
    measurements["cpu_seconds"] = (
        round(user + system, 6) if user is not None and system is not None else None
    )
    return measurements


def value_differences(actual: object, expected: object, path: str = "$") -> list[dict]:
    if type(actual) is not type(expected):
        return [{"path": path, "actual": actual, "expected": expected}]
    if isinstance(actual, dict):
        differences = []
        for key in sorted(set(actual) | set(expected)):
            if key not in actual or key not in expected:
                differences.append({"path": f"{path}.{key}", "actual": actual.get(key), "expected": expected.get(key)})
            else:
                differences.extend(value_differences(actual[key], expected[key], f"{path}.{key}"))
            if len(differences) >= 12:
                break
        return differences
    if isinstance(actual, list):
        if len(actual) != len(expected):
            return [{"path": f"{path}.length", "actual": len(actual), "expected": len(expected)}]
        differences = []
        for index, (left, right) in enumerate(zip(actual, expected)):
            differences.extend(value_differences(left, right, f"{path}[{index}]"))
            if len(differences) >= 12:
                break
        return differences
    if actual != expected:
        return [{"path": path, "actual": actual, "expected": expected}]
    return []


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def build_request(root_source: str, modules: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/test/digest.mncs",
        "root_source": root_source,
        "callable_roots": ROOTS,
        "modules": modules,
        "generic_seeds": [
            {
                "module": "mncs.test.suite",
                "function": "summarize",
                "type_arguments": [{"kind": "nat", "value": 4}],
            },
            {
                "module": "mncs.test.suite",
                "function": "summarize",
                "type_arguments": [{"kind": "nat", "value": 10}],
            },
            {
                "module": "mncs.test.assertions",
                "function": "equals_bytes",
                "type_arguments": [{"kind": "nat", "value": 4}],
            },
        ],
        "capabilities": [],
        "verification_policy": "native-callable-set/1",
    }


def vm_batch(
    artifact: dict, inputs: list[dict], expected: list[list[dict]],
    suite_expected: dict[str, object],
) -> tuple[dict, dict, list[dict]]:
    calls = []
    for index, argument in enumerate(inputs):
        calls.append({
            "id": f"digest-case-{index}",
            "request": {
                "schema_version": "0.1",
                "target": {"module": MODULE, "function": "build_digest"},
                "arguments": [argument],
                "type_arguments": [],
                "step_budget": 3_000_000,
            },
        })
    calls.extend([
        {
            "id": "request-schema",
            "request": {
                "schema_version": "0.1",
                "target": {"module": MODULE, "function": "request_schema"},
                "arguments": [],
                "type_arguments": [],
                "step_budget": 10_000,
            },
        },
        {
            "id": "result-schema",
            "request": {
                "schema_version": "0.1",
                "target": {"module": MODULE, "function": "result_schema"},
                "arguments": [],
                "type_arguments": [],
                "step_budget": 10_000,
            },
        },
    ])
    for function in suite_expected:
        calls.append({
            "id": f"native-suite-{function}",
            "request": {
                "schema_version": "0.1",
                "target": {"module": MODULE, "function": function},
                "arguments": [],
                "type_arguments": [],
                "step_budget": 3_000_000,
            },
        })

    with tempfile.TemporaryDirectory(prefix="mncs-native-digest-batch-") as raw:
        directory = Path(raw)
        artifact_path = directory / "application.vm.json"
        calls_path = directory / "calls.json"
        output_path = directory / "results.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        calls_path.write_text(json.dumps(calls, sort_keys=True) + "\n")
        started = time.monotonic()
        process = subprocess.run(
            ["/usr/bin/time", "-v", str(VM_BIN), "batch",
             "--artifact", str(artifact_path), "--calls", str(calls_path),
             "--output", str(output_path)],
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        wall = time.monotonic() - started
        if process.returncode:
            raise AssertionError(f"canonical VM batch failed: {process.stderr[-3000:]}")
        peak_rss = re.search(
            r"Maximum resident set size \(kbytes\): (\d+)", process.stderr
        )
        cpu = cpu_times(process.stderr)
        document = json.loads(output_path.read_text())

    rows = document.get("results", [])
    if (document.get("artifact_id") != artifact.get("artifact_id")
            or len(rows) != len(calls)
            or [row.get("id") for row in rows] != [row["id"] for row in calls]):
        raise AssertionError(f"canonical VM returned an unexpected batch: {document}")

    normalized = []
    for index, row in enumerate(rows):
        if row.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"VM call {row.get('id')} did not complete: {row}")
        record = row.get("record", {})
        if record.get("artifact_id") != artifact.get("artifact_id"):
            raise AssertionError("one VM call used a different application artifact")
        returned = record.get("returned", [])
        if len(returned) != 1:
            raise AssertionError(f"VM call returned an unexpected value list: {row}")
        normalized.append(digest_app.support.decode_wire(
            digest_app.support.vm_to_wire(returned[0])
        ))

    expected_values = []
    for outputs in expected:
        if len(outputs) != 1:
            raise AssertionError(f"Stage-0 fixture has an unexpected return shape: {outputs}")
        expected_values.append(outputs[0])
    if normalized[:len(expected_values)] != expected_values:
        mismatch = next(
            index for index, pair in enumerate(zip(normalized, expected_values))
            if pair[0] != pair[1]
        )
        raise AssertionError(
            "native digest result differs from the pinned Stage-0 fixture: "
            + json.dumps(
                value_differences(normalized[mismatch], expected_values[mismatch]),
                sort_keys=True,
            )
        )
    if normalized[len(expected_values):len(expected_values) + 2] != [
        list(b"mncs.test-digest-request/1"),
        list(b"mncs.test-digest/1"),
    ]:
        raise AssertionError(
            "digest schema callables returned unexpected bytes: "
            + json.dumps(normalized[len(expected_values):len(expected_values) + 2])
        )
    suite_values = normalized[len(expected_values) + 2:]
    if suite_values != list(suite_expected.values()):
        raise AssertionError(
            "native Test workflow differs from the Stage-0 project oracle: "
            + json.dumps(
                value_differences(suite_values, list(suite_expected.values())),
                sort_keys=True,
            )
        )
    return document, {
        "wall_seconds": round(wall, 6),
        **cpu,
        "peak_rss_kib": int(peak_rss.group(1)) if peak_rss else None,
        "calls": len(calls),
        "call_ids": [row["id"] for row in calls],
    }, normalized


def vm_retained_session(
    artifact: dict, inputs: list[dict], expected: list[list[dict]],
    suite_expected: dict[str, object],
) -> tuple[dict, list[dict]]:
    """Run the digest callables over one long-lived canonical VM process."""
    calls = [
        ("digest-budget-exhaustion", {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": "build_digest"},
            "arguments": [inputs[0]],
            "type_arguments": [],
            "step_budget": 1,
        }, "budget_exhausted", None),
        ("digest-invalid-typed-input", {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": "build_digest"},
            "arguments": [{
                "integer": {
                    "type": {"bits": 64, "signed": False},
                    "value": 7,
                },
            }],
            "type_arguments": [],
            "step_budget": 3_000_000,
        }, "invalid_request", None),
    ]
    for index, argument in enumerate(inputs):
        calls.append((f"digest-case-{index}", {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": "build_digest"},
            "arguments": [argument],
            "type_arguments": [],
            "step_budget": 3_000_000,
        }, "completed", expected[index][0]))
    calls.extend([
        ("request-schema", {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": "request_schema"},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 10_000,
        }, "completed", list(b"mncs.test-digest-request/1")),
        ("result-schema", {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": "result_schema"},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 10_000,
        }, "completed", list(b"mncs.test-digest/1")),
    ])
    calls.extend(
        (f"native-suite-{function}", {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": function},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 3_000_000,
        }, "completed", value)
        for function, value in suite_expected.items()
    )

    with tempfile.TemporaryDirectory(prefix="mncs-native-digest-session-") as raw:
        artifact_path = Path(raw) / "application.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        stderr_file = tempfile.TemporaryFile(mode="w+t")
        started = time.monotonic()
        process = subprocess.Popen(
            ["/usr/bin/time", "-v", str(VM_BIN), "serve", "--artifact", str(artifact_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            text=True,
            bufsize=1,
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def read_lines() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        reader = threading.Thread(target=read_lines, daemon=True)
        reader.start()

        def receive() -> dict:
            try:
                line = lines.get(timeout=30)
            except queue.Empty as error:
                raise TimeoutError("canonical VM retained session stopped responding") from error
            if line is None:
                raise RuntimeError("canonical VM retained session closed its output")
            return json.loads(line)

        responses = []
        try:
            ready = receive()
            if (ready.get("schema_version") != "mncs.vm.session/1"
                    or ready.get("status") != "ready"
                    or ready.get("artifact_id") != artifact.get("artifact_id")):
                raise AssertionError(f"canonical VM admitted an unexpected retained artifact: {ready}")
            if process.stdin is None:
                raise AssertionError("canonical VM retained session has no request pipe")
            for index, (call_id, request, outcome, expected_value) in enumerate(calls):
                process.stdin.write(json.dumps({
                    "schema_version": "mncs.vm.session/1",
                    "id": call_id,
                    "request": request,
                }, sort_keys=True) + "\n")
                process.stdin.flush()
                response = receive()
                result = response.get("result", {})
                record = result.get("record", {})
                execution = result.get("execution", {})
                if (response.get("id") != call_id
                        or response.get("ok") is not True
                        or result.get("outcome", {}).get("kind") != outcome
                        or record.get("artifact_id") != artifact.get("artifact_id")):
                    raise AssertionError(f"retained VM call {call_id} returned an invalid result: {response}")
                if expected_value is not None:
                    returned = execution.get("returned", [])
                    normalized = [digest_app.support.decode_wire(value) for value in returned]
                    if normalized != [expected_value] or execution.get("status") != "returned":
                        raise AssertionError(
                            f"retained VM call {call_id} differs from its Stage-0 oracle: {normalized}"
                        )
                responses.append({
                    "id": call_id,
                    "outcome": outcome,
                    "artifact_id": record.get("artifact_id"),
                    "steps": record.get("usage", {}).get("steps"),
                    "returned_sha256": (
                        sha(json.dumps(expected_value, sort_keys=True,
                                       separators=(",", ":")).encode())
                        if expected_value is not None else None
                    ),
                })
            process.stdin.close()
            if process.wait(timeout=30) != 0:
                raise AssertionError("canonical VM retained session exited unsuccessfully")
            reader.join(timeout=2)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        wall = time.monotonic() - started
        stderr_file.seek(0)
        stderr = stderr_file.read()
        stderr_file.close()

    peak_rss = re.search(r"Maximum resident set size \(kbytes\): (\d+)", stderr)
    cpu = cpu_times(stderr)
    return {
        "ready_artifact_id": ready.get("artifact_id"),
        "process_count": 1,
        "call_count": len(responses),
        "call_ids": [row["id"] for row in responses],
        "wall_seconds": round(wall, 6),
        **cpu,
        "peak_rss_kib": int(peak_rss.group(1)) if peak_rss else None,
        "low_fuel_recovery_in_same_session": (
            responses[0]["outcome"] == "budget_exhausted"
            and responses[2]["outcome"] == "completed"
        ),
        "typed_failure_rejected_without_poisoning_following_calls": (
            responses[1]["outcome"] == "invalid_request"
            and all(row["outcome"] == "completed" for row in responses[2:])
        ),
        "results": responses,
    }, responses


def main() -> None:
    if EVIDENCE_PATH.exists():
        raise SystemExit(f"refusing to overwrite existing evidence: {EVIDENCE_PATH}")
    if not PROBE.is_file() or not VM_BIN.is_file():
        raise SystemExit(f"missing probe or canonical VM binary: {PROBE}, {VM_BIN}")
    fixture = json.loads(digest_app.FIXTURE_PATH.read_text(encoding="utf-8"))
    base_root_source = digest_app.ROOT_SOURCE_PATH.read_text(encoding="utf-8")
    modules = {
        name: path.read_text(encoding="utf-8")
        for name, path in digest_app.MODULE_PATHS.items()
    }
    source_paths = {MODULE: digest_app.ROOT_SOURCE_PATH, **digest_app.MODULE_PATHS}
    source_hashes = {
        name: sha(path.read_bytes()) for name, path in sorted(source_paths.items())
    }
    fixture_hashes = {
        row["module"]: row["sha256"].removeprefix("sha256:")
        for row in fixture["application"]["module_sources"]
    }
    if {name: value.removeprefix("sha256:") for name, value in source_hashes.items()} != fixture_hashes:
        raise AssertionError("digest sources no longer match the pinned Stage-0 fixture")
    if fixture["execution"].get("exact_result_agreement") is not True:
        raise AssertionError("the Gate A fixture lacks exact Stage-0/VM result agreement")

    root_source = base_root_source + SUITE_FIXTURE_SOURCE
    effective_source_hashes = dict(source_hashes)
    effective_source_hashes[MODULE] = sha(root_source.encode("utf-8"))
    suite_fixture_calls = [
        {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": function},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 3_000_000,
        }
        for function in (SUITE_FIXTURE_FUNCTION, SUITE_EDGE_FIXTURE_FUNCTION)
    ]
    oracle_probe = digest_app.support.Probe(
        CACHE_DIR / "suite-oracle", executable=PROBE
    )
    try:
        suite_oracle, suite_oracle_metrics = oracle_probe.send({"project_oracle": {
            "root": root_source,
            "modules": modules,
            "calls": suite_fixture_calls,
        }}, timeout=180)
        oracle_probe.close()
    except BaseException:
        if oracle_probe.proc.poll() is None:
            oracle_probe.proc.kill()
            oracle_probe.proc.wait(timeout=5)
        raise
    suite_oracle_executions = suite_oracle.get("executions", [])
    if not suite_oracle.get("valid") or len(suite_oracle_executions) != len(suite_fixture_calls):
        raise AssertionError(
            "Stage-0 could not execute the native Test workflow fixtures: "
            + json.dumps(suite_oracle.get("diagnostics"), sort_keys=True)
        )
    suite_expected = {}
    for call, execution in zip(suite_fixture_calls, suite_oracle_executions):
        function = call["target"]["function"]
        if execution.get("status") != "returned":
            raise AssertionError(
                f"Stage-0 Test workflow fixture {function} did not return: "
                + json.dumps(execution, sort_keys=True)
            )
        suite_expected[function] = digest_app.support.decode_wire(
            execution["returned"][0]
        )

    edge_summary = suite_expected[SUITE_EDGE_FIXTURE_FUNCTION].get("summary", {})
    expected_edge_summary = {
        "total": 10,
        "passed": 1,
        "failed": 7,
        "skipped": 1,
        "unsupported": 2,
        "assertion_count": 2,
        "failure_count": 7,
        "assertion_failures": 1,
        "setup_failures": 1,
        "compile_failures": 1,
        "runtime_failures": 1,
        "timeout_failures": 1,
        "infrastructure_failures": 1,
        "verdict_code": 1,
    }
    if {key: edge_summary.get(key) for key in expected_edge_summary} != expected_edge_summary:
        raise AssertionError(
            "native Test suite summary does not match the declared 10-case oracle: "
            + json.dumps(edge_summary, sort_keys=True)
        )

    request = build_request(root_source, modules)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cold_probe = digest_app.support.Probe(CACHE_DIR, executable=PROBE)
    try:
        built, cold_metrics = cold_probe.send({"compile_application": request}, timeout=180)
        cold_probe.close()
    except BaseException:
        if cold_probe.proc.poll() is None:
            cold_probe.proc.kill()
            cold_probe.proc.wait(timeout=5)
        raise
    if (built.get("schema_version") != "mncs.compiler.application-build/5"
            or built.get("status") != "verified"
            or built.get("cache", {}).get("status") != "miss"
            or built.get("unresolved_obligations") != []):
        raise AssertionError(f"real digest callable set did not verify: {built}")
    artifact = built.get("artifact", {}).get("value")
    if not isinstance(artifact, dict):
        raise AssertionError("application-build/5 omitted the canonical VM artifact")
    expected_roots = {(MODULE, row["function"]) for row in ROOTS}
    artifact_callables = {
        (row.get("module"), row.get("name")) for row in artifact.get("callables", [])
    }
    suite_specializations = {
        (row.get("module"), row.get("name"))
        for row in artifact.get("callables", [])
        if row.get("module") == "mncs.test.suite"
        and row.get("name", "").startswith("summarize__spec_")
    }
    assertion_specializations = {
        (row.get("module"), row.get("name"))
        for row in artifact.get("callables", [])
        if row.get("module") == "mncs.test.assertions"
        and row.get("name", "").startswith("equals_bytes__spec_")
    }
    if len(suite_specializations) != 2 or len(assertion_specializations) != 1:
        raise AssertionError(
            "the Test suite and generic assertion specializations were not selected exactly: "
            + json.dumps({
                "suite": sorted(suite_specializations),
                "assertions": sorted(assertion_specializations),
            }, sort_keys=True)
        )
    if artifact_callables != expected_roots | suite_specializations | assertion_specializations:
        raise AssertionError(
            f"artifact callable closure is wrong: {sorted(artifact_callables)}"
        )
    if artifact.get("requirements", {}).get("capabilities", []) != []:
        raise AssertionError("pure digest callable artifact acquired unexpected capabilities")

    warm_probe = digest_app.support.Probe(CACHE_DIR, executable=PROBE)
    try:
        warm, warm_metrics = warm_probe.send({"compile_application": request}, timeout=180)
        warm_probe.close()
    except BaseException:
        if warm_probe.proc.poll() is None:
            warm_probe.proc.kill()
            warm_probe.proc.wait(timeout=5)
        raise
    warm_work = warm.get("cache", {}).get("verification_work", {})
    if (warm.get("cache", {}).get("status") != "hit"
            or warm.get("artifact", {}).get("identity") != built["artifact"].get("identity")
            or warm_work.get("front_end_invocations") != 0):
        raise AssertionError("fresh-process digest reuse re-ran frontend or changed artifact")

    comment_modules = dict(modules)
    comment_modules["mncs.test.assertions"] += (
        "\n// source-only edit for selected executable-closure reuse\n"
    )
    comment_probe = digest_app.support.Probe(CACHE_DIR, executable=PROBE)
    try:
        comment_build, comment_metrics = comment_probe.send(
            {"compile_application": build_request(root_source, comment_modules)},
            timeout=180,
        )
        comment_probe.close()
    except BaseException:
        if comment_probe.proc.poll() is None:
            comment_probe.proc.kill()
            comment_probe.proc.wait(timeout=5)
        raise
    comment_work = comment_build.get("cache", {}).get("verification_work", {})
    if (
        comment_build.get("status") != "verified"
        or comment_build.get("cache", {}).get("status") != "hit"
        or comment_build.get("cache", {}).get("reuse_basis")
        != "verified_application_executable_closure"
        or comment_build.get("artifact", {}).get("identity")
        != built.get("artifact", {}).get("identity")
        or comment_work.get("front_end_invocations") != 1
        or comment_work.get("backend_artifact") != "reused"
        or comment_build.get("source_snapshot", {}).get("identity")
        == built.get("source_snapshot", {}).get("identity")
    ):
        raise AssertionError(
            "multi-callable comment edit did not verify the new snapshot and reuse its unchanged executable closure"
        )
    current_interfaces = [
        row.get("interface", {}).get("identity")
        for row in comment_build.get("callable_scope", {}).get("callable_roots", [])
    ]
    if current_interfaces != [
        row.get("interface", {}).get("identity")
        for row in built.get("callable_scope", {}).get("callable_roots", [])
    ] or [
        row.get("interface", {}).get("identity")
        for row in comment_build.get("entries", [])
    ] != current_interfaces:
        raise AssertionError("semantic reuse did not refresh every selected callable interface")
    comment_source_file = next(
        (row for row in comment_build.get("source_snapshot", {}).get("files", [])
         if row.get("source_id") == "mncs.test.assertions"),
        None,
    )
    if comment_source_file is None or comment_source_file.get("sha256") != sha(
        comment_modules["mncs.test.assertions"].encode()
    ):
        raise AssertionError("semantic reuse retained the cached imported source identity")

    suite_modules = dict(modules)
    empty_total = "        total: 0,"
    if suite_modules["mncs.test.suite"].count(empty_total) != 1:
        raise AssertionError("suite.empty source anchor changed")
    suite_modules["mncs.test.suite"] = suite_modules["mncs.test.suite"].replace(
        empty_total, "        total: 0 +% 0,", 1
    )
    dependency_probe = digest_app.support.Probe(CACHE_DIR, executable=PROBE)
    try:
        dependency_build, dependency_metrics = dependency_probe.send(
            {"compile_application": build_request(root_source, suite_modules)},
            timeout=180,
        )
        dependency_probe.close()
    except BaseException:
        if dependency_probe.proc.poll() is None:
            dependency_probe.proc.kill()
            dependency_probe.proc.wait(timeout=5)
        raise
    dependency_work = dependency_build.get("cache", {}).get("verification_work", {})
    if (
        dependency_build.get("status") != "verified"
        or dependency_build.get("cache", {}).get("status") != "miss"
        or dependency_build.get("cache", {}).get("reuse_basis")
        == "verified_application_executable_closure"
        or dependency_work.get("front_end_invocations") != 1
        or dependency_work.get("backend_artifact") != "generated"
    ):
        raise AssertionError(
            "reachable suite.empty edit reused a stale multi-callable executable artifact"
        )

    dependency_exact_probe = digest_app.support.Probe(CACHE_DIR, executable=PROBE)
    try:
        dependency_exact, dependency_exact_metrics = dependency_exact_probe.send(
            {"compile_application": build_request(root_source, suite_modules)},
            timeout=180,
        )
        dependency_exact_probe.close()
    except BaseException:
        if dependency_exact_probe.proc.poll() is None:
            dependency_exact_probe.proc.kill()
            dependency_exact_probe.proc.wait(timeout=5)
        raise
    dependency_exact_work = dependency_exact.get("cache", {}).get(
        "verification_work", {}
    )
    if (
        dependency_exact.get("cache", {}).get("status") != "hit"
        or dependency_exact.get("artifact", {}).get("identity")
        != dependency_build.get("artifact", {}).get("identity")
        or dependency_exact_work.get("front_end_invocations") != 0
    ):
        raise AssertionError("fresh-process reuse failed after reachable dependency invalidation")

    dependency_snapshot = dependency_build.get("source_snapshot", {}).get("identity")
    corrupted_entries = []
    for cache_path in (CACHE_DIR / "application-builds").glob("*.json"):
        cache_entry = json.loads(cache_path.read_text(encoding="utf-8"))
        response = cache_entry.get("response", {})
        if response.get("source_snapshot", {}).get("identity") == dependency_snapshot:
            corrupted_entries.append((cache_path, cache_entry))
    if len(corrupted_entries) != 1:
        raise AssertionError(
            "could not select the unique exact application cache entry for corruption testing"
        )
    corrupted_cache_path, corrupted_cache_entry = corrupted_entries[0]
    corrupted_cache_entry["response"]["artifact"]["identity"] = "sha256:" + "0" * 64
    corrupted_cache_path.write_text(
        json.dumps(corrupted_cache_entry, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    corrupted_probe = digest_app.support.Probe(CACHE_DIR, executable=PROBE)
    try:
        corruption_recovery, corruption_recovery_metrics = corrupted_probe.send(
            {"compile_application": build_request(root_source, suite_modules)},
            timeout=180,
        )
        corrupted_probe.close()
    except BaseException:
        if corrupted_probe.proc.poll() is None:
            corrupted_probe.proc.kill()
            corrupted_probe.proc.wait(timeout=5)
        raise
    corruption_work = corruption_recovery.get("cache", {}).get(
        "verification_work", {}
    )
    if (
        corruption_recovery.get("status") != "verified"
        or corruption_recovery.get("cache", {}).get("status") != "miss"
        or corruption_work.get("front_end_invocations") != 1
        or corruption_recovery.get("artifact", {}).get("identity")
        != dependency_build.get("artifact", {}).get("identity")
    ):
        raise AssertionError(
            "tampered cache response was not rejected and rebuilt from the verified source"
        )

    vm_result, vm_metrics, normalized_results = vm_batch(
        dependency_build.get("artifact", {}).get("value"),
        fixture["execution"]["inputs"],
        fixture["execution"]["canonical_vm_results"],
        suite_expected,
    )
    retained_session, retained_results = vm_retained_session(
        dependency_build.get("artifact", {}).get("value"),
        fixture["execution"]["inputs"],
        fixture["execution"]["canonical_vm_results"],
        suite_expected,
    )
    if (not retained_session["low_fuel_recovery_in_same_session"]
            or not retained_session["typed_failure_rejected_without_poisoning_following_calls"]
            or retained_results[1]["outcome"] != "invalid_request"):
        raise AssertionError("retained VM session failure handling changed later valid calls")
    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.native-test-workflow-batch",
        "compiler_revision": git(ROOT, "rev-parse", "HEAD"),
        "compiler_branch": git(ROOT, "branch", "--show-current"),
        "probe_sha256": sha(PROBE.read_bytes()),
        "canonical_vm_sha256": sha(VM_BIN.read_bytes()),
        "test_harness_sha256": sha(Path(__file__).read_bytes()),
        "stage0_revision": json.loads(
            (ROOT / "mncs-language.lock.json").read_text()
        )["revision"],
        "test_repository_revision": git(digest_app.TEST_ROOT, "rev-parse", "HEAD"),
        "stdlib_repository_revision": git(digest_app.STDLIB_ROOT, "rev-parse", "HEAD"),
        "fixture_source_revision": fixture["producer"]["test_revision"],
        "source_hashes": source_hashes,
        "effective_source_hashes": effective_source_hashes,
        "harness_suite_overlay": {
            "functions": [f"{MODULE}::{name}" for name in suite_expected],
            "sha256": sha(SUITE_FIXTURE_SOURCE.encode("utf-8")),
            "stage0_project_oracle_measurement": suite_oracle_metrics,
            "expected_results": suite_expected,
            "native_edge_summary_contract": expected_edge_summary,
            "generic_specializations": sorted(
                f"{module}::{name}"
                for module, name in suite_specializations | assertion_specializations
            ),
        },
        "source_snapshot_identity": built.get("source_snapshot", {}).get("identity"),
        "request_schema": request["schema_version"],
        "response_schema": built["schema_version"],
        "artifact_identity": built["artifact"].get("identity"),
        "artifact_bytes": len(json.dumps(artifact, sort_keys=True).encode("utf-8")),
        "selected_callables": sorted(
            f"{module}::{name}" for module, name in artifact_callables
        ),
        "callable_interfaces": [
            {
                "module": row.get("module"),
                "function": row.get("function"),
                "status": row.get("status"),
                "interface_identity": row.get("interface", {}).get("identity"),
            }
            for row in built.get("callable_scope", {}).get("callable_roots", [])
        ],
        "cold_verification_work": built.get("cache", {}).get("verification_work"),
        "fresh_process_reuse": {
            "status": warm.get("cache", {}).get("status"),
            "front_end_invocations": warm_work.get("front_end_invocations"),
            "artifact_identity": warm.get("artifact", {}).get("identity"),
            "verification_work": warm_work,
        },
        "comment_edit_executable_closure_reuse": {
            "status": comment_build.get("cache", {}).get("status"),
            "reuse_basis": comment_build.get("cache", {}).get("reuse_basis"),
            "source_snapshot_identity": comment_build.get("source_snapshot", {}).get("identity"),
            "artifact_identity": comment_build.get("artifact", {}).get("identity"),
            "verification_work": comment_work,
            "measurement": comment_metrics,
            "selected_callable_interface_identities": current_interfaces,
            "changed_module_sha256": comment_source_file.get("sha256"),
        },
        "reachable_dependency_edit_invalidation": {
            "edited_function": "mncs.test.suite::empty",
            "semantics_preserving_expression": "total: 0 +% 0",
            "status": dependency_build.get("cache", {}).get("status"),
            "reuse_basis": dependency_build.get("cache", {}).get("reuse_basis"),
            "artifact_identity": dependency_build.get("artifact", {}).get("identity"),
            "verification_work": dependency_work,
            "measurement": dependency_metrics,
            "fresh_process_reuse": {
                "status": dependency_exact.get("cache", {}).get("status"),
                "front_end_invocations": dependency_exact_work.get("front_end_invocations"),
                "artifact_identity": dependency_exact.get("artifact", {}).get("identity"),
                "measurement": dependency_exact_metrics,
            },
        },
        "tampered_cache_rejection": {
            "response_field_changed_without_updating_digest": "artifact.identity",
            "cache_status": corruption_recovery.get("cache", {}).get("status"),
            "front_end_invocations": corruption_work.get("front_end_invocations"),
            "artifact_identity": corruption_recovery.get("artifact", {}).get("identity"),
            "artifact_matches_verified_rebuild": (
                corruption_recovery.get("artifact", {}).get("identity")
                == dependency_build.get("artifact", {}).get("identity")
            ),
            "measurement": corruption_recovery_metrics,
        },
        "oracle_fixture": {
            "kind": fixture["kind"] if "kind" in fixture else fixture["objective"],
            "exact_stage0_canonical_vm_agreement": fixture["execution"]["exact_result_agreement"],
            "digest_cases": len(fixture["execution"]["inputs"]),
        },
        "batch": {
            "call_count": len(vm_result.get("results", [])),
            "artifact_identity": vm_result.get("artifact_id"),
            "summary": vm_result.get("summary"),
            "measurement": vm_metrics,
            "normalized_result_sha256": [
                sha(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                for value in normalized_results
            ],
        },
        "retained_vm_session": retained_session,
        "measurements": {"cold_build": cold_metrics, "fresh_process_hit": warm_metrics},
        "language_verified_facts_reused": 0,
        "multi_callable_verified_vm_artifact_reused_after_full_frontend": (
            comment_work.get("backend_artifact") == "reused"
        ),
        "scope": (
            "The real mncs.test.digest source was compiled into one multi-callable application-build/5 "
            "artifact. An imported comment edit reran the full frontend and reused the selected verified "
            "VM closure while rebinding all five current callable interfaces. A semantics-preserving "
            "edit to reachable mncs.test.suite::empty invalidated the closure and regenerated the artifact. "
            "Three Stage-0-backed build_digest inputs, both schema callables, and two harness roots "
            "exercised the real Test suite's summarize<4> and summarize<10>, equals_bytes<4>, "
            "all failure categories, and Test/Suite projections. They ran in one bounded canonical "
            "VM batch and one retained VM session. The retained session recovered from low fuel and "
            "rejected typed input before completing later calls. A cache response with an altered "
            "artifact identity and stale digest was rejected and recomputed from source. This proves "
            "a wider native Test semantic workflow, while Language verified-fact reuse remains zero; "
            "it does not establish Forge/Environment/Store execution or full mncs-test conversion."
        ),
    }
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
