#!/usr/bin/env python3
"""Verify bounded and paged UTF-8 scalar helpers across Stage-0 and the VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import test_cp0001 as cp0001
import test_vm_emit
import test_vm_segment

ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.source.v1"
PAGE_BOUND = 1024
VIEW_BOUND = 1024
STEP_BUDGET = 8_000_000
FUNCTIONS = ("validate_utf8", "utf8_scalar", "validate_utf8_global", "utf8_scalar_global")
CORPUS = [
    ("empty", b""),
    ("ascii", b"MNCS source"),
    ("two_byte_lower_boundary", b"\xc2\x80"),
    ("two_byte_upper_boundary", b"\xdf\xbf"),
    ("three_byte_lower_boundary", b"\xe0\xa0\x80"),
    ("three_byte_middle", "café".encode()),
    ("three_byte_upper_boundary", b"\xef\xbf\xbf"),
    ("four_byte_lower_boundary", b"\xf0\x90\x80\x80"),
    ("four_byte_upper_boundary", b"\xf4\x8f\xbf\xbf"),
    ("stray_continuation", b"\x80"),
    ("impossible_lead", b"\xff"),
    ("overlong_two_byte", b"\xc0\x80"),
    ("overlong_three_byte", b"\xe0\x80\x80"),
    ("surrogate", b"\xed\xa0\x80"),
    ("above_unicode_max", b"\xf4\x90\x80\x80"),
    ("bad_middle_continuation", b"\xe2\x28\xa1"),
    ("truncated_two_byte", b"\xc2"),
    ("truncated_three_byte", b"\xe2\x82"),
    ("truncated_four_byte", b"\xf0\x90\x80"),
    ("valid_then_bad", b"A\xf0\x90\x80\x80\xed\xa0\x80"),
]
GENERIC_SEEDS = [
    {"module": MODULE, "function": "validate_utf8", "type_arguments": [cp0001.nat_arg(VIEW_BOUND)]},
    {"module": MODULE, "function": "utf8_scalar", "type_arguments": [cp0001.nat_arg(VIEW_BOUND)]},
    {"module": MODULE, "function": "validate_utf8_global", "type_arguments": [cp0001.nat_arg(PAGE_BOUND), cp0001.nat_arg(VIEW_BOUND)]},
    {"module": MODULE, "function": "utf8_scalar_global", "type_arguments": [cp0001.nat_arg(PAGE_BOUND), cp0001.nat_arg(VIEW_BOUND)]},
]


def expected_validation(raw: bytes) -> dict:
    index = 0
    while index < len(raw):
        lead = raw[index]
        if lead < 0x80:
            index += 1
            continue
        if 0xC2 <= lead <= 0xDF:
            need = 1
        elif 0xE0 <= lead <= 0xEF:
            need = 2
        elif 0xF0 <= lead <= 0xF4:
            need = 3
        else:
            return {"valid": False, "error_offset": index}
        for continuation_index in range(1, need + 1):
            byte_index = index + continuation_index
            if byte_index >= len(raw):
                return {"valid": False, "error_offset": len(raw)}
            continuation = raw[byte_index]
            if not 0x80 <= continuation <= 0xBF:
                return {"valid": False, "error_offset": byte_index}
            if continuation_index == 1 and (
                (lead == 0xE0 and continuation < 0xA0)
                or (lead == 0xED and continuation >= 0xA0)
                or (lead == 0xF0 and continuation < 0x90)
                or (lead == 0xF4 and continuation >= 0x90)
            ):
                return {"valid": False, "error_offset": byte_index}
        index += need + 1
    return {"valid": True, "error_offset": 0}


def expected_scalar(raw: bytes, at: int) -> dict:
    if at >= len(raw):
        return {"scalar": 0, "next": at, "valid": False}
    lead = raw[at]
    if lead < 0x80:
        return {"scalar": lead, "next": at + 1, "valid": True}
    if 0xC2 <= lead <= 0xDF:
        width = 2
    elif 0xE0 <= lead <= 0xEF:
        width = 3
    elif 0xF0 <= lead <= 0xF4:
        width = 4
    else:
        return {"scalar": 0, "next": at + 1, "valid": False}
    candidate = raw[at:at + width]
    try:
        decoded = candidate.decode("utf-8")
    except UnicodeDecodeError:
        return {"scalar": 0, "next": at + 1, "valid": False}
    if len(decoded) != 1:
        return {"scalar": 0, "next": at + 1, "valid": False}
    return {"scalar": ord(decoded), "next": at + width, "valid": True}


def check_backend(backend: str) -> tuple[list[dict], dict]:
    old_backend = os.environ.get("MNCS_PROBE_BACKEND")
    os.environ["MNCS_PROBE_BACKEND"] = backend
    probe = cp0001.Probe("source", MODULE, GENERIC_SEEDS)
    observed = []
    vm_cases = []
    try:
        for case_id, raw in CORPUS:
            expected = expected_validation(raw)
            actual = probe.run_single(MODULE, "validate_utf8", raw, bound=VIEW_BOUND)
            if actual != expected:
                raise AssertionError({"backend": backend, "case": case_id, "expected": expected, "actual": actual})
            observed.append({"kind": "bounded_validation", "id": case_id, **actual})
            vm_cases.append({
                "id": f"bounded-validation:{case_id}",
                "function": "validate_utf8",
                "args": [cp0001.byte_sequence(raw)],
                "type_args": [cp0001.nat_arg(VIEW_BOUND)],
                "expected": expected,
            })
            for offset in range(len(raw) + 1):
                expected_step = expected_scalar(raw, offset)
                actual_step = probe.run_single(
                    MODULE, "utf8_scalar", raw,
                    extra=(cp0001.integer(offset),), bound=VIEW_BOUND,
                )
                if actual_step != expected_step:
                    raise AssertionError({
                        "backend": backend, "case": case_id, "offset": offset,
                        "expected": expected_step, "actual": actual_step,
                    })
                observed.append({"kind": "bounded_scalar", "id": case_id, "offset": offset, **actual_step})
                vm_cases.append({
                    "id": f"bounded-scalar:{case_id}:{offset}",
                    "function": "utf8_scalar",
                    "args": [cp0001.byte_sequence(raw), cp0001.integer(offset)],
                    "type_args": [cp0001.nat_arg(VIEW_BOUND)],
                    "expected": expected_step,
                })

        global_cases = []
        for case_id, raw in CORPUS:
            for stride in (1, 2, 3, 4, 7, 1024):
                pages = cp0001.chunk(raw, stride)
                expected = expected_validation(raw)
                actual = probe.run_global(MODULE, "validate_utf8_global", pages, stride, len(raw))
                if actual != expected:
                    raise AssertionError({"backend": backend, "case": case_id, "stride": stride, "expected": expected, "actual": actual})
                global_cases.append({"id": case_id, "stride": stride, "result": actual})
                vm_cases.append({
                    "id": f"global-validation:{case_id}:stride-{stride}",
                    "function": "validate_utf8_global",
                    "args": [cp0001.pages_value(pages), cp0001.integer(stride), cp0001.integer(len(raw))],
                    "type_args": [cp0001.nat_arg(PAGE_BOUND), cp0001.nat_arg(VIEW_BOUND)],
                    "expected": expected,
                })
                for offset in range(len(raw) + 1):
                    expected_step = expected_scalar(raw, offset)
                    actual_step = probe.run_global(
                        MODULE, "utf8_scalar_global", pages, stride, len(raw),
                        extra=(cp0001.integer(offset),),
                    )
                    if actual_step != expected_step:
                        raise AssertionError({
                            "backend": backend, "case": case_id, "stride": stride,
                            "offset": offset, "expected": expected_step, "actual": actual_step,
                        })
                    vm_cases.append({
                        "id": f"global-scalar:{case_id}:stride-{stride}:offset-{offset}",
                        "function": "utf8_scalar_global",
                        "args": [cp0001.pages_value(pages), cp0001.integer(stride), cp0001.integer(len(raw)), cp0001.integer(offset)],
                        "type_args": [cp0001.nat_arg(PAGE_BOUND), cp0001.nat_arg(VIEW_BOUND)],
                        "expected": expected_step,
                    })
        emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError({"backend": backend, "emit_vm_artifact": emitted})
    finally:
        probe.close()
        if old_backend is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = old_backend
    return vm_cases, {"observed": observed, "global_cases": global_cases, "artifact": artifact}


def main() -> dict:
    test_vm_emit.ensure_probe()
    test_vm_emit.ensure_vm()
    reference_cases, reference = check_backend("reference_interpreter")
    cranelift_cases, cranelift = check_backend("cranelift")
    if reference_cases != cranelift_cases:
        raise AssertionError("reference interpreter and Cranelift used different test requests")
    artifact = reference["artifact"]
    calls = [{**row, "step_budget": STEP_BUDGET} for row in reference_cases]
    with tempfile.TemporaryDirectory(prefix="mncs-vm-source-utf8-") as raw_dir:
        directory = Path(raw_dir)
        artifact_path = directory / "source-utf8.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_rss_kib, calls_bytes, results_bytes = test_vm_segment.run_batch(
            artifact_path, calls, directory, module=MODULE
        )
        if len(document.get("results", [])) != len(reference_cases):
            raise AssertionError("canonical VM omitted UTF-8 scalar cases")
        for case, row in zip(reference_cases, document["results"]):
            if row.get("id") != case["id"] or row.get("outcome") != {"kind": "completed"}:
                raise AssertionError({"case": case["id"], "vm_result": row})
            returned = row.get("record", {}).get("returned", [])
            actual = cp0001.decode(test_vm_segment.vm_to_wire(returned[0])) if len(returned) == 1 else None
            if actual != case["expected"]:
                raise AssertionError({"case": case["id"], "expected": case["expected"], "actual": actual})

    source_path = ROOT / "src/compiler/source.mncs"
    return {
        "schema_version": "mncs.compiler.source-utf8-scalar-foundation/1",
        "status": "verified",
        "module": MODULE,
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "case_count": len(reference_cases),
        "validation_corpus_count": len(CORPUS),
        "page_strides": [1, 2, 3, 4, 7, 1024],
        "backend_agreement": {
            "reference_interpreter": True,
            "cranelift": True,
            "canonical_vm": True,
            "python_utf8_oracle": True,
        },
        "vm_artifact_id": artifact["artifact_id"],
        "canonical_vm_wall_seconds": round(vm_wall, 6),
        "canonical_vm_peak_rss_kib": vm_peak_rss_kib,
        "canonical_vm_calls_bytes": calls_bytes,
        "canonical_vm_results_bytes": results_bytes,
        "limitations": [
            "This script focuses on UTF-8 validation and scalar stepping; Unicode identifier, whitespace, lexer, parser-header, and source-location behavior are covered by the separate Unicode lexer and Project checks.",
            "This does not implement or measure Language-owned verified-fact reuse.",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, sort_keys=True))
