#!/usr/bin/env python3
"""Run the source -> verified Flow -> value SSA handoff on the canonical VM.

Stage-0 supplies an independent SSA shape oracle. Compiler application-build
seals the selected wrapper and reachable generic specialization; the wrapper
invokes Flow and passes its typed result directly into SSA inside each VM call.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_flow
from test_vm_segment import run_batch, vm_to_wire, test_vm_emit


ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.ssa.v1"
FUNCTION = "lower_flow_unit_value_ssa"
APP_MODULE = "demo.flow_ssa_frontier"
APP_FUNCTION = "run"
TYPE_ARGS = [
    {"kind": "nat", "value": test_flow.PAGE_BOUND},
    {"kind": "nat", "value": test_flow.STRIDE_BOUND},
]
STEP_BUDGET = 8_000_000
CASES = [
    (
        "fallthrough_join",
        next(case[1] for case in test_flow.CASES if case[0] == "fallthrough_join"),
        {"functions": 1, "branches": 1, "returns": 2},
        0,
    ),
    (
        "conditional_return_counted_iteration",
        "mncs 0.18; module pressure.flow; fn f(flag: bool) -> (r: u64) { "
        "iterate i up_to 4 carrying state: u64 = 0 { "
        "if flag { return state; } next state = state + 1; "
        "} return state; }",
        {"functions": 1, "branches": 2, "returns": 2},
        1,
    ),
]
PROBE = Path(os.environ.get(
    "MNCS_PROBE_BIN", ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe"
))
MODULE_NAMES = ["source", "lexer", "parser", "segment", "decl", "flow", "ssa"]
APP_SOURCE = (
    "mncs 0.18; module demo.flow_ssa_frontier; use mncs.compiler.ssa.v1 as ssa; "
    "fn run(pages: [[byte; up_to 1024]; up_to 1024], stride: u64, total: u64) "
    "-> (result: ssa.ValueSsaSummary) { "
    "return ssa.lower_flow_unit_value_ssa<1024, 1024>(pages, stride, total); }"
)


def application_build_request() -> dict:
    modules = {
        f"mncs.compiler.{name}.v1": (ROOT / "src/compiler" / f"{name}.mncs").read_text()
        for name in MODULE_NAMES
    }
    return {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "demo/flow_ssa_frontier.mncs",
        "root_source": APP_SOURCE,
        "entry": {"module": APP_MODULE, "function": APP_FUNCTION},
        "modules": modules,
        "generic_seeds": [{
            "module": MODULE,
            "function": FUNCTION,
            "type_arguments": TYPE_ARGS,
        }],
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def evidence_output_path() -> Path:
    configured = os.environ.get("MNCS_FLOW_SSA_FRONTIER_EVIDENCE_PATH")
    if configured:
        output = Path(configured)
        if not output.is_absolute():
            output = ROOT / output
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        output = ROOT / "evidence" / f"self-host-flow-ssa-{stamp}-{os.getpid()}.json"

    try:
        parent = output.parent.resolve(strict=True)
        parent.relative_to(ROOT.resolve())
    except (OSError, ValueError) as error:
        raise SystemExit(
            f"Flow-to-SSA evidence path must stay inside the Compiler checkout: {output}"
        ) from error
    output = parent / output.name
    if output.exists() or output.is_symlink():
        raise SystemExit(f"refusing to overwrite immutable evidence: {output}")
    return output


class Probe:
    """Stage-0 oracle and current-source canonical artifact emitter."""

    def __init__(self) -> None:
        env = dict(os.environ)
        env.pop("MNCS_PROBE_BACKEND", None)
        env["MNCS_PROBE_MODULES"] = ",".join(MODULE_NAMES)
        env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
            "module": MODULE,
            "function": FUNCTION,
            "type_arguments": TYPE_ARGS,
        }])
        env.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build/probe-cache"))
        env.setdefault(
            "MNCS_PROBE_APPLICATION_CACHE_DIR",
            str(ROOT / ".build/flow-ssa-frontier-application-cache"),
        )
        env["MNCS_PROBE_TOOLCHAIN_IDENTITY_ROOT"] = str(ROOT)
        self.proc = subprocess.Popen(
            [str(PROBE)], cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )

    def send(self, request: dict) -> dict:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"Stage-0 probe stopped: {self.proc.poll()}")
        return json.loads(line)

    def close(self) -> str:
        assert self.proc.stdin is not None and self.proc.stderr is not None
        self.proc.stdin.close()
        stderr = self.proc.stderr.read()
        result = self.proc.wait(timeout=30)
        if result != 0:
            raise RuntimeError(f"Stage-0 probe exited {result}: {stderr[-2000:]}")
        return stderr


def memory_snapshot() -> dict | None:
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, raw = line.split(":", 1)
            if key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    if set(values) != {"MemTotal", "MemAvailable"}:
        return None
    return {"total_bytes": values["MemTotal"], "available_bytes": values["MemAvailable"]}


def cgroup_snapshot() -> dict:
    result = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            result[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            result[name] = None
    return result


def field(wire: dict, name: str) -> dict:
    return dict(wire["record"]["fields"])[name]


def wire_linked(value: dict) -> list[dict]:
    items = []
    while value["finite"]["discriminant"] == 1:
        fields = dict(value["finite"]["payload"])
        items.append(fields["head"])
        value = fields["tail"]
    assert value["finite"]["discriminant"] == 0, value
    return items


def semantic_digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main() -> None:
    output = evidence_output_path()
    if not PROBE.exists():
        raise SystemExit(f"Stage-0 probe missing: {PROBE}")
    host_before = memory_snapshot()
    cgroup_before = cgroup_snapshot()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    probe = Probe()
    cases = []
    for name, source, expected_shape, expected_iterations in CASES:
        oracle = probe.send({"ssa": source})
        if oracle.get("ssa") is None or oracle.get("diagnostics"):
            raise AssertionError(f"Stage-0 SSA oracle rejected {name}: {oracle}")
        oracle_functions = oracle["ssa"]["functions"]
        oracle_shape = {
            "functions": len(oracle_functions),
            "branches": sum(
                "ConditionalBranch" in block["terminator"]
                for function in oracle_functions for block in function["blocks"]
            ),
            "returns": sum(
                "Return" in block["terminator"]
                for function in oracle_functions for block in function["blocks"]
            ),
        }
        if oracle_shape != expected_shape:
            raise AssertionError(f"Stage-0 SSA fixture changed for {name}: {oracle_shape}")
        iterations = [
            loop for function in oracle_functions
            for loop in function.get("bounded_iterations", [])
        ]
        if len(iterations) != expected_iterations:
            raise AssertionError(f"Stage-0 iteration facts changed for {name}: {iterations}")
        if any(not loop.get("header") or not loop.get("backedge") for loop in iterations):
            raise AssertionError(f"Stage-0 did not retain the counted-loop cycle for {name}: {iterations}")
        data = source.encode()
        cases.append({
            "name": name,
            "source": source,
            "source_bytes": len(data),
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "arguments": test_flow.logical_args(data),
            "oracle_shape": oracle_shape,
            "expected_iterations": expected_iterations,
            "stage0_iterations": iterations,
        })

    build = probe.send({"compile_application": application_build_request()})
    if (
        build.get("schema_version") != "mncs.compiler.application-build/4"
        or build.get("status") != "verified"
        or build.get("entry", {}).get("status") != "verified"
        or build.get("unresolved_obligations") != []
    ):
        raise AssertionError(f"selected Flow-to-SSA application did not verify: {json.dumps({
            "schema_version": build.get("schema_version"),
            "status": build.get("status"),
            "entry": build.get("entry"),
            "unresolved_obligations": build.get("unresolved_obligations"),
            "diagnostics": build.get("diagnostics"),
            "cache": build.get("cache"),
        }, sort_keys=True)}")
    artifact = (build.get("artifact") or {}).get("value")
    if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
        raise AssertionError(f"verified Flow-to-SSA application omitted its VM artifact: {build.get('artifact')}")
    artifact_bytes = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    probe_stderr = probe.close()

    batch_cases = [{
        "id": f"flow-ssa-application.run:{case['name']}",
        "function": APP_FUNCTION,
        "args": case["arguments"],
        "type_args": [],
        "step_budget": STEP_BUDGET,
    } for case in cases]
    with tempfile.TemporaryDirectory(prefix="mncs-vm-flow-ssa-") as directory_name:
        directory = Path(directory_name)
        vm_artifact_path = directory / "flow-ssa-current.json"
        vm_artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_kb, calls_bytes, results_bytes = run_batch(
            vm_artifact_path, batch_cases, directory, module=APP_MODULE
        )

    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different Flow-to-SSA artifact")
    results = document.get("results", [])
    if len(results) != len(cases):
        raise AssertionError(f"canonical VM returned {len(results)} results for {len(cases)} SSA cases")
    native_values = []
    for case, batch_case, result in zip(cases, batch_cases, results, strict=True):
        if result.get("id") != batch_case["id"] or result.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed Flow-to-SSA case {case['name']}: {result}")
        returned = result["record"].get("returned", [])
        if len(returned) != 1:
            raise AssertionError(f"canonical VM returned the wrong value count for {case['name']}: {result}")
        summary_wire = vm_to_wire(returned[0])
        summary = test_flow.decode(summary_wire)
        if not summary["valid"] or summary["function_count"] != 1:
            raise AssertionError(f"Flow-to-SSA summary is invalid for {case['name']}: {summary}")
        if summary["supported_function_count"] != 1 or summary["verified_function_count"] != 1:
            raise AssertionError(f"Flow-to-SSA did not verify {case['name']}: {summary}")
        functions = wire_linked(field(summary_wire, "functions"))
        actual_shape = {"functions": len(functions), "branches": 0, "returns": 0}
        for function in functions:
            for block in wire_linked(field(function, "blocks")):
                terminator = field(block, "terminator")["finite"]["discriminant"]
                actual_shape["branches"] += terminator == 0
                actual_shape["returns"] += terminator == 2
        if actual_shape != case["oracle_shape"]:
            raise AssertionError(
                f"canonical VM SSA shape differs from Stage-0 for {case['name']}: "
                f"{actual_shape} != {case['oracle_shape']}"
            )
        native_values.append({
            "name": case["name"],
            "summary": summary,
            "shape": actual_shape,
            "execution_steps": result["record"]["usage"]["steps"],
        })

    module_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in MODULE_NAMES
    }
    vm_repo = test_vm_emit.VM_REPO
    vm_binary = test_vm_emit.VM_BIN
    vm_revision = subprocess.check_output(["git", "-C", str(vm_repo), "rev-parse", "HEAD"], text=True).strip()
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (child_after.ru_utime - child_before.ru_utime) + (child_after.ru_stime - child_before.ru_stime)
    report = {
        "schema_version": "mncs.compiler.self-consumption-frontier/2",
        "kind": "flow-to-ssa-application-canonical-vm-frontier",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "compiler": {
            "tested_source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "branch": subprocess.check_output(["git", "branch", "--show-current"], text=True).strip(),
            "selected_worktree": str(ROOT),
            "selected_modules": MODULE_NAMES,
            "module_source_sha256": module_hashes,
            "test_harness": "tools/test_vm_flow_ssa_frontier.py",
            "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "toolchains": {
            "stage0_probe": str(PROBE),
            "stage0_probe_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "vm_binary": str(vm_binary),
            "vm_binary_sha256": hashlib.sha256(vm_binary.read_bytes()).hexdigest(),
            "vm_repository": str(vm_repo),
            "vm_revision": vm_revision,
        },
        "workload": {
            "module": MODULE,
            "application_entry": f"{APP_MODULE}::{APP_FUNCTION}",
            "selected_generic_call": f"{MODULE}::{FUNCTION}<1024,1024> -> flow.lower_unit -> lower_value_ssa",
            "cases": [{
                "name": case["name"],
                "source_sha256": case["source_sha256"],
                "source_bytes": case["source_bytes"],
                "stage0_ssa_shape": case["oracle_shape"],
                "expected_iterations": case["expected_iterations"],
                "stage0_iteration_facts": case["stage0_iterations"],
            } for case in cases],
            "artifact_id": artifact["artifact_id"],
            "artifact_json_bytes": len(artifact_bytes),
            "artifact_json_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
        },
        "verification": {
            "application_build_schema": build.get("schema_version"),
            "application_build_status": build.get("status"),
            "application_entry_status": build.get("entry", {}).get("status"),
            "application_cache_status": build.get("cache", {}).get("status"),
            "application_unresolved_obligations": build.get("unresolved_obligations"),
            "canonical_vm_calls": len(native_values),
            "canonical_vm_batch_case_count": len(native_values),
            "canonical_vm_semantic_digest": semantic_digest(native_values),
            "canonical_vm_execution_steps": {
                value["name"]: value["execution_steps"] for value in native_values
            },
            "canonical_vm_ssa_shapes": [
                {"name": value["name"], **value["shape"]} for value in native_values
            ],
            "source_to_flow_to_ssa_executed_in_one_vm_batch": True,
            "flow_result_passed_to_value_ssa_by_mncs_source": True,
            "verified_functions": sum(
                value["summary"]["verified_function_count"] for value in native_values
            ),
            "stage0_shape_matches_canonical_vm": all(
                value["shape"] == case["oracle_shape"]
                for value, case in zip(native_values, cases, strict=True)
            ),
            "stage0_loop_header_and_backedge_cases": sum(
                bool(case["stage0_iterations"]) for case in cases
            ),
            "language_verified_facts_reused": 0,
            "stage1_claimed": False,
            "stage2_claimed": False,
        },
        "measurements": {
            "canonical_vm_batch_seconds": round(vm_wall, 6),
            "whole_frontier_seconds": round(time.monotonic() - started, 6),
            "child_cpu_seconds": round(child_cpu, 6),
            "child_peak_rss_kib": child_after.ru_maxrss,
            "canonical_vm_peak_rss_kib": vm_peak_kb,
            "host_memory_before": host_before,
            "cgroup_memory_before": cgroup_before,
            "cgroup_memory_after": cgroup_snapshot(),
            "canonical_vm_batch_bytes": {"calls": calls_bytes, "results": results_bytes},
            "stage0_probe_stderr_sha256": hashlib.sha256(probe_stderr.encode()).hexdigest(),
            "cgroup_peak_attributable_to_test": False,
        },
        "claim_boundary": (
            "This selected application artifact contains the wrapper entry, its reachable compiler "
            "callable closure, and the explicit lower_flow_unit_value_ssa<1024,1024> specialization. "
            "The canonical VM batch starts each case from logical source pages; MNCS source runs "
            "flow.lower_unit, derives local nominal identities from the returned proof unit, and "
            "passes that exact Flow result to value SSA. Both verified SSA function shapes match the "
            "pinned Stage-0 oracle. It does not establish Language-owned verified-fact reuse, "
            "Cranelift differential coverage, or compiler succession."
        ),
    }
    report["evidence_artifact"] = output.relative_to(ROOT).as_posix()
    try:
        with output.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2) + "\n")
    except FileExistsError as error:
        raise SystemExit(f"refusing to overwrite immutable evidence: {output}") from error
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
