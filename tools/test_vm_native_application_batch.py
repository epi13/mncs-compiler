#!/usr/bin/env python3
"""Compile several selected MNCS callables into one canonical VM artifact.

The fixture is intentionally small: it verifies the versioned multi-callable
build contract, exact-snapshot process reuse, selected-callable cache
isolation, and one admitted VM batch containing both exported functions.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time

import test_vm_native_application as app

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(os.environ.get(
    "MNCS_BATCH_PROBE_BIN",
    ROOT / "tools/stage0-probe/target/debug/mncs-compiler-stage0-probe",
))
VM_BIN = Path(os.environ.get(
    "MNCS_BATCH_VM_BIN",
    app.VM_ROOT / "target/debug/mncs-vm",
))
CACHE_DIR = ROOT / ".build/native-app-readiness/batch-cache" / f"run-{os.getpid()}-{time.time_ns()}"
MODULE = "tests.native_batch"
SOURCE_ID = "tests/native_batch.mncs"
SOURCE = """mncs 0.18;
module tests.native_batch;
fn increment(value: u64) -> (result: u64) { return helper(value); }
fn helper(value: u64) -> (result: u64) { return value +% 1; }
fn decrement(value: u64) -> (result: u64) { return value -% 1; }
fn unused(value: u64) -> (result: u64) { return value +% 9; }
"""


def integer(value: int) -> dict:
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def request(roots: list[dict]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": SOURCE_ID,
        "root_source": SOURCE,
        "callable_roots": roots,
        "modules": {},
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "native-callable-set/1",
    }


def vm_batch(artifact: dict) -> tuple[dict, float, int | None]:
    cases = [
        {"id": "increment-41", "function": "increment", "arguments": [integer(41)]},
        {"id": "decrement-43", "function": "decrement", "arguments": [integer(43)]},
    ]
    calls = [{"id": case["id"], "request": {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": case["function"]},
        "arguments": case["arguments"],
        "type_arguments": [],
        "step_budget": 10_000,
    }} for case in cases]
    with tempfile.TemporaryDirectory(prefix="mncs-native-callable-batch-") as raw:
        directory = Path(raw)
        artifact_path = directory / "application.vm.json"
        calls_path = directory / "calls.json"
        output_path = directory / "results.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        calls_path.write_text(json.dumps(calls, sort_keys=True) + "\n")
        started = time.monotonic()
        completed = subprocess.run(
            ["/usr/bin/time", "-v", str(VM_BIN), "batch",
             "--artifact", str(artifact_path), "--calls", str(calls_path),
             "--output", str(output_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        wall = time.monotonic() - started
        if completed.returncode != 0:
            raise AssertionError(f"canonical VM batch failed: {completed.stderr[-3000:]}")
        peak_rss_kib = None
        match = re.search(r"Maximum resident set size \(kbytes\): (\d+)", completed.stderr)
        if match:
            peak_rss_kib = int(match.group(1))
        return json.loads(output_path.read_text()), wall, peak_rss_kib


def main() -> None:
    if not PROBE.is_file() or not VM_BIN.is_file():
        raise SystemExit(f"missing probe or canonical VM binary: {PROBE}, {VM_BIN}")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    roots = [
        {"module": MODULE, "function": "increment"},
        {"module": MODULE, "function": "decrement"},
    ]
    first_probe = app.Probe(CACHE_DIR, executable=PROBE)
    try:
        built, build_metrics = first_probe.send({"compile_application": request(roots)})
        first_probe.close()
    except BaseException:
        if first_probe.proc.poll() is None:
            first_probe.proc.kill()
            first_probe.proc.wait(timeout=5)
        raise
    if built.get("schema_version") != "mncs.compiler.application-build/5":
        raise AssertionError("multi-callable request did not return application-build/5")
    if built.get("status") != "verified" or built.get("cache", {}).get("status") != "miss":
        raise AssertionError(f"initial multi-callable build was not verified: {built.get('status')}")
    artifact = built.get("artifact", {}).get("value")
    if not isinstance(artifact, dict):
        raise AssertionError("multi-callable build omitted its canonical VM artifact")
    selected = {
        (row.get("module"), row.get("name"))
        for row in artifact.get("callables", [])
    }
    if selected != {(MODULE, "increment"), (MODULE, "decrement")}:
        raise AssertionError(f"artifact exported the wrong callable set: {selected}")
    rows = built.get("callable_scope", {}).get("callable_roots", [])
    if [(row.get("module"), row.get("function")) for row in rows] != [
            (MODULE, "decrement"), (MODULE, "increment")]:
        raise AssertionError(f"response did not report canonical selected callable identities: {rows}")
    if any(row.get("status") != "verified" for row in rows):
        raise AssertionError(f"a selected callable interface was not verified: {rows}")

    warm_probe = app.Probe(CACHE_DIR, executable=PROBE)
    try:
        warm, warm_metrics = warm_probe.send({"compile_application": request(roots)})
        warm_probe.close()
    except BaseException:
        if warm_probe.proc.poll() is None:
            warm_probe.proc.kill()
            warm_probe.proc.wait(timeout=5)
        raise
    if (warm.get("cache", {}).get("status") != "hit"
            or warm.get("artifact", {}).get("identity") != built["artifact"]["identity"]):
        raise AssertionError("fresh-process exact multi-callable reuse changed the artifact")
    if warm.get("cache", {}).get("verification_work", {}).get("front_end_invocations") != 0:
        raise AssertionError("exact multi-callable cache hit unexpectedly invoked the frontend")

    subset_probe = app.Probe(CACHE_DIR, executable=PROBE)
    try:
        subset, subset_metrics = subset_probe.send({"compile_application": request([roots[0]])})
        subset_probe.close()
    except BaseException:
        if subset_probe.proc.poll() is None:
            subset_probe.proc.kill()
            subset_probe.proc.wait(timeout=5)
        raise
    subset_callables = {
        row.get("name") for row in subset.get("artifact", {}).get("value", {}).get("callables", [])
    }
    if (subset.get("status") != "verified"
            or subset.get("cache", {}).get("status") != "miss"
            or subset_callables != {"increment"}):
        raise AssertionError("cache identity did not isolate a different selected callable set")

    negative_probe = app.Probe(CACHE_DIR, executable=PROBE)
    try:
        duplicate, _ = negative_probe.send(
            {"compile_application": request([roots[0], roots[0]])}
        )
        invalid_request = request(roots)
        invalid_request["root_source"] = SOURCE.replace(
            "return value +% 1;", "return value +% ;", 1
        )
        invalid, _ = negative_probe.send({"compile_application": invalid_request})
        missing_request = request([roots[0], {"module": MODULE, "function": "missing"}])
        missing, _ = negative_probe.send({"compile_application": missing_request})
        negative_probe.close()
    except BaseException:
        if negative_probe.proc.poll() is None:
            negative_probe.proc.kill()
            negative_probe.proc.wait(timeout=5)
        raise
    if (duplicate.get("schema_version") != "mncs.compiler.application-build/5"
            or duplicate.get("error", {}).get("code") != "MCA112"
            or duplicate.get("artifact") is not None):
        raise AssertionError(f"duplicate callable identity was not rejected: {duplicate}")
    if (invalid.get("schema_version") != "mncs.compiler.application-build/5"
            or invalid.get("status") != "rejected"
            or not invalid.get("diagnostics")
            or invalid.get("artifact") is not None
            or invalid.get("cache", {}).get("publication") != "not_published"):
        raise AssertionError(f"invalid changed source was not freshly rejected: {invalid}")
    if (missing.get("status") != "unsupported"
            or missing.get("artifact") is not None
            or not missing.get("unresolved_obligations")):
        raise AssertionError(f"unknown selected callable produced a false success: {missing}")

    vm_result, vm_wall, vm_peak_rss_kib = vm_batch(artifact)
    results = vm_result.get("results", [])
    expected = [42, 42]
    if len(results) != 2:
        raise AssertionError(f"VM batch returned the wrong number of results: {results}")
    for row, value in zip(results, expected):
        if row.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"VM call did not complete: {row}")
        returned = row.get("record", {}).get("returned", [])
        if len(returned) != 1 or app.vm_to_wire(returned[0]) != integer(value):
            raise AssertionError(f"unexpected result for {row.get('id')}: {returned}")

    report = {
        "schema_version": 1,
        "kind": "mncs.compiler.native-application-multi-callable-proof",
        "compiler_head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "probe_sha256": app.sha(PROBE.read_bytes()),
        "request_schema": "mncs.compiler.application-build-request/2",
        "response_schema": built["schema_version"],
        "artifact_identity": built["artifact"]["identity"],
        "selected_callables": rows,
        "fresh_process_reuse": {
            "status": warm.get("cache", {}).get("status"),
            "frontend_invocations": warm.get("cache", {}).get("verification_work", {}).get("front_end_invocations"),
            "artifact_identity": warm.get("artifact", {}).get("identity"),
        },
        "subset_cache_isolation": {
            "status": subset.get("cache", {}).get("status"),
            "artifact_callables": sorted(subset_callables),
        },
        "negative_cases": {
            "duplicate_callable_identity": duplicate.get("error", {}).get("code"),
            "invalid_source_status": invalid.get("status"),
            "invalid_source_publication": invalid.get("cache", {}).get("publication"),
            "missing_callable_status": missing.get("status"),
        },
        "vm_results": results,
        "measurements": {
            "cold_build": build_metrics,
            "fresh_process_hit": warm_metrics,
            "subset_build": subset_metrics,
            "canonical_vm_batch_wall_seconds": round(vm_wall, 6),
            "canonical_vm_batch_peak_rss_kib": vm_peak_rss_kib,
        },
        "language_fact_reuse": 0,
        "scope": "Two selected MNCS functions were emitted in one canonical VM artifact and called in one admitted batch. This proves the compiler contract slice only; it is not Forge, Environment, Store, or production mncs-test workflow evidence.",
    }
    report_path = ROOT / ".build/native-app-readiness/multi-callable.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
