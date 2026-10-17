"""Verify application-build/6 returns the frontend's current source Test inventory.

The inventory is derived by mncs-language during the same frontend invocation
that verifies callable roots and emits the VM artifact. This exercises exact
cache reuse, source-location refresh on a comment edit, fresh-process artifact
reuse after an unselected root test body edit, body-sensitive test identity,
and invalid-source suppression without reconstructing tests from source text
in this harness.
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

import application_build_provider as provider
import test_application_build_provider as provider_tests
import test_vm_native_application as app

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(os.environ.get(
    "MNCS_TEST_INVENTORY_PROBE_BIN",
    ROOT / ".build/native-app-readiness/mncs-compiler-stage0-probe",
))
MODULE = "tests.native_inventory_build"
SOURCE_ID = "tests/native_inventory_build.mncs"
SOURCE = """mncs 0.18;
module tests.native_inventory_build;

fn selected_entry() -> (result: u64) { return 7; }
test second_case() -> (result: u64) { return 2; }
test first_case() -> (result: u64) { return 1; }
"""


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def request(source: str, callable_roots: list[dict] | None = None) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/3",
        "target": "mncs-vm-0.1",
        "root_source_id": SOURCE_ID,
        "root_source": source,
        "callable_roots": ([{"module": MODULE, "function": "selected_entry"}]
                            if callable_roots is None else callable_roots),
        "modules": {},
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "application-build-source-test-inventory/1",
    }


def require_inventory(response: dict, expected_names: set[str] | None = None) -> dict:
    if (response.get("schema_version") != "mncs.compiler.application-build/6"
            or response.get("status") != "verified"):
        raise AssertionError("application-build/6 did not verify: " + json.dumps({
            "schema_version": response.get("schema_version"),
            "status": response.get("status"),
            "diagnostics": response.get("diagnostics"),
        }, indent=2))
    inventory = response.get("source_test_inventory")
    if not isinstance(inventory, dict):
        raise TypeError("verified build omitted its compiler-issued source Test inventory")
    if (inventory.get("schema_version") != "mncs.test-inventory/1"
            or inventory.get("scope") != "source_module"
            or inventory.get("module") != MODULE):
        raise AssertionError("application-build inventory has the wrong Language contract")
    tests = inventory.get("tests")
    expected_names = {"first_case", "second_case"} if expected_names is None else expected_names
    if not isinstance(tests, list) or {row.get("name") for row in tests} != expected_names:
        raise AssertionError("source Test inventory did not retain exactly the root test declarations")
    if any(not row.get("test_case_identity") or not row.get("function_identity")
           or not row.get("declaration_identity") or not row.get("semantic_fingerprint")
           for row in tests):
        raise AssertionError("source Test inventory lost compiler-issued declaration identities")
    if not inventory.get("source_artifact_identity") or not inventory.get("subject_fingerprint"):
        raise AssertionError("source Test inventory omitted its source or subject identity")
    return inventory


def frontend_invocations(response: dict) -> int | None:
    return (response.get("cache", {}).get("verification_work", {})
            .get("front_end_invocations"))


def execute_inventory_batch(artifact: dict, inventory: dict) -> tuple[dict, float, int | None]:
    tests = inventory.get("tests", [])
    calls = []
    for row in tests:
        if row.get("module") != MODULE or row.get("generic_params") or row.get("inputs"):
            raise AssertionError(f"inventory test is outside the supported zero-argument slice: {row}")
        calls.append({
            "id": row["test_case_identity"],
            "request": {
                "schema_version": "0.1",
                "target": {"module": row["module"], "function": row["name"]},
                "arguments": [],
                "type_arguments": [],
                "step_budget": 100_000,
            },
        })
    with tempfile.TemporaryDirectory(prefix="mncs-test-inventory-batch-") as raw:
        directory = Path(raw)
        artifact_path = directory / "application.vm.json"
        calls_path = directory / "calls.json"
        output_path = directory / "results.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
        calls_path.write_text(json.dumps(calls, sort_keys=True) + "\n", encoding="utf-8")
        started = time.monotonic()
        completed = subprocess.run(
            ["/usr/bin/time", "-v", str(app.VM_BIN), "batch",
             "--artifact", str(artifact_path), "--calls", str(calls_path),
             "--output", str(output_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        wall = time.monotonic() - started
        if completed.returncode != 0:
            raise AssertionError(f"canonical VM inventory batch failed: {completed.stderr[-2500:]}")
        match = re.search(r"Maximum resident set size \(kbytes\): (\d+)", completed.stderr)
        peak_rss = int(match.group(1)) if match else None
        return json.loads(output_path.read_text(encoding="utf-8")), wall, peak_rss


def main() -> None:
    global PROBE
    probe_build = None
    if not os.environ.get("MNCS_TEST_INVENTORY_PROBE_BIN"):
        probe_build = provider_tests.ensure_current_probe()
        PROBE = app.PROBE
    if not PROBE.is_file() or not app.VM_BIN.is_file():
        raise SystemExit(f"Stage-0 probe or canonical VM is missing: {PROBE}, {app.VM_BIN}")
    expected_pairs = {
        "mncs.compiler.application-build-request/1": "mncs.compiler.application-build/4",
        "mncs.compiler.application-build-request/2": "mncs.compiler.application-build/5",
        "mncs.compiler.application-build-request/3": "mncs.compiler.application-build/6",
    }
    if provider.RESPONSE_SCHEMA_BY_REQUEST != expected_pairs:
        raise AssertionError("Environment provider does not accept all versioned build contracts")
    manifest = json.loads((ROOT / ".mncs/project.json").read_text(encoding="utf-8"))
    contract_rows = [
        row for row in manifest.get("contracts", {}).get("provides", [])
        if isinstance(row, dict) and row.get("contract") == "native-application-build"
    ]
    if (len(contract_rows) != 1 or contract_rows[0].get("version") != "6"
            or contract_rows[0].get("artifact_contract", {}).get("request_schema")
            != "mncs.compiler.application-build-request/3"):
        raise AssertionError("Compiler manifest does not advertise application-build/6")
    provider_tests.validate_environment_contract_descriptor()

    with tempfile.TemporaryDirectory(
        prefix=".application-test-inventory-", dir=ROOT
    ) as raw:
        directory = Path(raw)
        program_cache = directory / "program-cache"
        application_cache = directory / "application-cache"

        probe = app.Probe(program_cache, executable=PROBE,
                          application_cache_dir=application_cache)
        try:
            cold, cold_metrics = probe.send({"compile_application": request(SOURCE)}, timeout=180.0)
        finally:
            probe.close()
        cold_inventory = require_inventory(cold)
        if (cold.get("cache", {}).get("status") != "miss"
                or frontend_invocations(cold) != 1):
            raise AssertionError("cold inventory build did not report one frontend invocation")

        # The caller selects its production entry and a bounded set of Test
        # declarations in one application-build/6 request. The returned
        # Language inventory supplies the stable call identifiers for a single
        # canonical VM batch over that exact artifact.
        inventory_roots = [
            {"module": MODULE, "function": "selected_entry"},
            {"module": MODULE, "function": "first_case"},
            {"module": MODULE, "function": "second_case"},
        ]
        runner_dir = directory / "inventory-runner"
        runner_dir.mkdir()
        runner_probe = app.Probe(
            runner_dir / "program-cache",
            executable=PROBE,
            application_cache_dir=runner_dir / "application-cache",
        )
        try:
            runner_build, runner_build_metrics = runner_probe.send(
                {"compile_application": request(SOURCE, inventory_roots)}, timeout=180.0
            )
        finally:
            runner_probe.close()
        runner_inventory = require_inventory(runner_build)
        runner_artifact = runner_build.get("artifact", {}).get("value")
        if (runner_build.get("cache", {}).get("status") != "miss"
                or frontend_invocations(runner_build) != 1
                or not isinstance(runner_artifact, dict)):
            raise AssertionError("inventory runner did not build one verified artifact with one frontend")
        expected_test_names = {"first_case", "second_case"}
        runner_tests = runner_inventory.get("tests", [])
        if {row.get("name") for row in runner_tests} != expected_test_names:
            raise AssertionError("runner inventory changed the selected source Test set")
        artifact_callables = {
            (row.get("module"), row.get("name"))
            for row in runner_artifact.get("callables", [])
        }
        if not set((MODULE, name) for name in expected_test_names) <= artifact_callables:
            raise AssertionError("selected inventory tests are missing from the verified artifact")

        oracle_calls = [{
            "schema_version": "0.1",
            "target": {"module": row["module"], "function": row["name"]},
            "arguments": [],
            "type_arguments": [],
            "step_budget": 100_000,
        } for row in runner_tests]
        oracle_probe = app.Probe(runner_dir / "oracle-cache", executable=PROBE)
        try:
            inventory_oracle, inventory_oracle_metrics = oracle_probe.send({"project_oracle": {
                "root": SOURCE,
                "modules": {},
                "calls": oracle_calls,
            }}, timeout=180.0)
        finally:
            oracle_probe.close()
        if (not inventory_oracle.get("valid")
                or len(inventory_oracle.get("executions", [])) != len(runner_tests)):
            raise AssertionError("Stage-0 Test inventory oracle did not execute the selected cases")
        inventory_vm, inventory_vm_wall, inventory_vm_peak_rss = execute_inventory_batch(
            runner_artifact, runner_inventory
        )
        inventory_vm_rows = inventory_vm.get("results", [])
        if (len(inventory_vm_rows) != len(runner_tests)
                or inventory_vm.get("artifact_id") != runner_artifact.get("artifact_id")):
            raise AssertionError("canonical VM batch did not retain the selected artifact and Test count")
        inventory_results: dict[str, int] = {}
        for test_row, vm_row, oracle_row in zip(
            runner_tests, inventory_vm_rows, inventory_oracle["executions"]
        ):
            if (vm_row.get("id") != test_row["test_case_identity"]
                    or vm_row.get("outcome") != {"kind": "completed"}
                    or vm_row.get("record", {}).get("artifact_id") != runner_artifact.get("artifact_id")):
                raise AssertionError(f"inventory-selected Test did not complete: {vm_row}")
            actual_rows = vm_row.get("record", {}).get("returned", [])
            expected_rows = oracle_row.get("returned", [])
            if len(actual_rows) != 1 or len(expected_rows) != 1:
                raise AssertionError(f"inventory-selected Test returned the wrong arity: {test_row['name']}")
            actual = app.decode_wire(app.vm_to_wire(actual_rows[0]))
            expected = app.decode_wire(expected_rows[0])
            if actual != expected:
                raise AssertionError(f"canonical VM differs from Stage-0 for {test_row['name']}")
            inventory_results[test_row["name"]] = actual
        if inventory_results != {"first_case": 1, "second_case": 2}:
            raise AssertionError(f"selected inventory Test results were unexpected: {inventory_results}")

        warm_probe = app.Probe(program_cache, executable=PROBE,
                               application_cache_dir=application_cache)
        try:
            warm, warm_metrics = warm_probe.send(
                {"compile_application": request(SOURCE)}, timeout=90.0
            )
        finally:
            warm_probe.close()
        warm_inventory = require_inventory(warm)
        if (warm.get("cache", {}).get("status") != "hit"
                or frontend_invocations(warm) != 0
                or warm.get("artifact", {}).get("identity") != cold.get("artifact", {}).get("identity")
                or warm_inventory != cold_inventory):
            raise AssertionError("fresh-process exact reuse changed the inventory or artifact")

        edit_probe = app.Probe(program_cache, executable=PROBE,
                               application_cache_dir=application_cache)
        try:
            comment_source = SOURCE.replace(
                "test second_case()", "// comment shifts source locations\ntest second_case()", 1
            )
            comment_response, comment_metrics = edit_probe.send(
                {"compile_application": request(comment_source)}, timeout=180.0
            )
            comment_inventory = require_inventory(comment_response)
            before = {row["name"]: row for row in cold_inventory["tests"]}
            after = {row["name"]: row for row in comment_inventory["tests"]}
            if (after["second_case"]["source_span"]["start"]
                    <= before["second_case"]["source_span"]["start"]):
                raise AssertionError("comment edit did not refresh current source locations")
            if any(after[name]["test_case_identity"] != before[name]["test_case_identity"]
                   for name in before):
                raise AssertionError("comment-only edit changed semantic Test identities")
            if frontend_invocations(comment_response) != 1:
                raise AssertionError("comment edit did not obtain a fresh root-source inventory")
            comment_work = comment_response.get("cache", {}).get("verification_work", {})
            if (comment_response.get("cache", {}).get("status") != "hit"
                    or comment_response.get("cache", {}).get("reuse_basis")
                    != "verified_application_executable_closure"
                    or comment_work.get("backend_artifact") != "reused"):
                raise AssertionError("comment edit did not reuse its unchanged executable closure")

            body_source = SOURCE.replace(
                "test second_case() -> (result: u64) { return 2; }",
                "test second_case() -> (result: u64) { return 9; }",
                1,
            )
            body_probe = app.Probe(program_cache, executable=PROBE,
                                   application_cache_dir=application_cache)
            try:
                body_response, body_metrics = body_probe.send(
                    {"compile_application": request(body_source)}, timeout=180.0
                )
            finally:
                body_probe.close()
            body_inventory = require_inventory(body_response)
            body_tests = {row["name"]: row for row in body_inventory["tests"]}
            if (body_tests["second_case"]["test_case_identity"]
                    == after["second_case"]["test_case_identity"]
                    or body_tests["first_case"]["test_case_identity"]
                    != after["first_case"]["test_case_identity"]):
                raise AssertionError("test-body edit did not invalidate only its own test identity")
            comment_closure = comment_response.get("dependency_graph", {}).get(
                "application_executable_closure_identity"
            )
            body_closure = body_response.get("dependency_graph", {}).get(
                "application_executable_closure_identity"
            )
            if not comment_closure or body_closure != comment_closure:
                raise AssertionError("unselected Test body edit changed the selected executable closure")
            if frontend_invocations(body_response) != 1:
                raise AssertionError("unselected root Test body edit did not run the frontend")
            if body_metrics.get("request_sequence") != 1:
                raise AssertionError("root Test body edit was not checked in a fresh process")
            body_work = body_response.get("cache", {}).get("verification_work", {})
            if (body_response.get("cache", {}).get("status") != "hit"
                    or body_response.get("cache", {}).get("reuse_basis")
                    != "verified_application_executable_closure"
                    or body_work.get("backend_artifact") != "reused"
                    or body_work.get("ssa_verification") != "reused_verified_vm_artifact"
                    or body_response.get("artifact", {}).get("identity")
                    != comment_response.get("artifact", {}).get("identity")):
                raise AssertionError("unselected Test body edit did not reuse its verified VM artifact")

            invalid_source = body_source.replace("return 9;", "return missing_value;", 1)
            invalid_response, invalid_metrics = edit_probe.send(
                {"compile_application": request(invalid_source)}, timeout=180.0
            )
            if (invalid_response.get("status") != "rejected"
                    or invalid_response.get("artifact") is not None
                    or invalid_response.get("source_test_inventory") is not None
                    or not invalid_response.get("diagnostics")
                    or frontend_invocations(invalid_response) != 1):
                raise AssertionError("invalid source reused prior artifact or Test inventory success")

            no_tests_source = SOURCE.split("test second_case()", maxsplit=1)[0]
            empty_response, empty_metrics = edit_probe.send(
                {"compile_application": request(no_tests_source)}, timeout=180.0
            )
            empty_inventory = require_inventory(empty_response, expected_names=set())
            if empty_inventory.get("tests") != [] or frontend_invocations(empty_response) != 1:
                raise AssertionError("valid source without tests did not return a fresh empty inventory")
        finally:
            edit_probe.close()

        provider_request_path = directory / "provider-request.json"
        provider_tests.write_json(provider_request_path, request(SOURCE))
        provider_output_dir = directory / "provider-output"
        provider_output_dir.mkdir()
        provider_cache_dir = directory / "provider-cache"
        provider_summary, provider_response, provider_elapsed, provider_metrics = (
            provider_tests.invoke(
                [
                    "--request-file", provider_request_path.relative_to(ROOT).as_posix(),
                    "--cache-dir", provider_cache_dir.relative_to(ROOT).as_posix(),
                ],
                output_dir=provider_output_dir,
                cache_dir=provider_cache_dir,
            )
        )
        provider_inventory = require_inventory(provider_response)
        if (provider_summary.get("response_schema") != provider_response["schema_version"]
                or provider_response.get("schema_version")
                != "mncs.compiler.application-build/6"
                or provider_inventory != cold_inventory
                or not (provider_output_dir / "native-application-build.json").is_file()):
            raise AssertionError("application-build provider did not publish the /6 inventory result")

        report = {
            "schema_version": 1,
            "kind": "mncs.compiler.application-build-source-test-inventory-proof",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler": {
                "revision": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
                ).strip(),
                "branch": subprocess.check_output(
                    ["git", "branch", "--show-current"], cwd=ROOT, text=True
                ).strip(),
                "probe_sha256": sha(PROBE.read_bytes()),
                "probe_build": probe_build,
                "harness_sha256": sha(Path(__file__).read_bytes()),
                "language_revision": json.loads(
                    (ROOT / "mncs-language.lock.json").read_text()
                )["revision"],
            },
            "provider_adapter": {
                "response_schema": provider_response["schema_version"],
                "source_test_count": len(provider_inventory["tests"]),
                "wall_seconds": round(provider_elapsed, 6),
                "process_tree_metrics": provider_metrics,
            },
            "inventory_selected_batch": {
                "build_frontend_invocations": frontend_invocations(runner_build),
                "inventory_test_count": len(runner_tests),
                "inventory_test_case_identities": {
                    row["name"]: row["test_case_identity"] for row in runner_tests
                },
                "verified_artifact_identity": runner_build["artifact"]["identity"],
                "canonical_vm_artifact_identity": inventory_vm["artifact_id"],
                "canonical_vm_batch_count": len(inventory_vm_rows),
                "canonical_vm_results": inventory_results,
                "stage0_equivalent": True,
                "build_process_metrics": runner_build_metrics,
                "stage0_oracle_metrics": inventory_oracle_metrics,
                "canonical_vm_batch_wall_seconds": round(inventory_vm_wall, 6),
                "canonical_vm_batch_peak_rss_kib": inventory_vm_peak_rss,
            },
            "contract": {
                "request_schema": "mncs.compiler.application-build-request/3",
                "response_schema": cold["schema_version"],
                "source_test_inventory_schema": cold_inventory["schema_version"],
                "source_test_inventory_scope": cold_inventory["scope"],
                "source_artifact_identity": cold_inventory["source_artifact_identity"],
                "subject_identity": cold_inventory["subject_identity"],
                "subject_fingerprint": cold_inventory["subject_fingerprint"],
                "test_case_identities": {
                    row["name"]: row["test_case_identity"]
                    for row in cold_inventory["tests"]
                },
                "selected_artifact_identity": cold["artifact"]["identity"],
            },
            "checks": {
                "one_frontend_supplies_build_and_inventory": (
                    frontend_invocations(cold) == 1
                    and len(cold_inventory["tests"]) == 2
                ),
                "fresh_process_exact_hit_preserves_inventory": (
                    warm.get("cache", {}).get("status") == "hit"
                    and frontend_invocations(warm) == 0
                    and warm_inventory == cold_inventory
                ),
                "comment_edit_refreshes_locations_only": True,
                "unselected_test_body_changes_only_its_case_identity": True,
                "unselected_test_edit_preserves_executable_closure": (
                    body_closure == comment_closure
                ),
                "unselected_root_test_edit_artifact_identity": {
                    "before": comment_response.get("artifact", {}).get("identity"),
                    "after": body_response.get("artifact", {}).get("identity"),
                    "reused": body_response.get("artifact", {}).get("identity")
                    == comment_response.get("artifact", {}).get("identity"),
                    "reuse_basis": body_response.get("cache", {}).get("reuse_basis"),
                    "ssa_verification": body_work.get("ssa_verification"),
                    "fresh_process": body_metrics.get("request_sequence") == 1,
                },
                "fresh_process_changed_root_reuses_verified_artifact": (
                    body_metrics.get("request_sequence") == 1
                    and body_work.get("backend_artifact") == "reused"
                    and body_work.get("ssa_verification") == "reused_verified_vm_artifact"
                ),
                "comment_edit_reuses_executable_closure": (
                    comment_response.get("cache", {}).get("reuse_basis")
                    == "verified_application_executable_closure"
                    and comment_work.get("backend_artifact") == "reused"
                ),
                "invalid_source_suppresses_inventory_and_artifact": True,
                "valid_source_without_tests_returns_empty_inventory": True,
                "provider_adapter_publishes_inventory_response": (
                    provider_summary.get("response_schema")
                    == "mncs.compiler.application-build/6"
                    and provider_inventory == cold_inventory
                ),
                "one_build_emits_artifact_inventory_and_selected_test_callables": (
                    frontend_invocations(runner_build) == 1
                    and len(runner_tests) == 2
                    and set((MODULE, name) for name in expected_test_names) <= artifact_callables
                ),
                "inventory_ids_drive_one_batch_on_same_artifact": (
                    len(inventory_vm_rows) == len(runner_tests)
                    and all(vm.get("id") == test["test_case_identity"]
                            for vm, test in zip(inventory_vm_rows, runner_tests))
                    and inventory_vm.get("artifact_id") == runner_artifact.get("artifact_id")
                ),
                "selected_test_batch_matches_stage0": True,
                "language_verified_facts_reused": 0,
            },
            "measurements": {
                "cold_build": cold_metrics,
                "fresh_process_exact_hit": warm_metrics,
                "comment_edit": comment_metrics,
                "unselected_test_body_edit": body_metrics,
                "invalid_source": invalid_metrics,
                "valid_source_without_tests": empty_metrics,
            },
            "verification_work": {
                "cold_build": cold.get("cache", {}).get("verification_work"),
                "fresh_process_exact_hit": warm.get("cache", {}).get("verification_work"),
                "comment_edit": comment_response.get("cache", {}).get("verification_work"),
                "unselected_test_body_edit": body_response.get("cache", {}).get("verification_work"),
                "invalid_source": invalid_response.get("cache", {}).get("verification_work"),
                "valid_source_without_tests": empty_response.get("cache", {}).get("verification_work"),
            },
            "scope": (
                "The Compiler returns the Language-owned source_module test inventory from the same "
                "frontend result used to verify callable roots and emit application-build/6. A fresh "
                "process can locate a prior artifact candidate across root snapshots; changed source "
                "still runs the full frontend before executable-closure reuse. Exact fresh-process "
                "reuse preserves the inventory; edits refresh source spans or the "
                "affected test identity, and invalid input publishes neither inventory nor artifact. "
                "A selected zero-argument source Test subset can now be emitted with the production "
                "entry, selected from the returned inventory, and executed in one canonical VM batch "
                "with Stage-0 agreement. This does not implement Language verification-fact reuse "
                "or change the production mncs-test consumer."
            ),
        }
        evidence = ROOT / "evidence/application-build-test-inventory-20261017.json"
        evidence.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
