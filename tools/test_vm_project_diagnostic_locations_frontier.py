#!/usr/bin/env python3
"""Execute the Project diagnostic mapper in Stage-0 and the canonical VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PROJECT_MODULE = "mncs.compiler.project.v1"
TYPE_ARGS = [{"kind": "nat", "value": 2}, {"kind": "nat", "value": 1024}]
STEP_BUDGET = 8_000_000

os.environ["MNCS_PROBE_BACKEND"] = "reference_interpreter"
os.environ["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa,project"
os.environ["MNCS_PROBE_EXECUTION_MODULES"] = PROJECT_MODULE
os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
    "module": PROJECT_MODULE,
    "function": "locate_project_diagnostics_global",
    "type_arguments": TYPE_ARGS,
}])

import test_project as project
from test_vm_segment import run_batch, vm_to_wire
import test_vm_emit


def finite(type_name: str, variant: str, discriminant: int,
           payload=()):
    return {"finite": {
        "discriminant": discriminant,
        "payload": [[name, value] for name, value in payload],
        "type_identity": f"mncs:0.2:finite-type:{PROJECT_MODULE}::{type_name}",
        "variant_identity": (
            f"mncs:0.2:finite-variant:{PROJECT_MODULE}::{type_name}::{variant}"
        ),
    }}


def diagnostic(variant: str, discriminant: int, source_index: int,
               start: int = 0, end: int = 0):
    payload = [("source_index", project.integer(source_index))]
    if variant in {
        "DuplicateModule", "MissingImport", "ParseFailed", "ProofFailed",
        "FlowFailed", "ImportLimitExceeded",
    }:
        payload.extend([("start", project.integer(start)),
                        ("end", project.integer(end))])
    if variant == "FlowFailed":
        payload.append(("code", project.integer(0)))
    if variant == "ImportLimitExceeded":
        payload.append(("limit", project.integer(0)))
    return finite("ProjectDiagnostic", variant, discriminant, payload)


def diagnostic_list(items: list[dict]):
    result = finite("DiagnosticList", "DiagNil", 0)
    for item in reversed(items):
        result = finite("DiagnosticList", "DiagCons", 1,
                        [("head", item), ("tail", result)])
    return result


def source_request(identities, raw: bytes, *, source_index=0,
                   page_count_override=None, diagnostics=None):
    chunks = project.chunk(raw, 1024)
    source = project.source_value(
        identities, "project-diagnostic-source.mncs",
        "project-diagnostic-source.mncs", 0,
        len(chunks) if page_count_override is None else page_count_override,
        1024, len(raw),
    )
    return {
        "schema_version": "0.1",
        "target": {"module": PROJECT_MODULE,
                   "function": "locate_project_diagnostics_global"},
        "arguments": [
            {"sequence": {"values": [source]}},
            project.pages_value(chunks),
            diagnostic_list(diagnostics or [diagnostic(
                "MissingImport", 3, source_index, 0, 0)]),
        ],
        "type_arguments": TYPE_ARGS,
        "step_budget": STEP_BUDGET,
    }


def decode_locations(value):
    decoded = project.decode(value)
    rows = []
    node = decoded
    while node["$v"] == 1:
        rows.append(node["$p"]["head"])
        node = node["$p"]["tail"]
    assert node["$v"] == 0, node
    return rows


def line_col(raw: bytes, offset: int):
    prefix = raw[:offset]
    line = prefix.count(b"\n") + 1
    return line, offset - prefix.rfind(b"\n")


def rss_high_water_kib(pid: int):
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def main():
    test_vm_emit.ensure_vm()
    probe = project.Probe()
    started = time.monotonic()
    try:
        identities = project.identity_map(probe)
        raw = (b"mncs 0.18;\nmodule demo.multi_page;\n"
               + b"\n" * 1100
               + b"use demo.absent as absent;\n"
               + b"fn main() -> (r: u64) { return 1; }\n")
        assert len(raw) > 1024
        start = raw.index(b"demo.absent")
        end = start + len(b"demo.absent")
        cases = [
            {
                "id": "multi-page-two-spans",
                "raw": raw,
                "request": source_request(identities, raw, diagnostics=[
                    diagnostic("MissingImport", 3, 0, start, end),
                    diagnostic("ParseFailed", 4, 0, start, end),
                ]),
                "expected_spans": [(start, end), (start, end)],
            },
            {
                "id": "malformed-page-slice",
                "raw": raw,
                "request": source_request(
                    identities, raw, page_count_override=0,
                    diagnostics=[diagnostic("MissingImport", 3, 0, start, end)],
                ),
                "expected_spans": [(start, end)],
                "expect_mapped": False,
            },
            {
                "id": "location-free-diagnostic",
                "raw": raw,
                "request": source_request(
                    identities, raw,
                    diagnostics=[diagnostic("SourceCountMismatch", 1, 1)],
                ),
                "expected_spans": [None],
                "expect_mapped": False,
            },
            {
                "id": "invalid-span-source-index",
                "raw": raw,
                "request": source_request(
                    identities, raw,
                    diagnostics=[diagnostic("MissingImport", 3, 64, start, end)],
                ),
                "expected_spans": [(start, end)],
                "expect_mapped": False,
            },
            {
                "id": "out-of-range-span",
                "raw": raw,
                "request": source_request(
                    identities, raw,
                    diagnostics=[diagnostic("MissingImport", 3, 0, start, len(raw) + 1)],
                ),
                "expected_spans": [(start, len(raw) + 1)],
                "expect_mapped": False,
            },
            {
                "id": "reversed-span",
                "raw": raw,
                "request": source_request(
                    identities, raw,
                    diagnostics=[diagnostic("MissingImport", 3, 0, start + 1, start)],
                ),
                "expected_spans": [(start + 1, start)],
                "expect_mapped": False,
            },
        ]

        references = []
        for case in cases:
            result = probe.send(case["request"])
            assert result.get("status") == "returned", (case["id"], result)
            references.append(result)

        emission = probe.send({"emit_vm_artifact": {"module": PROJECT_MODULE}})
        artifact = emission.get("artifact")
        assert isinstance(artifact, dict), {
            "response_keys": sorted(emission),
            "status": emission.get("status"),
        }
        assert artifact.get("schema_version") == "mncs.vm.artifact/1", artifact.get("schema_version")
        assert "program" not in artifact
        assert artifact.get("artifact_id", "").startswith("sha256:")
        generic_rows = [
            row for row in artifact.get("generic_entrypoints", [])
            if row.get("generic_function") == "locate_project_diagnostics_global"
        ]
        assert len(generic_rows) == 1 and generic_rows[0].get("args_spellings") == ["2", "1024"], {
            "generic_entrypoint_count": len(generic_rows),
            "generic_entrypoints": generic_rows,
        }

        vm_cases = [{
            "id": case["id"],
            "function": "locate_project_diagnostics_global",
            "args": case["request"]["arguments"],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        } for case in cases]
        with tempfile.TemporaryDirectory(prefix="project-diagnostic-locations-") as directory:
            tmp = Path(directory)
            artifact_path = tmp / "project.vm.json"
            artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
            document, vm_wall, vm_peak_kib, calls_bytes, results_bytes = run_batch(
                artifact_path, vm_cases, tmp, module=PROJECT_MODULE)

        assert document.get("artifact_id") == artifact.get("artifact_id")
        vm_results = document.get("results", [])
        assert len(vm_results) == len(cases), document
        rows_summary = []
        typed_results = []
        for case, reference, vm_result in zip(cases, references, vm_results):
            assert vm_result.get("id") == case["id"]
            assert vm_result.get("outcome") == {"kind": "completed"}, vm_result
            vm_returned = vm_result.get("record", {}).get("returned", [])
            assert len(vm_returned) == 1, vm_result
            actual = project.decode(vm_to_wire(vm_returned[0]))
            expected = project.decode(reference["returned"][0])
            assert actual == expected, {"case": case["id"], "reference": expected, "vm": actual}
            typed_results.append({"case": case["id"], "result": actual})
            locations = decode_locations(reference["returned"][0])
            assert len(locations) == len(case["expected_spans"]), locations
            for location, byte_span in zip(locations, case["expected_spans"]):
                if byte_span is None:
                    assert location["has_span"] is False and location["mapped"] is False, location
                    continue
                if not case.get("expect_mapped", True):
                    assert location["has_span"] is True and location["mapped"] is False, location
                    assert location["start"] == {"line": 1, "col": 1}, location
                    assert location["end"] == {"line": 1, "col": 1}, location
                    continue
                assert location["mapped"] is True, location
                span_start, span_end = byte_span
                assert (location["start"]["line"], location["start"]["col"]) == line_col(raw, span_start), location
                assert (location["end"]["line"], location["end"]["col"]) == line_col(raw, span_end), location
            rows_summary.append({
                "case": case["id"],
                "reference_steps": reference["steps"],
                "vm_steps": vm_result.get("record", {}).get("usage", {}).get("steps"),
                "locations": locations,
            })

        source_bytes = (ROOT / "src/compiler/project.mncs").read_bytes()
        typed_result_sha256 = hashlib.sha256(json.dumps(
            typed_results, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        report = {
            "schema_version": 1,
            "kind": "mncs.compiler.project-diagnostic-location-canonical-vm-frontier",
            "status": "passed",
            "compiler_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "target_module": PROJECT_MODULE,
            "target_function": "locate_project_diagnostics_global",
            "type_arguments": [2, 1024],
            "source_sha256": "sha256:" + hashlib.sha256(source_bytes).hexdigest(),
            "artifact_identity": artifact["artifact_id"],
            "generic_entrypoint": generic_rows[0],
            "artifact_sha256": "sha256:" + hashlib.sha256(
                json.dumps(artifact, sort_keys=True).encode()).hexdigest(),
            "test_source_sha256": "sha256:" + hashlib.sha256(
                Path(__file__).read_bytes()).hexdigest(),
            "stage0_revision": json.loads(
                (ROOT / "mncs-language.lock.json").read_text())["revision"],
            "source_bytes": len(raw),
            "cases": rows_summary,
            "multi_page_line": line_col(raw, start)[0],
            "reference_vm_typed_results_equal": True,
            "typed_result_sha256": "sha256:" + typed_result_sha256,
            "language_verified_facts_reused": 0,
            "stage1_claimed": False,
            "stage2_claimed": False,
            "vm_wall_seconds": round(vm_wall, 6),
            "vm_peak_rss_kib": vm_peak_kib,
            "probe_peak_rss_kib": rss_high_water_kib(next(iter(probe._procs.values())).pid),
            "calls_bytes": calls_bytes,
            "results_bytes": results_bytes,
            "elapsed_seconds": round(time.monotonic() - started, 6),
        }
        output = ROOT / ".build" / "project-diagnostic-locations-canonical-vm-frontier.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
    finally:
        probe.close()


if __name__ == "__main__":
    main()
