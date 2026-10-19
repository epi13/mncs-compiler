#!/usr/bin/env python3
"""Execute current Source byte_at_global directly across three backends."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import test_cp0001 as cp0001
import test_vm_emit
import test_vm_segment
from mncs_source import extract_mncs_function, write_json_exclusive

ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.source.v1"
FUNCTION = "byte_at_global"
PAGE_BOUND = 1024
STRIDE_BOUND = 1024
STEP_BUDGET = 1_000_000


def cases() -> list[dict]:
    raw = bytes((0, 1, 127, 128, 255, 65, 66, 13, 10))
    result = []
    for stride in (1, 2, 3, 4, 7, 1024):
        pages = cp0001.chunk(raw, stride)
        for offset in range(len(raw) + 2):
            result.append({
                "id": f"valid-stride-{stride}-offset-{offset}",
                "pages": pages,
                "stride": stride,
                "total": len(raw),
                "offset": offset,
                "expected": raw[offset] if offset < len(raw) else 256,
                "layout": "validated-page-composition",
            })
    result.extend([
        {
            "id": "zero-stride",
            "pages": [b"abc"], "stride": 0, "total": 3, "offset": 0,
            "expected": 256, "layout": "malformed-total-function-sentinel",
        },
        {
            "id": "empty-source",
            "pages": [], "stride": 4, "total": 0, "offset": 0,
            "expected": 256, "layout": "empty-source-sentinel",
        },
        {
            "id": "missing-page",
            "pages": [b"abcd"], "stride": 4, "total": 8, "offset": 4,
            "expected": 256, "layout": "malformed-total-function-sentinel",
        },
        {
            "id": "short-page-edge",
            "pages": [b"a"], "stride": 4, "total": 4, "offset": 1,
            "expected": 256, "layout": "malformed-total-function-sentinel",
        },
    ])
    return result


def generic_seed() -> list[dict]:
    return [{
        "module": MODULE,
        "function": FUNCTION,
        "type_arguments": [cp0001.nat_arg(PAGE_BOUND), cp0001.nat_arg(STRIDE_BOUND)],
    }]


def run_backend(backend: str, observations: list[dict]) -> tuple[list[dict], dict | None]:
    prior = os.environ.get("MNCS_PROBE_BACKEND")
    os.environ["MNCS_PROBE_BACKEND"] = backend
    probe = cp0001.Probe("source", MODULE, generic_seed())
    rows = []
    artifact = None
    try:
        for sample in observations:
            started = time.monotonic()
            response = probe.run_global_wire(
                MODULE,
                FUNCTION,
                sample["pages"],
                sample["stride"],
                sample["total"],
                extra=(cp0001.integer(sample["offset"]),),
                budget=STEP_BUDGET,
            )
            elapsed = time.monotonic() - started
            actual = cp0001.decode(response["returned"][0])
            if actual != sample["expected"]:
                raise AssertionError(
                    f"{backend} {sample['id']}: {actual} != {sample['expected']}"
                )
            rows.append({
                "id": sample["id"],
                "value": actual,
                "steps": response["steps"],
                "wall_seconds": round(elapsed, 6),
            })
        if backend == "reference_interpreter":
            emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
            artifact = emitted.get("artifact")
            if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
                raise AssertionError(f"Source emitted no canonical VM artifact: {emitted}")
            roots = [
                row for row in artifact.get("generic_entrypoints", [])
                if row.get("generic_function") == FUNCTION
                and tuple(row.get("args_spellings", [])) == ("1024", "1024")
            ]
            if not roots:
                raise AssertionError("canonical VM artifact omits byte_at_global<1024,1024>")
    finally:
        probe.close()
        if prior is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = prior
    return rows, artifact


def vm_calls(observations: list[dict]) -> list[dict]:
    return [{
        "id": sample["id"],
        "function": FUNCTION,
        "args": [
            cp0001.pages_value(sample["pages"]),
            cp0001.integer(sample["stride"]),
            cp0001.integer(sample["total"]),
            cp0001.integer(sample["offset"]),
        ],
        "type_args": [cp0001.nat_arg(PAGE_BOUND), cp0001.nat_arg(STRIDE_BOUND)],
        "step_budget": STEP_BUDGET,
    } for sample in observations]


def source_memory_snapshot() -> dict | None:
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


def cgroup_memory_snapshot() -> dict:
    return {
        name: ((Path("/sys/fs/cgroup") / name).read_text().strip()
               if (Path("/sys/fs/cgroup") / name).exists() else None)
        for name in ("memory.current", "memory.max", "memory.peak")
    }


def evidence_path(ephemeral: bool) -> Path:
    if ephemeral:
        return ROOT / ".build/source-byte-at-global-canonical-vm-frontier.json"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return ROOT / "evidence" / f"self-host-source-byte-at-global-{stamp}-{os.getpid()}.json"


def publish_report(report: dict, output: Path, ephemeral: bool) -> None:
    if ephemeral:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    else:
        write_json_exclusive(output, report)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ephemeral-output", action="store_true")
    args = parser.parse_args()
    test_vm_emit.ensure_probe()
    test_vm_emit.ensure_vm()

    source_path = ROOT / "src/compiler/source.mncs"
    source = source_path.read_bytes()
    function_source, function_start, function_end = extract_mncs_function(source, FUNCTION)
    observations = cases()
    memory_before = source_memory_snapshot()
    cgroup_before = cgroup_memory_snapshot()
    children_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()

    reference_rows, artifact = run_backend("reference_interpreter", observations)
    cranelift_rows, _ = run_backend("cranelift", observations)
    if [row["value"] for row in cranelift_rows] != [row["value"] for row in reference_rows]:
        raise AssertionError("Cranelift byte_at_global results differ from the reference interpreter")

    calls = vm_calls(observations)
    vm_rows = []
    with tempfile.TemporaryDirectory(prefix="mncs-vm-source-byte-at-global-") as raw_dir:
        directory = Path(raw_dir)
        artifact_path = directory / "source-byte-at-global.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n", encoding="utf-8")
        document, vm_wall, vm_peak_kib, calls_bytes, results_bytes = test_vm_segment.run_batch(
            artifact_path, calls, directory, module=MODULE
        )
    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different Source artifact")
    vm_results = document.get("results")
    if not isinstance(vm_results, list) or len(vm_results) != len(observations):
        raise AssertionError(
            f"canonical VM returned {len(vm_results) if isinstance(vm_results, list) else 'no'} "
            f"results for {len(observations)} cases"
        )
    for sample, response, expected in zip(observations, vm_results, reference_rows):
        if response.get("id") != sample["id"] or response.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {sample['id']}: {response}")
        returned = response.get("record", {}).get("returned", [])
        actual = cp0001.decode(test_vm_segment.vm_to_wire(returned[0])) if len(returned) == 1 else None
        if actual != expected["value"] or actual != sample["expected"]:
            raise AssertionError(f"canonical VM {sample['id']}: {actual} != {sample['expected']}")
        vm_rows.append({
            "id": sample["id"],
            "value": actual,
            "steps": response["record"]["usage"]["steps"],
        })

    children_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (
        children_after.ru_utime - children_before.ru_utime
        + children_after.ru_stime - children_before.ru_stime
    )
    semantic_digest = hashlib.sha256(json.dumps(
        [[row["id"], row["value"]] for row in reference_rows],
        separators=(",", ":"),
    ).encode()).hexdigest()
    verification = {
        "reference_matches_expected": (
            len(reference_rows) == len(observations)
            and all(row["value"] == sample["expected"]
                    for row, sample in zip(reference_rows, observations))
        ),
        "cranelift_matches_reference": (
            len(cranelift_rows) == len(reference_rows)
            and [row["value"] for row in cranelift_rows]
            == [row["value"] for row in reference_rows]
        ),
        "canonical_vm_matches_reference": (
            len(vm_rows) == len(reference_rows)
            and [row["value"] for row in vm_rows]
            == [row["value"] for row in reference_rows]
        ),
        "result_counts_match": (
            len(reference_rows) == len(cranelift_rows) == len(vm_rows) == len(observations)
        ),
    }
    if not all(verification.values()):
        raise AssertionError(f"incomplete or divergent backend results: {verification}")
    vm_repository = Path(test_vm_emit.VM_REPO)
    vm_binary = Path(test_vm_emit.VM_BIN)
    report = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "source-byte-at-global-canonical-vm-frontier",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "compiler": {
            "tested_source_revision": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
            "selected_worktree": str(ROOT),
            "source_sha256": hashlib.sha256(source).hexdigest(),
            "function_source": {
                "name": FUNCTION,
                "span": [function_start, function_end],
                "sha256": hashlib.sha256(function_source).hexdigest(),
            },
            "harness": "tools/test_vm_source_byte_at_global_frontier.py",
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "toolchains": {
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "stage0_probe_sha256": hashlib.sha256(test_vm_emit.PROBE.read_bytes()).hexdigest(),
            "vm_revision": subprocess.check_output(["git", "-C", str(vm_repository), "rev-parse", "HEAD"], text=True).strip(),
            "vm_binary_sha256": hashlib.sha256(vm_binary.read_bytes()).hexdigest(),
        },
        "target": {
            "module": MODULE,
            "function": FUNCTION,
            "generic_arguments": [PAGE_BOUND, STRIDE_BOUND],
            "canonical_vm_artifact_id": artifact["artifact_id"],
        },
        "workload": {
            "case_count": len(observations),
            "cases": [{
                "id": sample["id"],
                "stride": sample["stride"],
                "total": sample["total"],
                "offset": sample["offset"],
                "expected": sample["expected"],
                "layout": sample["layout"],
            } for sample in observations],
        },
        "backends": {
            "reference_interpreter": reference_rows,
            "cranelift": cranelift_rows,
            "canonical_vm": vm_rows,
        },
        "verification": {
            **verification,
            "semantic_digest_sha256": semantic_digest,
            "language_verified_facts_reused": 0,
            "stage1_claimed": False,
            "stage2_claimed": False,
        },
        "measurements": {
            "canonical_vm_batch_seconds": round(vm_wall, 6),
            "whole_frontier_seconds": round(time.monotonic() - started, 6),
            "child_cpu_seconds": round(child_cpu, 6),
            "child_peak_rss_kib": children_after.ru_maxrss,
            "canonical_vm_peak_rss_kib": vm_peak_kib,
            "host_memory_before": memory_before,
            "cgroup_memory_before": cgroup_before,
            "cgroup_memory_after": cgroup_memory_snapshot(),
            "canonical_vm_batch_bytes": {"calls": calls_bytes, "results": results_bytes},
        },
        "scope": (
            "Directly executes the exact current source.byte_at_global definition through a fresh canonical VM artifact. "
            "Valid pages cover six strides and every byte offset through two past EOF; bounded malformed layouts cover "
            "zero stride, empty input, absent page, and short-page edge sentinels. Reference interpreter, Cranelift, and "
            "canonical VM agree. This does not prove the full Source module, Language-owned fact reuse, or compiler succession."
        ),
    }
    output = evidence_path(args.ephemeral_output)
    report["evidence_artifact"] = output.relative_to(ROOT).as_posix()
    publish_report(report, output, args.ephemeral_output)
    print(json.dumps({
        "evidence_artifact": output.relative_to(ROOT).as_posix(),
        "status": report["status"],
        "tested_source_revision": report["compiler"]["tested_source_revision"],
        "function_sha256": report["compiler"]["function_source"]["sha256"],
        "case_count": len(observations),
        "artifact_id": artifact["artifact_id"],
        "semantic_digest_sha256": semantic_digest,
        "language_verified_facts_reused": 0,
        "measurements": report["measurements"],
    }, indent=2))


if __name__ == "__main__":
    main()
