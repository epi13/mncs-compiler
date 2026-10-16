#!/usr/bin/env python3
"""Check `up_to` declaration parsing across logical page boundaries.

The helper cases exercise every offset within strides one through seven. The
parser cases then verify that a real sequence type retains the same meaning
when its source is split across those page boundaries.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import test_vm_decl as vm_decl
import test_decl

ROOT = Path(__file__).resolve().parents[1]
MODULE = vm_decl.MODULE
TYPE_ARGS = vm_decl.TYPE_ARGS
STEP_BUDGET = test_decl.EXECUTION_STEP_BUDGET


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def pages(data: bytes, stride: int) -> list[bytes]:
    return [data[offset:offset + stride] for offset in range(0, len(data), stride)]


def page_args(data: bytes, stride: int) -> list[dict]:
    return [
        test_decl.pages_value(pages(data, stride)),
        test_decl.integer(stride),
        test_decl.integer(len(data)),
    ]


class Probe:
    def __init__(self, source_root: Path, backend: str | None):
        env = os.environ.copy()
        env["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl"
        env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
            {"module": MODULE, "function": name, "type_arguments": TYPE_ARGS}
            for name in ("parse_unit", "check_unit", "is_up_to_bound")
        ])
        env["MNCS_PROBE_CACHE_DIR"] = str(
            ROOT / ".build/native-app-readiness/decl-up-to-window-cache"
        )
        env["MNCS_PROBE_TOOLCHAIN_IDENTITY_ROOT"] = str(ROOT)
        if backend is None:
            env.pop("MNCS_PROBE_BACKEND", None)
        else:
            env["MNCS_PROBE_BACKEND"] = backend
        self.process = subprocess.Popen(
            [str(vm_decl.PROBE)], cwd=source_root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )

    def send(self, request: dict) -> dict:
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"Stage-0 probe stopped: {self.process.poll()}")
        return json.loads(line)

    def close(self) -> str:
        assert self.process.stdin is not None and self.process.stderr is not None
        self.process.stdin.close()
        stderr = self.process.stderr.read()
        result = self.process.wait(timeout=30)
        if result != 0:
            raise RuntimeError(f"Stage-0 probe exited {result}: {stderr[-2000:]}")
        return stderr


def make_cases(probe: Probe) -> tuple[list[dict], list[dict]]:
    helper_cases = []
    for stride in range(1, 8):
        for start in range(stride):
            data = b"x" * start + b"up_to 4"
            helper_cases.append({
                "id": f"helper-valid-stride-{stride}-offset-{start}",
                "kind": "helper",
                "function": "is_up_to_bound",
                "args": page_args(data, stride) + [
                    test_decl.integer(start), test_decl.integer(len(data)),
                ],
                "type_args": TYPE_ARGS,
                "step_budget": 100_000,
                "expected": True,
            })

    for call_id, data, end, expected in (
        ("helper-nonspace-suffix", b"up_toX4", 7, False),
        ("helper-short-prefix", b"up_to", 5, False),
        ("helper-different-prefix", b"no_to 4", 7, False),
    ):
        helper_cases.append({
            "id": call_id,
            "kind": "helper",
            "function": "is_up_to_bound",
            "args": page_args(data, 2) + [test_decl.integer(0), test_decl.integer(end)],
            "type_args": TYPE_ARGS,
            "step_budget": 100_000,
            "expected": expected,
        })

    parse_cases = []
    for pad, stride in enumerate(range(2, 8)):
        text = (
            "mncs 0.18; module t; record R { " + " " * pad
            + "bytes: [byte; up_to 4] } "
            "fn f() -> (r: R) { return R { bytes: [0 as byte; 4] }; }"
        )
        data = text.encode()
        oracle = probe.send({"oracle": text})
        if oracle.get("diagnostics"):
            raise AssertionError(f"Stage-0 rejected the valid page-split fixture: {text}")
        parse_cases.append({
            "id": f"parse-viewed-bound-stride-{stride}",
            "kind": "parse",
            "function": "parse_unit",
            "args": page_args(data, stride),
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
            "text": text,
            "data": data,
            "oracle": oracle,
            "expected_type_kind": 8,
        })

    exact_text = (
        "mncs 0.18; module t; record R { bytes: [byte; up_toN] } "
        "fn f() -> (r: R) { return R { bytes: [0 as byte; 4] }; }"
    )
    exact_data = exact_text.encode()
    exact_oracle = probe.send({"oracle": exact_text})
    if exact_oracle.get("diagnostics"):
        raise AssertionError("Stage-0 rejected the valid exact-Nat identifier fixture")
    parse_cases.append({
        "id": "parse-exact-bound-prefix-lookalike",
        "kind": "parse",
        "function": "parse_unit",
        "args": page_args(exact_data, 5),
        "type_args": TYPE_ARGS,
        "step_budget": STEP_BUDGET,
        "text": exact_text,
        "data": exact_data,
        "oracle": exact_oracle,
        "expected_type_kind": 7,
    })
    return helper_cases, parse_cases


def call_request(case: dict) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": case["function"]},
        "arguments": case["args"],
        "type_arguments": case["type_args"],
        "step_budget": case["step_budget"],
    }


def check_result(case: dict, wire: list[dict]) -> None:
    if len(wire) != 1:
        raise AssertionError(f"{case['id']} returned the wrong value count")
    if case["kind"] == "helper":
        actual = test_decl.decode(wire[0])
        if actual is not case["expected"]:
            raise AssertionError(f"{case['id']} returned {actual}, expected {case['expected']}")
        return

    unit = test_decl.decode(wire[0])
    test_decl.check_pos_parse(unit, case["oracle"], case["data"], case["text"])
    record = test_decl.flist(unit["records"], 1)[0]
    field = test_decl.flist(record["fields"], 1)[0]
    if field["type_kind"] != case["expected_type_kind"]:
        raise AssertionError(
            f"{case['id']} classified the sequence bound as {field['type_kind']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source-root", type=Path, default=ROOT,
        help="checkout root whose src/compiler files the Stage-0 probe should read",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    source_root = args.source_root.resolve(strict=True)
    if not (source_root / "src/compiler/decl.mncs").is_file():
        raise SystemExit("source root does not contain src/compiler/decl.mncs")

    probe = Probe(source_root, None)
    started = time.monotonic()
    helper_cases, parse_cases = make_cases(probe)
    emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
    artifact = emitted.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
        raise AssertionError(f"Stage-0 did not emit a canonical VM artifact: {emitted}")

    cases = helper_cases + parse_cases
    reference_steps = []
    for case in cases:
        result = probe.send(call_request(case))
        if result.get("status") != "returned":
            raise AssertionError(f"reference interpreter failed {case['id']}: {result}")
        reference_steps.append(result.get("steps"))
        check_result(case, result["returned"])
    reference_stderr = probe.close()

    cranelift_probe = Probe(source_root, "cranelift")
    cranelift_started = time.monotonic()
    cranelift_steps = []
    for case in cases:
        result = cranelift_probe.send(call_request(case))
        if result.get("status") != "returned":
            raise AssertionError(f"Cranelift failed {case['id']}: {result}")
        cranelift_steps.append(result.get("steps"))
        check_result(case, result["returned"])
    cranelift_stderr = cranelift_probe.close()
    cranelift_elapsed = time.monotonic() - cranelift_started

    with tempfile.TemporaryDirectory(prefix="mncs-decl-up-to-window-") as raw:
        temp = Path(raw)
        artifact_path = temp / "decl.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        vm_document, vm_wall, vm_peak_kib, calls_bytes, results_bytes = vm_decl.run_batch(
            artifact_path, cases, temp, module=MODULE
        )
    if vm_document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different artifact")
    for case, row in zip(cases, vm_document.get("results", [])):
        if row.get("id") != case["id"] or row.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {case['id']}: {row}")
        returned = [vm_decl.vm_to_wire(value) for value in row["record"]["returned"]]
        check_result(case, returned)

    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.decl-up-to-four-byte-window-frontier",
        "compiler_revision": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "decl_source_sha256": sha((source_root / "src/compiler/decl.mncs").read_bytes()),
        "source_root": str(source_root),
        "probe_sha256": sha(vm_decl.PROBE.read_bytes()),
        "vm_artifact_identity": artifact.get("artifact_id"),
        "helper_cases": len(helper_cases),
        "parser_cases": len(parse_cases),
        "reference_steps": {
            "helper": sum(reference_steps[:len(helper_cases)]),
            "parse": sum(reference_steps[len(helper_cases):]),
        },
        "canonical_vm": {
            "wall_seconds": round(vm_wall, 6),
            "peak_rss_kib": vm_peak_kib,
            "calls_bytes": calls_bytes,
            "results_bytes": results_bytes,
        },
        "cranelift": {
            "cases": len(cases),
            "elapsed_seconds": round(cranelift_elapsed, 6),
            "reported_steps_each_request": sorted(set(cranelift_steps)),
            "step_note": "Cranelift request counters are not comparable to interpreter fuel.",
            "stderr_sha256": sha(cranelift_stderr.encode()),
        },
        "probe_stderr_sha256": sha(reference_stderr.encode()),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "scope": (
            "The up_to prefix helper is checked at every start offset within strides 1 through 7, "
            "with fresh Stage-0 parser results for six page-split sequence declarations and an "
            "exact-Nat prefix lookalike. The reference interpreter, Cranelift, and one canonical "
            "VM artifact must agree. This is a "
            "selected declaration parser frontier, not full decl-module proof."
        ),
    }
    encoded = json.dumps(report, indent=2) + "\n"
    if args.report:
        args.report.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
