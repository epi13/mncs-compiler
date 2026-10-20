#!/usr/bin/env python3
"""Check digest artifact reuse after an uncalled imported body edit.

The edited source snapshot must pass the complete Compiler frontend, while its
unreachable equals_i64 body stays outside the selected digest executable
closure. The Stage-0 oracle and canonical VM provide independent result checks.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import test_vm_native_digest_batch as digest_batch
import test_vm_native_digest_interface_invalidation as invalidation

ROOT = digest_batch.ROOT
EVIDENCE = ROOT / "evidence" / (
    f"native-app-readiness-digest-unreachable-body-"
    f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}.json"
)
ASSERTIONS = "mncs.test.assertions"
FUNCTION = "equals_i64"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def require_reuse(build: dict, label: str, *, frontend: int, basis: str) -> dict:
    if build.get("status") != "verified":
        raise AssertionError(f"{label} did not verify: {build}")
    if build.get("cache", {}).get("status") != "hit":
        raise AssertionError(f"{label} did not reuse the application artifact: {build}")
    if build.get("cache", {}).get("reuse_basis") != basis:
        raise AssertionError(f"{label} used an unexpected reuse basis: {build.get('cache')}")
    work = build.get("cache", {}).get("verification_work", {})
    if work.get("front_end_invocations") != frontend:
        raise AssertionError(f"{label} frontend count was {work.get('front_end_invocations')}, expected {frontend}")
    if frontend and work.get("front_end_outcome") != "verified":
        raise AssertionError(f"{label} did not complete fresh frontend verification: {work}")
    if work.get("backend_artifact") != "reused":
        raise AssertionError(f"{label} did not reuse its backend artifact: {work}")
    artifact = build.get("artifact", {}).get("value")
    if not artifact:
        raise AssertionError(f"{label} omitted the executable artifact")
    return artifact


def oracle_calls(inputs: list[dict]) -> list[dict]:
    calls = [{
        "schema_version": "0.1",
        "target": {"module": digest_batch.MODULE, "function": "build_digest"},
        "arguments": [argument],
        "type_arguments": [],
        "step_budget": 3_000_000,
    } for argument in inputs]
    calls.extend([
        {
            "schema_version": "0.1",
            "target": {"module": digest_batch.MODULE, "function": "request_schema"},
            "arguments": [], "type_arguments": [], "step_budget": 10_000,
        },
        {
            "schema_version": "0.1",
            "target": {"module": digest_batch.MODULE, "function": "result_schema"},
            "arguments": [], "type_arguments": [], "step_budget": 10_000,
        },
    ])
    return calls


def digest_only_request(root_source: str, modules: dict[str, str]) -> dict:
    request = digest_batch.build_request(root_source, modules)
    request["callable_roots"] = [
        row for row in digest_batch.ROOTS
        if row["function"] not in {
            digest_batch.SUITE_FIXTURE_FUNCTION,
            digest_batch.SUITE_EDGE_FIXTURE_FUNCTION,
        }
    ]
    return request


def main() -> None:
    if EVIDENCE.exists():
        raise SystemExit(f"refusing to overwrite immutable evidence: {EVIDENCE}")
    if not digest_batch.PROBE.is_file() or not digest_batch.VM_BIN.is_file():
        raise SystemExit(f"missing Stage-0 probe or canonical VM: {digest_batch.PROBE}, {digest_batch.VM_BIN}")

    fixture = json.loads(digest_batch.digest_app.FIXTURE_PATH.read_text(encoding="utf-8"))
    root_source = (
        digest_batch.digest_app.ROOT_SOURCE_PATH.read_text(encoding="utf-8")
        + digest_batch.SUITE_FIXTURE_SOURCE
    )
    modules = {
        name: path.read_text(encoding="utf-8")
        for name, path in digest_batch.digest_app.MODULE_PATHS.items()
    }
    baseline, baseline_metrics, baseline_stderr = invalidation.compile_application(
        digest_only_request(root_source, modules)
    )
    if baseline.get("status") != "verified" or baseline.get("cache", {}).get("status") != "miss":
        raise AssertionError(f"cold baseline did not produce a verified miss: {baseline}")
    baseline_artifact = baseline.get("artifact", {}).get("value")
    if not baseline_artifact:
        raise AssertionError("baseline digest build omitted its canonical artifact")
    baseline_document, baseline_vm_metrics, baseline_values = digest_batch.vm_batch(
        baseline_artifact,
        fixture["execution"]["inputs"],
        fixture["execution"]["canonical_vm_results"],
        {},
    )

    old_body = "return Assertion { passed: expected == actual, expected: expected, actual: actual, code: code };"
    new_body = "return Assertion { passed: actual == expected, expected: expected, actual: actual, code: code };"
    original_assertions = modules[ASSERTIONS]
    if original_assertions.count(old_body) != 1:
        raise AssertionError("equals_i64 source anchor is absent or ambiguous")
    edited_modules = dict(modules)
    edited_modules[ASSERTIONS] = original_assertions.replace(old_body, new_body, 1)
    edited_request = digest_only_request(root_source, edited_modules)
    edited, edited_metrics, edited_stderr = invalidation.compile_application(edited_request)
    edited_artifact = require_reuse(
        edited, "unreachable imported body edit", frontend=1,
        basis="verified_application_executable_closure",
    )
    edited_cache = edited.get("cache", {})
    edited_work = edited_cache.get("verification_work", {})
    if (edited.get("artifact", {}).get("identity")
            != baseline.get("artifact", {}).get("identity")
            or edited.get("source_snapshot", {}).get("identity")
            == baseline.get("source_snapshot", {}).get("identity")):
        raise AssertionError("source snapshot or executable artifact identities did not show expected separation")
    if edited.get("source_snapshot", {}).get("identity") is None:
        raise AssertionError("edited build omitted its source snapshot identity")
    callable_names = [row.get("name") for row in edited_artifact.get("callables", [])]
    if any(name == FUNCTION or name.startswith(FUNCTION + "__spec_") for name in callable_names):
        raise AssertionError("unreachable equals_i64 was incorrectly admitted to the digest executable closure")
    if edited_work.get("function_body_verification") != "recomputed_with_language_fact_reuse":
        raise AssertionError(f"changed source did not receive complete body verification: {edited_work}")
    if not edited_work.get("reused_semantic_facts"):
        raise AssertionError("independent Language function-body facts were not reused")
    if not any("mncs.test.assertions::equals_i64" in subject
               for subject in edited_work.get("recomputed_semantic_facts", [])):
        raise AssertionError("the changed unreachable function body was not reverified")

    oracle_modules = dict(modules)
    oracle_modules[ASSERTIONS] = edited_modules[ASSERTIONS]
    oracle, oracle_metrics = invalidation.stage0_project_oracle(
        root_source,
        oracle_modules,
        oracle_calls(fixture["execution"]["inputs"]),
    )
    oracle_values = [
        digest_batch.digest_app.support.decode_wire(row["returned"][0])
        for row in oracle.get("executions", [])
    ]
    if oracle_values != baseline_values:
        raise AssertionError("Stage-0 outputs changed after the unreachable semantics-preserving body edit")
    edited_document, edited_vm_metrics, edited_values = digest_batch.vm_batch(
        edited_artifact,
        fixture["execution"]["inputs"],
        fixture["execution"]["canonical_vm_results"],
        {},
    )
    if (edited_values != baseline_values
            or edited_document.get("artifact_id") != baseline_document.get("artifact_id")):
        raise AssertionError("canonical VM output changed after reusing the executable closure")

    warm, warm_metrics, warm_stderr = invalidation.compile_application(edited_request)
    warm_artifact = require_reuse(
        warm, "fresh-process edited snapshot reuse", frontend=0,
        basis="exact_source_snapshot",
    )
    if (warm.get("artifact", {}).get("identity") != edited.get("artifact", {}).get("identity")
            or warm_artifact.get("artifact_id") != edited_artifact.get("artifact_id")
            or warm.get("cache", {}).get("reuse_basis") != "exact_source_snapshot"):
        raise AssertionError("fresh-process source snapshot reuse changed the artifact")

    evidence = {
        "schema_version": "mncs.compiler.application-incremental-evidence/1",
        "kind": "mncs-test-digest-unreachable-imported-body-closure-reuse",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "compiler": {
            "revision": invalidation.git(ROOT, "rev-parse", "HEAD"),
            "branch": invalidation.git(ROOT, "branch", "--show-current"),
            "test_harness": "tools/test_vm_native_digest_unreachable_body_reuse.py",
            "test_harness_sha256": sha(Path(__file__).read_bytes()),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "probe_sha256": sha(digest_batch.PROBE.read_bytes()),
            "vm_revision": invalidation.git(digest_batch.digest_app.VM_ROOT, "rev-parse", "HEAD"),
            "vm_sha256": sha(digest_batch.VM_BIN.read_bytes()),
            "test_repository_revision": invalidation.git(digest_batch.digest_app.TEST_ROOT, "rev-parse", "HEAD"),
            "stdlib_repository_revision": invalidation.git(digest_batch.digest_app.STDLIB_ROOT, "rev-parse", "HEAD"),
            "fixture_sha256": sha(digest_batch.digest_app.FIXTURE_PATH.read_bytes()),
        },
        "changed_subject": {
            "module": ASSERTIONS,
            "function": FUNCTION,
            "old_expression": "expected == actual",
            "new_expression": "actual == expected",
            "semantics_preserving": True,
            "selected_as_executable_callable": False,
            "module_source_before_sha256": sha(original_assertions.encode()),
            "module_source_after_sha256": sha(edited_modules[ASSERTIONS].encode()),
        },
        "application_build": {
            "source_snapshot_changed": (
                edited.get("source_snapshot", {}).get("identity")
                != baseline.get("source_snapshot", {}).get("identity")
            ),
            "baseline_source_snapshot_identity": baseline.get("source_snapshot", {}).get("identity"),
            "edited_source_snapshot_identity": edited.get("source_snapshot", {}).get("identity"),
            "baseline_artifact_identity": baseline.get("artifact", {}).get("identity"),
            "edited_artifact_identity": edited.get("artifact", {}).get("identity"),
            "cache_status": edited_cache.get("status"),
            "reuse_basis": edited_cache.get("reuse_basis"),
            "front_end_invocations": edited_work.get("front_end_invocations"),
            "front_end_outcome": edited_work.get("front_end_outcome"),
            "function_body_verification": edited_work.get("function_body_verification"),
            "backend_artifact": edited_work.get("backend_artifact"),
            "ssa_verification": edited_work.get("ssa_verification"),
            "fresh_process_cache_status": warm.get("cache", {}).get("status"),
            "fresh_process_reuse_basis": warm.get("cache", {}).get("reuse_basis"),
            "fresh_process_front_end_invocations": warm.get("cache", {}).get("verification_work", {}).get("front_end_invocations"),
            "artifact_callable_names": callable_names,
        },
        "execution": {
            "stage0_matches_baseline": oracle_values == baseline_values,
            "canonical_vm_matches_stage0": edited_values == oracle_values,
            "canonical_vm_matches_fixture": edited_values == baseline_values,
            "result_count": len(edited_values),
            "artifact_id": edited_document.get("artifact_id"),
            "stage0_measurements": oracle_metrics,
            "baseline_vm_measurements": baseline_vm_metrics,
            "edited_vm_measurements": edited_vm_metrics,
        },
        "build_measurements": {
            "cold_baseline": {"cache": baseline.get("cache"), "metrics": baseline_metrics},
            "unreachable_body_edit": {"cache": edited_cache, "metrics": edited_metrics},
            "fresh_process_edit_hit": {"cache": warm.get("cache"), "metrics": warm_metrics},
        },
        "language_verified_facts_reused": len(edited_work.get("reused_semantic_facts", [])),
        "scope": (
            "The changed snapshot passed full frontend admission while the Language verifier reused "
            f"{len(edited_work.get('reused_semantic_facts', []))} independent verified function facts and "
            "reverified the edited unreachable function body. The unchanged selected digest executable "
            "closure reused its VM artifact, and a fresh process reused the edited source snapshot."
        ),
        "probe_stderr_bytes": {
            "baseline": len(baseline_stderr.encode()),
            "unreachable_body_edit": len(edited_stderr.encode()),
            "fresh_process_edit_hit": len(warm_stderr.encode()),
        },
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "passed",
        "kind": evidence["kind"],
        "source_snapshot_changed": evidence["application_build"]["source_snapshot_changed"],
        "artifact_reused_after_frontend": edited_work.get("backend_artifact") == "reused",
        "front_end_invocations": edited_work.get("front_end_invocations"),
            "language_verified_facts_reused": len(edited_work.get("reused_semantic_facts", [])),
        "stage0_vm_parity": evidence["execution"]["canonical_vm_matches_stage0"],
        "evidence": str(EVIDENCE.relative_to(ROOT)),
        "evidence_sha256": sha(EVIDENCE.read_bytes()),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
