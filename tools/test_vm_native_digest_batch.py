#!/usr/bin/env python3
"""Run the real MNCS Test digest callable set in one canonical VM batch.

The pinned Gate A fixture supplies Stage-0 oracle outputs for the current
digest source snapshot. This test compiles three real digest callables into a
single /5 artifact, checks a fresh-process cache hit, and runs the same
artifact through one bounded VM batch.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
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
EVIDENCE_PATH = ROOT / "evidence/native-app-readiness-digest-multi-callable-reuse-20261016.json"
MODULE = "mncs.test.digest"
ROOTS = [
    {"module": MODULE, "function": "build_digest"},
    {"module": MODULE, "function": "request_schema"},
    {"module": MODULE, "function": "result_schema"},
]


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


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
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "native-callable-set/1",
    }


def vm_batch(
    artifact: dict, inputs: list[dict], expected: list[list[dict]]
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
    if normalized[-2:] != [
        list(b"mncs.test-digest-request/1"),
        list(b"mncs.test-digest/1"),
    ]:
        raise AssertionError(f"digest schema callables returned unexpected bytes: {normalized[-2:]}")
    return document, {
        "wall_seconds": round(wall, 6),
        "peak_rss_kib": int(peak_rss.group(1)) if peak_rss else None,
        "calls": len(calls),
        "call_ids": [row["id"] for row in calls],
    }, normalized


def main() -> None:
    if not PROBE.is_file() or not VM_BIN.is_file():
        raise SystemExit(f"missing probe or canonical VM binary: {PROBE}, {VM_BIN}")
    fixture = json.loads(digest_app.FIXTURE_PATH.read_text(encoding="utf-8"))
    root_source = digest_app.ROOT_SOURCE_PATH.read_text(encoding="utf-8")
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
    artifact_roots = {
        (row.get("module"), row.get("name")) for row in artifact.get("callables", [])
    }
    if artifact_roots != expected_roots:
        raise AssertionError(f"artifact callable closure is wrong: {artifact_roots}")
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

    vm_result, vm_metrics, normalized_results = vm_batch(
        dependency_build.get("artifact", {}).get("value"),
        fixture["execution"]["inputs"],
        fixture["execution"]["canonical_vm_results"],
    )
    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.native-test-digest-multi-callable-batch",
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
        "source_snapshot_identity": built.get("source_snapshot", {}).get("identity"),
        "request_schema": request["schema_version"],
        "response_schema": built["schema_version"],
        "artifact_identity": built["artifact"].get("identity"),
        "artifact_bytes": len(json.dumps(artifact, sort_keys=True).encode("utf-8")),
        "selected_callables": sorted(f"{module}::{name}" for module, name in artifact_roots),
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
        "measurements": {"cold_build": cold_metrics, "fresh_process_hit": warm_metrics},
        "language_verified_facts_reused": 0,
        "multi_callable_verified_vm_artifact_reused_after_full_frontend": (
            comment_work.get("backend_artifact") == "reused"
        ),
        "scope": (
            "The real mncs.test.digest source was compiled into one multi-callable application-build/5 "
            "artifact. An imported comment edit reran the full frontend and reused the selected verified "
            "VM closure while rebinding all three current callable interfaces. A semantics-preserving "
            "edit to reachable mncs.test.suite::empty invalidated the closure and regenerated the artifact. "
            "Three Stage-0-backed build_digest inputs and both schema callables then ran in one bounded "
            "canonical VM batch. This does not reuse Language verified facts and does not establish "
            "Forge/Environment/Store execution or full mncs-test conversion."
        ),
    }
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
