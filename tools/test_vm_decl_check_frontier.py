#!/usr/bin/env python3
"""Three-executor differential for the current declaration checker.

This drives the MNCS `decl.check_unit` implementation through its real
parse, duplicate-name, call-resolution/span-walk, and stack-IR checks. Each
selected case runs on the pinned reference interpreter, Cranelift, and one
fresh canonical VM artifact. The expected check stage comes from the focused
declaration suite; this is not a claim that check_unit performs full typing.
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

import backend_policy
import test_vm_decl as decl
from test_vm_segment import run_batch, vm_to_wire, test_vm_emit


ROOT = Path(__file__).resolve().parents[1]
MODULE = decl.MODULE
TYPE_ARGS = decl.TYPE_ARGS
CASES = list(decl.test_decl.CHECKS)


def digest(values: list[object]) -> str:
    h = hashlib.sha256()
    for case, value in zip(CASES, values):
        h.update(json.dumps([case[0], value], sort_keys=True).encode() + b"\n")
    return h.hexdigest()


def make_case(index: int, text: str, stage: int) -> dict:
    data = text.encode()
    return {
        "id": f"decl.check_unit:case-{index:02d}",
        "function": "check_unit",
        "args": decl.test_decl.logical_args(data),
        "type_args": TYPE_ARGS,
        "step_budget": decl.test_decl.EXECUTION_STEP_BUDGET,
        "text": text,
        "expected_stage": stage,
        "source_bytes": len(data),
        "source_sha256": hashlib.sha256(data).hexdigest(),
    }


def check(case: dict, wire: list[dict]) -> dict:
    if len(wire) != 1:
        raise AssertionError(f"{case['id']} returned {len(wire)} values")
    value = decl.test_decl.decode(wire[0])
    decl.test_decl.check_check_unit(value, case["text"], case["expected_stage"])
    return value


def call_request(case: dict) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": case["function"]},
        "arguments": case["args"],
        "type_arguments": case["type_args"],
        "step_budget": case["step_budget"],
    }


def host_memory_snapshot() -> dict | None:
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


def main() -> None:
    if not decl.PROBE.exists():
        raise SystemExit(f"Stage-0 probe missing: {decl.PROBE}")
    host_before = host_memory_snapshot()
    cgroup_before = cgroup_snapshot()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    cache = ROOT / ".build/probe-cache"

    reference = decl.Probe(backend="reference_interpreter", cache_dir=cache)
    cases = [make_case(i, text, stage) for i, (text, stage) in enumerate(CASES)]
    reference_values, reference_steps = [], []
    reference_started = time.monotonic()
    for case in cases:
        result = reference.send(call_request(case))
        if result.get("status") != "returned":
            raise AssertionError(f"reference interpreter failed {case['id']}: {result}")
        reference_steps.append(result["steps"])
        reference_values.append(check(case, result["returned"]))
    emitted = reference.send({"emit_vm_artifact": {"module": MODULE}})
    artifact = emitted.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
        raise AssertionError(f"Stage-0 did not emit a canonical VM artifact: {emitted}")
    reference_wall = time.monotonic() - reference_started
    reference_stderr = reference.close()

    policy_backend = backend_policy.resolve("decl", {})
    native = decl.Probe(backend=policy_backend, cache_dir=cache)
    native_values, native_steps = [], []
    native_started = time.monotonic()
    for case in cases:
        result = native.send(call_request(case))
        if result.get("status") != "returned":
            raise AssertionError(f"{policy_backend} failed {case['id']}: {result}")
        native_steps.append(result["steps"])
        native_values.append(check(case, result["returned"]))
    native_wall = time.monotonic() - native_started
    native_stderr = native.close()

    with tempfile.TemporaryDirectory(prefix="mncs-vm-decl-check-") as tmp_name:
        tmp = Path(tmp_name)
        artifact_path = tmp / "decl-check-current.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        vm_document, vm_wall, vm_peak_kb, calls_bytes, results_bytes = run_batch(
            artifact_path, cases, tmp, module=MODULE
        )

    vm_values, vm_steps = [], []
    for case, result in zip(cases, vm_document["results"]):
        if result.get("id") != case["id"] or result.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {case['id']}: {result}")
        wire = [vm_to_wire(value) for value in result["record"]["returned"]]
        vm_values.append(check(case, wire))
        vm_steps.append(result["record"]["usage"]["steps"])

    digests = {
        "reference_interpreter": digest(reference_values),
        policy_backend: digest(native_values),
        "canonical_vm": digest(vm_values),
    }
    if len(set(digests.values())) != 1:
        raise AssertionError(f"semantic digests differ: {digests}")

    module_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in ("source", "lexer", "parser", "segment", "decl")
    }
    vm_path = test_vm_emit.VM_BIN
    vm_repository = test_vm_emit.VM_REPO
    vm_revision = subprocess.check_output(
        ["git", "-C", str(vm_repository), "rev-parse", "HEAD"], text=True
    ).strip()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after.ru_utime - child_before.ru_utime) + (after.ru_stime - child_before.ru_stime)
    finished = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    cases_out = [
        {key: case[key] for key in ("id", "expected_stage", "source_bytes", "source_sha256")}
        for case in cases
    ]
    verification = {
        "executor_case_counts": {
            "reference_interpreter": len(reference_values),
            policy_backend: len(native_values),
            "canonical_vm": len(vm_values),
        },
        "expected_check_stages": [case["expected_stage"] for case in cases],
        "returned_check_results": [
            {"stage": value["stage"], "ok": value["ok"], "ir_ok": value["ir_ok"],
             "fn_count": value["fn_count"], "expr_count": value["expr_count"],
             "err_span": [value["err_start"], value["err_end"]]}
            for value in vm_values
        ],
        "semantic_digests": digests,
        "execution_steps": {
            "reference_interpreter": reference_steps,
            policy_backend: native_steps,
            "canonical_vm": vm_steps,
        },
        "language_verified_facts_reused": 0,
        "stage1_claimed": False,
        "stage2_claimed": False,
    }
    measurements = {
        "reference_interpreter_cases_and_artifact_emission_seconds": round(reference_wall, 6),
        f"{policy_backend}_cases_seconds": round(native_wall, 6),
        "canonical_vm_batch_seconds": round(vm_wall, 6),
        "whole_frontier_seconds": round(time.monotonic() - started, 6),
        "child_cpu_seconds": round(child_cpu, 6),
        "child_peak_rss_kib": after.ru_maxrss,
        "canonical_vm_peak_rss_kib": vm_peak_kb,
        "host_memory_before": host_before,
        "cgroup_memory_before": cgroup_before,
        "cgroup_memory_after": cgroup_snapshot(),
        "canonical_vm_batch_bytes": {"calls": calls_bytes, "results": results_bytes},
        "cgroup_peak_attributable_to_test": False,
    }
    probe_path = Path(decl.PROBE)
    report = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "decl-check-unit-canonical-vm-frontier",
        "observed_at": finished,
        "status": "passed",
        "compiler": {
            "tested_source_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "branch": subprocess.check_output(
                ["git", "branch", "--show-current"], text=True
            ).strip(),
            "selected_worktree": str(ROOT),
            "selected_modules": ["source", "lexer", "parser", "segment", "decl"],
            "module_source_sha256": module_hashes,
            "test_harness": "tools/test_vm_decl_check_frontier.py",
            "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "decl_source_sha256": module_hashes["decl"],
        },
        "toolchains": {
            "stage0_probe": str(probe_path),
            "stage0_probe_sha256": hashlib.sha256(probe_path.read_bytes()).hexdigest(),
            "stage0_revision": decl.stage0_revision(),
            "native_backend": policy_backend,
            "vm_binary": str(vm_path),
            "vm_binary_sha256": hashlib.sha256(vm_path.read_bytes()).hexdigest(),
            "vm_repository": str(vm_repository),
            "vm_revision": vm_revision,
        },
        "workload": {
            "module": MODULE,
            "function": "check_unit<1024,1024>",
            "artifact_id": artifact["artifact_id"],
            "selected_case_count": len(cases),
            "cases": cases_out,
        },
        "verification": verification,
        "measurements": measurements,
        "claim_boundary": (
            "The current decl.check_unit implementation parses each complete selected unit, "
            "checks duplicate function names, resolves function calls and source spans, and "
            "validates structural stack IR. It does not establish full type checking, Language "
            "verified-fact reuse, full decl-module verification, or compiler succession."
        ),
        "raw_executor_notes": {
            "reference_interpreter_stderr_sha256": hashlib.sha256(reference_stderr.encode()).hexdigest(),
            "native_backend_stderr_sha256": hashlib.sha256(native_stderr.encode()).hexdigest(),
        },
    }
    evidence_path = ROOT / os.environ.get(
        "MNCS_DECL_CHECK_FRONTIER_EVIDENCE_PATH",
        "evidence/self-host-decl-check-unit-canonical-vm-20261016.json",
    )
    evidence_path.write_text(json.dumps(report, indent=2) + "\n")
    performance_path = ROOT / ".build/vm-efficiency/decl-check-frontier.json"
    performance_path.parent.mkdir(parents=True, exist_ok=True)
    performance_path.write_text(json.dumps(report, indent=2) + "\n")
    report["evidence_artifact"] = evidence_path.relative_to(ROOT).as_posix()
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
