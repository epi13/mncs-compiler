#!/usr/bin/env python3
"""Probe native application closure emission for the Project compiler target."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import probe_vm_project_target_import_cache_frontier as frontier  # noqa: E402
import test_vm_emit  # noqa: E402
import test_vm_native_application as app  # noqa: E402

PROBE = ROOT / "tools/stage0-probe/target/release/mncs-compiler-stage0-probe"
VM = test_vm_emit.VM_BIN
PROJECT_MODULE = frontier.PROJECT_MODULE
TYPE_ARGS = frontier.TYPE_ARGS
STEP_BUDGET = frontier.STEP_BUDGET
BUILD_CACHE = ROOT / ".build/project-target-application-cache"
ARTIFACT_PATH = ROOT / ".build/project-target-application-closure.vm.json"
EVIDENCE_PATH = ROOT / "evidence/project-target-import-cache-application-closure-20261018.json"
COLD_CACHE_MISS = {
    "cache_status": "miss",
    "artifact_identity": "sha256:76e70078e0dffc0e81bd1dbcd20cb4b9c2d24374f939759eef70c0e1c7108b57",
    "wall_seconds": 174.245,
    "cpu_seconds": 171.35,
    "peak_rss_bytes": 5196525568,
    "peak_cgroup_memory_bytes": None,
}


def build_request() -> dict:
    sources = sorted((ROOT / "src/compiler").glob("*.mncs"))
    module_names: dict[Path, str] = {}
    source_text: dict[Path, str] = {}
    for path in sources:
        source = path.read_text()
        match = re.search(r"\bmodule\s+([^;]+);", source)
        if not match:
            raise ValueError(f"module declaration missing in {path.name}")
        module_names[path] = match.group(1)
        source_text[path] = source
    root_source = ROOT / "src/compiler/project.mncs"
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": "src/compiler/project.mncs",
        "root_source": source_text[root_source],
        "callable_roots": [{
            "module": PROJECT_MODULE,
            "function": "module_resolution_cache_lookup_done",
        }],
        "modules": {
            module_names[path]: source_text[path]
            for path in sources if path != root_source
        },
        "generic_seeds": [{
            "module": PROJECT_MODULE,
            "function": "compile_project_target",
            "type_arguments": TYPE_ARGS,
        }],
        "capabilities": [],
        "verification_policy": "mncs-compiler-project-target-selected-closure/1",
    }


def run_application_build(request: dict) -> tuple[dict, dict, float, str]:
    probe = app.Probe(
        cache_dir=BUILD_CACHE,
        executable=PROBE,
        application_cache_dir=BUILD_CACHE / "application",
    )
    started = time.monotonic()
    try:
        response, metrics = probe.send({"compile_application": request}, timeout=300.0)
        wall = time.monotonic() - started
        stderr = probe.close()
        return response, metrics, wall, stderr[-2000:]
    except BaseException:
        if probe.proc.poll() is None:
            probe.proc.kill()
            probe.proc.wait(timeout=5)
        raise


def run_project_vm(artifact: dict, artifact_id: str) -> dict:
    frontier._seed_target_specialization(target_only=True)
    probe = frontier.project.Probe()
    try:
        backend = probe.send({"execution_status": True})
        identities = frontier.project.identity_map(probe)
        dependency, root_source, sources = frontier._fixture()
        request = frontier._target_request(
            frontier.project.request_value(identities, sources), 1,
        )
        request["step_budget"] = STEP_BUDGET
        response = probe.send(request)
        if response.get("status") != "returned":
            raise AssertionError({"cranelift_response": response})
        cranelift_value = frontier.project.decode(response["returned"][0])
        modules = frontier.project.flist(cranelift_value["modules"])
        target = next(item for item in modules if item["source_index"] == 1)
        oracle = probe.send({"project_oracle": {
            "root": root_source,
            "modules": {"demo.dep": dependency},
        }})

        calls_path = ROOT / ".build/project-target-application-calls.json"
        output_path = ROOT / ".build/project-target-application-results.json"
        calls_path.write_text(json.dumps([{
            "id": "compile-target-with-two-import-cache-hits",
            "request": {
                "schema_version": "0.1",
                "target": {
                    "module": PROJECT_MODULE,
                    "function": "compile_project_target",
                },
                "arguments": request["arguments"],
                "type_arguments": TYPE_ARGS,
                "step_budget": STEP_BUDGET,
            },
        }]))
        started = time.monotonic()
        run = subprocess.run(
            ["/usr/bin/time", "-v", str(VM), "batch", "--artifact", str(ARTIFACT_PATH),
             "--calls", str(calls_path), "--output", str(output_path)],
            capture_output=True, text=True,
        )
        vm_wall = time.monotonic() - started
        document = (
            json.loads(output_path.read_text())
            if output_path.is_file() else json.loads(run.stdout)
        )
        results = document.get("results", [])
        row = results[0] if len(results) == 1 else document
        outcome = row.get("outcome", {}) if isinstance(row, dict) else {}
        peak_rss_kib = None
        for line in run.stderr.splitlines():
            if "Maximum resident set size" in line:
                peak_rss_kib = int(line.split(":", 1)[1].strip().split()[0])

        equivalent = None
        if outcome == {"kind": "completed"}:
            returned = row["record"]["returned"]
            if len(returned) != 1:
                raise AssertionError({"canonical_vm_returned": returned})
            value = frontier.project.decode(frontier.vm_to_wire(returned[0]))
            equivalent = value == cranelift_value
            if not equivalent:
                raise AssertionError({"cranelift": cranelift_value, "canonical_vm": value})
        elif outcome.get("kind") == "invalid_request":
            reason = outcome.get("reason", "")
            if "DAG expansion exceeds decoder limits" not in reason:
                raise AssertionError({"return_code": run.returncode, "outcome": outcome})
        else:
            raise AssertionError({"return_code": run.returncode, "document": document})

        return {
            "status": "vm_executed_and_equivalent" if equivalent else "vm_artifact_admission_refused",
            "artifact_identity": artifact_id,
            "artifact_json_bytes": ARTIFACT_PATH.stat().st_size,
            "artifact_json_sha256": hashlib.sha256(ARTIFACT_PATH.read_bytes()).hexdigest(),
            "cranelift_project_result_valid": cranelift_value["valid"],
            "cranelift_value_ssa_valid": cranelift_value["value_ssa_valid"],
            "cranelift_import_cache_hits": cranelift_value["import_resolution_cache_hits"],
            "target_flow_proof_valid": target["flow"]["proof"]["ok"],
            "target_ssa_valid": target["value_ssa"]["valid"],
            "target_verified_function_count": target["value_ssa"]["verified_function_count"],
            "project_oracle_valid": oracle["valid"],
            "project_oracle_function_count": len(oracle["program"]["functions"]),
            "project_oracle_ssa_function_count": len(oracle["ssa"]["functions"]),
            "cranelift_backend": backend.get("backend"),
            "canonical_vm_return_code": run.returncode,
            "canonical_vm_outcome": outcome,
            "canonical_vm_semantics_executed": equivalent is True,
            "canonical_vm_wall_seconds": round(vm_wall, 6),
            "canonical_vm_peak_rss_kib": peak_rss_kib,
            "cranelift_canonical_vm_equivalent": equivalent,
            "canonical_vm_stderr_tail": run.stderr[-2000:],
        }
    finally:
        probe.close()


def main() -> dict:
    test_vm_emit.ensure_vm()
    previous_report = (
        json.loads(EVIDENCE_PATH.read_text()) if EVIDENCE_PATH.is_file() else None
    )
    request = build_request()
    response, metrics, build_wall, probe_stderr = run_application_build(request)
    if response.get("status") != "verified":
        raise AssertionError({
            "application_build_status": response.get("status"),
            "diagnostic_codes": [row.get("code") for row in response.get("diagnostics", [])],
            "unresolved_obligations": response.get("unresolved_obligations"),
        })
    artifact = (response.get("artifact") or {}).get("value")
    if not isinstance(artifact, dict):
        raise AssertionError("verified application build omitted its VM artifact")
    artifact_id = artifact.get("artifact_id")
    selected_module = (
        artifact.get("code", {})
        .get("mncs-selected-ssa", {})
        .get("module", {})
    )
    selected_record_types = selected_module.get("record_types", [])
    artifact_path = ARTIFACT_PATH.with_suffix(".json.tmp")
    artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
    artifact_path.replace(ARTIFACT_PATH)

    vm = run_project_vm(artifact, artifact_id)
    previous_artifact_bytes = None
    previous_artifact_identity = None
    previous_execution_status = None
    if previous_report is not None:
        previous_artifact_bytes = previous_report.get(
            "application_artifact_json_bytes",
            previous_report.get("artifact_json_bytes"),
        )
        previous_artifact_identity = previous_report.get(
            "application_artifact_identity",
            previous_report.get("artifact_identity"),
        )
        previous_execution_status = previous_report.get("status")
    artifact_bytes = ARTIFACT_PATH.stat().st_size
    if vm["status"] == "vm_executed_and_equivalent":
        evidence_scope = (
            "The native application compiler verified the selected callable closure "
            "containing a concrete compile_project_target specialization. The canonical "
            "VM admitted and executed the emitted application, and its result matched "
            "the Cranelift result and independent Project oracle for this fixture. "
            "Language-owned verified-fact reuse is not exercised by this evidence."
        )
    else:
        evidence_scope = (
            "The native application compiler verified a selected callable closure containing "
            "a concrete compile_project_target specialization. Cranelift and the independent "
            "Project oracle returned valid results with the same input fixture, but canonical "
            "VM admission refused the artifact, so no VM semantics or cross-backend equivalence "
            "is claimed."
        )
    report = {
        "schema_version": "mncs.compiler.project-target-application-closure-frontier/1",
        "status": vm["status"],
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "test_harness": "tools/probe_vm_project_target_application_closure.py",
        "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
        "compiler_branch": "campaign/native-application-readiness-20261013",
        "compiler_head": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "stage0_revision": json.loads(
            (ROOT / "mncs-language.lock.json").read_text()
        )["revision"],
        "application_build_request_schema": request["schema_version"],
        "application_build_response_schema": response.get("schema_version"),
        "application_build_status": response.get("status"),
        "application_build_cache_status": response.get("cache", {}).get("status"),
        "application_build_source_snapshot_identity": response.get("source_snapshot", {}).get("identity"),
        "application_build_identity": (response.get("artifact") or {}).get("identity"),
        "application_callable_root": request["callable_roots"][0],
        "generic_seed": request["generic_seeds"][0],
        "application_callable_count": len(artifact.get("callables", [])),
        "application_ssa_record_type_count": len(selected_record_types),
        "previous_application_execution_status": previous_execution_status,
        "previous_application_artifact_identity": previous_artifact_identity,
        "previous_application_artifact_json_bytes": previous_artifact_bytes,
        "application_generic_entrypoint": next(
            row for row in artifact.get("generic_entrypoints", [])
            if row.get("generic_function") == "compile_project_target"
        ),
        "application_build_wall_seconds": round(build_wall, 6),
        "application_build_metrics": metrics,
        "initial_cache_miss_measurement": (
            COLD_CACHE_MISS if artifact_id == COLD_CACHE_MISS["artifact_identity"] else None
        ),
        "application_build_probe_stderr_tail": probe_stderr,
        "application_artifact_identity": artifact_id,
        "application_artifact_json_bytes": artifact_bytes,
        "application_artifact_json_bytes_reduced_from_previous": (
            previous_artifact_bytes - artifact_bytes
            if isinstance(previous_artifact_bytes, int) else None
        ),
        "application_artifact_json_sha256": hashlib.sha256(
            ARTIFACT_PATH.read_bytes(),
        ).hexdigest(),
        "language_verified_facts_reused": 0,
        **vm,
        "stage1_claimed": False,
        "stage2_claimed": False,
        "evidence_scope": evidence_scope,
    }
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
