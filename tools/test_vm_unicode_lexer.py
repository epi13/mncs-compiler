#!/usr/bin/env python3
"""Compare Unicode lexer, header, and source locations with Stage-0."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time

import test_cp0001 as cp
import test_vm_emit
import test_vm_native_application as native_app
import test_vm_segment

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "mncs.compiler.source.v1"
SEGMENT = "mncs.compiler.segment.v1"
PARSER = "mncs.compiler.parser.v1"
PAGE_BOUND = 1024
VIEW_BOUND = 1024
STEP_BUDGET = 8_000_000
STRIDES = (1, 2, 3, 4, 7, 64, 1024)
TEXTS = [
    "café value2",
    "_Αλφα βeta",
    "Библиотека变量",
    "कर्म",
    "𐐀deseret 𝔘",
    "name١٢!",
    "a\u0301",
    "\u00a0left\u2003right\u2028x",
    "// café\nvalue",
    "/* 漢字 */变量",
    "a😀b",
    "\u200bname",
]
MALFORMED_COMMENT_CASES = [
    ("line-comment-bad-continuation", b"// ok \xe2(\xa1\n", 6),
    ("block-comment-invalid-lead", b"/* ok \xff */", 6),
    ("block-comment-truncated-scalar", b"/* ok \xf0\x9f", 6),
]
HEADERS = [
    "mncs 0.18; module café.demo;",
    "mncs 0.18; module 𐐀.demo;",
    "mncs 0.18; // Ελληνικά\nmodule café.demo;",
]

test_vm_emit.VM_REPO = native_app.VM_ROOT
test_vm_emit.VM_BIN = native_app.VM_BIN


def token_rows(oracle: dict) -> list[dict]:
    kind_ids = {value: int(key) for key, value in
                json.loads((ROOT / "src/compiler/token-kinds.json").read_text()).items()}
    diagnostics = {
        (item["span"]["start"], item["span"]["end"]): item["code"]
        for item in oracle["diagnostics"]
    }
    rows = []
    for token in oracle["tokens"]:
        span = token["span"]
        code = diagnostics.get((span["start"], span["end"]))
        rows.append({
            "kind": kind_ids[token["kind"]],
            "start": span["start"],
            "end": span["end"],
            "diagnostic": {"MNL001": 1, "MNL002": 2}.get(code, 0),
        })
    return rows


def boundary_offsets(text: str) -> list[tuple[int, int, int]]:
    rows = []
    offset = 0
    line = 1
    column = 1
    for scalar in text:
        rows.append((offset, line, column))
        encoded = scalar.encode("utf-8")
        offset += len(encoded)
        if scalar == "\n":
            line += 1
            column = 1
        else:
            column += 1
    rows.append((offset, line, column))
    return rows


def probe_seeds() -> list[dict]:
    return [
        {"module": SEGMENT, "function": "next_token",
         "type_arguments": [cp.nat_arg(VIEW_BOUND)]},
        {"module": SEGMENT, "function": "significant",
         "type_arguments": [cp.nat_arg(VIEW_BOUND)]},
        {"module": SEGMENT, "function": "next_token_global",
         "type_arguments": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(VIEW_BOUND)]},
        {"module": SEGMENT, "function": "significant_global",
         "type_arguments": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(VIEW_BOUND)]},
        {"module": PARSER, "function": "header",
         "type_arguments": [cp.nat_arg(VIEW_BOUND)]},
        {"module": PARSER, "function": "header_global",
         "type_arguments": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(VIEW_BOUND)]},
        {"module": SOURCE, "function": "line_col",
         "type_arguments": [cp.nat_arg(VIEW_BOUND)]},
        {"module": SOURCE, "function": "line_col_global",
         "type_arguments": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(VIEW_BOUND)]},
    ]


def request_for(probe: cp.Probe, module: str, function: str, args: list[dict],
                type_args: list[dict], budget: int = STEP_BUDGET) -> dict:
    result = probe.send({
        "schema_version": "0.1",
        "target": {"module": module, "function": function},
        "arguments": args,
        "type_arguments": type_args,
        "step_budget": budget,
    })
    if result.get("status") != "returned":
        raise AssertionError({"module": module, "function": function, "result": result})
    return cp.decode(result["returned"][0])


def run_tokens(probe: cp.Probe, raw: bytes, expected: list[dict], stride: int | None) -> list[dict]:
    actual = []
    cursor = 0
    while cursor < len(raw):
        if stride is None:
            row = probe.run_single(
                SEGMENT, "next_token", raw, extra=(cp.integer(cursor),), bound=VIEW_BOUND,
            )
        else:
            pages = cp.chunk(raw, stride)
            row = probe.run_global(
                SEGMENT, "next_token_global", pages, stride, len(raw),
                extra=(cp.integer(cursor),),
            )
        actual.append(row)
        if row["end"] <= cursor:
            raise AssertionError({"cursor": cursor, "token": row})
        cursor = row["end"]
        if len(actual) > len(raw) + 1:
            raise AssertionError("lexer failed to make bounded progress")
    if actual != expected:
        raise AssertionError({"stride": stride, "expected": expected, "actual": actual})
    if stride is None:
        eof = probe.run_single(
            SEGMENT, "next_token", raw, extra=(cp.integer(len(raw)),), bound=VIEW_BOUND,
        )
    else:
        eof = probe.run_global(
            SEGMENT, "next_token_global", cp.chunk(raw, stride), stride, len(raw),
            extra=(cp.integer(len(raw)),),
        )
    if eof != {"kind": 0, "start": len(raw), "end": len(raw), "diagnostic": 0}:
        raise AssertionError({"stride": stride, "eof": eof})
    return actual


def cgroup_snapshot() -> dict:
    result = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            result[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            result[name] = None
    return result


def main() -> dict:
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    memory_before = cgroup_snapshot()
    started = time.monotonic()
    modules = "source,lexer,segment,parser"
    execution_modules = f"{SEGMENT},{PARSER},{SOURCE}"
    previous_backend = os.environ.get("MNCS_PROBE_BACKEND")
    os.environ["MNCS_PROBE_BACKEND"] = "reference_interpreter"
    reference = cp.Probe(modules, execution_modules, probe_seeds())
    vm_tokens: list[dict] = []
    vm_headers: list[dict] = []
    vm_locations: list[dict] = []
    malformed_comment_summaries: list[dict] = []
    token_count = 0
    try:
        reference.send({"execution_status": True})
        source_cases = []
        for text in TEXTS:
            raw = text.encode("utf-8")
            oracle = reference.send({"oracle": text})["lexical"]
            expected = token_rows(oracle)
            bounded = run_tokens(reference, raw, expected, None)
            token_count += len(bounded)
            for index, token in enumerate(bounded):
                vm_tokens.append({
                    "id": f"bounded:{len(source_cases)}:{index}",
                    "function": "next_token",
                    "args": [cp.byte_sequence(raw), cp.integer(token["start"])],
                    "type_args": [cp.nat_arg(VIEW_BOUND)],
                    "step_budget": STEP_BUDGET,
                    "expected": token,
                })
            for stride in STRIDES:
                rows = run_tokens(reference, raw, expected, stride)
                for index, token in enumerate(rows):
                    vm_tokens.append({
                        "id": f"global:{len(source_cases)}:{stride}:{index}",
                        "function": "next_token_global",
                        "args": [cp.pages_value(cp.chunk(raw, stride)), cp.integer(stride),
                                 cp.integer(len(raw)), cp.integer(token["start"])],
                        "type_args": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(VIEW_BOUND)],
                        "step_budget": STEP_BUDGET,
                        "expected": token,
                    })
            locations = []
            for offset, line, column in boundary_offsets(text):
                expected_location = {"line": line, "col": column}
                actual_location = reference.run_single(
                    SOURCE, "line_col", raw, extra=(cp.integer(offset),), bound=VIEW_BOUND,
                )
                if actual_location != expected_location:
                    raise AssertionError({"text": text, "offset": offset,
                                          "expected": expected_location,
                                          "actual": actual_location})
                locations.append((offset, expected_location))
                vm_locations.append({
                    "id": f"bounded-location:{len(source_cases)}:{offset}",
                    "function": "line_col",
                    "args": [cp.byte_sequence(raw), cp.integer(offset)],
                    "type_args": [cp.nat_arg(VIEW_BOUND)],
                    "step_budget": STEP_BUDGET,
                    "expected": expected_location,
                })
                for stride in STRIDES:
                    global_location = reference.run_global(
                        SOURCE, "line_col_global", cp.chunk(raw, stride), stride, len(raw),
                        extra=(cp.integer(offset),),
                    )
                    if global_location != expected_location:
                        raise AssertionError({"text": text, "stride": stride,
                                              "offset": offset,
                                              "expected": expected_location,
                                              "actual": global_location})
                    vm_locations.append({
                        "id": f"global-location:{len(source_cases)}:{stride}:{offset}",
                        "function": "line_col_global",
                        "args": [cp.pages_value(cp.chunk(raw, stride)), cp.integer(stride),
                                 cp.integer(len(raw)), cp.integer(offset)],
                        "type_args": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(VIEW_BOUND)],
                        "step_budget": STEP_BUDGET,
                        "expected": expected_location,
                    })
            source_cases.append({"text": text, "tokens": len(expected),
                                 "location_offsets": len(locations)})

        for case_id, raw, error_offset in MALFORMED_COMMENT_CASES:
            expected = {"kind": 7, "start": error_offset,
                        "end": error_offset + 1, "diagnostic": 3}
            bounded_rows = {}
            for function in ("next_token", "significant"):
                actual = reference.run_single(
                    SEGMENT, function, raw, extra=(cp.integer(0),), bound=VIEW_BOUND,
                )
                if actual != expected:
                    raise AssertionError({"case": case_id, "function": function,
                                          "expected": expected, "actual": actual})
                bounded_rows[function] = actual
                vm_tokens.append({
                    "id": f"malformed-comment:{case_id}:bounded:{function}",
                    "function": function,
                    "args": [cp.byte_sequence(raw), cp.integer(0)],
                    "type_args": [cp.nat_arg(VIEW_BOUND)],
                    "step_budget": STEP_BUDGET,
                    "expected": expected,
                })
            global_case_count = 0
            for stride in STRIDES:
                pages = cp.chunk(raw, stride)
                for function in ("next_token_global", "significant_global"):
                    actual = reference.run_global(
                        SEGMENT, function, pages, stride, len(raw),
                        extra=(cp.integer(0),),
                    )
                    if actual != expected:
                        raise AssertionError({"case": case_id, "stride": stride,
                                              "function": function,
                                              "expected": expected, "actual": actual})
                    vm_tokens.append({
                        "id": f"malformed-comment:{case_id}:global:{stride}:{function}",
                        "function": function,
                        "args": [cp.pages_value(pages), cp.integer(stride),
                                 cp.integer(len(raw)), cp.integer(0)],
                        "type_args": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(VIEW_BOUND)],
                        "step_budget": STEP_BUDGET,
                        "expected": expected,
                    })
                    global_case_count += 1
            malformed_comment_summaries.append({
                "id": case_id, "input_sha256": hashlib.sha256(raw).hexdigest(),
                "error_offset": error_offset, "expected": expected,
                "bounded_functions": sorted(bounded_rows),
                "global_function_stride_cases": global_case_count,
            })

        for header_index, text in enumerate(HEADERS):
            raw = text.encode("utf-8")
            bounded = reference.run_single(PARSER, "header", raw, bound=VIEW_BOUND)
            if bounded["code"] != 0 or bounded["state"] != 8:
                raise AssertionError({"header": text, "result": bounded})
            vm_headers.append({
                "id": f"bounded-header:{header_index}",
                "function": "header",
                "args": [cp.byte_sequence(raw)],
                "type_args": [cp.nat_arg(VIEW_BOUND)],
                "step_budget": STEP_BUDGET,
                "expected": bounded,
            })
            for stride in STRIDES:
                global_header = reference.run_global(
                    PARSER, "header_global", cp.chunk(raw, stride), stride, len(raw),
                )
                if global_header != bounded:
                    raise AssertionError({"header": text, "stride": stride,
                                          "bounded": bounded, "global": global_header})
                vm_headers.append({
                    "id": f"global-header:{header_index}:{stride}",
                    "function": "header_global",
                    "args": [cp.pages_value(cp.chunk(raw, stride)), cp.integer(stride),
                             cp.integer(len(raw))],
                    "type_args": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(VIEW_BOUND)],
                    "step_budget": STEP_BUDGET,
                    "expected": bounded,
                })

        # Invalid source bytes are not representable in a Rust String. The
        # Source UTF-8 validator corpus separately anchors those diagnostics.
        malformed = reference.run_single(PARSER, "header", b"mncs \xc2;", bound=VIEW_BOUND)
        if malformed["code"] != 8 or malformed["start"] != 6 or malformed["end"] != 7:
            raise AssertionError({"malformed_header": malformed})

        segment_artifact = reference.send({"emit_vm_artifact": {"module": SEGMENT}})["artifact"]
        parser_artifact = reference.send({"emit_vm_artifact": {"module": PARSER}})["artifact"]
        source_artifact = reference.send({"emit_vm_artifact": {"module": SOURCE}})["artifact"]
        if any(not isinstance(item, dict) or item.get("schema_version") != "mncs.vm.artifact/1"
               for item in (segment_artifact, parser_artifact, source_artifact)):
            raise AssertionError("a Unicode frontier artifact did not use mncs.vm.artifact/1")
    finally:
        reference.close()
        if previous_backend is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = previous_backend

    os.environ["MNCS_PROBE_BACKEND"] = "cranelift"
    cranelift = cp.Probe(modules, execution_modules, probe_seeds())
    try:
        for text in TEXTS:
            raw = text.encode("utf-8")
            expected = token_rows(cranelift.send({"oracle": text})["lexical"])
            run_tokens(cranelift, raw, expected, None)
            for stride in STRIDES:
                run_tokens(cranelift, raw, expected, stride)
            for offset, line, column in boundary_offsets(text):
                value = cranelift.run_single(
                    SOURCE, "line_col", raw, extra=(cp.integer(offset),), bound=VIEW_BOUND,
                )
                if value != {"line": line, "col": column}:
                    raise AssertionError((text, offset, value))
                for stride in STRIDES:
                    global_value = cranelift.run_global(
                        SOURCE, "line_col_global", cp.chunk(raw, stride), stride, len(raw),
                        extra=(cp.integer(offset),),
                    )
                    if global_value != {"line": line, "col": column}:
                        raise AssertionError((text, stride, offset, global_value))
        for case_id, raw, error_offset in MALFORMED_COMMENT_CASES:
            expected = {"kind": 7, "start": error_offset,
                        "end": error_offset + 1, "diagnostic": 3}
            for function in ("next_token", "significant"):
                actual = cranelift.run_single(
                    SEGMENT, function, raw, extra=(cp.integer(0),), bound=VIEW_BOUND,
                )
                if actual != expected:
                    raise AssertionError({"backend": "cranelift", "case": case_id,
                                          "function": function, "expected": expected,
                                          "actual": actual})
            for stride in STRIDES:
                pages = cp.chunk(raw, stride)
                for function in ("next_token_global", "significant_global"):
                    actual = cranelift.run_global(
                        SEGMENT, function, pages, stride, len(raw),
                        extra=(cp.integer(0),),
                    )
                    if actual != expected:
                        raise AssertionError({"backend": "cranelift", "case": case_id,
                                              "stride": stride, "function": function,
                                              "expected": expected, "actual": actual})
        for text in HEADERS:
            raw = text.encode("utf-8")
            bounded = cranelift.run_single(PARSER, "header", raw, bound=VIEW_BOUND)
            if bounded["code"] != 0:
                raise AssertionError({"backend": "cranelift", "header": text,
                                     "result": bounded})
            for stride in STRIDES:
                global_header = cranelift.run_global(
                    PARSER, "header_global", cp.chunk(raw, stride), stride, len(raw),
                )
                if global_header != bounded:
                    raise AssertionError({"backend": "cranelift", "header": text,
                                          "stride": stride, "result": global_header})
        cranelift.close()
    except Exception:
        try:
            cranelift.proc.kill()
        except Exception:
            pass
        raise
    finally:
        if previous_backend is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = previous_backend

    vm_started = time.monotonic()
    vm_peak_rss = None
    vm_calls_bytes = 0
    vm_results_bytes = 0
    with tempfile.TemporaryDirectory(prefix="mncs-vm-unicode-") as raw_dir:
        directory = Path(raw_dir)
        rows_by_module = [
            (SEGMENT, segment_artifact, vm_tokens),
            (PARSER, parser_artifact, vm_headers),
            (SOURCE, source_artifact, vm_locations),
        ]
        for module, artifact, cases in rows_by_module:
            artifact_path = directory / f"{module.rsplit('.', 2)[-2]}.vm.json"
            artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
            document, wall, peak, calls_bytes, results_bytes = test_vm_segment.run_batch(
                artifact_path, cases, directory, module=module,
            )
            vm_peak_rss = max(vm_peak_rss or 0, peak or 0)
            vm_calls_bytes += calls_bytes
            vm_results_bytes += results_bytes
            if len(document.get("results", [])) != len(cases):
                raise AssertionError({"module": module, "expected_calls": len(cases),
                                      "actual_calls": len(document.get("results", []))})
            for case, row in zip(cases, document["results"]):
                if row.get("id") != case["id"] or row.get("outcome") != {"kind": "completed"}:
                    raise AssertionError({"case": case["id"], "result": row})
                returned = row.get("record", {}).get("returned", [])
                actual = cp.decode(test_vm_segment.vm_to_wire(returned[0])) if len(returned) == 1 else None
                if actual != case["expected"]:
                    raise AssertionError({"case": case["id"], "expected": case["expected"],
                                          "actual": actual})
    vm_wall = time.monotonic() - vm_started
    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after_children.ru_utime - before_children.ru_utime
                 + after_children.ru_stime - before_children.ru_stime)
    source_hashes = {
        name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        for name in ("src/compiler/source.mncs", "src/compiler/lexer.mncs",
                     "src/compiler/parser.mncs", "src/compiler/kernel.mncs",
                     "tools/generate_unicode_classifier.rs")
    }
    result = {
        "schema_version": "mncs.compiler.unicode-lexer-frontier/2",
        "status": "verified",
        "rustc": subprocess.run(["rustc", "--version"], capture_output=True,
                                 text=True, check=True).stdout.strip(),
        "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_hashes": source_hashes,
        "sample_count": len(TEXTS),
        "malformed_comment_case_count": len(malformed_comment_summaries),
        "malformed_comment_cases": malformed_comment_summaries,
        "header_count": len(HEADERS),
        "strides": list(STRIDES),
        "token_count_per_backend": token_count,
        "token_vm_call_count": len(vm_tokens),
        "header_vm_call_count": len(vm_headers),
        "location_vm_call_count": len(vm_locations),
        "case_summaries": source_cases,
        "backend_agreement": {
            "stage0_reference_tokens": True,
            "cranelift_tokens": True,
            "canonical_vm_tokens": True,
            "unicode_scalar_columns": True,
            "unicode_module_headers": True,
            "invalid_utf8_inside_comments": True,
        },
        "artifact_ids": {
            "segment": segment_artifact["artifact_id"],
            "parser": parser_artifact["artifact_id"],
            "source": source_artifact["artifact_id"],
        },
        "canonical_vm_wall_seconds": round(vm_wall, 6),
        "canonical_vm_peak_rss_kib": vm_peak_rss,
        "canonical_vm_calls_bytes": vm_calls_bytes,
        "canonical_vm_results_bytes": vm_results_bytes,
        "child_cpu_seconds": round(child_cpu, 6),
        "cgroup_before": memory_before,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "limitations": [
            "This proves selected Unicode scalar properties and lexer/parser/source-location paths across the listed corpus and page strides.",
            "Malformed UTF-8 inside line and block comments is reported as a diagnostic in bounded and paged lexer entrypoints; this direct lexer check uses explicit byte expectations because Rust strings cannot represent malformed UTF-8.",
            "A separate Project check covers one Unicode module through SSA; this corpus does not establish arbitrary multi-module application execution.",
            "Language-owned verified-fact reuse is not implemented or measured by these checks.",
        ],
    }
    evidence_path = ROOT / "evidence/unicode-comment-utf8-scan-state-20261019.json"
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, sort_keys=True))
