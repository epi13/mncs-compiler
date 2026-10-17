#!/usr/bin/env python3
"""Run the source -> verified Flow -> value SSA handoff on the canonical VM.

Stage-0 provides an independent SSA shape oracle and emits the current-source
artifact. The MNCS wrapper under test invokes Flow and passes its typed result
directly into SSA inside one canonical VM call.
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
TYPE_ARGS = [
    {"kind": "nat", "value": test_flow.PAGE_BOUND},
    {"kind": "nat", "value": test_flow.STRIDE_BOUND},
]
STEP_BUDGET = 8_000_000
SOURCE = next(case[1] for case in test_flow.CASES if case[0] == "fallthrough_join")
PROBE = Path(os.environ.get(
    "MNCS_PROBE_BIN", ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe"
))
MODULE_NAMES = ["source", "lexer", "parser", "segment", "decl", "flow", "ssa"]


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


def artifact_cache_identity() -> tuple[Path, Path, str, dict]:
    cache_root = ROOT / ".build" / "flow-ssa-frontier-cache"
    artifact_path = cache_root / "artifact.json"
    manifest_path = cache_root / "manifest.json"
    inputs = {
        "module_source_sha256": {
            name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
            for name in MODULE_NAMES
        },
        "project_manifest_sha256": hashlib.sha256((ROOT / ".mncs/project.json").read_bytes()).hexdigest(),
        "stage0_lock_sha256": hashlib.sha256((ROOT / "mncs-language.lock.json").read_bytes()).hexdigest(),
        "stage0_probe_sha256": hashlib.sha256(PROBE.read_bytes()).hexdigest(),
        "module": MODULE,
        "generic_seed": {"function": FUNCTION, "type_arguments": [1024, 1024]},
    }
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return artifact_path, manifest_path, key, inputs


def main() -> None:
    if not PROBE.exists():
        raise SystemExit(f"Stage-0 probe missing: {PROBE}")
    host_before = memory_snapshot()
    cgroup_before = cgroup_snapshot()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    source = SOURCE
    data = source.encode()
    arguments = test_flow.logical_args(data)

    probe = Probe()
    oracle = probe.send({"ssa": source})
    if oracle.get("ssa") is None or oracle.get("diagnostics"):
        raise AssertionError(f"Stage-0 SSA oracle rejected the selected source: {oracle}")
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
    if oracle_shape != {"functions": 1, "branches": 1, "returns": 2}:
        raise AssertionError(f"Stage-0 SSA fixture changed: {oracle_shape}")

    artifact_path, cache_manifest_path, cache_key, cache_inputs = artifact_cache_identity()
    artifact_cache_hit = False
    artifact = None
    if artifact_path.exists() and cache_manifest_path.exists():
        try:
            cached_manifest = json.loads(cache_manifest_path.read_text())
            cached_bytes = artifact_path.read_bytes()
            candidate = json.loads(cached_bytes)
            if (
                cached_manifest.get("schema_version") == "mncs.compiler.canonical-artifact-cache/1"
                and cached_manifest.get("cache_key") == cache_key
                and cached_manifest.get("inputs") == cache_inputs
                and cached_manifest.get("artifact_sha256") == hashlib.sha256(cached_bytes).hexdigest()
                and cached_manifest.get("artifact_id") == candidate.get("artifact_id")
                and candidate.get("schema_version") == "mncs.vm.artifact/1"
            ):
                artifact = candidate
                artifact_cache_hit = True
        except (OSError, ValueError, TypeError):
            artifact = None
    if artifact is None:
        emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError(f"Stage-0 did not emit a canonical SSA artifact: {emitted}")
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_bytes = json.dumps(artifact, sort_keys=True) + "\n"
        artifact_path.write_text(artifact_bytes)
        cache_manifest_path.write_text(json.dumps({
            "schema_version": "mncs.compiler.canonical-artifact-cache/1",
            "cache_key": cache_key,
            "inputs": cache_inputs,
            "artifact_id": artifact["artifact_id"],
            "artifact_sha256": hashlib.sha256(artifact_bytes.encode()).hexdigest(),
        }, indent=2) + "\n")
    probe_stderr = probe.close()

    batch_case = {
        "id": "ssa.lower_flow_unit_value_ssa:fallthrough_join",
        "function": FUNCTION,
        "args": arguments,
        "type_args": TYPE_ARGS,
        "step_budget": STEP_BUDGET,
    }
    with tempfile.TemporaryDirectory(prefix="mncs-vm-flow-ssa-") as directory_name:
        directory = Path(directory_name)
        vm_artifact_path = directory / "flow-ssa-current.json"
        vm_artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_kb, calls_bytes, results_bytes = run_batch(
            vm_artifact_path, [batch_case], directory, module=MODULE
        )

    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different Flow-to-SSA artifact")
    result = document.get("results", [None])[0]
    if result is None or result.get("id") != batch_case["id"] or result.get("outcome") != {"kind": "completed"}:
        raise AssertionError(f"canonical VM failed Flow-to-SSA execution: {result}")
    returned = result["record"].get("returned", [])
    if len(returned) != 1:
        raise AssertionError(f"canonical VM returned the wrong value count: {result}")
    summary_wire = vm_to_wire(returned[0])
    summary = test_flow.decode(summary_wire)
    if not summary["valid"] or summary["function_count"] != 1:
        raise AssertionError(f"Flow-to-SSA summary is invalid: {summary}")
    if summary["supported_function_count"] != 1 or summary["verified_function_count"] != 1:
        raise AssertionError(f"Flow-to-SSA did not verify its selected function: {summary}")
    functions = wire_linked(field(summary_wire, "functions"))
    actual_shape = {
        "functions": len(functions),
        "branches": 0,
        "returns": 0,
    }
    for function in functions:
        for block in wire_linked(field(function, "blocks")):
            terminator = field(block, "terminator")["finite"]["discriminant"]
            actual_shape["branches"] += terminator == 0
            actual_shape["returns"] += terminator == 2
    if actual_shape != oracle_shape:
        raise AssertionError(f"canonical VM SSA shape differs from Stage-0: {actual_shape} != {oracle_shape}")

    module_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in MODULE_NAMES
    }
    vm_repo = test_vm_emit.VM_REPO
    vm_binary = test_vm_emit.VM_BIN
    vm_revision = subprocess.check_output(["git", "-C", str(vm_repo), "rev-parse", "HEAD"], text=True).strip()
    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (child_after.ru_utime - child_before.ru_utime) + (child_after.ru_stime - child_before.ru_stime)
    native_value = {
        "summary": summary,
        "shape": actual_shape,
        "execution_steps": result["record"]["usage"]["steps"],
    }
    report = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "flow-to-ssa-canonical-vm-frontier",
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
            "function": f"{FUNCTION}<1024,1024> -> flow.lower_unit -> lower_value_ssa",
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "source_bytes": len(data),
            "artifact_id": artifact["artifact_id"],
            "artifact_cache_key": cache_key,
            "artifact_cache_sha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
            "stage0_shape": oracle_shape,
        },
        "verification": {
            "canonical_vm_calls": 1,
            "artifact_cache_hit": artifact_cache_hit,
            "canonical_vm_semantic_digest": semantic_digest(native_value),
            "canonical_vm_execution_steps": native_value["execution_steps"],
            "canonical_vm_ssa_shape": actual_shape,
            "source_to_flow_to_ssa_executed_in_one_vm_call": True,
            "flow_result_passed_to_value_ssa_by_mncs_source": True,
            "verified_functions": summary["verified_function_count"],
            "stage0_shape_matches_canonical_vm": True,
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
            "This selected current-source canonical VM call starts from logical source pages, "
            "runs flow.lower_unit, derives local nominal identities from the returned proof unit, "
            "and passes that exact Flow result to value SSA. Its verified SSA function count and "
            "branch/return shape match the pinned Stage-0 oracle. It does not establish full SSA "
            "module proof, Language-owned verified-fact reuse, Cranelift differential coverage, "
            "or compiler succession."
        ),
    }
    report["evidence_artifact"] = "evidence/self-host-flow-ssa-canonical-vm-20261016.json"
    output = ROOT / os.environ.get("MNCS_FLOW_SSA_FRONTIER_EVIDENCE_PATH", report["evidence_artifact"])
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
