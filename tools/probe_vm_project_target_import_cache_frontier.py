#!/usr/bin/env python3
"""Probe target-only Project compilation across Stage 0 and the canonical VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
PROJECT_MODULE = "mncs.compiler.project.v1"
TYPE_ARGS = [
    {"kind": "nat", "value": 1024},
    {"kind": "nat", "value": 1024},
]
STEP_BUDGET = 8_000_000
ARTIFACT_INPUTS = [
    "src/compiler/source.mncs", "src/compiler/lexer.mncs",
    "src/compiler/parser.mncs", "src/compiler/kernel.mncs",
    "src/compiler/segment.mncs", "src/compiler/decl.mncs",
    "src/compiler/flow.mncs", "src/compiler/ssa.mncs",
    "src/compiler/project.mncs", "mncs-language.lock.json",
    "tools/vm_emit.rs", "tools/vm-artifact-codec/src/lib.rs",
    "tools/vm-artifact-codec/Cargo.toml",
    "tools/stage0-probe/src/main.rs",
    "tools/stage0-probe/src/application.rs",
    "tools/stage0-probe/src/provider.rs",
    "tools/stage0-probe/build.rs",
    "tools/stage0-probe/Cargo.toml",
    "tools/stage0-probe/Cargo.lock",
]

os.environ["MNCS_PROBE_BACKEND"] = "cranelift"
os.environ["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa,project"
os.environ["MNCS_PROBE_EXECUTION_MODULES"] = PROJECT_MODULE
os.environ["MNCS_PROBE_REQUEST_TIMEOUT_S"] = "300"

import test_project as project  # noqa: E402
from probe_compiler_module_frontier import _seed_target_specialization  # noqa: E402
from test_project_target_module import _target_request  # noqa: E402
from test_vm_segment import vm_to_wire  # noqa: E402
import test_vm_emit  # noqa: E402


def _fixture():
    dependency = (
        "mncs 0.18; module demo.dep; "
        "fn value(x: u64) -> (result: u64) { return x; }\n"
    )
    root = (
        "mncs 0.18; module demo.root;\n"
        "use demo.dep as d0;\n"
        "use demo.dep as d1;\n"
        "use demo.dep as d2;\n"
        "fn sum(x: u64) -> (result: u64) { "
        "return d0.value(x) + d1.value(x) + d2.value(x); }\n"
    )
    sources = [
        ("00-dep.mncs", Path("00-dep.mncs"), dependency.encode()),
        ("zz-root.mncs", Path("zz-root.mncs"), root.encode()),
    ]
    return dependency, root, sources


def _artifact_source_identity() -> str:
    digest = hashlib.sha256()
    for name in ARTIFACT_INPUTS:
        data = (ROOT / name).read_bytes()
        digest.update(name.encode() + b"\0" + len(data).to_bytes(8, "big") + data)
    digest.update(json.dumps({
        "module": PROJECT_MODULE,
        "function": "compile_project_target",
        "type_arguments": TYPE_ARGS,
    }, sort_keys=True).encode())
    return digest.hexdigest()


def _load_or_emit_artifact(probe) -> tuple[dict, float, bool]:
    cache_dir = ROOT / ".build"
    cache_dir.mkdir(exist_ok=True)
    artifact_path = cache_dir / "project-target-import-cache-vm.json"
    identity_path = cache_dir / "project-target-import-cache-vm.identity.json"
    source_identity = _artifact_source_identity()
    if artifact_path.is_file() and identity_path.is_file():
        identity = json.loads(identity_path.read_text())
        if identity.get("source_identity_sha256") == source_identity:
            artifact = json.loads(artifact_path.read_text())
            if artifact.get("artifact_id") == identity.get("artifact_id"):
                return artifact, 0.0, True

    started = time.monotonic()
    emitted = probe.send({"emit_vm_artifact": {"module": PROJECT_MODULE}})
    artifact = emitted.get("artifact")
    assert isinstance(artifact, dict), emitted
    assert artifact.get("schema_version") == "mncs.vm.artifact/1", artifact
    assert "program" not in artifact
    entrypoints = [
        row for row in artifact.get("generic_entrypoints", [])
        if row.get("generic_function") == "compile_project_target"
    ]
    assert len(entrypoints) == 1, entrypoints
    temporary = artifact_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(artifact, sort_keys=True) + "\n")
    os.replace(temporary, artifact_path)
    identity_path.write_text(json.dumps({
        "source_identity_sha256": source_identity,
        "artifact_id": artifact["artifact_id"],
    }, indent=2) + "\n")
    return artifact, time.monotonic() - started, False


def _run_vm_batch(
    artifact_path: Path, vm_case: dict,
) -> tuple[dict, float, int | None, int, int, int, int, str]:
    run_dir = ROOT / ".build" / "project-target-import-cache-vm-run"
    run_dir.mkdir(exist_ok=True)
    calls_path = run_dir / "calls.json"
    output_path = run_dir / "results.json"
    calls_path.write_text(json.dumps([{
        "id": vm_case["id"],
        "request": {
            "schema_version": "0.1",
            "target": {"module": PROJECT_MODULE, "function": vm_case["function"]},
            "arguments": vm_case["args"],
            "type_arguments": vm_case["type_args"],
            "step_budget": vm_case["step_budget"],
        },
    }]))
    test_vm_emit.ensure_vm()
    command = [
        "/usr/bin/time", "-v", str(test_vm_emit.VM_BIN), "batch",
        "--artifact", str(artifact_path),
        "--calls", str(calls_path),
        "--output", str(output_path),
    ]
    started = time.monotonic()
    completed = subprocess.run(command, capture_output=True, text=True)
    wall = time.monotonic() - started
    peak_kib = None
    for line in completed.stderr.splitlines():
        if "Maximum resident set size" in line:
            peak_kib = int(line.split(":", 1)[1].strip().split()[0])
    if output_path.is_file():
        document = json.loads(output_path.read_text())
    else:
        document = json.loads(completed.stdout)
    return (
        document, wall, peak_kib, calls_path.stat().st_size,
        output_path.stat().st_size if output_path.is_file() else 0,
        completed.returncode, len(completed.stdout.encode()), completed.stderr[-4000:],
    )


def main() -> dict:
    test_vm_emit.ensure_vm()
    _seed_target_specialization(target_only=True)
    probe = project.Probe()
    try:
        execution = probe.send({"execution_status": True})
        identities = project.identity_map(probe)
        dependency, root, sources = _fixture()
        target_index = 1
        request = _target_request(
            project.request_value(identities, sources), target_index,
        )
        request["step_budget"] = STEP_BUDGET

        started = time.monotonic()
        response = probe.send(request)
        cranelift_request_seconds = time.monotonic() - started
        assert response.get("status") == "returned", response
        cranelift_value = project.decode(response["returned"][0])
        assert cranelift_value["valid"] is True, cranelift_value["diagnostics"]
        assert cranelift_value["value_ssa_valid"] is True, cranelift_value
        assert cranelift_value["source_count"] == 2
        assert cranelift_value["module_count"] == 2
        assert cranelift_value["import_count"] == 3
        assert cranelift_value["import_resolution_cache_hits"] == 2
        modules = project.flist(cranelift_value["modules"])
        target = next(item for item in modules if item["source_index"] == target_index)
        assert target["flow"]["proof"]["ok"] is True
        assert target["value_ssa"]["valid"] is True
        assert target["value_ssa"]["verified_function_count"] == 1

        oracle = probe.send({"project_oracle": {
            "root": root,
            "modules": {"demo.dep": dependency},
        }})
        assert oracle.get("valid") is True, oracle
        assert oracle.get("program") is not None and oracle.get("ssa") is not None, oracle
        assert oracle["valid"] == cranelift_value["valid"]
        assert oracle["module_resolutions"], oracle
        assert len(oracle["program"]["functions"]) == 2
        assert len(oracle["ssa"]["functions"]) == 2

        artifact, artifact_emission_seconds, artifact_cache_reused = (
            _load_or_emit_artifact(probe)
        )
        entrypoints = [
            row for row in artifact.get("generic_entrypoints", [])
            if row.get("generic_function") == "compile_project_target"
        ]
        assert len(entrypoints) == 1, entrypoints

        vm_case = {
            "id": "compile-target-with-two-import-cache-hits",
            "function": "compile_project_target",
            "args": request["arguments"],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        }
        artifact_path = ROOT / ".build" / "project-target-import-cache-vm.json"
        (
            document, vm_wall, vm_peak_kib, calls_bytes, results_bytes,
            vm_return_code, vm_stdout_bytes, vm_stderr_tail,
        ) = _run_vm_batch(artifact_path, vm_case)
        rows = document.get("results", [])
        row = rows[0] if len(rows) == 1 else document
        outcome = row.get("outcome", {}) if isinstance(row, dict) else {}
        vm_value = None
        equivalent = None
        if outcome == {"kind": "completed"}:
            returned = row["record"]["returned"]
            assert len(returned) == 1, returned
            vm_value = project.decode(vm_to_wire(returned[0]))
            equivalent = vm_value == cranelift_value
            if not equivalent:
                raise AssertionError({
                    "cranelift": cranelift_value,
                    "canonical_vm": vm_value,
                })
        elif outcome.get("kind") == "invalid_request":
            reason = outcome.get("reason", "")
            if "DAG expansion exceeds decoder limits" not in reason:
                raise AssertionError({
                    "return_code": vm_return_code,
                    "document": document,
                    "stderr_tail": vm_stderr_tail,
                })
        else:
            raise AssertionError({
                "return_code": vm_return_code,
                "document": document,
                "stderr_tail": vm_stderr_tail,
            })

        status = (
            "vm_executed_and_equivalent" if equivalent is True
            else "vm_artifact_admission_refused"
        )
        report = {
            "schema_version": "mncs.compiler.project-target-import-cache-vm-admission-frontier/1",
            "status": status,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler_head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                capture_output=True, text=True,
            ).stdout.strip(),
            "compiler_source_identity_sha256": _artifact_source_identity(),
            "test_harness": "tools/probe_vm_project_target_import_cache_frontier.py",
            "target_module": PROJECT_MODULE,
            "target_function": "compile_project_target",
            "type_arguments": ["1024", "1024"],
            "stage0_revision": json.loads(
                (ROOT / "mncs-language.lock.json").read_text()
            )["revision"],
            "cranelift_backend": execution.get("backend"),
            "stage0_project_oracle_valid": oracle["valid"],
            "canonical_vm_artifact_identity": artifact.get("artifact_id"),
            "canonical_vm_artifact_json_bytes": artifact_path.stat().st_size,
            "canonical_vm_artifact_json_sha256": hashlib.sha256(
                artifact_path.read_bytes(),
            ).hexdigest(),
            "artifact_cache_reused": artifact_cache_reused,
            "artifact_emission_seconds": round(artifact_emission_seconds, 6),
            "project_oracle_valid": oracle["valid"],
            "cranelift_canonical_vm_equivalent": equivalent,
            "stage0_semantic_digest_sha256": hashlib.sha256(json.dumps(
                cranelift_value, sort_keys=True,
            ).encode()).hexdigest(),
            "source_count": cranelift_value["source_count"],
            "module_count": cranelift_value["module_count"],
            "import_count": cranelift_value["import_count"],
            "import_resolution_cache_hits": cranelift_value["import_resolution_cache_hits"],
            "target_flow_proof_valid": target["flow"]["proof"]["ok"],
            "target_ssa_valid": target["value_ssa"]["valid"],
            "target_verified_function_count": target["value_ssa"]["verified_function_count"],
            "cranelift_request_seconds": round(cranelift_request_seconds, 6),
            "canonical_vm_wall_seconds": round(vm_wall, 6),
            "canonical_vm_peak_rss_kib": vm_peak_kib,
            "canonical_vm_return_code": vm_return_code,
            "canonical_vm_outcome": outcome,
            "canonical_vm_stdout_bytes": vm_stdout_bytes,
            "canonical_vm_stderr_tail": vm_stderr_tail,
            "canonical_vm_calls_bytes": calls_bytes,
            "canonical_vm_results_bytes": results_bytes,
            "step_budget": STEP_BUDGET,
        }
        output = ROOT / "evidence" / "project-target-import-cache-vm-admission-20261018.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
