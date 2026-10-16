#!/usr/bin/env python3
"""Verify the compiler's bounded global token batch through the canonical VM.

The Stage-0 oracle supplies token expectations. The reference interpreter,
Cranelift, and one emitted VM artifact must agree on the complete token list,
cursor, count, and EOF result across several page layouts.
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

import test_vm_global_segment_frontier as frontier
import test_vm_segment as bounded_vm

ROOT = Path(__file__).resolve().parents[1]
MODULE = frontier.MODULE
FUNCTION = "lex_batch_from"
TYPE_ARGUMENTS = [frontier.nat_arg(frontier.PAGE_BOUND),
                  frontier.nat_arg(frontier.STRIDE_BOUND)]
STRIDES = (1, 7, 64, 256)
SAMPLE_INDICES = (2, 7, 13)
STEP_BUDGET = frontier.STEP_BUDGET
EVIDENCE = ROOT / "evidence" / "self-host-segment-batch-canonical-vm-20261016.json"

# The shared probe wrapper gets its generic entrypoint list from this module
# global. Keep this frontier's artifact limited to the function it exercises.
frontier.GLOBAL_FUNCTIONS = (FUNCTION,)


def decode_wire(value: dict):
    if "record" in value:
        return {name: decode_wire(item) for name, item in value["record"]["fields"]}
    if "finite" in value:
        finite = value["finite"]
        return {
            "discriminant": finite["discriminant"],
            "payload": {name: decode_wire(item) for name, item in finite.get("payload", [])},
        }
    if "integer" in value:
        return value["integer"]["value"]
    if "boolean" in value:
        return value["boolean"]["value"]
    raise AssertionError(f"unexpected token batch wire value: {value}")


def expected_batch(raw: bytes, oracle: dict, kind_ids: dict[str, int]) -> dict:
    diagnostics = {
        (item["span"]["start"], item["span"]["end"]): item["code"]
        for item in oracle["diagnostics"]
    }
    tokens = []
    for item in oracle["tokens"]:
        start, end = item["span"]["start"], item["span"]["end"]
        code = diagnostics.get((start, end), "")
        diagnostic = int(code[3:]) if code.startswith("MNL") else 0
        tokens.append({
            "kind": kind_ids[item["kind"]],
            "start": start,
            "end": end,
            "diagnostic": diagnostic,
        })
    tokens.append({"kind": 0, "start": len(raw), "end": len(raw), "diagnostic": 0})
    return {"tokens": tokens, "next": len(raw), "eof": True, "count": len(tokens)}


def normalize_batch(value: dict) -> dict:
    result = decode_wire(value)
    tokens = []
    node = result["tokens"]
    while node["discriminant"] == 1:
        payload = node["payload"]
        tokens.append(payload["head"])
        node = payload["tail"]
    if node["discriminant"] != 0 or node["payload"]:
        raise AssertionError(f"token list did not terminate in TNil: {node}")
    return {
        "tokens": tokens,
        "next": result["next"],
        "eof": result["eof"],
        "count": result["count"],
    }


def make_request(raw: bytes, stride: int) -> dict:
    pages = frontier.pages_wire(raw, stride)
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": FUNCTION},
        "arguments": [pages, frontier.integer(stride), frontier.integer(len(raw)),
                      frontier.integer(0)],
        "type_arguments": TYPE_ARGUMENTS,
        "step_budget": STEP_BUDGET,
    }


def semantic_digest(cases: list[dict], values: list[dict]) -> str:
    digest = hashlib.sha256()
    for case, value in zip(cases, values):
        digest.update(json.dumps([case["id"], value], sort_keys=True).encode() + b"\n")
    return digest.hexdigest()


def main() -> None:
    if not bounded_vm.PROBE.is_file():
        raise SystemExit(f"Stage-0 probe is missing: {bounded_vm.PROBE}")
    if not frontier.test_vm_emit.VM_BIN.is_file():
        raise SystemExit(f"canonical VM binary is missing: {frontier.test_vm_emit.VM_BIN}")
    if EVIDENCE.exists():
        raise SystemExit(f"refusing to overwrite immutable evidence: {EVIDENCE}")

    sample_pool = frontier.segment.samples(smoke=False)
    raw_inputs = [sample_pool[index] for index in SAMPLE_INDICES]
    kind_table = json.loads((ROOT / "src/compiler/token-kinds.json").read_text())
    kind_ids = {name: int(identity) for identity, name in kind_table.items()}
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    timings = {}

    reference = frontier.Probe(None)
    try:
        reference_status = reference.send({"execution_status": True})
        if reference_status.get("backend") == "cranelift":
            raise AssertionError("reference probe unexpectedly selected Cranelift")
        expectations = []
        cases = []
        oracle_started = time.monotonic()
        for source_index, raw in zip(SAMPLE_INDICES, raw_inputs):
            oracle_response = reference.send({"oracle": raw.decode("ascii")})
            expected = expected_batch(raw, oracle_response["lexical"], kind_ids)
            for stride in STRIDES:
                request = make_request(raw, stride)
                cases.append({
                    "id": f"sample-{source_index}@stride-{stride}",
                    "request": request,
                    "raw": raw,
                })
                expectations.append(expected)

        reference_values = []
        for case, expected in zip(cases, expectations):
            result = reference.send(case["request"])
            if result.get("status") != "returned":
                raise AssertionError(f"{case['id']}: reference failed: {result}")
            actual = normalize_batch(result["returned"][0])
            if actual != expected:
                raise AssertionError(f"{case['id']}: reference differs from Stage-0: {actual} != {expected}")
            reference_values.append(actual)
        timings["stage0_oracle_and_reference"] = round(time.monotonic() - oracle_started, 6)

        emitted = reference.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError(f"reference did not emit a canonical VM artifact: {emitted}")
        rows = [row for row in artifact.get("generic_entrypoints", [])
                if row.get("generic_function") == FUNCTION]
        if not any(row.get("args_spellings") == ["1024", "1024"] for row in rows):
            raise AssertionError("artifact omitted lex_batch_from<1024, 1024>")
    finally:
        reference_stderr = reference.close()

    backend = bounded_vm.backend_policy.resolve("segment", {})
    if backend != "cranelift":
        raise AssertionError(f"expected Cranelift segment backend, got {backend}")
    cranelift = frontier.Probe(backend)
    cranelift_values = []
    try:
        status = cranelift.send({"execution_status": True})
        if status.get("backend") != "cranelift":
            raise AssertionError(f"probe did not select Cranelift: {status}")
        t0 = time.monotonic()
        for case, expected in zip(cases, expectations):
            result = cranelift.send(case["request"])
            if result.get("status") != "returned":
                raise AssertionError(f"{case['id']}: Cranelift failed: {result}")
            actual = normalize_batch(result["returned"][0])
            if actual != expected:
                raise AssertionError(f"{case['id']}: Cranelift differs from Stage-0")
            cranelift_values.append(actual)
        timings["cranelift_execution"] = round(time.monotonic() - t0, 6)
    finally:
        cranelift_stderr = cranelift.close()

    vm_values = []
    vm_cases = [{
        "id": case["id"],
        "function": FUNCTION,
        "args": case["request"]["arguments"],
        "type_args": case["request"]["type_arguments"],
        "step_budget": STEP_BUDGET,
    } for case in cases]
    with tempfile.TemporaryDirectory(prefix="mncs-vm-segment-batch-") as directory:
        temp = Path(directory)
        artifact_path = temp / "segment-batch.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_rss, calls_bytes, results_bytes = bounded_vm.run_batch(
            artifact_path, vm_cases, temp, module=MODULE
        )
        timings["canonical_vm_batch"] = round(vm_wall, 6)
        for case, expected, result in zip(cases, expectations, document["results"]):
            if result.get("id") != case["id"] or result.get("outcome") != {"kind": "completed"}:
                raise AssertionError(f"{case['id']}: canonical VM failed: {result}")
            returned = [bounded_vm.vm_to_wire(value) for value in result["record"]["returned"]]
            if len(returned) != 1:
                raise AssertionError(f"{case['id']}: canonical VM returned wrong value count")
            actual = normalize_batch(returned[0])
            if actual != expected:
                raise AssertionError(f"{case['id']}: canonical VM differs from Stage-0")
            vm_values.append(actual)

    digests = {
        "reference_interpreter": semantic_digest(cases, reference_values),
        "cranelift": semantic_digest(cases, cranelift_values),
        "canonical_vm": semantic_digest(cases, vm_values),
    }
    if len(set(digests.values())) != 1:
        raise AssertionError(f"executor semantic digests differ: {digests}")

    compiler_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    compiler_branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip()
    stage0_revision = json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"]
    stage0_sha = hashlib.sha256(bounded_vm.PROBE.read_bytes()).hexdigest()
    vm_sha = hashlib.sha256(frontier.test_vm_emit.VM_BIN.read_bytes()).hexdigest()
    vm_repo = frontier.test_vm_emit.VM_BIN.parents[2]
    vm_revision = subprocess.check_output(["git", "-C", str(vm_repo), "rev-parse", "HEAD"], text=True).strip()
    source_hashes = {
        name: hashlib.sha256((ROOT / "src/compiler" / f"{name}.mncs").read_bytes()).hexdigest()
        for name in ("source", "lexer", "segment")
    }
    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after_children.ru_utime - before_children.ru_utime
                 + after_children.ru_stime - before_children.ru_stime)
    timings["whole_frontier"] = round(time.monotonic() - started, 6)
    report = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "segment-batch-canonical-vm-current-source",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "claim_boundary": (
            "Selected segment.lex_batch_from<1024, 1024> cases preserve complete token batches, "
            "cursor, count, and EOF across three source corpus entries and page strides 1, 7, 64, "
            "and 256. Stage-0 expectations, reference interpreter, Cranelift, and one canonical VM "
            "artifact agree. This is a selected batch frontier, not full segment/lexer module proof."
        ),
        "compiler": {
            "tested_source_revision": compiler_revision,
            "branch": compiler_branch,
            "selected_worktree": str(ROOT),
            "selected_modules": ["source", "lexer", "segment"],
            "module_source_sha256": source_hashes,
            "target_module": MODULE,
            "target_function": FUNCTION,
            "type_arguments": ["1024", "1024"],
            "test_harness": "tools/test_vm_global_segment_batch_frontier.py",
            "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "toolchains": {
            "stage0_probe": str(bounded_vm.PROBE),
            "stage0_probe_sha256": stage0_sha,
            "stage0_revision": stage0_revision,
            "vm_binary": str(frontier.test_vm_emit.VM_BIN),
            "vm_binary_sha256": vm_sha,
            "vm_revision": vm_revision,
            "artifact_identity": artifact["artifact_id"],
        },
        "verification": {
            "case_count": len(cases),
            "source_count": len(raw_inputs),
            "page_strides": list(STRIDES),
            "executor_case_counts": {
                "reference_interpreter": len(reference_values),
                "cranelift": len(cranelift_values),
                "canonical_vm": len(vm_values),
            },
            "semantic_digests": digests,
            "stage0_checks": "complete token list, offsets, diagnostics, count, cursor, and EOF passed",
            "stage1_claimed": False,
            "stage2_claimed": False,
        },
        "measurements": {
            "timing_seconds": timings,
            "child_cpu_seconds": round(child_cpu, 6),
            "child_peak_rss_kib": after_children.ru_maxrss,
            "canonical_vm_peak_rss_kib": vm_rss,
            "canonical_vm_calls_bytes": calls_bytes,
            "canonical_vm_results_bytes": results_bytes,
        },
        "cases": [
            {"id": case["id"], "source_bytes": len(case["raw"]),
             "source_sha256": hashlib.sha256(case["raw"]).hexdigest(),
             "expected_token_count": expected["count"]}
            for case, expected in zip(cases, expectations)
        ],
        "probe_stderr_bytes": {
            "reference": len(reference_stderr.encode()),
            "cranelift": len(cranelift_stderr.encode()),
        },
    }
    EVIDENCE.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({
        "status": "passed",
        "kind": report["kind"],
        "compiler_revision_tested": compiler_revision,
        "case_count": len(cases),
        "semantic_digests": digests,
        "artifact_identity": artifact["artifact_id"],
        "timing_seconds": timings,
        "canonical_vm_peak_rss_kib": vm_rss,
        "evidence_sha256": hashlib.sha256(EVIDENCE.read_bytes()).hexdigest(),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
