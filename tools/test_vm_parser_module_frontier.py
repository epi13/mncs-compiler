#!/usr/bin/env python3
"""Verify parser.header and parser.header_global as direct canonical VM roots.

The existing parser frontier called these routines through kernel wrappers.
This test selects the parser-owned functions as artifact roots, compares them
with the Stage-0 project oracle and Cranelift, and runs the emitted artifact in
the canonical VM across bounded and paged header inputs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time

import test_vm_parser_global_frontier as prior
import test_vm_segment

frontend = prior.frontend
test_vm_emit = prior.test_vm_emit
ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.parser.v1"
FUNCTIONS = ("header", "finish", "step", "header_global", "finish_global", "step_global")
HEADER_FIELDS = (
    "code", "start", "end", "version_start", "version_end",
    "module_start", "module_end", "next_offset", "state",
)
BOUND = 1024
PAGE_BOUND = 1024
STRIDES = prior.STRIDES
STEP_BUDGET = prior.STEP_BUDGET
EVIDENCE = ROOT / "evidence" / "self-host-parser-module-canonical-vm-frontier-20261017.json"
BASE_GLOBAL_SEEDS = prior.global_seeds


def module_seeds() -> list[dict]:
    bounded = ("header", "finish", "step")
    global_functions = ("header_global", "finish_global", "step_global")
    return BASE_GLOBAL_SEEDS() + [
        {
            "module": MODULE,
            "function": function,
            "type_arguments": [frontend.nat_arg(BOUND)],
        }
        for function in bounded
    ] + [
        {
            "module": MODULE,
            "function": function,
            "type_arguments": [frontend.nat_arg(PAGE_BOUND),
                               frontend.nat_arg(BOUND)],
        }
        for function in global_functions
    ]


def open_probe(backend: str | None):
    original = prior.global_seeds
    prior.global_seeds = module_seeds
    try:
        return prior.Probe(backend)
    finally:
        prior.global_seeds = original


def request_bounded(raw: bytes, *, module: str = MODULE) -> dict:
    request = prior.request_bounded(raw)
    request["target"] = {"module": module, "function": "header"}
    request["type_arguments"] = [frontend.nat_arg(BOUND)]
    request["step_budget"] = STEP_BUDGET
    return request


def request_global(raw: bytes, stride: int, *, module: str = MODULE) -> dict:
    request = prior.request_global(raw, stride)
    request["target"] = {"module": module, "function": "header_global"}
    request["type_arguments"] = [frontend.nat_arg(PAGE_BOUND),
                                 frontend.nat_arg(BOUND)]
    request["step_budget"] = STEP_BUDGET
    return request


def header_value(identity: str, **changes: int) -> dict:
    values = {field: 0 for field in HEADER_FIELDS}
    values.update(changes)
    return {
        "record": {
            "type_identity": identity,
            "name": "Header",
            "fields": [[field, frontend.integer(values[field])] for field in HEADER_FIELDS],
        }
    }


def request_helper(case: dict) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": case["function"]},
        "arguments": case["args"],
        "type_arguments": case["type_args"],
        "step_budget": case["step_budget"],
    }


def helper_cases(probe, raw: bytes) -> list[dict]:
    records = probe.send({"record_types": MODULE})
    header_rows = [row for row in records if row.get("name") == "Header"]
    if len(header_rows) != 1:
        raise AssertionError(f"parser.Header identity is not unique: {header_rows}")
    identity = header_rows[0]["identity"]
    bounded_seed = header_value(identity)
    after_name = header_value(
        identity, module_start=18, module_end=23, next_offset=23, state=5
    )
    pages = prior.pages_wire(prior.pages_for(raw, 7))
    base = {
        "schema_version": "0.1",
        "step_budget": STEP_BUDGET,
    }
    return [
        {
            **base,
            "id": "helper/step-bounded-first-token",
            "function": "step",
            "args": [frontend.blob(raw), bounded_seed],
            "type_args": [frontend.nat_arg(BOUND)],
        },
        {
            **base,
            "id": "helper/finish-bounded-semicolon",
            "function": "finish",
            "args": [frontend.blob(raw), after_name],
            "type_args": [frontend.nat_arg(BOUND)],
        },
        {
            **base,
            "id": "helper/step-global-first-token",
            "function": "step_global",
            "args": [pages, frontend.integer(7), frontend.integer(len(raw)), bounded_seed],
            "type_args": [frontend.nat_arg(PAGE_BOUND), frontend.nat_arg(BOUND)],
        },
        {
            **base,
            "id": "helper/finish-global-complete",
            "function": "finish_global",
            "args": [pages, frontend.integer(7), frontend.integer(len(raw)), after_name],
            "type_args": [frontend.nat_arg(PAGE_BOUND), frontend.nat_arg(BOUND)],
        },
        {
            **base,
            "id": "helper/finish-global-fuel-boundary",
            "function": "finish_global",
            "args": [pages, frontend.integer(7), frontend.integer(len(raw)), bounded_seed],
            "type_args": [frontend.nat_arg(PAGE_BOUND), frontend.nat_arg(BOUND)],
        },
    ]


def semantic_digest(rows: list[tuple[str, dict]]) -> str:
    digest = hashlib.sha256()
    for identity, value in rows:
        digest.update(json.dumps([identity, value], sort_keys=True).encode() + b"\n")
    return digest.hexdigest()


def cgroup_memory_snapshot() -> dict:
    values = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            values[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            values[name] = None
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ephemeral-output",
        action="store_true",
        help="write the result manifest beneath .build for Environment verification",
    )
    args = parser.parse_args()
    evidence_path = (
        ROOT / ".build" / "self-host-parser-module-canonical-vm-frontier.json"
        if args.ephemeral_output else EVIDENCE
    )
    if not test_vm_emit.PROBE.is_file():
        raise SystemExit(f"Stage-0 probe is missing: {test_vm_emit.PROBE}")
    if not test_vm_emit.VM_BIN.is_file():
        raise SystemExit(f"canonical VM binary is missing: {test_vm_emit.VM_BIN}")

    inputs = prior.source_cases()
    global_cases = [
        {
            "id": f"{item['id']}@stride-{stride}",
            "input_id": item["id"],
            "raw": item["raw"],
            "stride": stride,
            "expected_code": item["expected_code"],
        }
        for item in inputs for stride in STRIDES
    ]
    children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    memory_before = cgroup_memory_snapshot()
    whole_started = time.monotonic()
    phases: dict[str, float] = {}
    bounded_reference: dict[str, dict] = {}
    global_reference: list[tuple[str, dict]] = []
    global_steps: list[int] = []
    oracle_checks = []
    helper_source = b"mncs 0.18; module alpha;"
    helpers: list[dict] = []
    helper_reference: dict[str, dict] = {}
    helper_reference_steps: dict[str, int] = {}
    artifact = None

    reference = open_probe(None)
    try:
        reference.send({"execution_status": True})
        helpers = helper_cases(reference, helper_source)
        phase_started = time.monotonic()
        for item in inputs:
            raw = item["raw"]
            oracle = reference.send({"oracle": raw.decode("utf-8")})
            # Decode once through the common helper, preserving the step count
            # for evidence while checking the direct parser entrypoint.
            direct_result = reference.send(request_bounded(raw))
            direct_value, direct_steps = prior.decode_result(direct_result, item["id"])
            bounded_reference[item["id"]] = direct_value
            if item["expected_code"] != 9:
                if direct_value["code"] != item["expected_code"]:
                    raise AssertionError(
                        f"{item['id']}: direct parser.header returned {direct_value}"
                    )
                if item["stage0_check"]:
                    prior.prior_frontier.check_against_stage0(
                        reference, item, direct_value, oracle
                    )
            if item["expected_code"] == 9 and direct_value["state"] != 8:
                raise AssertionError(
                    f"{item['id']}: bounded parser.header unexpectedly exhausted fuel"
                )
            oracle_checks.append({
                "id": item["id"],
                "expected_code": item["expected_code"],
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "stage0_diagnostics": len(oracle.get("diagnostics", [])),
                "direct_bounded_steps": direct_steps,
            })

        kernel_bounded = {}
        for item in inputs:
            if item["expected_code"] == 9:
                continue
            raw = item["raw"]
            request = prior.request_bounded(raw)
            kernel_bounded[item["id"]] = prior.decode_result(
                reference.send(request), item["id"]
            )[0]
            if kernel_bounded[item["id"]] != bounded_reference[item["id"]]:
                raise AssertionError(
                    f"{item['id']}: parser.header disagrees with kernel.parse_header"
                )

        for case in global_cases:
            value, steps = prior.decode_result(
                reference.send(request_global(case["raw"], case["stride"])), case["id"]
            )
            if value["code"] != case["expected_code"]:
                raise AssertionError(
                    f"{case['id']}: expected code {case['expected_code']}, got {value}"
                )
            if case["expected_code"] == 9 and value["state"] == 8:
                raise AssertionError(f"{case['id']}: fuel exhaustion reported success")
            if case["expected_code"] != 9 and value != bounded_reference[case["input_id"]]:
                raise AssertionError(
                    f"{case['id']}: parser.header_global disagrees with parser.header"
                )
            global_reference.append((case["id"], value))
            global_steps.append(steps)

        for case in helpers:
            value, steps = prior.decode_result(
                reference.send(request_helper(case)), case["id"]
            )
            helper_reference[case["id"]] = value
            helper_reference_steps[case["id"]] = steps
        phases["stage0_oracles_and_reference_execution"] = round(
            time.monotonic() - phase_started, 6
        )

        phase_started = time.monotonic()
        emitted = reference.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError(f"direct parser module emission failed: {emitted}")
        specialization_spellings = {
            "header": ("1024",),
            "finish": ("1024",),
            "step": ("1024",),
            "header_global": ("1024", "1024"),
            "finish_global": ("1024", "1024"),
            "step_global": ("1024", "1024"),
        }
        for function, spellings in specialization_spellings.items():
            rows = [row for row in artifact.get("generic_entrypoints", [])
                    if row.get("generic_function") == function]
            if not any(tuple(row.get("args_spellings", [])) == spellings for row in rows):
                raise AssertionError(f"artifact omitted parser.{function}{spellings}: {rows}")
        phases["direct_vm_emission"] = round(time.monotonic() - phase_started, 6)
    finally:
        reference.close()

    cranelift = open_probe("cranelift")
    bounded_cranelift: dict[str, dict] = {}
    global_cranelift: list[tuple[str, dict]] = []
    global_cranelift_steps: list[int] = []
    helper_cranelift: dict[str, dict] = {}
    helper_cranelift_steps: dict[str, int] = {}
    try:
        status = cranelift.send({"execution_status": True})
        if status.get("backend") != "cranelift":
            raise AssertionError(f"probe did not select Cranelift: {status}")
        phase_started = time.monotonic()
        for item in inputs:
            value, _ = prior.decode_result(
                cranelift.send(request_bounded(item["raw"])), item["id"]
            )
            if value != bounded_reference[item["id"]]:
                raise AssertionError(f"{item['id']}: Cranelift parser.header differs")
            bounded_cranelift[item["id"]] = value
        for case in global_cases:
            value, steps = prior.decode_result(
                cranelift.send(request_global(case["raw"], case["stride"])), case["id"]
            )
            expected = global_reference[len(global_cranelift)][1]
            if value != expected:
                raise AssertionError(f"{case['id']}: Cranelift parser.header_global differs")
            global_cranelift.append((case["id"], value))
            global_cranelift_steps.append(steps)
        for case in helpers:
            value, steps = prior.decode_result(
                cranelift.send(request_helper(case)), case["id"]
            )
            if value != helper_reference[case["id"]]:
                raise AssertionError(f"{case['id']}: Cranelift helper differs from Stage-0")
            helper_cranelift[case["id"]] = value
            helper_cranelift_steps[case["id"]] = steps
        phases["cranelift_execution"] = round(time.monotonic() - phase_started, 6)
    finally:
        cranelift.close()

    vm_cases = []
    for item in inputs:
        vm_cases.append({
            "id": f"bounded/{item['id']}",
            "kind": "bounded",
            "function": "header",
            "args": [frontend.blob(item["raw"])],
            "type_args": [frontend.nat_arg(BOUND)],
            "step_budget": STEP_BUDGET,
        })
    for case in global_cases:
        vm_cases.append({
            "id": f"global/{case['id']}",
            "kind": "global",
            "function": "header_global",
            "args": [
                prior.pages_wire(prior.pages_for(case["raw"], case["stride"])),
                frontend.integer(case["stride"]),
                frontend.integer(len(case["raw"])),
            ],
            "type_args": [frontend.nat_arg(PAGE_BOUND), frontend.nat_arg(BOUND)],
            "step_budget": STEP_BUDGET,
        })
    for case in helpers:
        vm_cases.append({
            "id": case["id"],
            "kind": "helper",
            "function": case["function"],
            "args": case["args"],
            "type_args": case["type_args"],
            "step_budget": case["step_budget"],
        })
    vm_bounded: dict[str, dict] = {}
    vm_global: list[tuple[str, dict]] = []
    vm_steps: list[int] = []
    vm_helpers: dict[str, dict] = {}
    vm_helper_steps: dict[str, int] = {}
    with tempfile.TemporaryDirectory(prefix="mncs-vm-parser-module-frontier-") as directory:
        temp = Path(directory)
        artifact_path = temp / "parser-module.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_rss, calls_bytes, results_bytes = test_vm_segment.run_batch(
            artifact_path, vm_cases, temp, module=MODULE
        )
        phases["canonical_vm_batch"] = round(vm_wall, 6)
        rows = document.get("results", [])
        if len(rows) != len(vm_cases):
            raise AssertionError(f"canonical VM returned {len(rows)} of {len(vm_cases)} calls")
        for index, (call, result) in enumerate(zip(vm_cases, rows)):
            if result.get("id") != call["id"] or result.get("outcome") != {"kind": "completed"}:
                raise AssertionError(f"{call['id']}: canonical VM did not complete: {result}")
            returned = result.get("record", {}).get("returned", [])
            if len(returned) != 1:
                raise AssertionError(f"{call['id']}: canonical VM returned the wrong value count")
            value = frontend.decode(test_vm_segment.vm_to_wire(returned[0]))
            if call["kind"] == "helper":
                expected = helper_reference[call["id"]]
                vm_helpers[call["id"]] = value
                vm_helper_steps[call["id"]] = result["record"]["usage"]["steps"]
            elif call["kind"] == "bounded":
                identity = call["id"].removeprefix("bounded/")
                expected = bounded_reference[identity]
                vm_bounded[identity] = value
            else:
                identity = call["id"].removeprefix("global/")
                expected = global_reference[index - len(inputs)][1]
                vm_global.append((identity, value))
                vm_steps.append(result["record"]["usage"]["steps"])
            if value != expected:
                raise AssertionError(f"{call['id']}: canonical VM disagrees with reference result")

    bounded_ids = [item["id"] for item in inputs]
    bounded_digests = {
        "reference_interpreter": semantic_digest(
            [(identity, bounded_reference[identity]) for identity in bounded_ids]
        ),
        "cranelift": semantic_digest(
            [(identity, bounded_cranelift[identity]) for identity in bounded_ids]
        ),
        "canonical_vm": semantic_digest(
            [(identity, vm_bounded[identity]) for identity in bounded_ids]
        ),
    }
    global_digests = {
        "reference_interpreter": semantic_digest(global_reference),
        "cranelift": semantic_digest(global_cranelift),
        "canonical_vm": semantic_digest(vm_global),
    }
    helper_rows = [(case["id"], helper_reference[case["id"]]) for case in helpers]
    helper_digests = {
        "reference_interpreter": semantic_digest(helper_rows),
        "cranelift": semantic_digest(
            [(case["id"], helper_cranelift[case["id"]]) for case in helpers]
        ),
        "canonical_vm": semantic_digest(
            [(case["id"], vm_helpers[case["id"]]) for case in helpers]
        ),
    }
    if (len(set(bounded_digests.values())) != 1
            or len(set(global_digests.values())) != 1
            or len(set(helper_digests.values())) != 1):
        raise AssertionError(
            "direct parser executor digests differ: "
            f"bounded={bounded_digests}, global={global_digests}, helpers={helper_digests}"
        )

    compiler_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    compiler_branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=ROOT, text=True
    ).strip()
    stage0_revision = json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"]
    vm_repo = test_vm_emit.VM_BIN.parents[2]
    vm_revision = subprocess.check_output(
        ["git", "-C", str(vm_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    source_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in ("source", "lexer", "parser", "segment", "kernel")
    }
    children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    cpu_seconds = (
        children_after.ru_utime - children_before.ru_utime
        + children_after.ru_stime - children_before.ru_stime
    )
    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.parser-module-direct-canonical-vm-frontier",
        "compiler_revision_tested": compiler_revision,
        "compiler_branch": compiler_branch,
        "stage0_revision": stage0_revision,
        "stage0_probe_sha256": hashlib.sha256(test_vm_emit.PROBE.read_bytes()).hexdigest(),
        "vm_revision": vm_revision,
        "vm_binary_sha256": hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest(),
        "selected_modules": ["source", "lexer", "parser", "segment", "kernel"],
        "module_source_sha256": source_hashes,
        "target_module": MODULE,
        "target_functions": list(FUNCTIONS),
        "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "artifact_identity": artifact["artifact_id"],
        "specializations": {
            "header": ["1024"],
            "finish": ["1024"],
            "step": ["1024"],
            "header_global": ["1024", "1024"],
            "finish_global": ["1024", "1024"],
            "step_global": ["1024", "1024"],
        },
        "bounded_cases": oracle_checks,
        "bounded_case_count": len(inputs),
        "global_case_count": len(global_cases),
        "helper_cases": [
            {
                "id": case["id"],
                "function": case["function"],
                "reference_result": helper_reference[case["id"]],
                "reference_steps": helper_reference_steps[case["id"]],
                "cranelift_steps": helper_cranelift_steps[case["id"]],
                "canonical_vm_result": vm_helpers[case["id"]],
                "canonical_vm_steps": vm_helper_steps[case["id"]],
            }
            for case in helpers
        ],
        "canonical_vm_case_count": len(vm_bounded) + len(vm_global) + len(vm_helpers),
        "strides": list(STRIDES),
        "semantic_digests": {
            "bounded": bounded_digests,
            "global": global_digests,
            "helpers": helper_digests,
        },
        "step_observations": {
            "reference_global": global_steps,
            "cranelift_global": global_cranelift_steps,
            "canonical_vm_global": vm_steps,
            "reference_helpers": helper_reference_steps,
            "cranelift_helpers": helper_cranelift_steps,
            "canonical_vm_helpers": vm_helper_steps,
        },
        "timing_seconds": {**phases, "whole_frontier": round(time.monotonic() - whole_started, 6)},
        "resource_measurements": {
            "child_cpu_seconds": round(cpu_seconds, 6),
            "child_peak_rss_kib": children_after.ru_maxrss,
            "canonical_vm_peak_rss_kib": vm_rss,
            "canonical_vm_calls_bytes": calls_bytes,
            "canonical_vm_results_bytes": results_bytes,
            "cgroup_before": memory_before,
            "cgroup_after": cgroup_memory_snapshot(),
            "cgroup_measurements_are_ambient": True,
        },
        "scope": (
            "All six current parser functions have executable selected specializations and are "
            "called directly in a fresh canonical VM batch. header and header_global agree with "
            "Stage-0 project checks where the bounded global fuel policy permits completion, "
            "Cranelift, and canonical VM across frontend header fixtures, long dotted module "
            "names, all tested page strides, non-ASCII input, and the fuel boundary. Direct "
            "finish/step helper calls use typed Header states and agree across the reference "
            "interpreter, Cranelift, and canonical VM. This covers every parser function at the "
            "listed specializations and selected states; it does not prove all type arguments, "
            "full compiler succession, Stage-1, or Stage-2."
        ),
        "stage1_claimed": False,
        "stage2_claimed": False,
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "passed",
        "evidence": str(evidence_path.relative_to(ROOT)),
        "target_module": MODULE,
        "target_functions": list(FUNCTIONS),
        "bounded_case_count": len(inputs),
        "global_case_count": len(global_cases),
        "canonical_vm_case_count": report["canonical_vm_case_count"],
        "artifact_identity": artifact["artifact_id"],
        "bounded_semantic_digest": bounded_digests["canonical_vm"],
        "global_semantic_digest": global_digests["canonical_vm"],
        "helper_semantic_digest": helper_digests["canonical_vm"],
        "timing_seconds": report["timing_seconds"],
        "resource_measurements": report["resource_measurements"],
    }, indent=2))


if __name__ == "__main__":
    main()
