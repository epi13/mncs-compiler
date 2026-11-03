#!/usr/bin/env python3
"""Check native Project diagnostic spans map against current source bytes."""

from __future__ import annotations

import json
import os
import hashlib
import statistics
import subprocess
import time
from copy import deepcopy
from pathlib import Path

PROJECT_MODULE = "mncs.compiler.project.v1"
NAT_ARGS = [{"kind": "nat", "value": 1024}, {"kind": "nat", "value": 1024}]

os.environ["MNCS_PROBE_EXECUTION_MODULES"] = PROJECT_MODULE
os.environ.setdefault("MNCS_PROBE_BACKEND", "cranelift")
os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
    {"module": PROJECT_MODULE, "function": "compile_project",
     "type_arguments": NAT_ARGS},
    {"module": PROJECT_MODULE, "function": "locate_project_diagnostics_global",
     "type_arguments": NAT_ARGS},
    {"module": PROJECT_MODULE,
     "function": "compile_project_target_with_diagnostic_locations",
     "type_arguments": NAT_ARGS},
])

import test_project as project


def line_col(source: bytes, offset: int) -> tuple[int, int]:
    prefix = source[:offset]
    line = prefix.count(b"\n") + 1
    last_newline = prefix.rfind(b"\n")
    return line, offset - last_newline


def run_project(probe, source: bytes, source_id: str, *, page_count_override=None,
                append_orphan_page=False, source_index_override=None,
                span_override=None, span_overrides=None):
    request = project.request_value(
        project.identity_map(probe),
        [(source_id, source_id, source)],
    )
    if page_count_override is not None:
        source_values = project.wire_sequence(
            project.wire_field(request["arguments"][0], "sources"))
        project.set_wire_field(
            source_values[0], "page_count", project.integer(page_count_override))
    if append_orphan_page:
        project.wire_sequence(request["arguments"][1]).append(
            project.byte_sequence(b"orphan"))

    compiled = probe.send(request)
    assert compiled.get("status") == "returned", compiled
    raw_result = compiled["returned"][0]
    diagnostics = deepcopy(project.wire_field(raw_result, "diagnostics"))
    if source_index_override is not None:
        first = project.wire_flist(diagnostics)[0]
        project.set_wire_variant_field(
            first, "source_index", project.integer(source_index_override))
    if span_override is not None:
        first = project.wire_flist(diagnostics)[0]
        project.set_wire_variant_field(first, "start", project.integer(span_override[0]))
        project.set_wire_variant_field(first, "end", project.integer(span_override[1]))
    if span_overrides is not None:
        diagnostic_values = project.wire_flist(diagnostics)
        assert len(diagnostic_values) == len(span_overrides), (
            len(diagnostic_values), len(span_overrides))
        for value, byte_span in zip(diagnostic_values, span_overrides):
            assert byte_span is not None
            project.set_wire_variant_field(
                value, "start", project.integer(byte_span[0]))
            project.set_wire_variant_field(
                value, "end", project.integer(byte_span[1]))
    locate_request = {
        "schema_version": "0.1",
        "target": {"module": PROJECT_MODULE,
                   "function": "locate_project_diagnostics_global"},
        "arguments": [
            project.wire_field(request["arguments"][0], "sources"),
            request["arguments"][1],
            diagnostics,
        ],
        "type_arguments": NAT_ARGS,
        "step_budget": 8_000_000,
    }
    located = probe.send(locate_request)
    assert located.get("status") == "returned", located
    projected = project.decode(located["returned"][0])
    # The public result is a finite list; decode() keeps the enum tree so the
    # helper can verify each original diagnostic is preserved in order.
    rows = []
    node = projected
    while node["$v"] == 1:
        rows.append(node["$p"]["head"])
        node = node["$p"]["tail"]
    assert node["$v"] == 0, node
    original = project.decode(diagnostics)
    original_rows = []
    node = original
    while node["$v"] == 1:
        original_rows.append(node["$p"]["head"])
        node = node["$p"]["tail"]
    assert node["$v"] == 0, node
    assert [row["diagnostic"] for row in rows] == original_rows
    return rows, compiled["steps"], located["steps"]


def run_project_with_integrated_locations(probe, source: bytes, source_id: str):
    request = project.request_value(
        project.identity_map(probe),
        [(source_id, source_id, source)],
    )
    request["target"]["function"] = "compile_project_target_with_diagnostic_locations"
    request["arguments"].append(project.integer(0))
    compiled = probe.send(request)
    assert compiled.get("status") == "returned", compiled
    wrapped = compiled["returned"][0]
    result = project.wire_field(wrapped, "result")
    diagnostics = project.wire_field(result, "diagnostics")
    original = project.decode(diagnostics)
    original_rows = []
    node = original
    while node["$v"] == 1:
        original_rows.append(node["$p"]["head"])
        node = node["$p"]["tail"]
    assert node["$v"] == 0, node

    locations = project.decode(project.wire_field(wrapped, "diagnostic_locations"))
    rows = []
    node = locations
    while node["$v"] == 1:
        rows.append(node["$p"]["head"])
        node = node["$p"]["tail"]
    assert node["$v"] == 0, node
    assert [row["diagnostic"] for row in rows] == original_rows
    return rows, compiled["steps"]


def decoded_locations(value):
    decoded = project.decode(value)
    rows = []
    node = decoded
    while node["$v"] == 1:
        rows.append(node["$p"]["head"])
        node = node["$p"]["tail"]
    assert node["$v"] == 0, node
    return rows


def run_many_locations(probe, source: bytes, source_id: str, *,
                       span_overrides=None, repeats=1):
    request = project.request_value(
        project.identity_map(probe), [(source_id, source_id, source)])
    compiled = probe.send(request)
    assert compiled.get("status") == "returned", compiled
    diagnostics = deepcopy(project.wire_field(compiled["returned"][0], "diagnostics"))
    if span_overrides is not None:
        values = project.wire_flist(diagnostics)
        assert len(values) == len(span_overrides), (len(values), len(span_overrides))
        for value, byte_span in zip(values, span_overrides):
            project.set_wire_variant_field(
                value, "start", project.integer(byte_span[0]))
            project.set_wire_variant_field(
                value, "end", project.integer(byte_span[1]))
    expected_diagnostics = project.decode(diagnostics)
    locate_request = {
        "schema_version": "0.1",
        "target": {"module": PROJECT_MODULE,
                   "function": "locate_project_diagnostics_global"},
        "arguments": [
            project.wire_field(request["arguments"][0], "sources"),
            request["arguments"][1],
            diagnostics,
        ],
        "type_arguments": NAT_ARGS,
        "step_budget": 8_000_000,
    }
    timings = []
    located = None
    for _ in range(repeats):
        before = time.perf_counter()
        located = probe.send(locate_request)
        timings.append(time.perf_counter() - before)
        assert located.get("status") == "returned", located
    rows = decoded_locations(located["returned"][0])
    original_rows = []
    node = expected_diagnostics
    while node["$v"] == 1:
        original_rows.append(node["$p"]["head"])
        node = node["$p"]["tail"]
    assert node["$v"] == 0, node
    assert [row["diagnostic"] for row in rows] == original_rows
    return rows, compiled["steps"], located["steps"], timings


def span(row):
    diag = row["diagnostic"]
    if diag["$v"] in (2, 3, 4, 5, 6, 8):
        return diag["$p"]["start"], diag["$p"]["end"]
    return None


def verify_span(rows, source: bytes):
    row = next(row for row in rows if span(row) is not None)
    assert row["mapped"] is True, row
    start, end = span(row)
    assert (row["start"]["line"], row["start"]["col"]) == line_col(source, start), row
    assert (row["end"]["line"], row["end"]["col"]) == line_col(source, end), row
    return row


def verify_all_spans(rows, source: bytes, expected_spans):
    assert len(rows) == len(expected_spans), (len(rows), len(expected_spans))
    for row, byte_span in zip(rows, expected_spans):
        assert row["mapped"] is True, row
        start, end = byte_span
        assert span(row) == byte_span, row
        assert (row["start"]["line"], row["start"]["col"]) == line_col(source, start), row
        assert (row["end"]["line"], row["end"]["col"]) == line_col(source, end), row


def main():
    started = time.monotonic()
    probe = project.Probe()
    try:
        missing = (
            b"mncs 0.18;\nmodule demo.root;\n"
            b"use demo.absent as absent;\n"
            b"fn main() -> (r: u64) { return 1; }\n"
        )
        missing_rows, missing_compile_steps, missing_location_steps = run_project(
            probe, missing, "missing.mncs")
        missing_row = verify_span(missing_rows, missing)
        assert missing_row["diagnostic"]["$v"] == 3, missing_row
        assert span(missing_row) == (
            missing.index(b"demo.absent"),
            missing.index(b"demo.absent") + len(b"demo.absent"),
        ), missing_row
        integrated_rows, integrated_steps = run_project_with_integrated_locations(
            probe, missing, "integrated-missing.mncs")
        integrated_row = verify_span(integrated_rows, missing)
        assert integrated_row["diagnostic"]["$v"] == 3, integrated_row
        assert span(integrated_row) == span(missing_row), integrated_row

        multi_page = (
            b"mncs 0.18;\nmodule demo.multi_page;\n"
            + b"\n" * 1100
            + b"use demo.absent as absent;\n"
            + b"fn main() -> (r: u64) { return 1; }\n"
        )
        assert len(multi_page) > 1024
        multi_page_rows, multi_page_compile_steps, multi_page_location_steps = run_project(
            probe, multi_page, "multi-page.mncs")
        multi_page_row = verify_span(multi_page_rows, multi_page)
        assert multi_page_row["diagnostic"]["$v"] == 3, multi_page_row
        assert span(multi_page_row) == (
            multi_page.index(b"demo.absent"),
            multi_page.index(b"demo.absent") + len(b"demo.absent"),
        ), multi_page_row

        many_import_count = 64
        many_imports = b"".join(
            f"use demo.absent_{index:02d} as missing_{index:02d};\n".encode()
            for index in range(many_import_count))
        many_missing = (
            b"mncs 0.18;\nmodule demo.many_missing;\n"
            + b"\n" * 32768
            + many_imports
            + b"fn main() -> (r: u64) { return 1; }\n"
        )
        many_spans = [
            (many_missing.index(f"demo.absent_{index:02d}".encode()),
             many_missing.index(f"demo.absent_{index:02d}".encode())
             + len(f"demo.absent_{index:02d}"))
            for index in range(many_import_count)
        ]
        many_integrated_rows, many_integrated_steps = run_project_with_integrated_locations(
            probe, many_missing, "many-missing-integrated.mncs")
        verify_all_spans(many_integrated_rows, many_missing, many_spans)

        ordered_rows, many_compile_steps, many_location_steps, ordered_times = run_many_locations(
            probe, many_missing, "many-missing-ordered.mncs", repeats=7)
        verify_all_spans(ordered_rows, many_missing, many_spans)
        out_of_order_spans = list(reversed(many_spans))
        out_of_order_rows, out_of_order_compile_steps, out_of_order_location_steps, out_of_order_times = run_many_locations(
            probe, many_missing, "many-missing-out-of-order.mncs",
            span_overrides=out_of_order_spans)
        verify_all_spans(out_of_order_rows, many_missing, out_of_order_spans)
        cursor_median_ms = statistics.median(ordered_times) * 1000

        broken = (
            b"mncs 0.18;\nmodule demo.broken;\n"
            b"fn main( -> (r: u64) { return 1; }\n"
        )
        broken_rows, broken_compile_steps, broken_location_steps = run_project(
            probe, broken, "broken.mncs")
        broken_row = verify_span(broken_rows, broken)
        assert broken_row["diagnostic"]["$v"] == 4, broken_row

        malformed_rows, malformed_compile_steps, malformed_location_steps = run_project(
            probe, missing, "invalid-pages.mncs", page_count_override=0)
        malformed_spans = [row for row in malformed_rows if row["has_span"]]
        assert malformed_spans and all(not row["mapped"] for row in malformed_spans), malformed_rows

        no_span_rows, no_span_compile_steps, no_span_location_steps = run_project(
            probe, missing, "orphan-page.mncs", append_orphan_page=True)
        no_span = next(row for row in no_span_rows if not row["has_span"])
        assert no_span["mapped"] is False, no_span
        assert no_span["diagnostic"]["$v"] == 1, no_span

        invalid_index_rows, invalid_index_compile_steps, invalid_index_location_steps = run_project(
            probe, missing, "invalid-index.mncs", source_index_override=64)
        invalid_index = next(row for row in invalid_index_rows if row["has_span"])
        assert invalid_index["source_index"] == 64 and invalid_index["mapped"] is False, invalid_index

        span_start = missing.index(b"demo.absent")
        invalid_end_rows, invalid_end_compile_steps, invalid_end_location_steps = run_project(
            probe, missing, "invalid-end-span.mncs",
            span_override=(span_start, len(missing) + 1))
        invalid_end = next(row for row in invalid_end_rows if row["has_span"])
        assert invalid_end["mapped"] is False, invalid_end
        assert span(invalid_end) == (span_start, len(missing) + 1), invalid_end
        assert invalid_end["start"] == {"line": 1, "col": 1}
        assert invalid_end["end"] == {"line": 1, "col": 1}

        reversed_rows, reversed_compile_steps, reversed_location_steps = run_project(
            probe, missing, "reversed-span.mncs",
            span_override=(span_start + 1, span_start))
        reversed_span = next(row for row in reversed_rows if row["has_span"])
        assert reversed_span["mapped"] is False, reversed_span
        assert span(reversed_span) == (span_start + 1, span_start), reversed_span
        assert reversed_span["start"] == {"line": 1, "col": 1}
        assert reversed_span["end"] == {"line": 1, "col": 1}

        print(json.dumps({
            "schema_version": 1,
            "kind": "project-diagnostic-source-locations",
            "status": "passed",
            "missing_import_span_matches_source": True,
            "integrated_target_compile_diagnostic_locations": True,
            "multi_page_span_matches_source": True,
            "multi_page_source_bytes": len(multi_page),
            "multi_page_span_line": multi_page_row["start"]["line"],
            "project_diagnostic_cursor": {
                "source_bytes": len(many_missing),
                "diagnostic_count": many_import_count,
                "integrated_compile_locations_match_python": True,
                "ordered_locations_match_python": True,
                "out_of_order_fallback_matches_python": True,
                "ordered_request_times_ms": [round(value * 1000, 3) for value in ordered_times],
                "ordered_request_median_ms": round(cursor_median_ms, 3),
                "ordered_location_steps": many_location_steps,
                "out_of_order_location_steps": out_of_order_location_steps,
            },
            "parse_failure_span_matches_source": True,
            "malformed_source_pages_fail_closed": True,
            "location_free_diagnostic_preserved": True,
            "invalid_span_source_index_fails_closed": True,
            "out_of_range_span_fails_closed": True,
            "reversed_span_fails_closed": True,
            "diagnostic_order_and_identity_preserved": True,
            "backend": "cranelift",
            "workspace_head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=project.ROOT,
                check=True, capture_output=True, text=True).stdout.strip(),
            "project_source_sha256": hashlib.sha256(
                (project.ROOT / "src/compiler/project.mncs").read_bytes()).hexdigest(),
            "test_source_sha256": hashlib.sha256(
                Path(__file__).read_bytes()).hexdigest(),
            "request_count": probe.requests,
            "steps": {
                "compile_missing": missing_compile_steps,
                "locate_missing": missing_location_steps,
                "integrated_compile_and_locate_missing": integrated_steps,
                "compile_multi_page": multi_page_compile_steps,
                "locate_multi_page": multi_page_location_steps,
                "integrated_many_diagnostics": many_integrated_steps,
                "compile_many_diagnostics": many_compile_steps,
                "locate_many_diagnostics": many_location_steps,
                "compile_out_of_order_diagnostics": out_of_order_compile_steps,
                "locate_out_of_order_diagnostics": out_of_order_location_steps,
                "compile_parse_failure": broken_compile_steps,
                "locate_parse_failure": broken_location_steps,
                "compile_malformed_pages": malformed_compile_steps,
                "locate_malformed_pages": malformed_location_steps,
                "compile_orphan_page": no_span_compile_steps,
                "locate_orphan_page": no_span_location_steps,
                "compile_invalid_index": invalid_index_compile_steps,
                "locate_invalid_index": invalid_index_location_steps,
                "compile_invalid_end_span": invalid_end_compile_steps,
                "locate_invalid_end_span": invalid_end_location_steps,
                "compile_reversed_span": reversed_compile_steps,
                "locate_reversed_span": reversed_location_steps,
            },
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }, indent=2))
    finally:
        probe.close()


if __name__ == "__main__":
    main()
