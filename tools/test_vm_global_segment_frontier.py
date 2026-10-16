#!/usr/bin/env python3
"""Verify global segment token cursors through one canonical VM artifact.

Cases are generated from the existing Stage-0-backed segment suite. The
logical-source entrypoints must preserve token kind, span, and diagnostic
across several page layouts in the reference interpreter, Cranelift, and VM.
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

import test_vm_segment as bounded_vm

segment = bounded_vm.test_segment
test_vm_emit = bounded_vm.test_vm_emit
native_app = __import__("test_vm_native_application")
ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.segment.v1"
GLOBAL_FUNCTIONS = ("next_token_global", "significant_global")
PAGE_BOUND = 1024
STRIDE_BOUND = 1024
STRIDES = (1, 7, 64, 256)
SAMPLE_INDICES = (2, 5, 6, 7, 8, 9, 10, 13, 15)
STEP_BUDGET = 8_000_000
OUT = ROOT / ".build" / "vm-global-segment-frontier.json"
EVIDENCE = ROOT / "evidence" / "self-host-global-segment-canonical-vm-20261016.json"

test_vm_emit.VM_REPO = native_app.VM_ROOT
test_vm_emit.VM_BIN = native_app.VM_BIN


def integer(value: int) -> dict:
    return segment.integer(value)


def nat_arg(value: int) -> dict:
    return segment.nat_arg(value)


def generic_seeds() -> list[dict]:
    return [
        {"module": MODULE, "function": function,
         "type_arguments": [nat_arg(PAGE_BOUND), nat_arg(STRIDE_BOUND)]}
        for function in GLOBAL_FUNCTIONS
    ]


class Probe:
    def __init__(self, backend: str | None):
        env = dict(os.environ)
        env["MNCS_PROBE_MODULES"] = segment.PROBE_MODULES
        env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(generic_seeds())
        env.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build" / "probe-cache"))
        if backend is None:
            env.pop("MNCS_PROBE_BACKEND", None)
        else:
            env["MNCS_PROBE_BACKEND"] = backend
        self.proc = subprocess.Popen(
            [str(bounded_vm.PROBE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT,
        )

    def send(self, request: dict) -> dict:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"probe terminated: {self.proc.poll()}")
        return json.loads(line)

    def close(self) -> str:
        assert self.proc.stdin is not None and self.proc.stderr is not None
        self.proc.stdin.close()
        status = self.proc.wait(timeout=120)
        stderr = self.proc.stderr.read()
        if status != 0:
            raise AssertionError(f"probe exited {status}: {stderr[-2000:]}")
        return stderr


def byte_sequence(raw: bytes) -> dict:
    return {"sequence": {"values": [{"byte": {"value": value}} for value in raw]}}


def pages_wire(raw: bytes, stride: int) -> dict:
    pages = [raw[offset:offset + stride] for offset in range(0, len(raw), stride)]
    return {"sequence": {"values": [byte_sequence(page) for page in pages]}}


def data_from_wire(value: dict) -> bytes:
    return bytes(item["byte"]["value"] for item in value["sequence"]["values"])


def make_request(case: dict, stride: int) -> dict:
    data = data_from_wire(case["args"][0])
    cursor = case["args"][1]["integer"]["value"]
    base_function = case["function"]
    global_function = f"{base_function}_global"
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": global_function},
        "arguments": [pages_wire(data, stride), integer(stride),
                      integer(len(data)), integer(cursor)],
        "type_arguments": [nat_arg(PAGE_BOUND), nat_arg(STRIDE_BOUND)],
        "step_budget": STEP_BUDGET,
    }


def make_vm_case(case: dict, stride: int) -> dict:
    request = make_request(case, stride)
    return {
        "id": f"{case['id']}@stride-{stride}",
        "function": request["target"]["function"],
        "args": request["arguments"],
        "type_args": request["type_arguments"],
        "step_budget": request["step_budget"],
        "expected": case["expected"],
        "source_id": case["id"],
        "stride": stride,
        "source_bytes": len(data_from_wire(case["args"][0])),
        "source_sha256": hashlib.sha256(data_from_wire(case["args"][0])).hexdigest(),
    }


def semantic_digest(cases: list[dict], values: list[dict]) -> str:
    digest = hashlib.sha256()
    for case, value in zip(cases, values):
        digest.update(json.dumps([case["id"], value], sort_keys=True).encode() + b"\n")
    return digest.hexdigest()


def cgroup_snapshot() -> dict:
    values = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            values[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            values[name] = None
    return values


def main() -> None:
    if not bounded_vm.PROBE.is_file():
        raise SystemExit(f"Stage-0 probe is missing: {bounded_vm.PROBE}")
    if not test_vm_emit.VM_BIN.is_file():
        raise SystemExit(f"canonical VM binary is missing: {test_vm_emit.VM_BIN}")

    sample_pool = segment.samples(smoke=False)
    texts = [sample_pool[index] for index in SAMPLE_INDICES]
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    memory_before = cgroup_snapshot()
    started = time.monotonic()
    phase_seconds = {}

    reference = Probe(None)
    try:
        reference_status = reference.send({"execution_status": True})
        if reference_status.get("backend") == "cranelift":
            raise AssertionError(f"reference probe unexpectedly selected Cranelift: {reference_status}")
        all_cases, token_count = bounded_vm.gen_cases(reference, texts, smoke=True)
        base_cases = [case for case in all_cases
                      if case["function"] in ("next_token", "significant")]
        if not base_cases:
            raise AssertionError("Stage-0 segment corpus generated no token cases")
        trivia_boundary_cases = []
        token_kinds = json.loads((ROOT / "src/compiler/token-kinds.json").read_text())
        kind_ids = {value: int(key) for key, value in token_kinds.items()}
        for trivia_count in (2049, 2050):
            raw = b"/**/" * trivia_count + b"x"
            oracle = reference.send({"oracle": raw.decode("ascii")})["lexical"]
            expected_token = oracle["tokens"][min(trivia_count, 2049)]
            span = expected_token["span"]
            diagnostics = {
                (item["span"]["start"], item["span"]["end"]): item["code"]
                for item in oracle["diagnostics"]
            }
            code = diagnostics.get((span["start"], span["end"]))
            expected = {
                "kind": kind_ids[expected_token["kind"]],
                "start": span["start"],
                "end": span["end"],
                "diagnostic": {"MNL001": 1, "MNL002": 2}.get(code, 0),
            }
            trivia_boundary_cases.append({
                "id": f"significant-global-trivia-limit-{trivia_count}",
                "function": "significant",
                "args": [byte_sequence(raw), integer(0)],
                "expected": expected,
                "step_budget": STEP_BUDGET,
            })
        expanded_cases = [(case, stride)
                          for case in base_cases for stride in STRIDES]
        # A 64-byte stride keeps this multi-page limit case within the
        # generated global function's 1024-page type bound.
        expanded_cases.extend((case, 64) for case in trivia_boundary_cases)
        case_grid = [make_vm_case(case, stride)
                     for case, stride in expanded_cases]

        t0 = time.monotonic()
        reference_values, reference_steps = [], []
        for (case, _stride), vm_case in zip(expanded_cases, case_grid):
            result = reference.send({
                "schema_version": "0.1",
                "target": {"module": MODULE, "function": vm_case["function"]},
                "arguments": vm_case["args"],
                "type_arguments": vm_case["type_args"],
                "step_budget": vm_case["step_budget"],
            })
            if result.get("status") != "returned":
                raise AssertionError(f"{vm_case['id']}: reference failed: {result}")
            bounded_vm.check_expected(case, result["returned"])
            reference_values.append(segment.decode(result["returned"][0]))
            reference_steps.append(result.get("steps", 0))

        t_emit = time.monotonic()
        emitted = reference.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError(f"direct emitter returned no canonical VM artifact: {emitted}")
        rows = [row for row in artifact.get("generic_entrypoints", [])
                if row.get("generic_function") in GLOBAL_FUNCTIONS]
        present = {(row.get("generic_function"), tuple(row.get("args_spellings", [])))
                   for row in rows}
        required = {(function, ("1024", "1024")) for function in GLOBAL_FUNCTIONS}
        if not required <= present:
            raise AssertionError(f"artifact omitted global segment entrypoints: {present}")
        phase_seconds["stage0_oracle_and_reference"] = round(t_emit - t0, 6)
        phase_seconds["direct_vm_emission"] = round(time.monotonic() - t_emit, 6)
    finally:
        reference_stderr = reference.close()

    backend = bounded_vm.backend_policy.resolve("segment", {})
    if backend != "cranelift":
        raise AssertionError(f"expected Cranelift segment backend, got {backend}")
    cranelift = Probe(backend)
    cranelift_values, cranelift_steps = [], []
    try:
        status = cranelift.send({"execution_status": True})
        if status.get("backend") != "cranelift":
            raise AssertionError(f"probe did not select Cranelift: {status}")
        t0 = time.monotonic()
        repeated_cases = [case for case, _stride in expanded_cases]
        for case, vm_case, expected in zip(repeated_cases, case_grid, reference_values):
            result = cranelift.send({
                "schema_version": "0.1",
                "target": {"module": MODULE, "function": vm_case["function"]},
                "arguments": vm_case["args"],
                "type_arguments": vm_case["type_args"],
                "step_budget": vm_case["step_budget"],
            })
            if result.get("status") != "returned":
                raise AssertionError(f"{vm_case['id']}: Cranelift failed: {result}")
            bounded_vm.check_expected(case, result["returned"])
            value = segment.decode(result["returned"][0])
            if value != expected:
                raise AssertionError(f"{vm_case['id']}: Cranelift differs from reference")
            cranelift_values.append(value)
            cranelift_steps.append(result.get("steps", 0))
        phase_seconds["cranelift_execution"] = round(time.monotonic() - t0, 6)
    finally:
        cranelift_stderr = cranelift.close()

    vm_values, vm_steps = [], []
    with tempfile.TemporaryDirectory(prefix="mncs-vm-global-segment-") as directory:
        temp = Path(directory)
        artifact_path = temp / "segment-global-cursor.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_rss, calls_bytes, results_bytes = bounded_vm.run_batch(
            artifact_path, case_grid, temp, module=MODULE
        )
        phase_seconds["canonical_vm_batch"] = round(vm_wall, 6)
        for index, (case, result) in enumerate(zip(case_grid, document["results"])):
            if result.get("id") != case["id"] or result.get("outcome") != {"kind": "completed"}:
                raise AssertionError(f"{case['id']}: canonical VM did not complete: {result}")
            returned = [bounded_vm.vm_to_wire(value)
                        for value in result["record"]["returned"]]
            if len(returned) != 1:
                raise AssertionError(f"{case['id']}: VM returned wrong value count")
            bounded_vm.check_expected(case, returned)
            value = segment.decode(returned[0])
            if value != reference_values[index] or value != cranelift_values[index]:
                raise AssertionError(f"{case['id']}: canonical VM differs")
            vm_values.append(value)
            vm_steps.append(result["record"]["usage"]["steps"])

    digests = {
        "reference_interpreter": semantic_digest(case_grid, reference_values),
        "cranelift": semantic_digest(case_grid, cranelift_values),
        "canonical_vm": semantic_digest(case_grid, vm_values),
    }
    if len(set(digests.values())) != 1:
        raise AssertionError(f"executor semantic digests differ: {digests}")

    compiler_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    compiler_branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=ROOT, text=True
    ).strip()
    stage0_revision = json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"]
    stage0_sha = hashlib.sha256(bounded_vm.PROBE.read_bytes()).hexdigest()
    vm_sha = hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest()
    vm_repo = test_vm_emit.VM_BIN.parents[2]
    vm_revision = subprocess.check_output(
        ["git", "-C", str(vm_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    source_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in ("source", "lexer", "segment")
    }
    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after_children.ru_utime - before_children.ru_utime
                 + after_children.ru_stime - before_children.ru_stime)
    whole_wall = round(time.monotonic() - started, 6)
    harness_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    expected_modes = {case["function"] for case in base_cases}
    input_hashes = sorted({case["source_sha256"] for case in case_grid})
    step_summaries = {
        name: {"count": len(values), "total": sum(values), "maximum": max(values, default=0)}
        for name, values in (
            ("reference_interpreter", reference_steps),
            ("cranelift", cranelift_steps),
            ("canonical_vm", vm_steps),
        )
    }
    per_function_case_counts = {
        function: sum(case["function"] == f"{function}_global" for case in case_grid)
        for function in sorted(expected_modes)
    }
    per_stride_case_counts = {
        str(stride): sum(case["stride"] == stride for case in case_grid)
        for stride in STRIDES
    }
    report = {
        "schema_version": 1,
        "kind": "mncs-compiler-global-segment-canonical-vm-frontier",
        "compiler_revision_tested": compiler_revision,
        "compiler_branch": compiler_branch,
        "reference_backend": reference_status.get("backend") or "reference_interpreter",
        "stage0_revision": stage0_revision,
        "stage0_probe_sha256": stage0_sha,
        "selected_modules": ["source", "lexer", "segment"],
        "module_source_sha256": source_hashes,
        "target_module": MODULE,
        "target_functions": sorted(f"{name}_global" for name in expected_modes),
        "test_harness_sha256": harness_sha,
        "artifact_identity": artifact["artifact_id"],
        "specializations": [row.get("args_spellings") for row in rows],
        "input_count": len(input_hashes),
        "input_sha256": input_hashes,
        "base_stage0_cases": len(base_cases),
        "page_strides": list(STRIDES),
        "case_count": len(case_grid),
        "per_function_case_counts": per_function_case_counts,
        "per_stride_case_counts": per_stride_case_counts,
        "executor_case_counts": {
            "reference_interpreter": len(reference_values),
            "cranelift": len(cranelift_values),
            "canonical_vm": len(vm_values),
        },
        "semantic_digests": digests,
        "stage0_generated_token_count": token_count,
        "cases": [
            {"id": case["id"], "source_id": case["source_id"],
             "stride": case["stride"], "source_bytes": case["source_bytes"],
             "source_sha256": case["source_sha256"],
             "expected": case["expected"]}
            for case in case_grid
        ],
        "steps": {
            "reference_interpreter": reference_steps,
            "cranelift": cranelift_steps,
            "canonical_vm": vm_steps,
        },
        "timing_seconds": {**phase_seconds, "whole_frontier": whole_wall},
        "resource_measurements": {
            "child_cpu_seconds": round(child_cpu, 6),
            "child_peak_rss_kib": after_children.ru_maxrss,
            "canonical_vm_peak_rss_kib": vm_rss,
            "canonical_vm_calls_bytes": calls_bytes,
            "canonical_vm_results_bytes": results_bytes,
            "cgroup_before": memory_before,
            "cgroup_after": cgroup_snapshot(),
            "cgroup_measurements_are_ambient": True,
        },
        "scope": (
            "Selected segment.next_token_global and significant_global cases from the "
            "existing Stage-0-backed segment corpus preserve token kinds, spans, and "
            "diagnostics across page strides 1, 7, 64, and 256. Stage-0 expectations, "
            "reference interpreter, Cranelift, and one fresh canonical VM artifact agree. "
            "This is a selected cursor frontier, not full segment/lexer module verification."
        ),
        "stage1_claimed": False,
        "stage2_claimed": False,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    evidence = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "global-segment-canonical-vm-current-source",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "claim_boundary": report["scope"],
        "compiler": {
            "tested_source_revision": compiler_revision,
            "branch": compiler_branch,
            "selected_worktree": str(ROOT),
            "selected_modules": report["selected_modules"],
            "module_source_sha256": source_hashes,
            "target_module": MODULE,
            "target_functions": report["target_functions"],
            "test_harness": "tools/test_vm_global_segment_frontier.py",
            "test_harness_sha256": harness_sha,
        },
        "toolchains": {
            "stage0_probe": str(bounded_vm.PROBE),
            "stage0_probe_sha256": stage0_sha,
            "stage0_revision": stage0_revision,
            "vm_binary": str(test_vm_emit.VM_BIN),
            "vm_binary_sha256": vm_sha,
            "vm_revision": vm_revision,
        },
        "verification": {
            "executor_case_counts": report["executor_case_counts"],
            "semantic_digests": digests,
            "stage0_checks": "all generated token kinds, spans, diagnostics, and significant-token outcomes passed",
            "stage1_claimed": False,
            "stage2_claimed": False,
        },
        "measurements": {
            "timing_seconds": report["timing_seconds"],
            "resource_measurements": report["resource_measurements"],
        },
        "raw_test_report": {
            key: value for key, value in report.items()
            if key not in ("cases", "steps")
        },
        "step_summaries": step_summaries,
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps({
        "status": "passed",
        "kind": report["kind"],
        "compiler_revision_tested": compiler_revision,
        "case_count": len(case_grid),
        "per_function_case_counts": per_function_case_counts,
        "per_stride_case_counts": per_stride_case_counts,
        "executor_case_counts": report["executor_case_counts"],
        "semantic_digests": digests,
        "artifact_identity": artifact["artifact_id"],
        "timing_seconds": report["timing_seconds"],
        "resource_measurements": report["resource_measurements"],
        "evidence_sha256": hashlib.sha256(EVIDENCE.read_bytes()).hexdigest(),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
