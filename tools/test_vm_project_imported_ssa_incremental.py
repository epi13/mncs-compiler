#!/usr/bin/env python3
"""Exercise imported-schema Flow/SSA application cache invalidation.

The workload is the selected Compiler self-consumption application from
test_vm_project_imported_flow_ssa.py. It uses an isolated cache and changes
only one source/interface fact at a time, then checks resulting artifacts in
the canonical VM.
"""
from __future__ import annotations

import copy
import atexit
import hashlib
import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import test_vm_project_imported_flow_ssa as frontier
from test_vm_segment import vm_to_wire

ROOT = frontier.ROOT
TARGET_MODULE = "mncs.compiler.ssa.v1"
TARGET_SOURCE = ROOT / "src/compiler/ssa.mncs"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def identity(build: dict) -> str:
    artifact = build.get("artifact", {}).get("value") or {}
    value = artifact.get("artifact_id")
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise AssertionError(f"build has no canonical artifact identity: {artifact}")
    return value


def build_observation(label: str, request: dict, build: dict, metrics: dict) -> dict:
    if (
        build.get("schema_version") != "mncs.compiler.application-build/4"
        or build.get("status") != "verified"
        or build.get("entry", {}).get("status") != "verified"
        or build.get("unresolved_obligations") != []
    ):
        raise AssertionError(f"{label} application did not verify: {json.dumps(build, sort_keys=True)[:5000]}")
    cache = build.get("cache", {})
    work = cache.get("verification_work", {})
    return {
        "label": label,
        "source_snapshot_identity": build.get("source_snapshot", {}).get("identity"),
        "artifact_identity": identity(build),
        "cache_status": cache.get("status"),
        "reuse_basis": cache.get("reuse_basis"),
        "backend_artifact": work.get("backend_artifact"),
        "front_end_invocations": work.get("front_end_invocations"),
        "program_function_count": work.get("program_function_count"),
        "reused_semantic_facts": work.get("reused_semantic_facts", []),
        "semantic_reverification_reason": work.get("semantic_reverification_reason"),
        "artifact_decision_reason": work.get("artifact_decision_reason"),
        "module_source_sha256": sha(request["modules"][TARGET_MODULE].encode()),
        "root_source_sha256": sha(request["root_source"].encode()),
        "measurement": metrics,
    }


def expected_cached_reuse(row: dict, label: str, artifact_id: str) -> None:
    if (
        row["cache_status"] != "hit"
        or row["backend_artifact"] != "reused"
        or row["artifact_identity"] != artifact_id
        or row["front_end_invocations"] != 1
    ):
        raise AssertionError(f"{label} did not reuse the unchanged executable closure: {row}")


def expected_rebuild(row: dict, label: str, prior_artifact_id: str) -> None:
    if (
        row["backend_artifact"] != "generated"
        or row["artifact_identity"] == prior_artifact_id
        or row["front_end_invocations"] != 1
    ):
        raise AssertionError(f"{label} did not rebuild after a reachable change: {row}")


def canonical_call(build: dict, imported_identity: str, *, wrong_schema: bool = False) -> tuple[dict, dict]:
    artifact = build["artifact"]["value"]
    interface = build["entry"]["interface"]
    args = frontier.build_inputs(
        interface,
        imported_identity,
        field_is_u64=not wrong_schema,
    )["arguments"]
    with tempfile.TemporaryDirectory(prefix="mncs-imported-ssa-cache-vm-") as name:
        directory = Path(name)
        artifact_path = directory / "artifact.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
        case_id = "wrong_imported_schema" if wrong_schema else "imported_Box_read_4"
        document, wall, peak_kib, calls_bytes, results_bytes = frontier.run_vm_batch(
            artifact_path,
            [{"id": case_id, "args": args}],
            directory,
        )
    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different imported-schema artifact")
    result = document["results"][0]
    if result.get("id") != case_id or result.get("outcome") != {"kind": "completed"}:
        raise AssertionError(f"canonical VM rejected {case_id}: {result}")
    returned = result.get("record", {}).get("returned", [])
    if len(returned) != 1:
        raise AssertionError(f"canonical VM returned the wrong value count for {case_id}: {result}")
    decoded = frontier.project.decode(vm_to_wire(returned[0]))
    if wrong_schema:
        if decoded.get("valid") is not False:
            raise AssertionError(f"wrong imported field schema was accepted: {decoded}")
    elif not decoded.get("valid") or decoded.get("verified_function_count") != 2:
        raise AssertionError(f"imported generic Flow/SSA result was not valid: {decoded}")
    return decoded, {
        "case_id": case_id,
        "artifact_identity": identity(build),
        "outcome": result.get("outcome"),
        "decoded": decoded,
        "wall_seconds": round(wall, 6),
        "peak_rss_kib": peak_kib,
        "calls_bytes": calls_bytes,
        "results_bytes": results_bytes,
    }


def main() -> None:
    build_root = ROOT / ".build/native-app-readiness"
    build_root.mkdir(parents=True, exist_ok=True)
    host_before = frontier.memory_snapshot()
    started = time.monotonic()
    stage0_metrics: dict = {}
    observations: list[dict] = []
    vm_runs: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="imported-ssa-cache-", dir=build_root) as cache_name:
        probe = frontier.Probe(Path(cache_name) / "cache")
        atexit.register(probe.close)
        oracle, stage0_metrics = probe.send({"project_oracle": {
            "root": frontier.ROOT_SOURCE,
            "modules": {"demo.dep": frontier.DEPENDENCY},
        }})
        if oracle.get("valid") is not True or not isinstance(oracle.get("ssa"), dict):
            raise AssertionError(f"Stage-0 rejected the imported schema fixture: {oracle.get('diagnostics')}")
        nominal_rows = [
            row for row in oracle.get("program", {}).get("record_types", [])
            if row.get("name") == "Box"
        ]
        if len(nominal_rows) != 1:
            raise AssertionError(f"Stage-0 did not retain the imported Box schema: {nominal_rows}")
        imported_identity = nominal_rows[0]["identity"]
        specializations = oracle["ssa"].get("generic_specializations", [])
        if not any(
            row.get("generic_function") == "mncs:0.2:function:demo.root::read"
            and row.get("canonical_args") == "value:4"
            for row in specializations
        ):
            raise AssertionError(f"Stage-0 did not resolve the generic read<4> call: {specializations}")

        baseline_request = frontier.application_request()
        cold_build, cold_metrics = probe.send({"compile_application": baseline_request})
        cold = build_observation("cold", baseline_request, cold_build, cold_metrics)
        if cold["cache_status"] != "miss" or cold["backend_artifact"] != "generated":
            raise AssertionError(f"isolated cache did not produce a cold build: {cold}")
        if cold["front_end_invocations"] != 1:
            raise AssertionError(f"cold build skipped the Language frontend: {cold}")
        observations.append(cold)

        warm_build, warm_metrics = probe.send({"compile_application": baseline_request})
        warm = build_observation("warm_unchanged", baseline_request, warm_build, warm_metrics)
        if (
            warm["cache_status"] != "hit"
            or warm["backend_artifact"] != "reused"
            or warm["artifact_identity"] != cold["artifact_identity"]
            or warm["front_end_invocations"] != 0
        ):
            raise AssertionError(f"unchanged request did not hit the exact artifact cache: {warm}")
        observations.append(warm)

        unused_request = copy.deepcopy(baseline_request)
        unused_request["modules"][TARGET_MODULE] += (
            "\nfn imported_ssa_cache_unreachable() -> (r: u64) { return 1; }\n"
        )
        unused_added_build, unused_added_metrics = probe.send({"compile_application": unused_request})
        unused_added = build_observation(
            "unreachable_callable_interface_added", unused_request, unused_added_build, unused_added_metrics
        )
        expected_rebuild(unused_added, "unreachable callable interface addition", cold["artifact_identity"])
        observations.append(unused_added)

        unused_edited_request = copy.deepcopy(unused_request)
        needle = "fn imported_ssa_cache_unreachable() -> (r: u64) { return 1; }"
        if unused_edited_request["modules"][TARGET_MODULE].count(needle) != 1:
            raise AssertionError("could not uniquely select the unreachable cache probe body")
        unused_edited_request["modules"][TARGET_MODULE] = (
            unused_edited_request["modules"][TARGET_MODULE].replace(
                needle,
                "fn imported_ssa_cache_unreachable() -> (r: u64) { return 2; }",
                1,
            )
        )
        unused_edit_build, unused_edit_metrics = probe.send({"compile_application": unused_edited_request})
        unused_edit = build_observation(
            "unreachable_body_edit", unused_edited_request, unused_edit_build, unused_edit_metrics
        )
        expected_cached_reuse(unused_edit, "unreachable body edit", unused_added["artifact_identity"])
        observations.append(unused_edit)
        probe_stderr = probe.close()
        atexit.unregister(probe.close)

        frontier.test_vm_emit.ensure_vm()
        edited_value, edited_vm = canonical_call(unused_edit_build, imported_identity)
        _, wrong_schema_vm = canonical_call(unused_edit_build, imported_identity, wrong_schema=True)
        vm_runs.extend([edited_vm, wrong_schema_vm])

    head = __import__("subprocess").check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    report = {
        "schema_version": "mncs.compiler.imported-ssa-incremental-frontier/1",
        "kind": "imported-nominal-generic-flow-ssa-application-cache-invalidation",
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "compiler": {
            "revision": head,
            "branch": __import__("subprocess").check_output(
                ["git", "branch", "--show-current"], cwd=ROOT, text=True
            ).strip(),
            "worktree": str(ROOT),
            "harness_sha256": sha(Path(__file__).read_bytes()),
            "base_harness_sha256": sha(Path(frontier.__file__).read_bytes()),
            "ssa_module_sha256": sha(TARGET_SOURCE.read_bytes()),
        },
        "workload": {
            "entry": f"{frontier.VM_MODULE}::run",
            "selected_function": f"{TARGET_MODULE}::{frontier.TARGET_FUNCTION}<4,64>",
            "generic_call": "demo.root::read<4>",
            "imported_nominal_identity": imported_identity,
            "stage0_oracle_valid": True,
            "stage0_generic_specialization_resolved": True,
            "cache_directory_was_isolated": True,
        },
        "application_builds": observations,
        "canonical_vm": {
            "runs": vm_runs,
            "imported_generic_result_valid": edited_value.get("valid") is True,
            "wrong_imported_schema_rejected": wrong_schema_vm["decoded"].get("valid") is False,
        },
        "semantic_verification": {
            "language_verified_facts_reused": 0,
            "changed_source_builds_frontend_invocations": [
                row["front_end_invocations"] for row in observations[2:]
            ],
            "unreachable_callable_interface_addition_regenerated_artifact": unused_added["artifact_identity"] != cold["artifact_identity"],
            "unreachable_body_edit_reused_interface_stable_artifact": unused_edit["artifact_identity"] == unused_added["artifact_identity"],
            "all_changed_source_fact_counts": [len(row["reused_semantic_facts"]) for row in observations[2:]],
        },
        "measurements": {
            "stage0_project_oracle": stage0_metrics,
            "whole_frontier_seconds": round(time.monotonic() - started, 6),
            "host_memory_before": host_before,
            "cgroup_memory_before": frontier.cgroup_snapshot(),
            "cgroup_memory_after": frontier.cgroup_snapshot(),
            "cgroup_peak_attributable_to_test": False,
        },
        "probe_stderr_sha256": sha(probe_stderr.encode()),
        "claim_boundary": (
            "This selected Compiler application proves exact-snapshot reuse, safe reuse after an unreachable "
            "body edit, regeneration after a reachable body or callable-interface edit, and canonical VM "
            "behavior for the imported generic/schema workload. Each changed source snapshot still runs the "
            "full Language frontend; no Language-owned verified facts are reused. It is not full module proof, "
            "Forge execution, or compiler succession."
        ),
    }
    out = ROOT / "evidence/self-host-imported-ssa-incremental-20261017.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
