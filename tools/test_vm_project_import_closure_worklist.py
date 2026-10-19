#!/usr/bin/env python3
"""Compare the Project import-closure worklist on Stage-0 and canonical VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import test_vm_emit  # noqa: E402
import test_vm_native_application as app  # noqa: E402

PROBE = ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe"
VM = test_vm_emit.VM_BIN
PROJECT_MODULE = "mncs.compiler.project.v1"
WRAPPER_MODULE = "probe.project_closure_worklist"
STEP_BUDGET = 2_000_000
BUILD_DIR = ROOT / ".build/project-import-closure-worklist-vm"


def source_bundle() -> dict[str, str]:
    modules = {}
    for path in sorted((ROOT / "src/compiler").glob("*.mncs")):
        source = path.read_text()
        match = re.search(r"\bmodule\s+([^;]+);", source)
        if not match:
            raise ValueError(f"module declaration missing in {path}")
        name = match.group(1)
        if name in modules:
            raise ValueError(f"duplicate compiler module identity: {name}")
        modules[name] = source
    return modules


def closure_function(name: str, target: int, edges: list[tuple[int, int]]) -> str:
    links = "project.ImportList.INil"
    for source_index, target_index in reversed(edges):
        link = (
            "project.ImportLink { "
            f"source_index: {source_index}, target_index: {target_index}, "
            "module_start: 0, module_end: 0, alias_start: 0, alias_end: 0, "
            "has_alias: false }"
        )
        links = (
            "project.ImportList.ICons { "
            f"head: {link}, tail: {links} }}"
        )
    return (
        f"fn {name}() -> (result: project.ProjectImportClosure) {{\n"
        f"  let imports: project.ImportList = {links};\n"
        f"  return project.project_import_closure(imports, {target});\n"
        "}\n"
    )


def wrapper_source() -> str:
    # In each graph, ascending link order makes each of the first four full
    # scans discover at most one new source. Both graphs then need the worklist.
    cycle_edges = [(index, index - 1) for index in range(1, 7)] + [(6, 7), (7, 6)]
    capacity_edges = [(index, index - 1) for index in range(1, 64)] + [(62, 63)]
    return (
        "mncs 0.18; module probe.project_closure_worklist;\n"
        "use mncs.compiler.project.v1 as project;\n"
        + closure_function("run", 7, cycle_edges)
        + closure_function("run_capacity", 63, capacity_edges)
    )


def build_request(modules: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": "probe/project_closure_worklist.mncs",
        "root_source": wrapper_source(),
        "callable_roots": [
            {"module": WRAPPER_MODULE, "function": "run"},
            {"module": WRAPPER_MODULE, "function": "run_capacity"},
        ],
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "project-import-closure-worklist-canonical-vm/1",
    }


def target_call(function: str) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": WRAPPER_MODULE, "function": function},
        "arguments": [],
        "type_arguments": [],
        "step_budget": STEP_BUDGET,
    }


def main() -> dict:
    test_vm_emit.ensure_probe()
    test_vm_emit.ensure_vm()
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    modules = source_bundle()
    source = wrapper_source()
    request = build_request(modules)
    probe = app.Probe(
        cache_dir=BUILD_DIR / "probe-cache",
        executable=PROBE,
        application_cache_dir=BUILD_DIR / "application-cache",
    )
    try:
        build_response, build_metrics = probe.send(
            {"compile_application": request}, timeout=300.0,
        )
        if build_response.get("status") != "verified":
            raise AssertionError({
                "status": build_response.get("status"),
                "diagnostics": build_response.get("diagnostics"),
                "unresolved_obligations": build_response.get("unresolved_obligations"),
            })
        artifact = (build_response.get("artifact") or {}).get("value")
        if not isinstance(artifact, dict):
            raise AssertionError("application build omitted its canonical VM artifact")

        calls = [target_call("run"), target_call("run_capacity")]
        oracle, oracle_metrics = probe.send({"project_oracle": {
            "root": source,
            "modules": modules,
            "calls": calls,
        }}, timeout=300.0)
        if oracle.get("valid") is not True or len(oracle.get("executions", [])) != len(calls):
            raise AssertionError({"stage0_project_oracle": oracle})
        expected = []
        for oracle_execution in oracle["executions"]:
            if oracle_execution.get("status") != "returned":
                raise AssertionError({"stage0_execution": oracle_execution})
            expected.append(app.decode_wire(oracle_execution["returned"][0]))
    finally:
        probe_stderr = probe.close()

    artifact_path = BUILD_DIR / "project-import-closure-worklist.vm.json"
    temporary_artifact = artifact_path.with_suffix(".json.tmp")
    temporary_artifact.write_text(json.dumps(artifact, sort_keys=True) + "\n")
    os.replace(temporary_artifact, artifact_path)
    calls_path = BUILD_DIR / "calls.json"
    results_path = BUILD_DIR / "results.json"
    calls_path.write_text(json.dumps([
        {"id": "reverse-cyclic-closure", "request": target_call("run")},
        {"id": "64-source-cyclic-closure", "request": target_call("run_capacity")},
    ]))

    started = time.monotonic()
    vm_run = subprocess.run(
        ["/usr/bin/time", "-v", str(VM), "batch", "--artifact", str(artifact_path),
         "--calls", str(calls_path), "--output", str(results_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    vm_wall = time.monotonic() - started
    if vm_run.returncode != 0:
        raise AssertionError({
            "vm_return_code": vm_run.returncode,
            "stdout": vm_run.stdout[-2000:],
            "stderr": vm_run.stderr[-4000:],
        })
    document = json.loads(results_path.read_text())
    rows = document.get("results", [])
    if len(rows) != 2 or any(row.get("outcome") != {"kind": "completed"} for row in rows):
        raise AssertionError({"canonical_vm_results": rows})
    actual = []
    for vm_row in rows:
        vm_returned = vm_row.get("record", {}).get("returned", [])
        if len(vm_returned) != 1:
            raise AssertionError({"canonical_vm_returned": vm_returned})
        actual.append(app.decode_wire(app.vm_to_wire(vm_returned[0])))
    expected_reachable = [
        [index < 8 for index in range(64)],
        [True for _ in range(64)],
    ]
    for index, (oracle_result, vm_result) in enumerate(zip(expected, actual)):
        if (vm_result != oracle_result
                or vm_result.get("reachable") != expected_reachable[index]
                or vm_result.get("changed") is not False):
            raise AssertionError({"stage0": oracle_result, "canonical_vm": vm_result})

    peak_rss_kib = None
    for line in vm_run.stderr.splitlines():
        if "Maximum resident set size" in line:
            peak_rss_kib = int(line.split(":", 1)[1].strip().split()[0])
    return {
        "schema_version": "mncs.compiler.project-import-closure-worklist-vm/2",
        "status": "verified",
        "compiler_source_root": str(ROOT),
        "wrapper_module": WRAPPER_MODULE,
        "project_module": PROJECT_MODULE,
        "source_count": len(modules),
        "scenarios": [
            {
                "callable": "run",
                "import_link_count": 8,
                "target_source_index": 7,
                "cycle_edges": [[6, 7], [7, 6]],
                "reachable_indices": [
                    index for index, value in enumerate(actual[0]["reachable"]) if value
                ],
            },
            {
                "callable": "run_capacity",
                "import_link_count": 64,
                "target_source_index": 63,
                "cycle_edges": [[62, 63], [63, 62]],
                "reachable_indices": [
                    index for index, value in enumerate(actual[1]["reachable"]) if value
                ],
            },
        ],
        "worklist_fallback_exercised_by_graph_depth": True,
        "stage0_valid": oracle["valid"],
        "stage0_execution": expected,
        "canonical_vm_execution": actual,
        "stage0_vm_equivalent": actual == expected,
        "artifact_id": artifact["artifact_id"],
        "artifact_json_bytes": artifact_path.stat().st_size,
        "artifact_json_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        "application_build": build_metrics,
        "stage0_oracle": oracle_metrics,
        "canonical_vm_wall_seconds": round(vm_wall, 6),
        "canonical_vm_peak_rss_kib": peak_rss_kib,
        "canonical_vm_return_code": vm_run.returncode,
        "canonical_vm_stderr_tail": vm_run.stderr[-2000:],
        "probe_stderr_tail": probe_stderr[-1000:],
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, sort_keys=True))
