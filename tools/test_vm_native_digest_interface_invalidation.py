#!/usr/bin/env python3
"""Exercise imported-interface and generic-body invalidation on the digest app.

This extends the real mncs-test application-build workload. An imported
signature change must rebuild its callers, and an edited generic body must
rebuild its explicitly selected specialization. Stage-0 and canonical VM
results establish that both rebuilt artifacts retain the intended behavior.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_vm_native_digest_batch as digest_batch

ROOT = digest_batch.ROOT
EVIDENCE = ROOT / "evidence" / "native-app-readiness-digest-interface-invalidation-20261016.json"
ASSERTIONS = "mncs.test.assertions"
SUITE = "mncs.test.suite"
GENERIC_FUNCTION = "equals_bytes"
GENERIC_BOUND = 4


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def compile_application(request: dict) -> tuple[dict, dict, str]:
    probe = digest_batch.digest_app.support.Probe(
        digest_batch.CACHE_DIR, executable=digest_batch.PROBE
    )
    try:
        result, metrics = probe.send({"compile_application": request}, timeout=180)
        stderr = probe.close()
    except BaseException:
        if probe.proc.poll() is None:
            probe.proc.kill()
            probe.proc.wait(timeout=5)
        raise
    if result.get("schema_version") != "mncs.compiler.application-build/5":
        raise AssertionError(f"unexpected application-build contract: {result.get('schema_version')}")
    return result, metrics, stderr


def stage0_project_oracle(root_source: str, modules: dict[str, str], calls: list[dict]) -> tuple[dict, dict]:
    probe = digest_batch.digest_app.support.Probe(
        digest_batch.CACHE_DIR, executable=digest_batch.PROBE
    )
    try:
        result, metrics = probe.send({"project_oracle": {
            "root": root_source,
            "modules": modules,
            "calls": calls,
        }}, timeout=180)
        probe.close()
    except BaseException:
        if probe.proc.poll() is None:
            probe.proc.kill()
            probe.proc.wait(timeout=5)
        raise
    if not result.get("valid"):
        raise AssertionError(f"Stage-0 rejected workload: {result.get('diagnostics')}")
    executions = result.get("executions", [])
    if len(executions) != len(calls) or any(row.get("status") != "returned" for row in executions):
        raise AssertionError(f"Stage-0 did not return every workload call: {executions}")
    return result, metrics


def byte_sequence(raw: bytes) -> dict:
    return {"sequence": {"values": [{"byte": {"value": value}} for value in raw]}}


def generic_calls() -> list[dict]:
    expected = bytes(range(GENERIC_BOUND))
    different = bytearray(expected)
    different[-1] ^= 1
    return [
        {
            "schema_version": "0.1",
            "target": {"module": ASSERTIONS, "function": GENERIC_FUNCTION},
            "arguments": [byte_sequence(expected), byte_sequence(expected),
                          digest_batch.digest_app.unsigned(17)],
            "type_arguments": [{"kind": "nat", "value": GENERIC_BOUND}],
            "step_budget": 100_000,
        },
        {
            "schema_version": "0.1",
            "target": {"module": ASSERTIONS, "function": GENERIC_FUNCTION},
            "arguments": [byte_sequence(expected), byte_sequence(bytes(different)),
                          digest_batch.digest_app.unsigned(23)],
            "type_arguments": [{"kind": "nat", "value": GENERIC_BOUND}],
            "step_budget": 100_000,
        },
    ]


def vm_generic_batch(artifact: dict, callable_name: str, calls: list[dict]) -> tuple[list[dict], dict]:
    vm = digest_batch.digest_app.support
    vm_calls = [{
        "id": f"generic-equals-{index}",
        "request": {
            **call,
            "target": {"module": ASSERTIONS, "function": callable_name},
            "type_arguments": [],
        },
    } for index, call in enumerate(calls)]
    with tempfile.TemporaryDirectory(prefix="mncs-digest-generic-") as raw_directory:
        directory = Path(raw_directory)
        artifact_path = directory / "application.vm.json"
        calls_path = directory / "calls.json"
        output_path = directory / "results.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        calls_path.write_text(json.dumps(vm_calls, sort_keys=True) + "\n")
        started = time.monotonic()
        process = subprocess.run(
            ["/usr/bin/time", "-v", str(digest_batch.VM_BIN), "batch",
             "--artifact", str(artifact_path), "--calls", str(calls_path),
             "--output", str(output_path)],
            capture_output=True, text=True, timeout=90, check=False,
        )
        wall = time.monotonic() - started
        if process.returncode:
            raise AssertionError(f"canonical VM generic batch failed: {process.stderr[-3000:]}")
        document = json.loads(output_path.read_text())
    rows = document.get("results", [])
    if (document.get("artifact_id") != artifact.get("artifact_id")
            or len(rows) != len(vm_calls)):
        raise AssertionError(f"canonical VM returned an unexpected generic batch: {document}")
    values = []
    for row, call in zip(rows, vm_calls):
        if row.get("id") != call["id"] or row.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"generic VM call did not complete: {row}")
        record = row.get("record", {})
        if (record.get("artifact_id") != artifact.get("artifact_id")
                or len(record.get("returned", [])) != 1):
            raise AssertionError(f"generic call receipt does not bind the selected artifact: {row}")
        values.append(vm.decode_wire(vm.vm_to_wire(record["returned"][0])))
    peak_rss = re.search(r"Maximum resident set size \(kbytes\): (\d+)", process.stderr)
    steps = [row["record"].get("usage", {}).get("steps") for row in rows]
    return values, {
        "wall_seconds": round(wall, 6),
        "cpu": digest_batch.cpu_times(process.stderr),
        "peak_rss_kib": int(peak_rss.group(1)) if peak_rss else None,
        "steps": steps,
        "artifact_id": document.get("artifact_id"),
    }


def require_verified_build(build: dict, label: str, expected_cache_status: str) -> dict:
    if (build.get("status") != "verified"
            or build.get("cache", {}).get("status") != expected_cache_status
            or build.get("unresolved_obligations") != []):
        raise AssertionError(f"{label} did not produce the expected verified build: {build}")
    return build.get("artifact", {}).get("value")


def main() -> None:
    if EVIDENCE.exists():
        raise SystemExit(f"refusing to overwrite immutable evidence: {EVIDENCE}")
    if not digest_batch.PROBE.is_file() or not digest_batch.VM_BIN.is_file():
        raise SystemExit(f"missing Stage-0 probe or canonical VM: {digest_batch.PROBE}, {digest_batch.VM_BIN}")

    fixture = json.loads(digest_batch.digest_app.FIXTURE_PATH.read_text(encoding="utf-8"))
    root_source = digest_batch.digest_app.ROOT_SOURCE_PATH.read_text(encoding="utf-8")
    modules = {
        name: path.read_text(encoding="utf-8")
        for name, path in digest_batch.digest_app.MODULE_PATHS.items()
    }
    inputs = fixture["execution"]["inputs"]
    expected = fixture["execution"]["canonical_vm_results"]
    base_request = digest_batch.build_request(root_source, modules)

    baseline, baseline_metrics, baseline_stderr = compile_application(base_request)
    baseline_artifact = require_verified_build(baseline, "baseline digest app", "miss")
    if baseline_artifact is None:
        raise AssertionError("baseline digest application omitted its canonical artifact")
    baseline_vm_document, baseline_vm_metrics, baseline_vm_values = digest_batch.vm_batch(
        baseline_artifact, inputs, expected
    )

    suite_before = modules[SUITE]
    suite_signature = "fn empty() -> (result: SuiteSummary) {"
    suite_revised_signature = "fn empty(seed: u64) -> (result: SuiteSummary) {"
    if (suite_before.count(suite_signature) != 1
            or suite_before.count("        total: 0,") != 1
            or root_source.count("SuiteSummary = empty()") != 1):
        raise AssertionError("suite.empty source anchors changed")
    signature_modules = dict(modules)
    signature_modules[SUITE] = suite_before.replace(
        suite_signature, suite_revised_signature, 1
    ).replace("        total: 0,", "        total: seed,", 1)
    signature_modules[SUITE] = signature_modules[SUITE].replace(
        "SuiteSummary = empty()", "SuiteSummary = empty(0)"
    )
    signature_root = root_source.replace(
        "SuiteSummary = empty()", "SuiteSummary = empty(0)", 1
    )
    signature_request = digest_batch.build_request(signature_root, signature_modules)
    signature_build, signature_metrics, signature_stderr = compile_application(signature_request)
    signature_artifact = require_verified_build(
        signature_build, "imported signature edit", "miss"
    )
    signature_work = signature_build.get("cache", {}).get("verification_work", {})
    if (signature_artifact is None
            or signature_build.get("artifact", {}).get("identity")
            == baseline.get("artifact", {}).get("identity")
            or signature_work.get("front_end_invocations", 0) < 1
            or signature_work.get("backend_artifact") != "generated"):
        raise AssertionError("imported suite.empty signature edit reused stale verification or backend output")
    signature_vm_document, signature_vm_metrics, signature_vm_values = digest_batch.vm_batch(
        signature_artifact, inputs, expected
    )
    if signature_vm_values != baseline_vm_values:
        raise AssertionError("signature migration changed the real digest application results")

    signature_warm, signature_warm_metrics, signature_warm_stderr = compile_application(
        signature_request
    )
    require_verified_build(signature_warm, "fresh-process signature cache reuse", "hit")
    if (signature_warm.get("artifact", {}).get("identity")
            != signature_build.get("artifact", {}).get("identity")
            or signature_warm.get("cache", {}).get("verification_work", {}).get(
                "front_end_invocations") != 0):
        raise AssertionError("fresh process did not reuse the verified signature-migrated artifact")

    generic_request = dict(base_request)
    generic_request["generic_seeds"] = [{
        "module": ASSERTIONS,
        "function": GENERIC_FUNCTION,
        "type_arguments": [{"kind": "nat", "value": GENERIC_BOUND}],
    }]
    generic_build, generic_metrics, generic_stderr = compile_application(generic_request)
    generic_artifact = require_verified_build(generic_build, "selected generic build", "miss")
    if generic_artifact is None:
        raise AssertionError("selected generic build omitted its canonical artifact")
    generic_rows = [row for row in generic_artifact.get("callables", [])
                    if row.get("module") == ASSERTIONS
                    and row.get("name", "").startswith(f"{GENERIC_FUNCTION}__spec_")]
    if len(generic_rows) != 1:
        raise AssertionError(f"expected one selected equals_bytes specialization, got {generic_rows}")
    generic_callable = generic_rows[0]["name"]
    calls = generic_calls()
    stage0_modules = {
        name: path.read_text(encoding="utf-8")
        for name, path in digest_batch.digest_app.support.MODULE_FILES.items()
    }
    stage0_modules[ASSERTIONS] = modules[ASSERTIONS]
    stage0_root = digest_batch.digest_app.support.APP_SOURCE
    generic_oracle, oracle_metrics = stage0_project_oracle(stage0_root, stage0_modules, calls)
    oracle_values = [
        digest_batch.digest_app.support.decode_wire(row["returned"][0])
        for row in generic_oracle["executions"]
    ]
    generic_vm_values, generic_vm_metrics = vm_generic_batch(
        generic_artifact, generic_callable, calls
    )
    if generic_vm_values != oracle_values:
        raise AssertionError(f"selected generic VM specialization differs from Stage-0: {generic_vm_values} != {oracle_values}")

    generic_warm, generic_warm_metrics, generic_warm_stderr = compile_application(
        generic_request
    )
    require_verified_build(generic_warm, "fresh-process generic cache reuse", "hit")
    if (generic_warm.get("artifact", {}).get("identity")
            != generic_build.get("artifact", {}).get("identity")
            or generic_warm.get("cache", {}).get("verification_work", {}).get(
                "front_end_invocations") != 0):
        raise AssertionError("fresh process did not reuse the selected generic artifact")

    generic_modules = dict(modules)
    generic_anchor = "next same = same && expected[i] == actual[i];"
    if modules[ASSERTIONS].count(generic_anchor) != 1:
        raise AssertionError("equals_bytes generic body anchor changed")
    generic_modules[ASSERTIONS] = modules[ASSERTIONS].replace(
        generic_anchor,
        "next same = (expected[i] == actual[i]) && same;",
        1,
    )
    edited_request = digest_batch.build_request(root_source, generic_modules)
    edited_request["generic_seeds"] = generic_request["generic_seeds"]
    edited_build, edited_metrics, edited_stderr = compile_application(edited_request)
    edited_artifact = require_verified_build(edited_build, "generic body edit", "miss")
    edited_work = edited_build.get("cache", {}).get("verification_work", {})
    if (edited_artifact is None
            or edited_build.get("artifact", {}).get("identity")
            == generic_build.get("artifact", {}).get("identity")
            or edited_work.get("front_end_invocations", 0) < 1
            or edited_work.get("backend_artifact") != "generated"):
        raise AssertionError("generic body edit reused stale specialization verification or backend output")
    edited_rows = [row for row in edited_artifact.get("callables", [])
                   if row.get("module") == ASSERTIONS
                   and row.get("name", "").startswith(f"{GENERIC_FUNCTION}__spec_")]
    if len(edited_rows) != 1:
        raise AssertionError(f"edited build omitted equals_bytes specialization: {edited_rows}")
    edited_callable = edited_rows[0]["name"]
    edited_stage0_modules = dict(stage0_modules)
    edited_stage0_modules[ASSERTIONS] = generic_modules[ASSERTIONS]
    edited_oracle, edited_oracle_metrics = stage0_project_oracle(
        stage0_root, edited_stage0_modules, calls
    )
    edited_oracle_values = [
        digest_batch.digest_app.support.decode_wire(row["returned"][0])
        for row in edited_oracle["executions"]
    ]
    edited_vm_values, edited_vm_metrics = vm_generic_batch(
        edited_artifact, edited_callable, calls
    )
    if edited_vm_values != edited_oracle_values or edited_vm_values != generic_vm_values:
        raise AssertionError("edited generic specialization differs from Stage-0 or prior semantics")

    edited_warm, edited_warm_metrics, edited_warm_stderr = compile_application(edited_request)
    require_verified_build(edited_warm, "fresh-process edited generic reuse", "hit")
    if (edited_warm.get("artifact", {}).get("identity")
            != edited_build.get("artifact", {}).get("identity")
            or edited_warm.get("cache", {}).get("verification_work", {}).get(
                "front_end_invocations") != 0):
        raise AssertionError("fresh process did not reuse the edited generic artifact")

    source_hashes = {
        "root": sha(root_source.encode()),
        **{name: sha(source.encode()) for name, source in sorted(modules.items())},
    }
    evidence = {
        "schema_version": "mncs.compiler.application-invalidation-evidence/1",
        "kind": "mncs-test-digest-imported-interface-and-generic-invalidation",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "compiler": {
            "revision": git(ROOT, "rev-parse", "HEAD"),
            "branch": git(ROOT, "branch", "--show-current"),
            "test_harness": "tools/test_vm_native_digest_interface_invalidation.py",
            "test_harness_sha256": sha(Path(__file__).read_bytes()),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "probe_sha256": sha(digest_batch.PROBE.read_bytes()),
            "vm_revision": git(digest_batch.digest_app.VM_ROOT, "rev-parse", "HEAD"),
            "vm_sha256": sha(digest_batch.VM_BIN.read_bytes()),
            "fixture": "evidence/native-app-readiness-test-digest.json",
            "fixture_sha256": sha(digest_batch.digest_app.FIXTURE_PATH.read_bytes()),
            "source_hashes": source_hashes,
            "test_repository_revision": git(digest_batch.digest_app.TEST_ROOT, "rev-parse", "HEAD"),
            "stdlib_repository_revision": git(digest_batch.digest_app.STDLIB_ROOT, "rev-parse", "HEAD"),
        },
        "imported_signature_edit": {
            "module": SUITE,
            "function": "empty",
            "old_signature": "empty() -> SuiteSummary",
            "new_signature": "empty(seed: u64) -> SuiteSummary",
            "changed_call_sites_pass_zero": True,
            "build_cache_status": signature_build.get("cache", {}).get("status"),
            "front_end_invocations": signature_work.get("front_end_invocations"),
            "backend_artifact": signature_work.get("backend_artifact"),
            "baseline_artifact_identity": baseline.get("artifact", {}).get("identity"),
            "rebuilt_artifact_identity": signature_build.get("artifact", {}).get("identity"),
            "fresh_process_cache_status": signature_warm.get("cache", {}).get("status"),
            "fresh_process_front_end_invocations": signature_warm.get("cache", {}).get(
                "verification_work", {}).get("front_end_invocations"),
            "fixture_results_unchanged": signature_vm_values == baseline_vm_values,
            "baseline_vm_artifact_id": baseline_vm_document.get("artifact_id"),
            "signature_vm_artifact_id": signature_vm_document.get("artifact_id"),
            "baseline_vm_result_count": len(baseline_vm_values),
            "signature_vm_result_count": len(signature_vm_values),
            "compile_measurements": signature_metrics,
            "vm_measurements": signature_vm_metrics,
        },
        "generic_specialization_body_edit": {
            "module": ASSERTIONS,
            "function": GENERIC_FUNCTION,
            "type_arguments": [str(GENERIC_BOUND)],
            "specialized_callable_before": generic_callable,
            "specialized_callable_after": edited_callable,
            "body_before_sha256": sha(modules[ASSERTIONS].encode()),
            "body_after_sha256": sha(generic_modules[ASSERTIONS].encode()),
            "build_cache_status": edited_build.get("cache", {}).get("status"),
            "front_end_invocations": edited_work.get("front_end_invocations"),
            "backend_artifact": edited_work.get("backend_artifact"),
            "baseline_artifact_identity": generic_build.get("artifact", {}).get("identity"),
            "rebuilt_artifact_identity": edited_build.get("artifact", {}).get("identity"),
            "fresh_process_cache_status": edited_warm.get("cache", {}).get("status"),
            "fresh_process_front_end_invocations": edited_warm.get("cache", {}).get(
                "verification_work", {}).get("front_end_invocations"),
            "stage0_matches_canonical_vm": edited_vm_values == edited_oracle_values,
            "semantic_result_unchanged": generic_vm_values == edited_vm_values,
            "stage0_baseline_results": oracle_values,
            "canonical_vm_baseline_results": generic_vm_values,
            "stage0_edited_results": edited_oracle_values,
            "canonical_vm_edited_results": edited_vm_values,
            "stage0_measurements": oracle_metrics,
            "stage0_edited_measurements": edited_oracle_metrics,
            "vm_baseline_measurements": generic_vm_metrics,
            "vm_edited_measurements": edited_vm_metrics,
            "compile_measurements": edited_metrics,
        },
        "build_measurements": {
            "baseline": {"cache": baseline.get("cache"), "metrics": baseline_metrics},
            "signature_edit": {"cache": signature_build.get("cache"), "metrics": signature_metrics},
            "signature_fresh_process": {"cache": signature_warm.get("cache"), "metrics": signature_warm_metrics},
            "generic_seed": {"cache": generic_build.get("cache"), "metrics": generic_metrics},
            "generic_seed_fresh_process": {"cache": generic_warm.get("cache"), "metrics": generic_warm_metrics},
            "generic_body_edit": {"cache": edited_build.get("cache"), "metrics": edited_metrics},
            "generic_body_edit_fresh_process": {"cache": edited_warm.get("cache"), "metrics": edited_warm_metrics},
        },
        "language_verified_facts_reused": 0,
        "scope": (
            "The Compiler ran a complete frontend verification for each changed source snapshot. "
            "These cases establish safe application artifact invalidation and fresh-process reuse; "
            "they do not establish Language-owned verified-fact reuse."
        ),
        "probe_stderr_bytes": {
            "baseline": len(baseline_stderr.encode()),
            "signature_edit": len(signature_stderr.encode()),
            "signature_fresh_process": len(signature_warm_stderr.encode()),
            "generic_seed": len(generic_stderr.encode()),
            "generic_seed_fresh_process": len(generic_warm_stderr.encode()),
            "generic_body_edit": len(edited_stderr.encode()),
            "generic_body_edit_fresh_process": len(edited_warm_stderr.encode()),
        },
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({
        "status": "passed",
        "kind": evidence["kind"],
        "signature_artifact_invalidated": (
            baseline.get("artifact", {}).get("identity")
            != signature_build.get("artifact", {}).get("identity")
        ),
        "generic_body_artifact_invalidated": (
            generic_build.get("artifact", {}).get("identity")
            != edited_build.get("artifact", {}).get("identity")
        ),
        "signature_fixture_parity": signature_vm_values == baseline_vm_values,
        "generic_oracle_vm_parity": edited_vm_values == edited_oracle_values,
        "generic_semantic_stability": edited_vm_values == generic_vm_values,
        "evidence": str(EVIDENCE.relative_to(ROOT)),
        "evidence_sha256": sha(EVIDENCE.read_bytes()),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
