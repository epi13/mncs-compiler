#!/usr/bin/env python3
"""Verify parser.header_global across logical page boundaries.

Stage-0 checks the ordinary header diagnostics and spans; the selected
kernel.parse_header_global callable must agree with the bounded parser,
Cranelift, and a freshly emitted canonical VM artifact for each page stride.
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

import test_vm_parser_frontier as prior_frontier

frontend = prior_frontier.frontend
native_app = prior_frontier.native_app
test_vm_emit = prior_frontier.test_vm_emit
run_batch = prior_frontier.run_batch
vm_to_wire = prior_frontier.vm_to_wire
ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.kernel.v1"
FUNCTION = "parse_header_global"
BOUND = 1024
PAGE_BOUND = 1024
STRIDES = (1, 7, 64, 1024)
STEP_BUDGET = 8_000_000
OUT = ROOT / ".build" / "vm-parser-header-global-frontier.json"
EVIDENCE = Path(os.environ.get(
    "MNCS_VM_PARSER_GLOBAL_EVIDENCE",
    ROOT / "evidence" / "self-host-parser-header-global-fuel-4096-20261019.json",
))

test_vm_emit.VM_REPO = native_app.VM_ROOT
test_vm_emit.VM_BIN = native_app.VM_BIN


def global_seeds() -> list[dict]:
    return [
        {"module": MODULE, "function": "parse_header",
         "type_arguments": [frontend.nat_arg(BOUND)]},
        {"module": MODULE, "function": FUNCTION,
         "type_arguments": [frontend.nat_arg(PAGE_BOUND),
                            frontend.nat_arg(BOUND)]},
    ]


class Probe:
    def __init__(self, backend: str | None):
        env = dict(os.environ)
        env["MNCS_PROBE_MODULES"] = frontend.PROBE_MODULES
        env["MNCS_PROBE_EXECUTION_MODULES"] = ",".join(
            f"mncs.compiler.{unit}.v1"
            for unit in ("source", "lexer", "parser", "segment", "kernel")
        )
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(global_seeds())
        env.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build" / "probe-cache"))
        if backend is None:
            env.pop("MNCS_PROBE_BACKEND", None)
        else:
            env["MNCS_PROBE_BACKEND"] = backend
        binary = env.get(
            "MNCS_PROBE_BIN",
            str(frontend.BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"),
        )
        self.proc = subprocess.Popen(
            [binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env,
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
        code = self.proc.wait(timeout=120)
        stderr = self.proc.stderr.read()
        if code != 0:
            raise AssertionError(f"probe exited {code}: {stderr[-2000:]}")
        return stderr


def integer(value: int) -> dict:
    return frontend.integer(value)


def pages_for(raw: bytes, stride: int) -> list[bytes]:
    return [raw[offset:offset + stride] for offset in range(0, len(raw), stride)]


def pages_wire(pages: list[bytes]) -> dict:
    return {"sequence": {"values": [frontend.blob(page) for page in pages]}}


def request_global(raw: bytes, stride: int) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": FUNCTION},
        "arguments": [pages_wire(pages_for(raw, stride)), integer(stride),
                      integer(len(raw))],
        "type_arguments": [frontend.nat_arg(PAGE_BOUND),
                           frontend.nat_arg(BOUND)],
        "step_budget": STEP_BUDGET,
    }


def request_bounded(raw: bytes) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": "parse_header"},
        "arguments": [frontend.blob(raw)],
        "type_arguments": [frontend.nat_arg(BOUND)],
        "step_budget": STEP_BUDGET,
    }


def decode_result(result: dict, case_id: str) -> tuple[dict, int]:
    if result.get("status") != "returned":
        raise AssertionError(f"{case_id}: parser returned {result.get('status')}: {result}")
    return frontend.decode(result["returned"][0]), result.get("steps", 0)


def source_cases() -> list[dict]:
    fixtures = json.loads((ROOT / "tests/fixtures/frontend.json").read_text())["headers"]
    cases = []
    for index, fixture in enumerate(fixtures):
        cases.append({
            "id": f"frontend-header-{index:02d}",
            "raw": frontend.fixed_bytes(fixture["text"]),
            "expected_code": fixture["code"],
            "version_span": fixture.get("version_span"),
            "module_span": fixture.get("module_span"),
            "stage0_check": True,
        })
    extended = b"mncs 0.18; module x;"
    cases.append({
        "id": "extended-header-65-byte-bound",
        "raw": extended + b" " * (65 - len(extended)),
        "expected_code": 0,
        "version_span": None,
        "module_span": None,
        "stage0_check": True,
    })
    twenty = b"mncs 0.18; module " + b".".join(
        f"s{index:02d}".encode() for index in range(20)
    ) + b";"
    cases.append({
        "id": "twenty-segment-header-within-fuel",
        "raw": twenty,
        "expected_code": 0,
        "version_span": None,
        "module_span": None,
        "stage0_check": True,
    })
    forty = b"mncs 0.18; module " + b".".join(
        f"s{index:02d}".encode() for index in range(40)
    ) + b";"
    cases.append({
        "id": "forty-segment-header-within-extended-fuel",
        "raw": forty,
        "expected_code": 0,
        "version_span": None,
        "module_span": None,
        "stage0_check": True,
    })
    hundred = b"mncs 0.18; module " + b".".join(b"x" for _ in range(100)) + b";"
    cases.append({
        "id": "hundred-segment-header-within-fuel",
        "raw": hundred,
        "expected_code": 0,
        "version_span": None,
        "module_span": None,
        "stage0_check": True,
    })
    one_thirty = b"mncs 0.18; module " + b".".join(b"x" for _ in range(130)) + b";"
    cases.append({
        "id": "one-hundred-thirty-segment-header-within-extended-fuel",
        "raw": one_thirty,
        "expected_code": 0,
        "version_span": None,
        "module_span": None,
        "stage0_check": True,
    })
    four_hundred = b"mncs 0.18; module " + b".".join(b"x" for _ in range(400)) + b";"
    cases.append({
        "id": "four-hundred-segment-header-within-fuel",
        "raw": four_hundred,
        "expected_code": 0,
        "version_span": None,
        "module_span": None,
        "stage0_check": True,
    })
    six_hundred = b"mncs 0.18; module " + b".".join(b"x" for _ in range(600)) + b";"
    cases.append({
        "id": "six-hundred-segment-header-within-batched-fuel",
        "raw": six_hundred,
        "expected_code": 0,
        "version_span": None,
        "module_span": None,
        "stage0_check": False,
        "strides": (2, 7, 64, 1024),
    })
    nine_hundred = b"mncs 0.18; module " + b".".join(b"x" for _ in range(900)) + b";"
    cases.append({
        "id": "nine-hundred-segment-header-within-batched-fuel",
        "raw": nine_hundred,
        "expected_code": 0,
        "version_span": None,
        "module_span": None,
        "stage0_check": False,
        "strides": (2, 7, 64, 1024),
    })
    beyond_batched_fuel = b"mncs 0.18; module " + b".".join(
        b"x" for _ in range(2047)
    ) + b";"
    cases.append({
        "id": "two-thousand-forty-seven-segment-header-fuel-exhaustion",
        "raw": beyond_batched_fuel,
        "expected_code": 9,
        "version_span": None,
        "module_span": None,
        "stage0_check": False,
        "strides": (7, 64, 1024),
    })
    cases.append({
        "id": "non-ascii-scalar-split-across-pages",
        "raw": b"mncs 0.18; module x; \xc3\xa9",
        "expected_code": 8,
        "version_span": None,
        "module_span": None,
        "stage0_check": True,
    })
    return cases


def semantic_digest(case_ids: list[str], values: list[dict]) -> str:
    digest = hashlib.sha256()
    for case_id, value in zip(case_ids, values):
        digest.update(json.dumps([case_id, value], sort_keys=True).encode() + b"\n")
    return digest.hexdigest()


def cgroup_memory_snapshot() -> dict:
    result = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            result[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            result[name] = None
    return result


def main() -> None:
    if not test_vm_emit.PROBE.is_file():
        raise SystemExit(f"Stage-0 probe is missing: {test_vm_emit.PROBE}")
    if not test_vm_emit.VM_BIN.is_file():
        raise SystemExit(f"canonical VM binary is missing: {test_vm_emit.VM_BIN}")

    inputs = source_cases()
    cases = [
        {
            "id": f"{item['id']}@stride-{stride}",
            "input_id": item["id"],
            "raw": item["raw"],
            "stride": stride,
            "expected_code": item["expected_code"],
        }
        for item in inputs for stride in item.get("strides", STRIDES)
    ]
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    memory_before = cgroup_memory_snapshot()
    started = time.monotonic()
    phase_seconds = {}
    reference_values, reference_steps = [], []
    bounded_by_id, oracle_checks = {}, []

    reference = Probe(None)
    try:
        reference.send({"execution_status": True})
        t0 = time.monotonic()
        for item in inputs:
            if item["expected_code"] == 9 or not item["stage0_check"]:
                continue
            raw = item["raw"]
            text = raw.decode("utf-8")
            oracle = reference.send({"oracle": text})
            bounded, _, _ = prior_frontier.decode_result(
                reference.send(request_bounded(raw)), item["id"]
            )
            bounded_by_id[item["id"]] = bounded
            if item["stage0_check"]:
                prior_frontier.check_against_stage0(reference, item, bounded, oracle)
            oracle_checks.append({
                "id": item["id"],
                "expected_code": item["expected_code"],
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "stage0_diagnostics": len(oracle.get("diagnostics", [])),
            })
        for case in cases:
            value, steps = decode_result(
                reference.send(request_global(case["raw"], case["stride"])), case["id"]
            )
            if value["code"] != case["expected_code"]:
                raise AssertionError(
                    f"{case['id']}: expected header code {case['expected_code']}, got {value}"
                )
            if case["expected_code"] == 9 and value["state"] == 8:
                raise AssertionError(f"{case['id']}: exhausted parser reported success: {value}")
            if case["input_id"] in bounded_by_id:
                bounded = bounded_by_id[case["input_id"]]
                if value != bounded:
                    raise AssertionError(
                        f"{case['id']}: global parser disagrees with bounded header: {value} != {bounded}"
                    )
            elif case["expected_code"] == 0:
                expected = {
                    "code": 0,
                    "version_start": 5,
                    "version_end": 9,
                    "module_start": 18,
                    "module_end": len(case["raw"]) - 1,
                    "next_offset": len(case["raw"]),
                    "state": 8,
                }
                if any(value.get(key) != expected_value
                       for key, expected_value in expected.items()):
                    raise AssertionError(
                        f"{case['id']}: long-header boundary mismatch: {value}"
                    )
            reference_values.append(value)
            reference_steps.append(steps)
        phase_seconds["stage0_oracles_and_reference_execution"] = round(
            time.monotonic() - t0, 6
        )

        t0 = time.monotonic()
        emitted = reference.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError(f"direct emitter returned no canonical VM artifact: {emitted}")
        rows = [entry for entry in artifact.get("generic_entrypoints", [])
                if entry.get("generic_function") == FUNCTION]
        if not any(tuple(row.get("args_spellings", [])) == ("1024", "1024")
                   for row in rows):
            raise AssertionError(f"artifact omitted parse_header_global<1024,1024>: {rows}")
        phase_seconds["direct_vm_emission"] = round(time.monotonic() - t0, 6)
    finally:
        reference.close()

    cranelift = Probe("cranelift")
    cranelift_values, cranelift_steps = [], []
    try:
        status = cranelift.send({"execution_status": True})
        if status.get("backend") != "cranelift":
            raise AssertionError(f"probe did not select Cranelift: {status}")
        t0 = time.monotonic()
        for case in cases:
            value, steps = decode_result(
                cranelift.send(request_global(case["raw"], case["stride"])), case["id"]
            )
            expected = reference_values[len(cranelift_values)]
            if value != expected:
                raise AssertionError(
                    f"{case['id']}: Cranelift disagrees with reference interpreter"
                )
            cranelift_values.append(value)
            cranelift_steps.append(steps)
        phase_seconds["cranelift_execution"] = round(time.monotonic() - t0, 6)
    finally:
        cranelift.close()

    vm_cases = [
        {
            "id": case["id"],
            "function": FUNCTION,
            "args": [pages_wire(pages_for(case["raw"], case["stride"])),
                     integer(case["stride"]), integer(len(case["raw"]))],
            "type_args": [frontend.nat_arg(PAGE_BOUND), frontend.nat_arg(BOUND)],
            "step_budget": STEP_BUDGET,
        }
        for case in cases
    ]
    vm_values, vm_steps = [], []
    with tempfile.TemporaryDirectory(prefix="mncs-vm-parser-header-global-") as directory:
        temp = Path(directory)
        artifact_path = temp / "kernel-parser-header-global.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_rss, calls_bytes, results_bytes = run_batch(
            artifact_path, vm_cases, temp, module=MODULE
        )
        phase_seconds["canonical_vm_batch"] = round(vm_wall, 6)
        for index, (case, result) in enumerate(zip(cases, document["results"])):
            if result.get("id") != case["id"] or result.get("outcome") != {"kind": "completed"}:
                raise AssertionError(f"{case['id']}: canonical VM did not complete: {result}")
            returned = result["record"]["returned"]
            if len(returned) != 1:
                raise AssertionError(f"{case['id']}: canonical VM returned wrong value count")
            value = frontend.decode(vm_to_wire(returned[0]))
            if value != reference_values[index] or value != cranelift_values[index]:
                raise AssertionError(f"{case['id']}: canonical VM result differs")
            vm_values.append(value)
            vm_steps.append(result["record"]["usage"]["steps"])

    case_ids = [case["id"] for case in cases]
    semantic_digests = {
        "reference_interpreter": semantic_digest(case_ids, reference_values),
        "cranelift": semantic_digest(case_ids, cranelift_values),
        "canonical_vm": semantic_digest(case_ids, vm_values),
    }
    if len(set(semantic_digests.values())) != 1:
        raise AssertionError(f"executor semantic digests differ: {semantic_digests}")

    compiler_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    compiler_branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=ROOT, text=True
    ).strip()
    stage0_revision = json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"]
    stage0_probe_sha = hashlib.sha256(test_vm_emit.PROBE.read_bytes()).hexdigest()
    vm_sha = hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest()
    vm_repo = test_vm_emit.VM_BIN.parents[2]
    vm_revision = subprocess.check_output(
        ["git", "-C", str(vm_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    source_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in ("source", "lexer", "parser", "kernel", "segment")
    }
    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after_children.ru_utime - before_children.ru_utime
                 + after_children.ru_stime - before_children.ru_stime)
    whole_wall = round(time.monotonic() - started, 6)
    harness_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report = {
        "schema_version": 1,
        "kind": "mncs-compiler-parser-header-global-canonical-vm-frontier",
        "compiler_revision_tested": compiler_revision,
        "compiler_branch": compiler_branch,
        "stage0_revision": stage0_revision,
        "stage0_probe_sha256": stage0_probe_sha,
        "selected_modules": ["source", "lexer", "parser", "kernel", "segment"],
        "module_source_sha256": source_hashes,
        "target_module": MODULE,
        "target_function": FUNCTION,
        "test_harness_sha256": harness_sha,
        "artifact_identity": artifact["artifact_id"],
        "specializations": [row.get("args_spellings") for row in rows],
        "inputs": oracle_checks,
        "cases": [
            {"id": case["id"], "input_id": case["input_id"],
             "stride": case["stride"], "bytes": len(case["raw"]),
             "expected_code": case["expected_code"]}
            for case in cases
        ],
        "case_count": len(cases),
        "executor_case_counts": {
            "reference_interpreter": len(reference_values),
            "cranelift": len(cranelift_values),
            "canonical_vm": len(vm_values),
        },
        "semantic_digests": semantic_digests,
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
            "cgroup_after": cgroup_memory_snapshot(),
            "cgroup_measurements_are_ambient": True,
        },
        "scope": (
            "The current kernel.parse_header_global callable executes parser.header_global "
            "over all frontend header fixtures, a 65-byte page split, a 20-segment header, "
            "100-, 130-, 400-, 600-, and 900-segment headers, a non-ASCII scalar split "
            "across pages, and the explicit 4096-token fuel boundary (2047 segments are "
            "rejected). Strides 1, 7, 64, and 1024 agree across Stage-0 checked bounded "
            "parser results, the reference interpreter, Cranelift, and one fresh canonical "
            "VM artifact. Long-header spans are checked against their generated byte layout. "
            "This is a selected helper frontier, not full parser-module verification or succession."
        ),
        "stage1_claimed": False,
        "stage2_claimed": False,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    evidence = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "parser-header-global-canonical-vm-current-source",
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
            "target_function": FUNCTION,
            "test_harness": "tools/test_vm_parser_global_frontier.py",
            "test_harness_sha256": harness_sha,
        },
        "evidence_artifact": str(EVIDENCE),
        "toolchains": {
            "stage0_probe": str(test_vm_emit.PROBE),
            "stage0_probe_sha256": stage0_probe_sha,
            "stage0_revision": stage0_revision,
            "vm_binary": str(test_vm_emit.VM_BIN),
            "vm_binary_sha256": vm_sha,
            "vm_revision": vm_revision,
        },
        "verification": {
            "executor_case_counts": report["executor_case_counts"],
            "semantic_digests": semantic_digests,
            "stage0_checks": "all bounded header diagnostics and span assertions passed",
            "stage1_claimed": False,
            "stage2_claimed": False,
        },
        "measurements": {
            "timing_seconds": report["timing_seconds"],
            "resource_measurements": report["resource_measurements"],
        },
        "raw_test_report": report,
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
