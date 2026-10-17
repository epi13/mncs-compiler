#!/usr/bin/env python3
"""Differentially verify global source line/column mapping in a canonical VM.

The selected Source callable is checked against the byte-based SourceSpan
convention across page strides, newline boundaries, out-of-range offsets,
multibyte UTF-8 bytes, and a real 64 KiB source. One freshly emitted artifact
is exercised through the reference interpreter, Cranelift, and canonical VM.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import tempfile
import time
from pathlib import Path

import test_cp0001 as cp0001
import test_vm_emit
import test_vm_native_application as native_app
import test_vm_segment

ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.source.v1"
FUNCTION = "line_col_global"
PAGE_BOUND = 1024
STRIDE_BOUND = 1024
STRIDES = (1, 7, 64, 1024)
STEP_BUDGET = 8_000_000
EVIDENCE = ROOT / "evidence" / "self-host-source-line-col-global-20261017.json"

test_vm_emit.VM_REPO = native_app.VM_ROOT
test_vm_emit.VM_BIN = native_app.VM_BIN


def seeds() -> list[dict]:
    return [{
        "module": MODULE,
        "function": FUNCTION,
        "type_arguments": [cp0001.nat_arg(PAGE_BOUND), cp0001.nat_arg(STRIDE_BOUND)],
    }]


def observations() -> list[dict]:
    rows: list[dict] = []
    base = [
        ("empty", b"", lambda raw: [0, 1]),
        ("single-line", b"mncs 0.2; module source;",
         lambda raw: [0, len(raw) // 2, len(raw), len(raw) + 1]),
        ("multiple-lines", b"first\nsecond\nlast",
         lambda raw: [5, 6, 7, 12, 13, len(raw)]),
        ("utf8-columns-count-bytes", "cafe:\u2603\nend".encode(),
         lambda raw: [5, 6, 7, 8, len(raw)]),
    ]
    for name, raw, select_offsets in base:
        for stride in STRIDES:
            rows.append({
                "id": f"{name}-stride-{stride}",
                "raw": raw,
                "stride": stride,
                "offsets": sorted(set(select_offsets(raw))),
            })
    for stride in (7, 64, 1024):
        for newline in (stride - 1, stride):
            raw = b"x" * newline + b"\nnext\nlast"
            rows.append({
                "id": f"newline-page-edge-{stride}-{newline}",
                "raw": raw,
                "stride": stride,
                "offsets": [newline, newline + 1],
            })
    long_source = b"x" * 32767 + b"\n" + b"y" * (64 * 1024 - 32768)
    rows.append({
        "id": "real-64k-source-stride-1024",
        "raw": long_source,
        "stride": 1024,
        "offsets": [32766, 32767, 32768, len(long_source), len(long_source) + 1],
    })
    return rows


def run_probe_backend(backend: str, cases: list[dict]) -> tuple[list[dict], dict | None]:
    previous = os.environ.get("MNCS_PROBE_BACKEND")
    os.environ["MNCS_PROBE_BACKEND"] = backend
    probe = cp0001.Probe("source", MODULE, seeds())
    rows: list[dict] = []
    artifact = None
    try:
        for case in cases:
            for offset in case["offsets"]:
                started = time.monotonic()
                returned = probe.run_global(
                    MODULE,
                    FUNCTION,
                    cp0001.chunk(case["raw"], case["stride"]),
                    case["stride"],
                    len(case["raw"]),
                    (cp0001.integer(offset),),
                    budget=STEP_BUDGET,
                )
                wall = time.monotonic() - started
                expected = cp0001.ref_line_col(case["raw"], offset)
                actual = (returned["line"], returned["col"])
                if actual != expected:
                    raise AssertionError(
                        f"{backend} {case['id']} offset {offset}: {actual} != {expected}"
                    )
                rows.append({
                    "id": case["id"], "offset": offset,
                    "line": actual[0], "column": actual[1],
                    "steps": probe.steps[-1], "wall_seconds": round(wall, 6),
                })
        if backend == "reference_interpreter":
            emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
            artifact = emitted.get("artifact")
            if (not isinstance(artifact, dict)
                    or artifact.get("schema_version") != "mncs.vm.artifact/1"):
                raise AssertionError(f"Source did not emit a canonical VM artifact: {emitted}")
            entrypoints = [
                row for row in artifact.get("generic_entrypoints", [])
                if row.get("generic_function") == FUNCTION
            ]
            if not any(tuple(row.get("args_spellings", [])) == ("1024", "1024")
                       for row in entrypoints):
                raise AssertionError(f"artifact omitted {FUNCTION}<1024,1024>")
    finally:
        probe.close()
        if previous is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = previous
    return rows, artifact


def vm_cases(cases: list[dict]) -> list[dict]:
    requests = []
    for case in cases:
        pages = cp0001.chunk(case["raw"], case["stride"])
        for offset in case["offsets"]:
            requests.append({
                "id": f"{case['id']}-offset-{offset}",
                "function": FUNCTION,
                "args": [
                    cp0001.pages_value(pages),
                    cp0001.integer(case["stride"]),
                    cp0001.integer(len(case["raw"])),
                    cp0001.integer(offset),
                ],
                "type_args": [cp0001.nat_arg(PAGE_BOUND),
                              cp0001.nat_arg(STRIDE_BOUND)],
                "step_budget": STEP_BUDGET,
            })
    return requests


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ephemeral-output", action="store_true")
    args = parser.parse_args()
    evidence_path = (
        ROOT / ".build" / "source-line-col-global-20261017.json"
        if args.ephemeral_output else EVIDENCE
    )
    if not test_vm_emit.PROBE.is_file():
        raise SystemExit(f"Stage-0 probe is missing: {test_vm_emit.PROBE}")
    test_vm_emit.ensure_vm()
    cases = observations()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    source_hash = hashlib.sha256((ROOT / "src/compiler/source.mncs").read_bytes()).hexdigest()
    reference_rows, artifact = run_probe_backend("reference_interpreter", cases)
    cranelift_rows, _ = run_probe_backend("cranelift", cases)
    expected_digest = [(row["id"], row["line"], row["column"]) for row in reference_rows]
    cranelift_digest = [(row["id"], row["line"], row["column"]) for row in cranelift_rows]
    if cranelift_digest != expected_digest:
        raise AssertionError("Cranelift line/column results differ from the reference interpreter")

    calls = vm_cases(cases)
    with tempfile.TemporaryDirectory(prefix="mncs-vm-source-line-col-") as raw_dir:
        directory = Path(raw_dir)
        artifact_path = directory / "source-line-col.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_rss_kib, calls_bytes, results_bytes = test_vm_segment.run_batch(
            artifact_path, calls, directory, module=MODULE
        )

    vm_rows = []
    if len(document.get("results", [])) != len(calls):
        raise AssertionError("canonical VM omitted line/column results")
    for call, result, expected in zip(calls, document["results"], expected_digest):
        if result.get("id") != call["id"] or result.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {call['id']}: {result}")
        returned = result.get("record", {}).get("returned", [])
        if len(returned) != 1:
            raise AssertionError(f"canonical VM returned wrong value count for {call['id']}")
        wire = test_vm_segment.vm_to_wire(returned[0])
        actual = cp0001.decode(wire)
        value = (actual["line"], actual["col"])
        if value != expected[1:]:
            raise AssertionError(f"canonical VM {call['id']}: {value} != {expected[1:]}")
        vm_rows.append({
            "id": call["id"], "line": value[0], "column": value[1],
            "steps": result["record"]["usage"]["steps"],
        })

    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = ((child_after.ru_utime - child_before.ru_utime)
                 + (child_after.ru_stime - child_before.ru_stime))
    artifact_id = artifact.get("artifact_id")
    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.source-line-col-global-canonical-vm-frontier",
        "status": "passed",
        "compiler_revision_tested": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "compiler_branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "source_sha256": source_hash,
        "target": {"module": MODULE, "function": FUNCTION,
                   "generic_arguments": [PAGE_BOUND, STRIDE_BOUND]},
        "oracle": "byte-based SourceSpan line/column convention: 1-based, newline resets column, UTF-8 columns count bytes",
        "page_strides": list(STRIDES),
        "input_count": len(cases),
        "request_count_per_executor": len(reference_rows),
        "offset_cases": [
            {"id": row["id"], "size_bytes": len(row["raw"]),
             "stride": row["stride"], "offsets": row["offsets"]}
            for row in cases
        ],
        "reference_interpreter": reference_rows,
        "cranelift": cranelift_rows,
        "canonical_vm": {
            "artifact_identity": artifact_id,
            "results": vm_rows,
            "wall_seconds": round(vm_wall, 6),
            "peak_rss_kib": vm_peak_rss_kib,
            "calls_file_bytes": calls_bytes,
            "results_file_bytes": results_bytes,
            "maximum_steps_per_call": max(row["steps"] for row in vm_rows),
        },
        "semantic_digest_sha256": hashlib.sha256(json.dumps(
            expected_digest, separators=(",", ":")
        ).encode()).hexdigest(),
        "measurements": {
            "reference_and_cranelift_child_cpu_seconds": round(child_cpu, 6),
            "python_harness_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "cgroup_memory": {
                name: ((Path("/sys/fs/cgroup") / name).read_text().strip()
                       if (Path("/sys/fs/cgroup") / name).exists() else None)
                for name in ("memory.current", "memory.max", "memory.peak")
            },
        },
        "language_verified_facts_reused": 0,
        "stage1_claimed": False,
        "stage2_claimed": False,
        "scope": (
            "The selected Source line_col_global callable maps byte offsets across canonical page layouts. "
            "Reference interpreter, Cranelift, and one fresh canonical VM artifact agree with the byte-based "
            "SourceSpan convention for newline edges, UTF-8 byte columns, clamped offsets, empty input, "
            "and a 64 KiB source. This is a selected source-location frontier, not full Source module proof, "
            "Unicode-scalar column semantics, Language verified-fact reuse, or compiler succession."
        ),
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "evidence": str(evidence_path.relative_to(ROOT)),
        "compiler_revision_tested": report["compiler_revision_tested"],
        "source_sha256": source_hash,
        "target": report["target"],
        "page_strides": list(STRIDES),
        "request_count_per_executor": len(reference_rows),
        "canonical_vm": {"artifact_identity": artifact_id,
                         "wall_seconds": round(vm_wall, 6),
                         "peak_rss_kib": vm_peak_rss_kib,
                         "maximum_steps_per_call": max(row["steps"] for row in vm_rows)},
        "language_verified_facts_reused": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
