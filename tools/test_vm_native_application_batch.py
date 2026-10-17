#!/usr/bin/env python3
"""Compile and batch-run a real subset of the native mncs-test core.

The source modules are read from the selected mncs-test checkout.  Python
transports typed calls, compares the canonical VM with the Stage-0 project
oracle, and records the bounded execution evidence; assertions, suite
aggregation, property generation, and snapshot witnesses execute in MNCS.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import test_vm_native_application as app

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = app.TEST_ROOT
PROBE = Path(os.environ.get(
    "MNCS_BATCH_PROBE_BIN",
    ROOT / ".build/native-app-readiness/mncs-compiler-stage0-probe",
))
VM_BIN = Path(os.environ.get(
    "MNCS_BATCH_VM_BIN",
    app.VM_ROOT / "target/debug/mncs-vm",
))
CACHE_DIR = ROOT / ".build/native-app-readiness/test-core-batch-cache" / f"run-{os.getpid()}-{time.time_ns()}"
MODULE = "tests.native_test_core_batch"
SOURCE_ID = "tests/native_test_core_batch.mncs"
MODULE_FILES = {
    "mncs.test.assertions": TEST_ROOT / "native/mncs/test/assertions.mncs",
    "mncs.test.suite": TEST_ROOT / "native/mncs/test/suite.mncs",
    "mncs.test.generative": TEST_ROOT / "native/mncs/test/generative.mncs",
    "mncs.test.snapshot": TEST_ROOT / "native/mncs/test/snapshot.mncs",
}

SOURCE = """mncs 0.18;
module tests.native_test_core_batch;

use mncs.test.assertions;
use mncs.test.suite;
use mncs.test.generative;
use mncs.test.snapshot;

fn passed_suite() -> (result: SuiteSummary) {
    let arithmetic: TestResult = from_assertion(equals_i64(42, 19 +% 23, 1001));
    let property_test: TestResult = from_assertion(equals_i64(58, replay(42, 7, 97) as i64, 1004));
    let bytes: TestResult = from_assertion(equals_bytes<4>([1, 2, 3, 4], [1, 2, 3, 4], 1008));
    let skipped: TestResult = skip(1006);
    return summarize<4>([arithmetic, property_test, bytes, skipped]);
}

fn failed_suite() -> (result: SuiteSummary) {
    let failed: TestResult = from_assertion(equals_i64(5, 3, 2001));
    let passed: TestResult = from_assertion(equals_i64(8, 4 +% 4, 2002));
    let skipped: TestResult = skip(2003);
    let unsupported_result: TestResult = unsupported(2004);
    return summarize<4>([failed, passed, skipped, unsupported_result]);
}

fn unsupported_suite() -> (result: SuiteSummary) {
    let passed: TestResult = from_assertion(equals_i64(9, 3 *% 3, 3001));
    let skipped: TestResult = skip(3002);
    let unsupported_result: TestResult = unsupported(3003);
    let empty_result: TestResult = pass();
    return summarize<4>([passed, skipped, unsupported_result, empty_result]);
}

fn assertion_projection() -> (result: TestProjection) {
    return project_test(from_assertion(equals_i64(5, 3, 4001)));
}

fn property_case(seed: u64, case_index: u64) -> (result: PropertyCase) {
    return draw_case(seed, case_index);
}

fn replay_case(seed: u64, case_index: u64, bound: u64) -> (result: u64) {
    return replay(seed, case_index, bound);
}

fn snapshot_witness() -> (result: Witness) {
    return bytes([1, 2, 3, 4]);
}
"""

ROOTS = [
    {"module": MODULE, "function": name}
    for name in (
        "assertion_projection",
        "failed_suite",
        "passed_suite",
        "property_case",
        "replay_case",
        "snapshot_witness",
        "unsupported_suite",
    )
]
GENERIC_SEEDS = [
    {
        "module": "mncs.test.assertions",
        "function": "equals_bytes",
        "type_arguments": [{"kind": "nat", "value": 4}],
    },
    {
        "module": "mncs.test.suite",
        "function": "summarize",
        "type_arguments": [{"kind": "nat", "value": 4}],
    },
]


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def build_request(roots: list[dict] | None = None, source: str = SOURCE) -> dict:
    modules = {name: path.read_text(encoding="utf-8") for name, path in MODULE_FILES.items()}
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": SOURCE_ID,
        "root_source": source,
        "callable_roots": ROOTS if roots is None else roots,
        "modules": modules,
        "generic_seeds": GENERIC_SEEDS,
        "capabilities": [],
        "verification_policy": "mncs-test-core-selected-callables/1",
    }


def vm_batch(artifact: dict, cases: list[dict]) -> tuple[dict, float, int | None]:
    calls = [{
        "id": case["id"],
        "request": {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": case["function"]},
            "arguments": case.get("arguments", []),
            "type_arguments": [],
            "step_budget": case.get("step_budget", 2_000_000),
        },
    } for case in cases]
    with tempfile.TemporaryDirectory(prefix="mncs-test-core-batch-") as raw:
        directory = Path(raw)
        artifact_path = directory / "application.vm.json"
        calls_path = directory / "calls.json"
        output_path = directory / "results.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
        calls_path.write_text(json.dumps(calls, sort_keys=True) + "\n", encoding="utf-8")
        started = time.monotonic()
        completed = subprocess.run(
            ["/usr/bin/time", "-v", str(VM_BIN), "batch",
             "--artifact", str(artifact_path), "--calls", str(calls_path),
             "--output", str(output_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        wall = time.monotonic() - started
        if completed.returncode != 0:
            raise AssertionError(f"canonical VM batch failed: {completed.stderr[-3000:]}")
        peak_rss_kib = None
        match = re.search(r"Maximum resident set size \(kbytes\): (\d+)", completed.stderr)
        if match:
            peak_rss_kib = int(match.group(1))
        return json.loads(output_path.read_text(encoding="utf-8")), wall, peak_rss_kib


def callable_rows(response: dict) -> list[dict]:
    rows = response.get("callable_scope", {}).get("callable_roots", [])
    if not isinstance(rows, list):
        raise TypeError("application response omitted selected callable identities")
    return rows


def main() -> None:
    if not PROBE.is_file() or not VM_BIN.is_file():
        raise SystemExit(f"missing probe or canonical VM binary: {PROBE}, {VM_BIN}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    request = build_request()
    probe = app.Probe(CACHE_DIR, executable=PROBE)
    try:
        built, build_metrics = probe.send({"compile_application": request}, timeout=180.0)
        if (built.get("schema_version") != "mncs.compiler.application-build/5"
                or built.get("status") != "verified"
                or built.get("cache", {}).get("status") != "miss"):
            raise AssertionError("production Test callable cold build failed: " + json.dumps({
                "schema_version": built.get("schema_version"),
                "status": built.get("status"),
                "cache": built.get("cache", {}).get("status"),
                "diagnostic_codes": [row.get("code") for row in built.get("diagnostics", [])],
            }))
        artifact = built.get("artifact", {}).get("value")
        if not isinstance(artifact, dict):
            raise TypeError("multi-module Test build omitted its canonical VM artifact")
        roots = callable_rows(built)
        root_identities = [(row.get("module"), row.get("function")) for row in roots]
        expected_roots = [(MODULE, row["function"]) for row in ROOTS]
        if root_identities != expected_roots or any(row.get("status") != "verified" for row in roots):
            raise AssertionError(f"selected Test callables differ from the request: {roots}")
        callables = {
            (row.get("module"), row.get("name")) for row in artifact.get("callables", [])
        }
        if not set(expected_roots).issubset(callables):
            raise AssertionError(f"artifact omitted selected Test callables: {callables}")
        if not any(module == "mncs.test.suite" and name.startswith("summarize__spec_")
                   for module, name in callables):
            raise AssertionError("artifact omitted the requested native generic suite fold")

        warm_probe = app.Probe(CACHE_DIR, executable=PROBE)
        try:
            warm, warm_metrics = warm_probe.send({"compile_application": request}, timeout=90.0)
            if (warm.get("cache", {}).get("status") != "hit"
                    or warm.get("artifact", {}).get("identity") != built["artifact"]["identity"]
                    or warm.get("cache", {}).get("verification_work", {}).get("front_end_invocations") != 0):
                raise AssertionError("fresh-process exact snapshot reuse changed or reverified the artifact")
        finally:
            warm_probe.close()

        subset_request = build_request([ROOTS[3]])
        subset_probe = app.Probe(CACHE_DIR, executable=PROBE)
        try:
            subset, subset_metrics = subset_probe.send(
                {"compile_application": subset_request}, timeout=180.0
            )
            subset_exports = {
                (row.get("module"), row.get("name"))
                for row in subset.get("artifact", {}).get("value", {}).get("callables", [])
            }
            subset_roots = {(module, name) for module, name in subset_exports
                            if module == MODULE and name == "property_case"}
            subset_generics = {(module, name) for module, name in subset_exports
                               if ((module == "mncs.test.assertions"
                                    and name.startswith("equals_bytes__spec_"))
                                   or (module == "mncs.test.suite"
                                       and name.startswith("summarize__spec_")))}
            if (subset.get("status") != "verified"
                    or subset.get("cache", {}).get("status") != "miss"
                    or len(subset_exports) != 3
                    or subset_roots != {(MODULE, "property_case")}
                    or len(subset_generics) != 2
                    or not any(module == "mncs.test.assertions" and name.startswith("equals_bytes__spec_")
                               for module, name in subset_generics)
                    or not any(module == "mncs.test.suite" and name.startswith("summarize__spec_")
                               for module, name in subset_generics)):
                raise AssertionError(f"selected-callable subset was not isolated: {subset_exports}")
        finally:
            subset_probe.close()

        invalid_request = build_request(source=SOURCE.replace(
            "return summarize<4>([arithmetic, property_test, bytes, skipped]);",
            "return summarize<4>([arithmetic, property, bytes, skipped_missing]);",
            1,
        ))
        invalid_probe = app.Probe(CACHE_DIR, executable=PROBE)
        try:
            invalid, _ = invalid_probe.send({"compile_application": invalid_request}, timeout=180.0)
            missing, _ = invalid_probe.send({"compile_application": build_request(
                [{"module": MODULE, "function": "missing_callable"}]
            )}, timeout=90.0)
        finally:
            invalid_probe.close()
        if (invalid.get("status") != "rejected" or not invalid.get("diagnostics")
                or invalid.get("artifact") is not None
                or invalid.get("cache", {}).get("publication") != "not_published"):
            raise AssertionError(f"invalid source was masked or published: {invalid}")
        if (missing.get("status") != "unsupported" or missing.get("artifact") is not None
                or not missing.get("unresolved_obligations")):
            raise AssertionError(f"missing callable produced a false success: {missing}")
        probe.close()
    except BaseException:
        if probe.proc.poll() is None:
            probe.proc.kill()
            probe.proc.wait(timeout=5)
        raise

    cases = [
        {"id": "passed-suite", "function": "passed_suite"},
        {"id": "failed-suite", "function": "failed_suite"},
        {"id": "unsupported-suite", "function": "unsupported_suite"},
        {"id": "assertion-projection", "function": "assertion_projection"},
        {"id": "property-case", "function": "property_case", "arguments": [
            {"integer": {"type": {"bits": 64, "signed": False}, "value": 42}},
            {"integer": {"type": {"bits": 64, "signed": False}, "value": 7}},
        ]},
        {"id": "property-replay", "function": "replay_case", "arguments": [
            {"integer": {"type": {"bits": 64, "signed": False}, "value": 42}},
            {"integer": {"type": {"bits": 64, "signed": False}, "value": 7}},
            {"integer": {"type": {"bits": 64, "signed": False}, "value": 97}},
        ]},
        {"id": "snapshot-witness", "function": "snapshot_witness"},
        {"id": "budget-limited-suite", "function": "passed_suite", "step_budget": 1},
        {"id": "invalid-property-case-type", "function": "property_case", "arguments": [
            {"boolean": {"value": True}},
            {"integer": {"type": {"bits": 64, "signed": False}, "value": 7}},
        ]},
        {"id": "suite-after-negative-calls", "function": "passed_suite"},
    ]
    oracle_probe = app.Probe(CACHE_DIR, executable=PROBE)
    try:
        oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
            "root": SOURCE,
            "modules": {name: path.read_text(encoding="utf-8") for name, path in MODULE_FILES.items()},
            "calls": [{
                "schema_version": "0.1",
                "target": {"module": MODULE, "function": case["function"]},
                "arguments": case.get("arguments", []),
                "type_arguments": [],
                "step_budget": 2_000_000,
            } for case in cases[:7]],
        }}, timeout=180.0)
        if not oracle.get("valid") or len(oracle.get("executions", [])) != 7:
            raise AssertionError("Stage-0 Test core oracle failed: " + json.dumps({
                "valid": oracle.get("valid"),
                "execution_count": len(oracle.get("executions", [])),
                "diagnostics": oracle.get("diagnostics"),
            }))
        oracle_probe.close()
    except BaseException:
        if oracle_probe.proc.poll() is None:
            oracle_probe.proc.kill()
            oracle_probe.proc.wait(timeout=5)
        raise

    vm_result, vm_wall, vm_peak_rss_kib = vm_batch(artifact, cases)
    results = vm_result.get("results", [])
    if len(results) != len(cases):
        raise AssertionError(f"canonical VM returned the wrong batch length: {results}")
    artifact_id = built.get("artifact", {}).get("value", {}).get("artifact_id")
    if vm_result.get("artifact_id") != artifact_id:
        raise AssertionError("canonical VM batch receipt does not bind the verified artifact")
    observed = []
    for case, actual_row, oracle_row in zip(cases, results, oracle["executions"]):
        if actual_row.get("id") != case["id"] or actual_row.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"native Test batch call did not complete: {actual_row}")
        if actual_row.get("record", {}).get("artifact_id") != artifact_id:
            raise AssertionError(f"Test call receipt is bound to a different artifact: {actual_row}")
        returned = actual_row.get("record", {}).get("returned", [])
        expected_values = oracle_row.get("returned", [])
        if len(returned) != 1 or len(expected_values) != 1:
            raise AssertionError(f"Test callable did not return one typed value: {case['id']}")
        actual = app.decode_wire(app.vm_to_wire(returned[0]))
        expected = app.decode_wire(expected_values[0])
        if actual != expected:
            raise AssertionError(f"canonical VM diverged from Stage-0 for {case['id']}: {actual} != {expected}")
        observed.append({"id": case["id"], "value": actual})

    budget_row, invalid_row, recovery_row = results[-3:]
    if (budget_row.get("id") != "budget-limited-suite"
            or budget_row.get("outcome", {}).get("kind") != "budget_exhausted"
            or budget_row.get("outcome", {}).get("dimension") != "steps"):
        raise AssertionError(f"bounded Test call did not fail with its step limit: {budget_row}")
    if (invalid_row.get("id") != "invalid-property-case-type"
            or invalid_row.get("outcome", {}).get("kind") != "invalid_request"):
        raise AssertionError(f"invalid typed Test input was not rejected at the call boundary: {invalid_row}")
    recovery_values = recovery_row.get("record", {}).get("returned", [])
    if (recovery_row.get("id") != "suite-after-negative-calls"
            or recovery_row.get("outcome") != {"kind": "completed"}
            or recovery_row.get("record", {}).get("artifact_id") != artifact_id
            or len(recovery_values) != 1
            or app.decode_wire(app.vm_to_wire(recovery_values[0])) != observed[0]["value"]):
        raise AssertionError(f"negative calls contaminated the following native suite call: {recovery_row}")

    by_id = {row["id"]: row["value"] for row in observed}
    passed = by_id["passed-suite"]
    failed = by_id["failed-suite"]
    unsupported = by_id["unsupported-suite"]
    witness = by_id["snapshot-witness"]
    if (passed["total"] != 4 or passed["passed"] != 3 or passed["skipped"] != 1
            or passed["failed"] != 0 or passed["verdict_code"] != 0):
        raise AssertionError(f"native suite pass/skip aggregation was wrong: {passed}")
    if (failed["total"] != 4 or failed["failed"] != 1 or failed["skipped"] != 1
            or failed["unsupported"] != 1 or failed["assertion_failures"] != 1
            or failed["verdict_code"] != 1):
        raise AssertionError(f"native suite failure precedence or counts were wrong: {failed}")
    if (unsupported["total"] != 4 or unsupported["passed"] != 2
            or unsupported["skipped"] != 1 or unsupported["unsupported"] != 1
            or unsupported["verdict_code"] != 3):
        raise AssertionError(f"native suite unsupported aggregation was wrong: {unsupported}")
    if (witness["length"] != 4 or witness["checksum"] != 30
            or by_id["property-case"]["seed"] != 42
            or by_id["property-case"]["case_index"] != 7
            or not 0 <= by_id["property-replay"] < 97):
        raise AssertionError("native generative/snapshot result did not preserve its bounded inputs")

    source_rows = [{
        "module": name,
        "source_sha256": sha(path.read_bytes()),
    } for name, path in sorted(MODULE_FILES.items())]
    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.native-mncs-test-core-batch-isolation-proof",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "compiler": {
            "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
            "harness_sha256": sha(Path(__file__).read_bytes()),
            "probe_sha256": sha(PROBE.read_bytes()),
            "canonical_vm_sha256": sha(VM_BIN.read_bytes()),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        },
        "test_repository": {
            "revision": subprocess.check_output(["git", "-C", str(TEST_ROOT), "rev-parse", "HEAD"], text=True).strip(),
            "production_modules": source_rows,
        },
        "application": {
            "module": MODULE,
            "source_sha256": sha(SOURCE.encode()),
            "source_snapshot_identity": built.get("source_snapshot", {}).get("identity"),
            "contract_schema": built.get("schema_version"),
            "artifact_identity": built.get("artifact", {}).get("identity"),
            "artifact_sha256": sha(json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()),
            "selected_roots": [{
                "module": row.get("module"),
                "function": row.get("function"),
                "status": row.get("status"),
                "interface_identity": row.get("interface", {}).get("identity"),
                "signature": row.get("interface", {}).get("signature"),
            } for row in roots],
            "selected_generic_seeds": GENERIC_SEEDS,
            "exported_callables": sorted([list(row) for row in callables]),
            "capability_dispatch": {
                "status": built.get("capability_dispatch", {}).get("status"),
                "binding_count": len(built.get("capability_dispatch", {}).get("bindings", [])),
                "unresolved_obligations": built.get("capability_dispatch", {}).get("unresolved_obligations"),
            },
        },
        "checks": {
            "fresh_process_exact_snapshot_reuse": warm.get("cache", {}).get("status"),
            "subset_selection_cache_isolation": subset.get("cache", {}).get("status"),
            "invalid_source_status": invalid.get("status"),
            "invalid_source_publication": invalid.get("cache", {}).get("publication"),
            "unknown_callable_status": missing.get("status"),
            "canonical_vm_batch_count": len(results),
            "stage0_oracle_execution_count": len(oracle.get("executions", [])),
            "execution_equivalent": True,
            "budget_exhaustion_isolated": True,
            "invalid_typed_input_isolated": True,
            "post_failure_call_matches_prior_suite": True,
            "language_verified_facts_reused": 0,
            "semantics_executed_in_mncs": [
                "assertion construction and projection",
                "suite pass, failure, skip, and unsupported aggregation",
                "seeded property case generation and replay",
                "bounded snapshot witness calculation",
            ],
        },
        "measurements": {
            "cold_application_build": build_metrics,
            "fresh_process_exact_hit": warm_metrics,
            "selected_callable_subset_build": subset_metrics,
            "stage0_oracle": oracle_metrics,
            "canonical_vm_batch_wall_seconds": round(vm_wall, 6),
            "canonical_vm_batch_peak_rss_kib": vm_peak_rss_kib,
            "canonical_vm_batch_invocations": 1,
        },
        "scope": (
            "Seven selected entrypoints from a multi-module production mncs-test core were compiled "
            "into one canonical VM artifact and invoked in one admitted VM session. Seven valid typed "
            "results matched Stage-0. A one-step budget exhaustion and an invalid typed argument were "
            "then isolated to their calls; the following valid suite call matched its earlier result. "
            "This is Compiler/VM workload evidence; it does not claim Forge, Environment, Store "
            "publication, or production CLI conversion."
        ),
    }
    evidence_path = ROOT / "evidence/native-app-readiness-test-core-batch-isolation-20261017.json"
    evidence_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
