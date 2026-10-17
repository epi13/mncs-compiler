"""Differential and performance frontier for the MNCS source fingerprint.

The source module computes FNV-1a over transported pages. This checks exact
results for short tails, page boundaries, and real 64/256 KiB inputs across
the reference interpreter, Cranelift, and a fresh canonical VM artifact.
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
FUNCTION = "fingerprint_global"
PAGE_BOUND = 1024
STRIDE_BOUND = 1024
STEP_BUDGET = 8_000_000
BASELINE_SOURCE_REVISION = "1c5795a8d063db0b1888a2d285ba9a89202802a5"
BASELINE_SOURCE_SHA256 = "52d68c0e5ab71f1badc996d7609dc6ae3d088051fea7cbbc9404d839efeec5bd"
BASELINE_REFERENCE_STEPS = {"fnv-64k": 918_480, "fnv-256k": 3_673_872}
BASELINE_REFERENCE_WALL_SECONDS = {"fnv-64k": 3.822357, "fnv-256k": 13.954254}
EVIDENCE = ROOT / "evidence" / "self-host-source-fingerprint-global-four-byte-20261017.json"

test_vm_emit.VM_REPO = native_app.VM_ROOT
test_vm_emit.VM_BIN = native_app.VM_BIN


def seeds() -> list[dict]:
    return [{
        "module": MODULE,
        "function": FUNCTION,
        "type_arguments": [cp0001.nat_arg(PAGE_BOUND), cp0001.nat_arg(STRIDE_BOUND)],
    }]


def fnv1a(data: bytes) -> int:
    value = 14695981039346656037
    for byte in data:
        value = ((value ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return value


def inputs() -> list[dict]:
    samples = [
        {"id": f"tail-{size}-stride-9", "raw": bytes((i * 37 + 191) & 0xFF for i in range(size)),
         "stride": 9}
        for size in range(18)
    ]
    samples.extend([
        {"id": "page-boundary-1023", "raw": bytes((i * 29 + 7) & 0xFF for i in range(1023)),
         "stride": 1024},
        {"id": "page-boundary-1024", "raw": bytes((i * 29 + 7) & 0xFF for i in range(1024)),
         "stride": 1024},
        {"id": "page-boundary-1025", "raw": bytes((i * 29 + 7) & 0xFF for i in range(1025)),
         "stride": 1024},
        {"id": "multiple-short-pages", "raw": bytes((i * 53 + 11) & 0xFF for i in range(79)),
         "stride": 17},
        {"id": "fnv-64k", "raw": b"a" * (64 * 1024), "stride": 1024},
        {"id": "fnv-256k", "raw": b"a" * (256 * 1024), "stride": 1024},
    ])
    return samples


def invoke_backend(backend: str, observations: list[dict]) -> tuple[list[dict], dict]:
    previous = os.environ.get("MNCS_PROBE_BACKEND")
    os.environ["MNCS_PROBE_BACKEND"] = backend
    probe = cp0001.Probe("source", MODULE, seeds())
    rows = []
    artifact = None
    try:
        for sample in observations:
            started = time.monotonic()
            response = probe.run_global_wire(
                MODULE,
                FUNCTION,
                cp0001.chunk(sample["raw"], sample["stride"]),
                sample["stride"],
                len(sample["raw"]),
                budget=STEP_BUDGET,
            )
            elapsed = time.monotonic() - started
            value = cp0001.decode(response["returned"][0])
            expected = fnv1a(sample["raw"])
            if value != expected:
                raise AssertionError(f"{backend} {sample['id']}: {value} != FNV-1a {expected}")
            rows.append({
                "id": sample["id"],
                "value": value,
                "steps": response["steps"],
                "wall_seconds": round(elapsed, 6),
            })
        if backend == "reference_interpreter":
            emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
            artifact = emitted.get("artifact")
            if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
                raise AssertionError(f"source module did not emit a canonical VM artifact: {emitted}")
    finally:
        probe.close()
        if previous is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = previous
    return rows, {"artifact": artifact}


def vm_cases(observations: list[dict]) -> list[dict]:
    return [{
        "id": sample["id"],
        "function": FUNCTION,
        "args": [
            cp0001.pages_value(cp0001.chunk(sample["raw"], sample["stride"])),
            cp0001.integer(sample["stride"]),
            cp0001.integer(len(sample["raw"])),
        ],
        "type_args": [cp0001.nat_arg(PAGE_BOUND), cp0001.nat_arg(STRIDE_BOUND)],
        "step_budget": STEP_BUDGET,
    } for sample in observations]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ephemeral-output", action="store_true")
    args = parser.parse_args()
    evidence_path = ROOT / ".build" / "source-fingerprint-global-four-byte-20261017.json" \
        if args.ephemeral_output else EVIDENCE

    if not test_vm_emit.PROBE.is_file():
        raise SystemExit(f"Stage-0 probe is missing: {test_vm_emit.PROBE}")
    test_vm_emit.ensure_vm()
    observations = inputs()
    child_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    source_path = ROOT / "src/compiler/source.mncs"
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    reference_rows, reference_meta = invoke_backend("reference_interpreter", observations)
    artifact = reference_meta["artifact"]
    cranelift_rows, _ = invoke_backend("cranelift", observations)
    if [row["value"] for row in cranelift_rows] != [row["value"] for row in reference_rows]:
        raise AssertionError("Cranelift fingerprint results differ from the reference interpreter")

    calls = vm_cases(observations)
    with tempfile.TemporaryDirectory(prefix="mncs-vm-source-fingerprint-") as raw_dir:
        directory = Path(raw_dir)
        artifact_path = directory / "source-fingerprint.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_rss_kib, calls_bytes, results_bytes = test_vm_segment.run_batch(
            artifact_path, calls, directory, module=MODULE
        )

    vm_rows = []
    for sample, result, expected_row in zip(observations, document.get("results", []), reference_rows):
        if result.get("id") != sample["id"] or result.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {sample['id']}: {result}")
        returned = result.get("record", {}).get("returned", [])
        if len(returned) != 1:
            raise AssertionError(f"canonical VM returned the wrong value count for {sample['id']}")
        value = cp0001.decode(test_vm_segment.vm_to_wire(returned[0]))
        if value != expected_row["value"] or value != fnv1a(sample["raw"]):
            raise AssertionError(f"canonical VM changed the fingerprint for {sample['id']}")
        vm_rows.append({
            "id": sample["id"],
            "value": value,
            "steps": result["record"]["usage"]["steps"],
        })

    child_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (child_after.ru_utime - child_before.ru_utime) + (
        child_after.ru_stime - child_before.ru_stime
    )
    reference_by_id = {row["id"]: row for row in reference_rows}
    current_large = {key: reference_by_id[key] for key in BASELINE_REFERENCE_STEPS}
    performance = {}
    for key, current in current_large.items():
        baseline_steps = BASELINE_REFERENCE_STEPS[key]
        performance[key] = {
            "baseline_reference_steps": baseline_steps,
            "candidate_reference_steps": current["steps"],
            "step_change_percent": round((current["steps"] / baseline_steps - 1) * 100, 2),
            "baseline_wall_seconds": BASELINE_REFERENCE_WALL_SECONDS[key],
            "candidate_wall_seconds": current["wall_seconds"],
            "wall_change_percent": round(
                (current["wall_seconds"] / BASELINE_REFERENCE_WALL_SECONDS[key] - 1) * 100, 2
            ),
        }

    toolchain = {
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "stage0_probe": str(test_vm_emit.PROBE),
        "stage0_probe_sha256": hashlib.sha256(test_vm_emit.PROBE.read_bytes()).hexdigest(),
        "vm_binary": str(test_vm_emit.VM_BIN),
        "vm_binary_sha256": hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest(),
        "vm_repository": str(test_vm_emit.VM_REPO),
        "vm_revision": subprocess.check_output(["git", "-C", str(test_vm_emit.VM_REPO), "rev-parse", "HEAD"], text=True).strip(),
    }
    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.source-fingerprint-global-four-byte-frontier",
        "status": "passed",
        "compiler_revision_tested": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "compiler_branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
        "selected_worktree": str(ROOT),
        "source_sha256": source_hash,
        "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "target": {"module": MODULE, "function": FUNCTION, "generic_arguments": [PAGE_BOUND, STRIDE_BOUND]},
        "toolchains": toolchain,
        "baseline": {
            "source_revision": BASELINE_SOURCE_REVISION,
            "source_sha256": BASELINE_SOURCE_SHA256,
            "reference_interpreter": {
                "steps": BASELINE_REFERENCE_STEPS,
                "wall_seconds": BASELINE_REFERENCE_WALL_SECONDS,
                "measurement_note": "Measured on the baseline checkout and then the candidate checkout in one same-toolchain run, with identical b'a' inputs; wall times are single-run workload observations.",
            },
        },
        "reference_interpreter": reference_rows,
        "cranelift": cranelift_rows,
        "canonical_vm": {
            "artifact_identity": artifact["artifact_id"],
            "results": vm_rows,
            "wall_seconds": round(vm_wall, 6),
            "peak_rss_kib": vm_peak_rss_kib,
            "calls_file_bytes": calls_bytes,
            "results_file_bytes": results_bytes,
        },
        "semantic_digest_sha256": hashlib.sha256(json.dumps(
            [[row["id"], row["value"]] for row in reference_rows], separators=(",", ":")
        ).encode()).hexdigest(),
        "measurements": {
            "performance_comparison": performance,
            "reference_and_cranelift_child_cpu_seconds": round(child_cpu, 6),
            "python_harness_peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "cgroup_memory": {
                name: ((Path("/sys/fs/cgroup") / name).read_text().strip()
                       if (Path("/sys/fs/cgroup") / name).exists() else None)
                for name in ("memory.current", "memory.max", "memory.peak")
            },
        },
        "request_count": len(observations),
        "language_verified_facts_reused": 0,
        "stage1_claimed": False,
        "stage2_claimed": False,
        "scope": "Four sequential FNV-1a bytes are folded per MNCS page-loop transition, preserving the exact 64-bit hash for empty input, all short-tail lengths, page boundaries, and 64/256 KiB inputs. Reference interpreter, Cranelift, and one fresh canonical VM artifact agree. This is a source-identity implementation frontier; it does not implement Language-owned verified-fact reuse or compiler succession.",
    }
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "evidence": str(evidence_path.relative_to(ROOT)),
        "compiler_revision_tested": report["compiler_revision_tested"],
        "source_sha256": source_hash,
        "reference_large_cases": [reference_by_id["fnv-64k"], reference_by_id["fnv-256k"]],
        "performance_comparison": performance,
        "canonical_vm": {"artifact_identity": artifact["artifact_id"], "case_count": len(vm_rows), "wall_seconds": round(vm_wall, 6), "peak_rss_kib": vm_peak_rss_kib},
        "language_verified_facts_reused": 0,
    }, indent=2))


if __name__ == "__main__":
    main()
