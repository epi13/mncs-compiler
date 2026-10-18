#!/usr/bin/env python3
"""Select production mncs-test provider cases by Language identity and batch them.

The selected roots call the real provider's pure result-folding functions.
The harness transports bounded typed calls and compares canonical VM values
with the Stage-0 project oracle; provider policy stays in MNCS.
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
STDLIB_ROOT = app.WORKSPACE_ROOT / "mncs-stdlib"
PROBE = Path(os.environ.get(
    "MNCS_PROVIDER_BATCH_PROBE_BIN",
    ROOT / ".build/native-app-readiness/mncs-compiler-stage0-probe",
))
VM_BIN = Path(os.environ.get(
    "MNCS_PROVIDER_BATCH_VM_BIN",
    app.VM_ROOT / "target/debug/mncs-vm",
))
CACHE_DIR = ROOT / ".build/native-app-readiness/provider-fold-cache" / f"run-{os.getpid()}-{time.time_ns()}"
MODULE = "tests.native_provider_fold_batch"
SOURCE_ID = "tests/native_provider_fold_batch.mncs"
MODULE_FILES = {
    "mncs.test.assertions": TEST_ROOT / "native/mncs/test/assertions.mncs",
    "mncs.test.provider": TEST_ROOT / "native/mncs/test/provider.mncs",
    "mncs.test.provider_protocol.v1": TEST_ROOT / "native/mncs/test/provider_protocol/v1.mncs",
    "mncs.core.sequences.v1": STDLIB_ROOT / "library/core/sequences.mncs",
    "mncs.std.application.v1": STDLIB_ROOT / "library/std/application.mncs",
}

SOURCE = """mncs 0.18;
module tests.native_provider_fold_batch;

use mncs.test.assertions;
use mncs.test.provider as provider;
use mncs.test.provider_protocol.v1 as protocol;

fn execution(id: byte, status: protocol.CallableExecutionStatus, result: TestResult)
    -> (result: protocol.ProviderTestExecution) {
    return protocol.ProviderTestExecution {
        test_case_identity: [id],
        declaration_identity: [id],
        callable_identity: [id],
        signature_identity: [id],
        artifact_identity: [id],
        execution_status: status,
        native_result: result
    };
}

fn fold_status_scenario(scenario: byte) -> (result: TestResult) {
    if scenario == 0 {
        let passed: protocol.ProviderTestExecution = execution(
            1, protocol.CallableExecutionStatus.RETURNED, from_assertion(equals_i64(42, 42, 9101))
        );
        let skipped: protocol.ProviderTestExecution = execution(2, protocol.CallableExecutionStatus.RETURNED, skip(9102));
        let budgeted: protocol.ProviderTestExecution = execution(3, protocol.CallableExecutionStatus.BUDGET_EXHAUSTED, pass());
        let unsupported_run: protocol.ProviderTestExecution = execution(4, protocol.CallableExecutionStatus.UNSUPPORTED, pass());
        return provider.native_result(provider.fold_selected(
            [passed, skipped, budgeted, unsupported_run]
        ));
    }
    if scenario == 1 {
        let failed: protocol.ProviderTestExecution = execution(
            5, protocol.CallableExecutionStatus.RETURNED, from_assertion(equals_i64(5, 3, 9201))
        );
        let precedence_unsupported: protocol.ProviderTestExecution = execution(6, protocol.CallableExecutionStatus.UNSUPPORTED, pass());
        return provider.native_result(provider.fold_selected([failed, precedence_unsupported]));
    }
    if scenario == 2 {
        let unsupported_run: protocol.ProviderTestExecution = execution(7, protocol.CallableExecutionStatus.UNSUPPORTED, pass());
        let skipped: protocol.ProviderTestExecution = execution(8, protocol.CallableExecutionStatus.RETURNED, skip(9202));
        return provider.native_result(provider.fold_selected([unsupported_run, skipped]));
    }
    let invalid: protocol.ProviderTestExecution = execution(9, protocol.CallableExecutionStatus.INVALID_REQUEST, pass());
    return provider.native_result(provider.fold_selected([invalid]));
}

test mixed_statuses() -> (result: TestResult) {
    return fold_status_scenario(0);
}

test failure_precedence() -> (result: TestResult) {
    return fold_status_scenario(1);
}

test unsupported_only() -> (result: TestResult) {
    return fold_status_scenario(2);
}

test invalid_request() -> (result: TestResult) {
    return fold_status_scenario(3);
}
"""

CALLABLE = {"module": MODULE, "function": "fold_status_scenario"}
TEST_NAMES = ("mixed_statuses", "failure_precedence", "unsupported_only", "invalid_request")
ROOTS = [CALLABLE] + [
    {"module": MODULE, "function": name} for name in TEST_NAMES
]
SCENARIOS = [
    {"id": "mixed_statuses", "scenario": 0},
    {"id": "failure_precedence", "scenario": 1},
    {"id": "unsupported_only", "scenario": 2},
    {"id": "invalid_request", "scenario": 3},
]


def call_request(call: dict) -> dict:
    if call["kind"] == "inventory_test":
        test = call["test"]
        return {
            "schema_version": "0.1",
            "target": {"module": test["module"], "function": test["name"]},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 2_000_000,
        }
    return {
        "schema_version": "0.1",
        "target": CALLABLE,
        "arguments": [{"byte": {"value": call["scenario"]}}],
        "type_arguments": [],
        "step_budget": 2_000_000,
    }


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def request(test_selection: dict | None = None) -> dict:
    modules = {name: path.read_text(encoding="utf-8") for name, path in MODULE_FILES.items()}
    return {
        "schema_version": "mncs.compiler.application-build-request/4",
        "target": "mncs-vm-0.1",
        "root_source_id": SOURCE_ID,
        "root_source": SOURCE,
        "callable_roots": [CALLABLE],
        "test_selection": {"kind": "all_source_cases"} if test_selection is None else test_selection,
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "mncs-test-provider-status-fold/1",
    }


def vm_batch(artifact: dict, calls: list[dict]) -> tuple[dict, float, int | None]:
    rows = [{
        "id": item["id"],
        "request": call_request(item),
    } for item in calls]
    with tempfile.TemporaryDirectory(prefix="mncs-test-provider-fold-") as raw:
        directory = Path(raw)
        artifact_path = directory / "provider.vm.json"
        calls_path = directory / "calls.json"
        output_path = directory / "results.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
        calls_path.write_text(json.dumps(rows, sort_keys=True) + "\n", encoding="utf-8")
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
            raise AssertionError(f"canonical VM provider batch failed: {completed.stderr[-2500:]}")
        match = re.search(r"Maximum resident set size \(kbytes\): (\d+)", completed.stderr)
        peak_rss = int(match.group(1)) if match else None
        return json.loads(output_path.read_text(encoding="utf-8")), wall, peak_rss


def main() -> None:
    if not PROBE.is_file() or not VM_BIN.is_file():
        raise SystemExit(f"missing probe or canonical VM binary: {PROBE}, {VM_BIN}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    inventory_request = request()
    probe = app.Probe(CACHE_DIR, executable=PROBE)
    try:
        inventory_build, inventory_build_metrics = probe.send(
            {"compile_application": inventory_request}, timeout=180.0
        )
        if (inventory_build.get("schema_version") != "mncs.compiler.application-build/7"
                or inventory_build.get("status") != "verified"
                or inventory_build.get("cache", {}).get("status") != "miss"):
            raise AssertionError("provider fold inventory build failed: " + json.dumps({
                "status": inventory_build.get("status"),
                "cache": inventory_build.get("cache", {}).get("status"),
                "diagnostic_codes": [row.get("code") for row in inventory_build.get("diagnostics", [])],
            }))
        inventory = inventory_build.get("source_test_inventory")
        if (not isinstance(inventory, dict)
                or inventory.get("schema_version") != "mncs.test-inventory/1"
                or inventory.get("scope") != "source_module"
                or inventory.get("module") != MODULE):
            raise AssertionError(f"provider fold build omitted its current Test inventory: {inventory}")
        inventory_tests = inventory.get("tests", [])
        if (not isinstance(inventory_tests, list)
                or {row.get("name") for row in inventory_tests} != set(TEST_NAMES)
                or any(not row.get("test_case_identity")
                       or not row.get("declaration_identity")
                       or not row.get("function_identity")
                       or not row.get("semantic_fingerprint")
                       for row in inventory_tests)):
            raise AssertionError(f"provider fold Test inventory lost stable case identities: {inventory_tests}")
        build_request = request({
            "kind": "case_identities",
            "test_case_identities": [row["test_case_identity"] for row in inventory_tests],
        })
        selection_probe = app.Probe(CACHE_DIR, executable=PROBE)
        try:
            built, build_metrics = selection_probe.send(
                {"compile_application": build_request}, timeout=180.0
            )
        finally:
            selection_probe.close()
        if (built.get("schema_version") != "mncs.compiler.application-build/7"
                or built.get("status") != "verified"
                or built.get("cache", {}).get("status") != "miss"):
            raise AssertionError("provider fold identity-selected build failed: " + json.dumps({
                "status": built.get("status"),
                "cache": built.get("cache", {}).get("status"),
                "diagnostic_codes": [row.get("code") for row in built.get("diagnostics", [])],
            }))
        selection = built.get("test_case_selection")
        if (not isinstance(selection, dict)
                or selection.get("schema_version") != "mncs.compiler.source-test-case-selection/1"
                or selection.get("status") != "verified"
                or selection.get("selection") != build_request["test_selection"]
                or selection.get("selected_test_case_identities")
                != [row["test_case_identity"] for row in sorted(
                    inventory_tests, key=lambda row: row["declaration_identity"]
                )]
                or selection.get("cases") != sorted(
                    inventory_tests, key=lambda row: row["declaration_identity"]
                )):
            raise AssertionError(f"Compiler did not resolve exact selected Test identities: {selection}")
        artifact = built.get("artifact", {}).get("value")
        if not isinstance(artifact, dict):
            raise TypeError("identity-selected provider fold build omitted its canonical VM artifact")
        rows = built.get("callable_scope", {}).get("callable_roots", [])
        selected_roots = {(row.get("module"), row.get("function")) for row in rows}
        expected_roots = {(row["module"], row["function"]) for row in ROOTS}
        if (selected_roots != expected_roots
                or len(rows) != len(ROOTS)
                or any(row.get("status") != "verified" for row in rows)):
            raise AssertionError(f"provider fold callables were not all verified: {rows}")

        warm_probe = app.Probe(CACHE_DIR, executable=PROBE)
        try:
            warm, warm_metrics = warm_probe.send(
                {"compile_application": build_request}, timeout=90.0
            )
            if (warm.get("cache", {}).get("status") != "hit"
                    or warm.get("artifact", {}).get("identity") != built["artifact"]["identity"]
                    or warm.get("cache", {}).get("verification_work", {}).get("front_end_invocations") != 0
                    or warm.get("source_test_inventory") != inventory
                    or warm.get("test_case_selection") != selection):
                raise AssertionError("provider fold fresh-process artifact reuse was not exact")
        finally:
            warm_probe.close()
        probe.close()
    except BaseException:
        if probe.proc.poll() is None:
            probe.proc.kill()
            probe.proc.wait(timeout=5)
        raise

    calls = [
        {**row, "kind": "scenario", "id": f"scenario:{row['id']}"}
        for row in SCENARIOS
    ] + [
        {"kind": "inventory_test", "id": row["test_case_identity"], "test": row}
        for row in inventory_tests
    ]
    edge_probe = app.Probe(CACHE_DIR, executable=PROBE)
    try:
        comment_request = request(build_request["test_selection"])
        comment_request["root_source"] = SOURCE.replace(
            "test mixed_statuses()",
            "// selection span must come from the current frontend\n\ntest mixed_statuses()",
            1,
        )
        comment_response, comment_metrics = edge_probe.send(
            {"compile_application": comment_request}, timeout=180.0
        )
        comment_inventory = comment_response.get("source_test_inventory", {})
        comment_selection = comment_response.get("test_case_selection", {})
        before_by_id = {
            row["test_case_identity"]: row for row in inventory_tests
        }
        after_by_id = {
            row["test_case_identity"]: row
            for row in comment_inventory.get("tests", [])
        }
        shifted_id = next(
            row["test_case_identity"]
            for row in inventory_tests
            if row["name"] == "mixed_statuses"
        )
        selected_after_comment = {
            row["test_case_identity"]: row
            for row in comment_selection.get("cases", [])
        }
        if (comment_response.get("status") != "verified"
                or comment_selection.get("status") != "verified"
                or set(after_by_id) != set(before_by_id)
                or after_by_id[shifted_id]["source_span"]["start"]
                <= before_by_id[shifted_id]["source_span"]["start"]
                or selected_after_comment[shifted_id]["source_span"]["start"]
                != after_by_id[shifted_id]["source_span"]["start"]):
            raise AssertionError("selected Test identities did not refresh to the current source locations")

        stale_request = request(build_request["test_selection"])
        stale_request["root_source"] = SOURCE.replace(
            "return fold_status_scenario(0);",
            "return fold_status_scenario(1);",
            1,
        )
        stale_response, stale_metrics = edge_probe.send(
            {"compile_application": stale_request}, timeout=180.0
        )
        if (stale_response.get("status") != "rejected"
                or stale_response.get("artifact") is not None
                or stale_response.get("test_case_selection", {}).get("status") != "rejected"
                or stale_response.get("source_test_inventory") is None
                or stale_response.get("cache", {}).get("verification_work", {}).get(
                    "front_end_invocations"
                ) != 1):
            raise AssertionError("a stale body-sensitive Test identity reused prior success")

        invalid_request = request(build_request["test_selection"])
        invalid_request["root_source"] = SOURCE.replace(
            "return fold_status_scenario(0);",
            "return missing_value;",
            1,
        )
        invalid_response, invalid_metrics = edge_probe.send(
            {"compile_application": invalid_request}, timeout=180.0
        )
        if (invalid_response.get("status") != "rejected"
                or invalid_response.get("artifact") is not None
                or invalid_response.get("source_test_inventory") is not None
                or invalid_response.get("test_case_selection", {}).get("status") != "rejected"
                or invalid_response.get("cache", {}).get("verification_work", {}).get(
                    "front_end_invocations"
                ) != 1):
            raise AssertionError("invalid source reused a prior selected Test artifact")
    finally:
        edge_probe.close()

    oracle_probe = app.Probe(CACHE_DIR, executable=PROBE)
    try:
        oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
            "root": SOURCE,
            "modules": {name: path.read_text(encoding="utf-8") for name, path in MODULE_FILES.items()},
            "calls": [call_request(item) for item in calls],
        }}, timeout=180.0)
        if not oracle.get("valid") or len(oracle.get("executions", [])) != len(calls):
            raise AssertionError("Stage-0 provider fold oracle failed: " + json.dumps({
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

    vm_result, vm_wall, vm_peak_rss = vm_batch(artifact, calls)
    results = vm_result.get("results", [])
    if (len(results) != len(calls)
            or vm_result.get("artifact_id") != artifact.get("artifact_id")):
        raise AssertionError("canonical VM provider batch lost its call count or artifact identity")
    observed: dict[str, dict] = {}
    observed_inventory_tests: dict[str, dict] = {}
    for call, vm_row, oracle_row in zip(calls, results, oracle["executions"]):
        if (vm_row.get("id") != call["id"]
                or vm_row.get("outcome") != {"kind": "completed"}
                or vm_row.get("record", {}).get("artifact_id") != artifact.get("artifact_id")):
            raise AssertionError(f"provider fold VM call did not complete on the admitted artifact: {vm_row}")
        actual_rows = vm_row.get("record", {}).get("returned", [])
        expected_rows = oracle_row.get("returned", [])
        if len(actual_rows) != 1 or len(expected_rows) != 1:
            raise AssertionError(f"provider fold returned the wrong arity: {call['function']}")
        actual = app.decode_wire(app.vm_to_wire(actual_rows[0]))
        expected = app.decode_wire(expected_rows[0])
        if actual != expected:
            raise AssertionError(f"canonical VM diverged from Stage-0 for {call['id']}")
        if call["kind"] == "scenario":
            observed[call["id"].removeprefix("scenario:")] = actual
        else:
            test_name = call["test"]["name"]
            if actual != observed[test_name]:
                raise AssertionError(f"inventory Test {test_name} changed its provider result")
            observed_inventory_tests[test_name] = actual

    mixed = observed["mixed_statuses"]
    precedence = observed["failure_precedence"]
    unsupported = observed["unsupported_only"]
    invalid = observed["invalid_request"]
    if (mixed["verdict_code"] != 1 or mixed["assertions"] != 1
            or mixed["failures"] != 1 or mixed["failure_code"] != 9004):
        raise AssertionError(f"mixed provider statuses were folded incorrectly: {mixed}")
    if (precedence["verdict_code"] != 1 or precedence["failures"] != 1
            or precedence["failure_code"] != 9201
            or precedence["expected"] != 5 or precedence["actual"] != 3):
        raise AssertionError(f"provider failure precedence or witness was wrong: {precedence}")
    if (unsupported["verdict_code"] != 3 or unsupported["failures"] != 0
            or unsupported["failure_code"] != 9003):
        raise AssertionError(f"unsupported provider outcome was folded incorrectly: {unsupported}")
    if (invalid["verdict_code"] != 1 or invalid["failures"] != 1
            or invalid["failure_code"] != 9005):
        raise AssertionError(f"invalid callable request was folded incorrectly: {invalid}")

    test_revision = subprocess.check_output(
        ["git", "-C", str(TEST_ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    stdlib_revision = subprocess.check_output(
        ["git", "-C", str(STDLIB_ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.native-test-provider-fold-batch-proof",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "compiler": {
            "revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
            "harness_sha256": sha(Path(__file__).read_bytes()),
            "probe_sha256": sha(PROBE.read_bytes()),
            "canonical_vm_sha256": sha(VM_BIN.read_bytes()),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        },
        "production_sources": {
            "test_revision": test_revision,
            "provider_sha256": sha(MODULE_FILES["mncs.test.provider"].read_bytes()),
            "protocol_sha256": sha(MODULE_FILES["mncs.test.provider_protocol.v1"].read_bytes()),
            "assertions_sha256": sha(MODULE_FILES["mncs.test.assertions"].read_bytes()),
            "stdlib_revision": stdlib_revision,
            "sequence_module_sha256": sha(MODULE_FILES["mncs.core.sequences.v1"].read_bytes()),
            "application_module_sha256": sha(MODULE_FILES["mncs.std.application.v1"].read_bytes()),
        },
        "application": {
            "module": MODULE,
            "source_sha256": sha(SOURCE.encode()),
            "source_snapshot_identity": built.get("source_snapshot", {}).get("identity"),
            "contract_schema": built.get("schema_version"),
            "artifact_identity": built.get("artifact", {}).get("identity"),
            "test_case_selection": {
                "schema_version": selection.get("schema_version"),
                "kind": selection.get("selection", {}).get("kind"),
                "selected_test_case_identities": selection.get("selected_test_case_identities"),
                "selected_test_roots": selection.get("selected_test_roots"),
            },
            "artifact_sha256": sha(json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()),
            "source_test_inventory": {
                "schema_version": inventory.get("schema_version"),
                "scope": inventory.get("scope"),
                "module": inventory.get("module"),
                "subject_fingerprint": inventory.get("subject_fingerprint"),
                "source_artifact_identity": inventory.get("source_artifact_identity"),
                "test_cases": [{
                    "name": row.get("name"),
                    "test_case_identity": row.get("test_case_identity"),
                    "declaration_identity": row.get("declaration_identity"),
                    "function_identity": row.get("function_identity"),
                    "semantic_fingerprint": row.get("semantic_fingerprint"),
                } for row in inventory_tests],
            },
            "selected_roots": [{
                "module": row.get("module"),
                "function": row.get("function"),
                "status": row.get("status"),
                "interface_identity": row.get("interface", {}).get("identity"),
            } for row in rows],
            "capability_dispatch_status": built.get("capability_dispatch", {}).get("status"),
            "capability_dispatch_bindings": len(built.get("capability_dispatch", {}).get("bindings", [])),
        },
        "verification": {
            "stage0_oracle_results": len(oracle.get("executions", [])),
            "canonical_vm_results": len(results),
            "selected_callable_count": len(rows),
            "repeated_callable_invocations": len(SCENARIOS),
            "inventory_test_invocations": len(observed_inventory_tests),
            "source_test_inventory_count": len(inventory_tests),
            "selected_test_identity_count": len(selection.get("selected_test_case_identities", [])),
            "comment_edit_rebound_current_span": True,
            "stale_body_sensitive_identity_rejected": stale_response.get("status") == "rejected",
            "invalid_source_rejected_without_artifact": invalid_response.get("artifact") is None,
            "typed_input_kind": "byte scenario selector",
            "typed_results_match": True,
            "fresh_process_cache_hit": warm.get("cache", {}).get("status"),
            "fresh_process_frontend_invocations": warm.get("cache", {}).get("verification_work", {}).get("front_end_invocations"),
            "language_verified_facts_reused": 0,
            "native_provider_status_cases": [
                "RETURNED success",
                "RETURNED assertion failure",
                "INVALID_REQUEST",
                "BUDGET_EXHAUSTED",
                "UNSUPPORTED",
                "failure-over-unsupported verdict precedence",
            ],
            "results": observed,
        },
        "measurements": {
            "cold_application_build": build_metrics,
            "inventory_selection_build": inventory_build_metrics,
            "fresh_process_exact_hit": warm_metrics,
            "comment_edit_selection_rebind": comment_metrics,
            "stale_case_rejection": stale_metrics,
            "invalid_source_rejection": invalid_metrics,
            "stage0_oracle": oracle_metrics,
            "canonical_vm_batch_wall_seconds": round(vm_wall, 6),
            "canonical_vm_batch_peak_rss_kib": vm_peak_rss,
            "canonical_vm_batch_invocations": 1,
            "canonical_vm_session_model": "one artifact admission and one retained VM Session for all calls",
        },
        "scope": (
            "One application-build/7 all-source selection returned the current Language-issued root-module "
            "Test inventory. A second application-build/7 request selected all four tests by exact "
            "test_case_identity and emitted one artifact exporting the repeated provider callable plus the "
            "selected Test declarations. One "
            "canonical VM batch invoked fold_status_scenario four times with distinct typed byte inputs "
            "and invoked each inventory-selected Test once. The calls reached production "
            "mncs.test.provider::fold_selected policy and all eight results matched Stage-0; each Test "
            "result also matched its direct scenario call. This does not execute provider_batch_app, "
            "admit provider_digest, invoke Forge, publish Store evidence, or establish production CLI integration."
        ),
    }
    evidence_path = ROOT / os.environ.get(
        "MNCS_TEST_PROVIDER_BATCH_EVIDENCE_PATH",
        "evidence/native-test-provider-fold-batch-20261017.json",
    )
    if evidence_path.exists():
        raise SystemExit(f"refusing to overwrite immutable evidence: {evidence_path}")
    evidence_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
