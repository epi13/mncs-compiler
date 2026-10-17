#!/usr/bin/env python3
"""Verify Flow consumes declaration proof results on the canonical VM.

The current MNCS `flow.lower_unit` implementation calls `decl.prove_unit`,
passes that typed result to `flow.lower_proven_unit`, and verifies the emitted
control-flow graph. Stage-0 supplies current diagnostics and SSA branch shape;
the complete MNCS handoff runs from one freshly emitted VM artifact.
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

import test_flow as flow_test
from test_vm_segment import run_batch, vm_to_wire, test_vm_emit


ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.flow.v1"
SELECTED = {
    "fallthrough_join",
    "unreachable_join",
    "bounded_iteration_counted",
    "bounded_iteration_sequence",
    "nested_bounded_iteration",
}
CASES = [case for case in flow_test.CASES if case[0] in SELECTED]
PROBE = Path(os.environ.get(
    "MNCS_PROBE_BIN", ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe"
))


class Probe:
    """Stage-0 oracle and direct artifact emitter, with no native execution."""

    def __init__(self) -> None:
        env = dict(os.environ)
        env.pop("MNCS_PROBE_BACKEND", None)
        env["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow"
        env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
            "module": MODULE,
            "function": "lower_unit",
            "type_arguments": [
                {"kind": "nat", "value": flow_test.PAGE_BOUND},
                {"kind": "nat", "value": flow_test.STRIDE_BOUND},
            ],
        }])
        env.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build/probe-cache"))
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


def semantic_digest(values: list[dict]) -> str:
    state = hashlib.sha256()
    for value in values:
        state.update(json.dumps([value["name"], value["value"]], sort_keys=True).encode() + b"\n")
    return state.hexdigest()


def main() -> None:
    if not PROBE.exists():
        raise SystemExit(f"Stage-0 probe missing: {PROBE}")
    host_before = memory_snapshot()
    cgroup_before = cgroup_snapshot()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    source_bound = max(len(case[1].encode()) for case in CASES)

    probe = Probe()
    cases = []
    for name, raw_source, expected_mnb_count, expected_shape, expected_iterations in CASES:
        source = raw_source.ljust(source_bound)
        if len(source.encode()) > flow_test.STRIDE_BOUND:
            raise AssertionError(f"selected fixture exceeds the current page width: {name}")
        elaboration = probe.send({"elaborate": source})
        expected_diagnostics = flow_test.oracle_diagnostics(elaboration)
        if len(expected_diagnostics) != expected_mnb_count:
            raise AssertionError(
                f"Stage-0 diagnostic count for {name}: {expected_diagnostics} != {expected_mnb_count}"
            )
        stage0_ssa_shape = None
        if expected_shape is not None:
            stage0 = probe.send({"ssa": source})
            if stage0.get("ssa") is None:
                raise AssertionError(f"Stage-0 did not provide SSA shape for {name}: {stage0}")
            functions = stage0["ssa"]["functions"]
            stage0_ssa_shape = {
                "branches": sum(
                    "ConditionalBranch" in block["terminator"]
                    for function in functions for block in function["blocks"]
                ),
                "returns": sum(
                    "Return" in block["terminator"]
                    for function in functions for block in function["blocks"]
                ),
            }
            if tuple(stage0_ssa_shape.values()) != expected_shape:
                raise AssertionError(f"Stage-0 SSA fixture changed for {name}: {stage0_ssa_shape}")
        stage0_iterations = []
        if expected_iterations:
            stage0 = probe.send({"ssa": source})
            if stage0.get("ssa") is None:
                raise AssertionError(f"Stage-0 did not provide bounded-iteration facts for {name}: {stage0}")
            stage0_iterations = [
                loop
                for function in stage0["ssa"]["functions"]
                for loop in function.get("bounded_iterations", [])
            ]
            if len(stage0_iterations) != expected_iterations:
                raise AssertionError(
                    f"Stage-0 bounded-iteration count for {name}: "
                    f"{stage0_iterations} != {expected_iterations}"
                )
        data = source.encode()
        cases.append({
            "name": name,
            "source": source,
            "source_bytes": len(data),
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "expected_mnb_count": expected_mnb_count,
            "expected_diagnostics": expected_diagnostics,
            "expected_shape": expected_shape,
            "stage0_ssa_shape": stage0_ssa_shape,
            "expected_iterations": expected_iterations,
            "stage0_iterations": stage0_iterations,
            "arguments": flow_test.logical_args(data),
            "type_arguments": [
                {"kind": "nat", "value": flow_test.PAGE_BOUND},
                {"kind": "nat", "value": flow_test.STRIDE_BOUND},
            ],
        })

    emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
    artifact = emitted.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
        raise AssertionError(f"Stage-0 did not emit a canonical Flow artifact: {emitted}")
    probe_stderr = probe.close()

    batch_cases = [{
        "id": f"flow.lower_unit:{case['name']}",
        "function": "lower_unit",
        "args": case["arguments"],
        "type_args": case["type_arguments"],
        "step_budget": 8_000_000,
    } for case in cases]
    with tempfile.TemporaryDirectory(prefix="mncs-vm-flow-proof-") as directory_name:
        directory = Path(directory_name)
        artifact_path = directory / "flow-current.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_kb, calls_bytes, results_bytes = run_batch(
            artifact_path, batch_cases, directory, module=MODULE
        )

    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different Flow artifact")
    vm_values = []
    vm_steps = []
    for case, batch_case, result in zip(cases, batch_cases, document.get("results", [])):
        if result.get("id") != batch_case["id"] or result.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {case['name']}: {result}")
        wire = [vm_to_wire(value) for value in result["record"]["returned"]]
        if len(wire) != 1:
            raise AssertionError(f"canonical VM returned the wrong value count for {case['name']}")
        value = flow_test.decode(wire[0])
        native_diagnostics = flow_test.native_diagnostics(value["diagnostics"])
        if native_diagnostics != case["expected_diagnostics"]:
            raise AssertionError(
                f"{case['name']} Flow diagnostics differ from Stage-0: "
                f"{native_diagnostics} != {case['expected_diagnostics']}"
            )
        if value["graph_ok"] is not True or value["valid"] is not (case["expected_mnb_count"] == 0):
            raise AssertionError(f"{case['name']} returned inconsistent Flow validity: {value}")
        shape = flow_test.graph_shape(value)
        if case["expected_shape"] is not None and (
            shape["branches"], shape["returns"]
        ) != case["expected_shape"]:
            raise AssertionError(f"{case['name']} CFG differs from expected shape: {shape}")
        if case["stage0_ssa_shape"] is not None and (
            shape["branches"], shape["returns"]
        ) != (case["stage0_ssa_shape"]["branches"], case["stage0_ssa_shape"]["returns"]):
            raise AssertionError(f"{case['name']} CFG shape differs from Stage-0 SSA")
        if shape["iteration_headers"] != case["expected_iterations"]:
            raise AssertionError(f"{case['name']} iteration headers differ: {shape}")
        vm_values.append({
            "name": case["name"],
            "value": value,
            "diagnostics": native_diagnostics,
            "graph_shape": shape,
        })
        vm_steps.append(result["record"]["usage"]["steps"])

    module_names = ["source", "lexer", "parser", "segment", "decl", "flow"]
    module_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in module_names
    }
    vm_repo = test_vm_emit.VM_REPO
    vm_binary = test_vm_emit.VM_BIN
    vm_revision = subprocess.check_output(
        ["git", "-C", str(vm_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (child_after.ru_utime - child_before.ru_utime) + (
        child_after.ru_stime - child_before.ru_stime
    )
    report = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "flow-proven-unit-canonical-vm-frontier",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "compiler": {
            "tested_source_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "branch": subprocess.check_output(["git", "branch", "--show-current"], text=True).strip(),
            "selected_worktree": str(ROOT),
            "selected_modules": module_names,
            "module_source_sha256": module_hashes,
            "test_harness": "tools/test_vm_flow_proven_frontier.py",
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
            "function": "lower_unit<1024,1024> -> lower_proven_unit<1024,1024>",
            "artifact_id": artifact["artifact_id"],
            "selected_case_count": len(cases),
            "cases": [{
                "name": case["name"],
                "source_bytes": case["source_bytes"],
                "source_sha256": case["source_sha256"],
                "stage0_diagnostics": [list(item) for item in case["expected_diagnostics"]],
                "expected_mnb_count": case["expected_mnb_count"],
                "stage0_ssa_shape": case["stage0_ssa_shape"],
                "expected_iterations": case["expected_iterations"],
                "stage0_bounded_iterations": [{
                    "bound": loop.get("bound"),
                    "domain": loop.get("domain"),
                    "has_header": bool(loop.get("header")),
                    "has_backedge": bool(loop.get("backedge")),
                } for loop in case["stage0_iterations"]],
            } for case in cases],
        },
        "verification": {
            "stage0_diagnostic_cases": len(cases),
            "canonical_vm_flow_cases": len(vm_values),
            "stage0_bounded_iteration_cases": sum(bool(case["stage0_iterations"]) for case in cases),
            "stage0_iteration_metadata_matches_vm": all(
                len(case["stage0_iterations"]) == case["expected_iterations"]
                and vm_values[index]["graph_shape"]["iteration_headers"] == case["expected_iterations"]
                and vm_values[index]["graph_shape"]["iteration_backedges"] == case["expected_iterations"]
                for index, case in enumerate(cases)
            ),
            "canonical_vm_semantic_digest": semantic_digest(vm_values),
            "canonical_vm_execution_steps": vm_steps,
            "canonical_vm_graph_shapes": [
                {"name": item["name"], **item["graph_shape"]} for item in vm_values
            ],
            "proof_passed_to_lower_proven_unit_by_mncs_source": True,
            "stage0_diagnostics_match_vm_flow_diagnostics": True,
            "typed_proof_ops_match_lowered_expression_ops": True,
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
            "This selected current-source Flow frontier executes flow.lower_unit, whose MNCS "
            "implementation passes decl.prove_unit output to flow.lower_proven_unit. The fresh "
            "canonical VM graph preserves typed proof operations, matches selected Stage-0 "
            "diagnostics and SSA shape, reports the bounded unreachable-join diagnostic, and "
            "preserves a counted-iteration header and backedge against Stage-0 metadata. "
            "It does not establish full flow-module proof, Language-owned verified-fact reuse, "
            "Cranelift differential coverage, or compiler succession."
        ),
    }
    output = ROOT / os.environ.get(
        "MNCS_FLOW_PROVEN_FRONTIER_EVIDENCE_PATH",
        "evidence/self-host-flow-proven-unit-canonical-vm-20261016.json",
    )
    report["evidence_artifact"] = output.relative_to(ROOT).as_posix()
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
