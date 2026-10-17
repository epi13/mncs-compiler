#!/usr/bin/env python3
"""Check and measure MNCS source ASCII admission on real compiler pages.

The selected source.ascii_global implementation runs in the reference
interpreter, Cranelift, and a freshly emitted canonical VM artifact. Large
inputs make the byte-scan cost visible while CP-0001 continues to cover short,
strided, malformed, and non-ASCII boundary cases.
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

import test_cp0001 as cp0001
import test_vm_native_application as native_app
import test_vm_emit
import test_vm_segment

ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.source.v1"
FUNCTION = "ascii_global"
STRIDE = 1024
PAGE_BOUND = 1024
SOURCE_BOUND = 1024
STEP_BUDGET = 8_000_000
EVIDENCE = ROOT / "evidence" / "self-host-source-ascii-global-early-exit-20261017.json"
PREVIOUS_SIXTEEN_BYTE_EVIDENCE = ROOT / "evidence" / "self-host-source-ascii-global-sixteen-byte-20261016.json"
PREVIOUS_EIGHT_BYTE_EVIDENCE = ROOT / "evidence" / "self-host-source-ascii-global-eight-byte-20261016.json"

test_vm_emit.VM_REPO = native_app.VM_ROOT
test_vm_emit.VM_BIN = native_app.VM_BIN


def seeds() -> list[dict]:
    return [{
        "module": MODULE,
        "function": FUNCTION,
        "type_arguments": [cp0001.nat_arg(PAGE_BOUND),
                           cp0001.nat_arg(SOURCE_BOUND)],
    }]


def inputs() -> list[dict]:
    length = 256 * 1024
    samples = [
        {"id": "ascii-64k", "raw": b"a" * (64 * 1024), "expected": True},
        {"id": "ascii-256k", "raw": b"a" * length, "expected": True},
        {"id": "nonascii-last-256k", "raw": b"a" * (length - 1) + b"\xff",
         "expected": False},
        {"id": "nonascii-first-256k", "raw": b"\xff" + b"a" * (length - 1),
         "expected": False},
    ]
    samples.extend(
        {"id": f"ascii-tail-{size}", "raw": b"a" * size,
         "expected": True, "stride": PAGE_BOUND}
        for size in range(34)
    )
    for offset in range(33):
        raw = bytearray(b"a" * 33)
        raw[offset] = 0xFF
        samples.append({
            "id": f"nonascii-offset-{offset}-strided-9",
            "raw": bytes(raw), "expected": False, "stride": 9,
        })
    return samples


def summarize_probe(probe: cp0001.Probe, sample: dict) -> tuple[bool, int, float]:
    raw = sample["raw"]
    stride = sample.get("stride", STRIDE)
    started = time.monotonic()
    result = probe.run_global_wire(
        MODULE, FUNCTION, cp0001.chunk(raw, stride), stride, len(raw),
        budget=STEP_BUDGET,
    )
    elapsed = time.monotonic() - started
    value = cp0001.decode(result["returned"][0])
    if value is not sample["expected"]:
        raise AssertionError(f"{sample['id']}: expected {sample['expected']}, got {value}")
    return value, result["steps"], elapsed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--ephemeral-output", action="store_true",
        help="write generated evidence under .build instead of evidence/",
    )
    args = parser.parse_args()
    evidence_path = (
        ROOT / ".build" / "source-ascii-global-early-exit-20261017.json"
        if args.ephemeral_output else EVIDENCE
    )
    if not test_vm_emit.PROBE.is_file():
        raise SystemExit(f"Stage-0 probe is missing: {test_vm_emit.PROBE}")
    test_vm_emit.ensure_vm()
    observations = inputs()
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    source_hash = hashlib.sha256((ROOT / "src/compiler/source.mncs").read_bytes()).hexdigest()
    results: dict[str, list[dict]] = {"reference_interpreter": [], "cranelift": []}

    prior_backend = os.environ.get("MNCS_PROBE_BACKEND")
    os.environ["MNCS_PROBE_BACKEND"] = "reference_interpreter"
    reference = cp0001.Probe("source", MODULE, seeds())
    try:
        for sample in observations:
            value, steps, elapsed = summarize_probe(reference, sample)
            results["reference_interpreter"].append({
                "id": sample["id"], "value": value, "steps": steps,
                "wall_seconds": round(elapsed, 6),
            })
        first_invalid = next(
            row for row in results["reference_interpreter"]
            if row["id"] == "nonascii-first-256k"
        )
        if first_invalid["steps"] >= 1_000:
            raise AssertionError(
                "ascii_global did not stop at the first invalid page: "
                f"{first_invalid['steps']} reference steps"
            )
        emitted = reference.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError(f"direct emitter returned no canonical VM artifact: {emitted}")
        rows = [row for row in artifact.get("generic_entrypoints", [])
                if row.get("generic_function") == FUNCTION]
        if not any(tuple(row.get("args_spellings", [])) == ("1024", "1024")
                   for row in rows):
            raise AssertionError(f"artifact omitted {FUNCTION}<1024,1024>: {rows}")
    finally:
        reference.close()

    os.environ["MNCS_PROBE_BACKEND"] = "cranelift"
    cranelift = cp0001.Probe("source", MODULE, seeds())
    try:
        for index, sample in enumerate(observations):
            value, steps, elapsed = summarize_probe(cranelift, sample)
            if value != results["reference_interpreter"][index]["value"]:
                raise AssertionError(f"{sample['id']}: Cranelift changed the reference result")
            results["cranelift"].append({
                "id": sample["id"], "value": value,
                "reported_steps": steps,
                "step_counter_note": "Cranelift request counter; not comparable to reference interpreter fuel",
                "wall_seconds": round(elapsed, 6),
            })
    finally:
        cranelift.close()
        if prior_backend is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = prior_backend

    vm_cases = [{
        "id": sample["id"],
        "function": FUNCTION,
        "args": [cp0001.pages_value(cp0001.chunk(sample["raw"], sample.get("stride", STRIDE))),
                 cp0001.integer(sample.get("stride", STRIDE)),
                 cp0001.integer(len(sample["raw"]))],
        "type_args": [cp0001.nat_arg(PAGE_BOUND),
                      cp0001.nat_arg(SOURCE_BOUND)],
        "step_budget": STEP_BUDGET,
    } for sample in observations]
    vm_results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="mncs-vm-source-ascii-") as raw_dir:
        directory = Path(raw_dir)
        artifact_path = directory / "source-ascii.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_rss_kib, calls_bytes, result_bytes = test_vm_segment.run_batch(
            artifact_path, vm_cases, directory, module=MODULE
        )
        for index, (sample, row) in enumerate(zip(observations, document["results"])):
            if row.get("id") != sample["id"] or row.get("outcome") != {"kind": "completed"}:
                raise AssertionError(f"{sample['id']}: canonical VM did not complete: {row}")
            returned = row["record"].get("returned", [])
            if len(returned) != 1:
                raise AssertionError(f"{sample['id']}: canonical VM returned the wrong value count")
            value = test_vm_segment.vm_to_wire(returned[0])["boolean"]["value"]
            expected = results["reference_interpreter"][index]["value"]
            if value is not expected or value != results["cranelift"][index]["value"]:
                raise AssertionError(f"{sample['id']}: canonical VM disagrees with other backends")
            vm_results.append({
                "id": sample["id"], "value": value,
                "steps": row["record"]["usage"]["steps"],
            })

    memory = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            memory[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            memory[name] = None
    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu_seconds = (
        after_children.ru_utime - before_children.ru_utime
        + after_children.ru_stime - before_children.ru_stime
    )
    previous_report = json.loads(PREVIOUS_EIGHT_BYTE_EVIDENCE.read_text(encoding="utf-8"))
    previous_reference = {
        row["id"]: row for row in previous_report["reference_interpreter"]
    }
    current_reference = {
        row["id"]: row for row in results["reference_interpreter"]
    }
    performance_comparison = {}
    for case_id in ("ascii-64k", "ascii-256k", "nonascii-last-256k",
                    "nonascii-first-256k"):
        previous = previous_reference[case_id]
        current = current_reference[case_id]
        performance_comparison[case_id] = {
            "previous_eight_byte_steps": previous["steps"],
            "current_sixteen_byte_steps": current["steps"],
            "step_change_percent": round(
                (current["steps"] / previous["steps"] - 1) * 100, 2),
            "previous_eight_byte_wall_seconds": previous["wall_seconds"],
            "current_sixteen_byte_wall_seconds": current["wall_seconds"],
            "wall_change_percent": round(
                (current["wall_seconds"] / previous["wall_seconds"] - 1) * 100, 2),
        }
    previous_sixteen_report = json.loads(
        PREVIOUS_SIXTEEN_BYTE_EVIDENCE.read_text(encoding="utf-8")
    )
    previous_sixteen_reference = {
        row["id"]: row for row in previous_sixteen_report["reference_interpreter"]
    }
    sixteen_byte_comparison = {}
    for case_id in ("ascii-64k", "ascii-256k", "nonascii-last-256k",
                    "nonascii-first-256k"):
        previous = previous_sixteen_reference[case_id]
        current = current_reference[case_id]
        sixteen_byte_comparison[case_id] = {
            "previous_sixteen_byte_steps": previous["steps"],
            "current_early_exit_steps": current["steps"],
            "step_change_percent": round(
                (current["steps"] / previous["steps"] - 1) * 100, 2),
            "previous_sixteen_byte_wall_seconds": previous["wall_seconds"],
            "current_early_exit_wall_seconds": current["wall_seconds"],
            "wall_change_percent": round(
                (current["wall_seconds"] / previous["wall_seconds"] - 1) * 100, 2),
        }
    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.source-ascii-global-first-invalid-page-frontier",
        "compiler_revision_tested": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "compiler_branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "stage0_probe_sha256": hashlib.sha256(test_vm_emit.PROBE.read_bytes()).hexdigest(),
        "vm_binary_sha256": hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest(),
        "vm_revision": subprocess.check_output(
            ["git", "-C", str(test_vm_emit.VM_REPO), "rev-parse", "HEAD"],
            text=True,
        ).strip(),
        "source_sha256": source_hash,
        "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "target": {"module": MODULE, "function": FUNCTION,
                   "generic_arguments": [PAGE_BOUND, SOURCE_BOUND]},
        "stride": STRIDE,
        "page_count_by_case": {
            sample["id"]: len(cp0001.chunk(sample["raw"], sample.get("stride", STRIDE)))
            for sample in observations
        },
        "reference_interpreter": results["reference_interpreter"],
        "cranelift": results["cranelift"],
        "canonical_vm": {
            "artifact_identity": artifact["artifact_id"],
            "results": vm_results,
            "wall_seconds": round(vm_wall, 6),
            "peak_rss_kib": vm_peak_rss_kib,
            "calls_file_bytes": calls_bytes,
            "results_file_bytes": result_bytes,
        },
        "resource_observation": {
            "reference_and_cranelift_child_cpu_seconds": round(child_cpu_seconds, 6),
            "python_harness_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "cgroup_memory": memory,
        },
        "previous_eight_byte_frontier": {
            "evidence_artifact": str(PREVIOUS_EIGHT_BYTE_EVIDENCE.relative_to(ROOT)),
            "source_sha256": previous_report["source_sha256"],
            "backend": "reference_interpreter",
            "observations": [previous_reference[case_id] for case_id in
                             ("ascii-64k", "ascii-256k",
                              "nonascii-last-256k", "nonascii-first-256k")],
            "measurement_note": (
                "The previous frontier used the same CP-0001 probe and page requests. "
                "Wall times are workload observations, not a general performance guarantee."
            ),
        },
        "performance_comparison_vs_previous_eight_byte_frontier": performance_comparison,
        "previous_sixteen_byte_frontier": {
            "evidence_artifact": str(PREVIOUS_SIXTEEN_BYTE_EVIDENCE.relative_to(ROOT)),
            "source_sha256": previous_sixteen_report["source_sha256"],
            "backend": "reference_interpreter",
            "observations": [previous_sixteen_reference[case_id] for case_id in
                             ("ascii-64k", "ascii-256k",
                              "nonascii-last-256k", "nonascii-first-256k")],
        },
        "performance_comparison_vs_previous_sixteen_byte_frontier": sixteen_byte_comparison,
        "language_verified_facts_reused": 0,
        "previous_four_byte_frontier": {
            "source_sha256": "28b185e4d0028dadca04535b09e567506fe71cded9fe1d79210bf332b0a3e44a",
            "evidence_artifact": "evidence/self-host-source-ascii-global-four-byte-20261016.json",
            "reference_interpreter_steps": {
                "ascii-64k": 740174,
                "ascii-256k": 2960654,
                "nonascii-last-256k": 2960628,
                "nonascii-first-256k": 3114,
            },
        },
        "scope": (
            "source.ascii_global and its bounded source.ascii helper inspect sixteen bytes per MNCS "
            "loop transition and return immediately when the first page containing a non-ASCII "
            "byte is found. ASCII tails "
            "from zero through thirty-three bytes and every non-ASCII position in a four-page "
            "thirty-three-byte view are checked alongside 64 KiB and 256 KiB cases. The selected "
            "source callable agrees across the reference interpreter, Cranelift, and one fresh "
            "canonical VM artifact. This is a bounded source-scanner frontier, not full "
            "source-module verification or compiler succession."
        ),
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    large_case_ids = ("ascii-64k", "ascii-256k", "nonascii-last-256k",
                      "nonascii-first-256k")
    print(json.dumps({
        "evidence": str(evidence_path.relative_to(ROOT)),
        "compiler_revision_tested": report["compiler_revision_tested"],
        "source_sha256": report["source_sha256"],
        "reference_large_cases": [row for row in results["reference_interpreter"]
                                  if row["id"] in large_case_ids],
        "cranelift_large_cases": [row for row in results["cranelift"]
                                  if row["id"] in large_case_ids],
        "canonical_vm": {
            "artifact_identity": artifact["artifact_id"],
            "case_count": len(vm_results),
            "wall_seconds": round(vm_wall, 6),
            "peak_rss_kib": vm_peak_rss_kib,
        },
        "performance_comparison_vs_previous_eight_byte_frontier": performance_comparison,
        "performance_comparison_vs_previous_sixteen_byte_frontier": sixteen_byte_comparison,
        "resource_observation": report["resource_observation"],
    }, indent=2))


if __name__ == "__main__":
    main()
