#!/usr/bin/env python3
"""Parse the actual Compiler lexer source header through the canonical VM."""
from __future__ import annotations

import hashlib
import json
import resource
from pathlib import Path
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_vm_parser_module_frontier as parser
import test_vm_segment
import test_vm_emit

ROOT = Path(__file__).resolve().parents[1]
RAW = (ROOT / "src/compiler/lexer.mncs").read_bytes()
STRIDE = 1024
PAGE_BOUND = 1024
BOUND = 1024
STEP_BUDGET = 8_000_000


def expected_header() -> dict[str, int]:
    version_start = RAW.index(b"0.18")
    module_name = b"mncs.compiler.lexer.v1"
    module_start = RAW.index(module_name)
    semicolon = RAW.index(b";", module_start)
    return {
        "code": 0,
        "start": 0,
        "end": 0,
        "version_start": version_start,
        "version_end": version_start + len(b"0.18"),
        "module_start": module_start,
        "module_end": module_start + len(module_name),
        "next_offset": semicolon + 1,
        "state": 8,
    }


def request() -> dict:
    return parser.request_global(RAW, STRIDE)


def main() -> dict:
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    overall_started = time.monotonic()
    req = request()
    expected = expected_header()

    reference_started = time.monotonic()
    reference = parser.open_probe(None)
    try:
        reference_status = reference.send({"execution_status": True})
        reference_result = reference.send(req)
        reference_value, reference_steps = parser.prior.decode_result(
            reference_result, "compiler-lexer-source-header"
        )
        if reference_value != expected:
            raise AssertionError({"reference": reference_value, "expected": expected})
        emitted = reference.send({"emit_vm_artifact": {"module": parser.MODULE}})
        artifact = emitted.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError({"artifact_emission": emitted})
    finally:
        reference.close()
    reference_wall = time.monotonic() - reference_started

    cranelift_started = time.monotonic()
    cranelift = parser.open_probe("cranelift")
    try:
        cranelift_status = cranelift.send({"execution_status": True})
        cranelift_result = cranelift.send(req)
        cranelift_value, cranelift_steps = parser.prior.decode_result(
            cranelift_result, "compiler-lexer-source-header"
        )
        if cranelift_value != reference_value:
            raise AssertionError({"reference": reference_value, "cranelift": cranelift_value})
    finally:
        cranelift.close()
    cranelift_wall = time.monotonic() - cranelift_started

    vm_case = {
        "id": "compiler-lexer-source-header",
        "function": "header_global",
        "args": req["arguments"],
        "type_args": req["type_arguments"],
        "step_budget": STEP_BUDGET,
    }
    vm_started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-compiler-source-header-") as tmp_name:
        tmp = Path(tmp_name)
        artifact_path = tmp / "parser.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall_reported, vm_peak_kib, calls_bytes, results_bytes = (
            test_vm_segment.run_batch(artifact_path, [vm_case], tmp, module=parser.MODULE)
        )
    vm_wall = time.monotonic() - vm_started
    rows = document.get("results", [])
    if len(rows) != 1 or rows[0].get("id") != vm_case["id"]:
        raise AssertionError({"canonical_vm_results": rows})
    row = rows[0]
    if row.get("outcome") != {"kind": "completed"}:
        raise AssertionError({"canonical_vm_outcome": row})
    vm_value = parser.frontend.decode(
        test_vm_segment.vm_to_wire(row["record"]["returned"][0])
    )
    if vm_value != reference_value:
        raise AssertionError({"reference": reference_value, "canonical_vm": vm_value})

    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after_children.ru_utime - before_children.ru_utime
                 + after_children.ru_stime - before_children.ru_stime)
    result = {
        "schema_version": "mncs.compiler.current-source-header-canonical-vm/1",
        "status": "passed_selected_current_source_header_frontier",
        "compiler_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "compiler_branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "vm_revision": subprocess.check_output(
            ["git", "-C", str(test_vm_emit.VM_REPO), "rev-parse", "HEAD"],
            text=True,
        ).strip(),
        "source_path": "src/compiler/lexer.mncs",
        "source_sha256": hashlib.sha256(RAW).hexdigest(),
        "source_bytes": len(RAW),
        "parser_source_sha256": hashlib.sha256(
            (ROOT / "src/compiler/parser.mncs").read_bytes()
        ).hexdigest(),
        "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "target_module": parser.MODULE,
        "target_function": "header_global",
        "type_arguments": [PAGE_BOUND, BOUND],
        "page_stride": STRIDE,
        "page_count": len(parser.prior.pages_for(RAW, STRIDE)),
        "expected_header": expected,
        "reference_interpreter_matches_expected": reference_value == expected,
        "cranelift_matches_reference": cranelift_value == reference_value,
        "canonical_vm_matches_reference": vm_value == reference_value,
        "canonical_vm_artifact_identity": artifact["artifact_id"],
        "canonical_vm_steps": row["record"]["usage"]["steps"],
        "measurements": {
            "reference_and_emission_wall_seconds": round(reference_wall, 6),
            "reference_steps": reference_steps,
            "cranelift_wall_seconds": round(cranelift_wall, 6),
            "cranelift_reported_steps": cranelift_steps,
            "canonical_vm_wall_seconds": round(vm_wall, 6),
            "canonical_vm_reported_wall_seconds": round(vm_wall_reported, 6),
            "canonical_vm_peak_rss_kib": vm_peak_kib,
            "canonical_vm_calls_bytes": calls_bytes,
            "canonical_vm_results_bytes": results_bytes,
            "child_cpu_seconds": round(child_cpu, 6),
            "total_wall_seconds": round(time.monotonic() - overall_started, 6),
        },
        "execution_status": {
            "reference_interpreter": "reference_interpreter",
            "cranelift": cranelift_status.get("backend"),
        },
        "scope": "The current lexer source file's declared module header was parsed by the current parser.header_global<P=1024,N=1024> implementation through the reference interpreter, Cranelift, and a fresh canonical VM artifact. This is a selected header parse only; declarations, bodies, semantic verification, Flow, SSA, and project compilation were not run.",
        "limitations": [
            "Only the leading MNCS version and module header were parsed; this does not demonstrate full Compiler source parsing.",
            "This proves no Language-owned verified-fact reuse and does not claim Stage-1 or Stage-2 succession.",
        ],
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_path = ROOT / f"evidence/self-host-compiler-source-header-canonical-vm-{timestamp}.json"
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["evidence_path"] = str(evidence_path.relative_to(ROOT))
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
