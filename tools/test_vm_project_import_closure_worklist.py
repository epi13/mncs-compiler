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
FULL_LINK_STEP_BUDGET = 8_000_000
BUILD_DIR = ROOT / ".build/project-import-closure-worklist-vm"

FIXED_GRAPHS = [
    {"name": "empty", "target": 5, "edges": []},
    {"name": "self_loop", "target": 7, "edges": [(7, 7)]},
    {
        "name": "diamond_with_cycle",
        "target": 11,
        "edges": [(11, 10), (11, 9), (10, 8), (9, 8), (8, 11)],
    },
    {
        "name": "invalid_and_disconnected_edges",
        "target": 5,
        "edges": [(5, 4), (4, 3), (64, 2), (3, 64), (20, 21)],
    },
    {
        "name": "four_round_reverse_chain",
        "target": 4,
        "edges": [(1, 0), (2, 1), (3, 2), (4, 3)],
    },
    {
        "name": "duplicate_edges",
        "target": 12,
        "edges": [(12, 11), (12, 11), (11, 10), (11, 10)],
    },
]
GENERATED_SEEDS = tuple(range(16))
REVERSE_CYCLIC_EDGES = [(index, index - 1) for index in range(1, 7)] + [(6, 7), (7, 6)]
CAPACITY_EDGES = [(index, index - 1) for index in range(1, 64)] + [(62, 63)]
SMALL_REVERSE_CHAIN_EDGES = [(index, index - 1) for index in range(1, 8)]
SMALL_SOURCE_BOUNDARY_EDGES = [(1, 0), (2, 1), (3, 2), (3, 4), (4, 0), (4, 3)]


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


def closure_function(
    name: str,
    target: int,
    edges: list[tuple[int, int]],
    source_count: int = 64,
) -> str:
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
        f"  return project.project_import_closure(imports, {target}, {source_count});\n"
        "}\n"
    )


def generated_edges(seed: int) -> list[tuple[int, int]]:
    return [
        edge
        for source_index in range(64)
        for edge in (
            (source_index, (source_index * 13 + seed) % 64),
            (source_index, (source_index * 29 + seed * 7 + 1) % 64),
        )
    ]


def reachable_indices(
    target: int,
    edges: list[tuple[int, int]],
    source_count: int = 64,
) -> list[int]:
    if target < 0 or target >= min(source_count, 64):
        return []
    reachable = {target}
    changed = True
    while changed:
        changed = False
        for source_index, target_index in edges:
            if source_index >= min(source_count, 64) or target_index >= min(source_count, 64):
                continue
            if source_index in reachable and target_index not in reachable:
                reachable.add(target_index)
                changed = True
    return sorted(reachable)


def wrapper_source() -> str:
    # In each graph, ascending link order makes the first scans discover only
    # one new source at a time, forcing deeper cases through the worklist.
    fixed_functions = "".join(
        closure_function(f"run_{graph['name']}", graph["target"], graph["edges"])
        for graph in FIXED_GRAPHS
    )
    return (
        "mncs 0.18; module probe.project_closure_worklist;\n"
        "use mncs.compiler.project.v1 as project;\n"
        "record ImportLinkBuild { imports: project.ImportList, count: u64 }\n"
        "record FullClosureSummary { closure: project.ProjectImportClosure, import_count: u64 }\n"
        "record GeneratedClosureSummary { closure: project.ProjectImportClosure, import_count: u64 }\n"
        + closure_function("run", 7, REVERSE_CYCLIC_EDGES)
        + closure_function("run_capacity", 63, CAPACITY_EDGES)
        + closure_function("run_dense_four_candidate", 3, [(3, 2), (2, 1), (1, 0)], 4)
        + closure_function("run_dense_four_two_scan_baseline", 3, [(3, 2), (2, 1), (1, 0)])
        + closure_function("run_small_reverse_chain_count_bound", 7, SMALL_REVERSE_CHAIN_EDGES, 8)
        + closure_function("run_small_reverse_chain_capacity_control", 7, SMALL_REVERSE_CHAIN_EDGES)
        + closure_function("run_small_source_edge_count_bound", 3, SMALL_SOURCE_BOUNDARY_EDGES, 4)
        + closure_function("run_small_source_edge_capacity_control", 3, SMALL_SOURCE_BOUNDARY_EDGES)
        + closure_function("run_source_count_out_of_range", 4, [], 4)
        + closure_function("run_invalid_target", 64, [])
        + fixed_functions
        + """fn import_target_for_source(source_index: u64) -> (result: u64) {
  if source_index == 0 { return 0; }
  return source_index - 1;
}
fn append_link_block(source_index: u64, target_index: u64, initial: ImportLinkBuild) -> (result: ImportLinkBuild) {
  iterate i up_to 64 carrying st: ImportLinkBuild = initial {
    let link: project.ImportLink = project.ImportLink {
      source_index: source_index, target_index: target_index,
      module_start: 0, module_end: 0, alias_start: 0, alias_end: 0,
      has_alias: false
    };
    let imports: project.ImportList = project.ImportList.ICons { head: link, tail: st.imports };
    next st = ImportLinkBuild { imports: imports, count: st.count + 1 };
  }
  return st;
}
fn build_full_link_list() -> (result: ImportLinkBuild) {
  let initial: ImportLinkBuild = ImportLinkBuild { imports: project.ImportList.INil, count: 0 };
  iterate i up_to 64 carrying st: ImportLinkBuild = initial {
    let source_index: u64 = 63 - i;
    let target_index: u64 = import_target_for_source(source_index);
    next st = append_link_block(source_index, target_index, st);
  }
  return st;
}
fn append_generated_source(seed: u64, source_index: u64, initial: ImportLinkBuild) -> (result: ImportLinkBuild) {
  let first_target: u64 = (source_index * 13 + seed) % 64;
  let second_target: u64 = (source_index * 29 + seed * 7 + 1) % 64;
  let first_link: project.ImportLink = project.ImportLink {
    source_index: source_index, target_index: first_target,
    module_start: 0, module_end: 0, alias_start: 0, alias_end: 0,
    has_alias: false
  };
  let second_link: project.ImportLink = project.ImportLink {
    source_index: source_index, target_index: second_target,
    module_start: 0, module_end: 0, alias_start: 0, alias_end: 0,
    has_alias: false
  };
  let first_import: project.ImportList = project.ImportList.ICons { head: first_link, tail: initial.imports };
  let imports: project.ImportList = project.ImportList.ICons { head: second_link, tail: first_import };
  return ImportLinkBuild { imports: imports, count: initial.count + 2 };
}
fn build_generated_graph(seed: u64) -> (result: ImportLinkBuild) {
  let initial: ImportLinkBuild = ImportLinkBuild { imports: project.ImportList.INil, count: 0 };
  iterate i up_to 64 carrying st: ImportLinkBuild = initial {
    next st = append_generated_source(seed, i, st);
  }
  return st;
}
fn run_full_link_capacity() -> (result: FullClosureSummary) {
  let built: ImportLinkBuild = build_full_link_list();
  let closure: project.ProjectImportClosure = project.project_import_closure(built.imports, 63, 64);
  return FullClosureSummary { closure: closure, import_count: built.count };
}
fn run_generated(seed: u64) -> (result: GeneratedClosureSummary) {
  let built: ImportLinkBuild = build_generated_graph(seed);
  let closure: project.ProjectImportClosure = project.project_import_closure(built.imports, seed % 64, 64);
  return GeneratedClosureSummary { closure: closure, import_count: built.count };
}
"""
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
            {"module": WRAPPER_MODULE, "function": "run_dense_four_candidate"},
            {"module": WRAPPER_MODULE, "function": "run_dense_four_two_scan_baseline"},
            {"module": WRAPPER_MODULE, "function": "run_small_reverse_chain_count_bound"},
            {"module": WRAPPER_MODULE, "function": "run_small_reverse_chain_capacity_control"},
            {"module": WRAPPER_MODULE, "function": "run_small_source_edge_count_bound"},
            {"module": WRAPPER_MODULE, "function": "run_small_source_edge_capacity_control"},
            {"module": WRAPPER_MODULE, "function": "run_source_count_out_of_range"},
            {"module": WRAPPER_MODULE, "function": "run_invalid_target"},
            {"module": WRAPPER_MODULE, "function": "run_full_link_capacity"},
            *[
                {"module": WRAPPER_MODULE, "function": f"run_{graph['name']}"}
                for graph in FIXED_GRAPHS
            ],
            {"module": WRAPPER_MODULE, "function": "run_generated"},
        ],
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "project-import-closure-worklist-canonical-vm/1",
    }


def target_call(
    function: str,
    step_budget: int = STEP_BUDGET,
    arguments: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": WRAPPER_MODULE, "function": function},
        "arguments": [] if arguments is None else arguments,
        "type_arguments": [],
        "step_budget": step_budget,
    }


def integer_argument(value: int) -> dict:
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


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

        call_specs = [
            {
                "id": "reverse-cyclic-closure", "function": "run",
                "target": 7, "edges": REVERSE_CYCLIC_EDGES,
            },
            {
                "id": "64-source-cyclic-closure", "function": "run_capacity",
                "target": 63, "edges": CAPACITY_EDGES,
            },
            {
                "id": "four-source-dense-first-scan", "function": "run_dense_four_candidate",
                "target": 3, "edges": [(3, 2), (2, 1), (1, 0)], "source_count": 4,
            },
            {
                "id": "four-source-dense-two-scan-baseline", "function": "run_dense_four_two_scan_baseline",
                "target": 3, "edges": [(3, 2), (2, 1), (1, 0)],
            },
            {
                "id": "eight-source-worklist-seed-count-bound", "function": "run_small_reverse_chain_count_bound",
                "target": 7, "edges": SMALL_REVERSE_CHAIN_EDGES, "source_count": 8,
            },
            {
                "id": "eight-source-worklist-seed-capacity-control", "function": "run_small_reverse_chain_capacity_control",
                "target": 7, "edges": SMALL_REVERSE_CHAIN_EDGES,
            },
            {
                "id": "small-source-count-filters-out-of-project-edges", "function": "run_small_source_edge_count_bound",
                "target": 3, "edges": SMALL_SOURCE_BOUNDARY_EDGES, "source_count": 4,
            },
            {
                "id": "64-slot-control-admits-same-edge-indices", "function": "run_small_source_edge_capacity_control",
                "target": 3, "edges": SMALL_SOURCE_BOUNDARY_EDGES,
            },
            {
                "id": "source-count-out-of-range-target", "function": "run_source_count_out_of_range",
                "target": 4, "edges": [], "source_count": 4,
            },
            {
                "id": "out-of-range-target", "function": "run_invalid_target",
                "target": 64, "edges": [],
            },
            {
                "id": "4096-link-cyclic-closure", "function": "run_full_link_capacity",
                "target": 63,
                "edges": [
                    edge
                    for source in range(64)
                    for edge in [(source, source - 1 if source > 0 else 0)]
                    for _ in range(64)
                ],
                "import_count": 4096,
            },
        ]
        for graph in FIXED_GRAPHS:
            call_specs.append({
                "id": graph["name"],
                "function": f"run_{graph['name']}",
                "target": graph["target"],
                "edges": graph["edges"],
            })
        for seed in GENERATED_SEEDS:
            call_specs.append({
                "id": f"generated-64-source-seed-{seed}",
                "function": "run_generated",
                "arguments": [integer_argument(seed)],
                "target": seed % 64,
                "edges": generated_edges(seed),
                "import_count": 128,
            })
        calls = [
            target_call(
                spec["function"],
                FULL_LINK_STEP_BUDGET if spec["id"] == "4096-link-cyclic-closure" else STEP_BUDGET,
                spec.get("arguments"),
            )
            for spec in call_specs
        ]
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
        {
            "id": spec["id"],
            "request": target_call(
                spec["function"],
                FULL_LINK_STEP_BUDGET if spec["id"] == "4096-link-cyclic-closure" else STEP_BUDGET,
                spec.get("arguments"),
            ),
        }
        for spec in call_specs
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
    if len(rows) != len(call_specs) or any(row.get("outcome") != {"kind": "completed"} for row in rows):
        raise AssertionError({"canonical_vm_results": rows})
    actual = []
    for vm_row in rows:
        vm_returned = vm_row.get("record", {}).get("returned", [])
        if len(vm_returned) != 1:
            raise AssertionError({"canonical_vm_returned": vm_returned})
        actual.append(app.decode_wire(app.vm_to_wire(vm_returned[0])))
    for index, (spec, oracle_result, vm_result) in enumerate(zip(call_specs, expected, actual)):
        closure_result = vm_result.get("closure") if "import_count" in spec else vm_result
        manual_reachable = reachable_indices(
            spec["target"], spec["edges"], spec.get("source_count", 64)
        )
        expected_reachable = [value in manual_reachable for value in range(64)]
        if (vm_result != oracle_result
                or closure_result.get("reachable") != expected_reachable
                or closure_result.get("changed") is not False):
            raise AssertionError({
                "scenario": spec["id"],
                "manual_reachable_indices": manual_reachable,
                "stage0": oracle_result,
                "canonical_vm": vm_result,
            })
        if "import_count" in spec and vm_result.get("import_count") != spec["import_count"]:
            raise AssertionError({
                "scenario": spec["id"],
                "expected_import_count": spec["import_count"],
                "generated_import_count": vm_result.get("import_count"),
            })

    peak_rss_kib = None
    for line in vm_run.stderr.splitlines():
        if "Maximum resident set size" in line:
            peak_rss_kib = int(line.split(":", 1)[1].strip().split()[0])
    return {
        "schema_version": "mncs.compiler.project-import-closure-worklist-vm/6",
        "status": "verified",
        "compiler_source_root": str(ROOT),
        "wrapper_module": WRAPPER_MODULE,
        "project_module": PROJECT_MODULE,
        "source_count": len(modules),
        "scenarios": [
            {
                "id": spec["id"],
                "callable": spec["function"],
                "target_source_index": spec["target"],
                "source_count": spec.get("source_count", 64),
                "import_link_count": spec.get("import_count", len(spec["edges"])),
                "expected_reachable_indices": reachable_indices(
                    spec["target"], spec["edges"], spec.get("source_count", 64)
                ),
                "canonical_vm_reachable_indices": [
                    index for index, value in enumerate(
                        (actual[position].get("closure", actual[position])["reachable"])
                    ) if value
                ],
                "canonical_vm_steps": rows[position].get("record", {}).get("usage", {}).get("steps"),
                "canonical_vm_max_memory_cells": rows[position].get("record", {}).get("usage", {}).get("max_memory_cells"),
            }
            for position, spec in enumerate(call_specs)
        ],
        "scenario_count": len(call_specs),
        "generated_graph_seed_count": len(GENERATED_SEEDS),
        "generated_graph_edges_per_seed": 128,
        "worklist_fallback_exercised_by_graph_depth": True,
        "three_way_equivalent": True,
        "stage0_valid": oracle["valid"],
        "stage0_execution_count": len(expected),
        "canonical_vm_execution_count": len(actual),
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
    report = main()
    rendered = json.dumps(report, indent=2, sort_keys=True)
    report_path = BUILD_DIR / "project-import-closure-graph-corpus-result.json"
    temporary_report = report_path.with_suffix(".json.tmp")
    temporary_report.write_text(rendered + "\n")
    os.replace(temporary_report, report_path)
    print(rendered)
