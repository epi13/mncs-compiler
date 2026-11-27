#!/usr/bin/env python3
"""Emit unchanged declaration callables and run them in a fresh canonical VM."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import tempfile
import time
from unittest.mock import patch

import compiler_diagnosis
import differential_evidence
import native_vm_artifact
import probe_compiler_module_frontier as frontier
import test_native_segment_lex_batch_vm_emit as batch
import test_project as project
import test_sem as sem
import test_vm_flow_ssa_frontier as flow_ssa_frontier

ROOT = batch.ROOT
DECL_MODULE = "mncs.compiler.decl.v1"
EMITTER = "mncs.compiler.vm_emit.v1"
PRODUCER = "compile_and_emit_project_vm_callable"
PROJECT_STRIDE = project.STRIDE_DEFAULT
MEMORY_LIMIT = 8 * 1024 ** 3
DEFAULT_STEP_BUDGET = 8_000_000
CONTROL_CALLABLE = "mncs:0.2:function:mncs.test.small_finite.v1::score"
LOCKED_ORACLE_ROOT = batch.single_emit.MAIN_ROOT
LOCKED_ORACLE_PROBE = LOCKED_ORACLE_ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe"

UNIT_CALLER_SRC = """\
mncs 0.19;
module mncs.test.parse_unit_signatures_caller.v1;
use mncs.compiler.decl.v1 as decl;

fn call_parse_unit_signatures(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64) -> (result: decl.Unit) {
    return decl.parse_unit_signatures<4, 32>(pages, stride, total);
}
"""
UNIT_CALLER = "mncs:0.2:function:mncs.test.parse_unit_signatures_caller.v1::call_parse_unit_signatures"

PARSE_UNIT_CALLER_SRC = """\
mncs 0.19;
module mncs.test.parse_unit_caller.v1;
use mncs.compiler.decl.v1 as decl;

fn call_parse_unit(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64) -> (result: decl.Unit) {
    return decl.parse_unit<4, 32>(pages, stride, total);
}
"""
PARSE_UNIT_CALLER = "mncs:0.2:function:mncs.test.parse_unit_caller.v1::call_parse_unit"

CHECK_UNIT_CALLER_SRC = """\
mncs 0.19;
module mncs.test.check_unit_caller.v1;
use mncs.compiler.decl.v1 as decl;

fn call_check_unit(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64) -> (result: decl.UnitCheck) {
    return decl.check_unit<4, 32>(pages, stride, total);
}
"""
CHECK_UNIT_CALLER = "mncs:0.2:function:mncs.test.check_unit_caller.v1::call_check_unit"

PROVE_UNIT_CALLER_SRC = """\
mncs 0.19;
module mncs.test.prove_unit_caller.v1;
use mncs.compiler.decl.v1 as decl;

fn call_prove_unit(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64) -> (result: decl.UnitProve) {
    return decl.prove_unit<4, 32>(pages, stride, total);
}
"""
PROVE_UNIT_CALLER = "mncs:0.2:function:mncs.test.prove_unit_caller.v1::call_prove_unit"

CANONICAL_FINITE_ID_CALLER_SRC = """\
mncs 0.19;
module mncs.test.canonical_finite_id_caller.v1;
use mncs.compiler.decl.v1 as decl;

fn call_canonical_finite_type_identity(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64, module_start: u64, module_end: u64, name_start: u64, name_end: u64) -> (result: decl.SemanticIdentity) {
    return decl.canonical_finite_type_identity<4, 32>(pages, stride, total, module_start, module_end, name_start, name_end);
}
"""
CANONICAL_FINITE_ID_CALLER = "mncs:0.2:function:mncs.test.canonical_finite_id_caller.v1::call_canonical_finite_type_identity"

FLOW_LOWER_UNIT_CALLER_SRC = """\
mncs 0.19;
module mncs.test.flow_lower_unit_caller.v1;
use mncs.compiler.flow.v1 as flow;

fn call_flow_lower_unit(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64) -> (result: flow.LoweredUnit) {
    return flow.lower_unit<4, 32>(pages, stride, total);
}
"""
FLOW_LOWER_UNIT_CALLER = "mncs:0.2:function:mncs.test.flow_lower_unit_caller.v1::call_flow_lower_unit"

SSA_LOWER_FLOW_UNIT_CALLER_SRC = """\
mncs 0.19;
module mncs.test.ssa_lower_flow_unit_caller.v1;
use mncs.compiler.ssa.v1 as ssa;

fn call_lower_flow_unit_value_ssa(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64) -> (result: ssa.ValueSsaSummary) {
    return ssa.lower_flow_unit_value_ssa<4, 32>(pages, stride, total);
}
"""
SSA_LOWER_FLOW_UNIT_CALLER = "mncs:0.2:function:mncs.test.ssa_lower_flow_unit_caller.v1::call_lower_flow_unit_value_ssa"

DECLARATION_CASES = {
    "parse_use": (batch.TARGET_PARSE_USE_CALLER, batch.PARSE_USE_CALLER_SRC),
    "parse_unit_signatures": (UNIT_CALLER, UNIT_CALLER_SRC),
    "parse_unit": (PARSE_UNIT_CALLER, PARSE_UNIT_CALLER_SRC),
    "check_unit": (CHECK_UNIT_CALLER, CHECK_UNIT_CALLER_SRC),
    "canonical_finite_type_identity": (CANONICAL_FINITE_ID_CALLER, CANONICAL_FINITE_ID_CALLER_SRC),
    "prove_unit": (PROVE_UNIT_CALLER, PROVE_UNIT_CALLER_SRC),
    "flow_lower_unit": (FLOW_LOWER_UNIT_CALLER, FLOW_LOWER_UNIT_CALLER_SRC),
    "ssa_lower_flow_unit": (SSA_LOWER_FLOW_UNIT_CALLER, SSA_LOWER_FLOW_UNIT_CALLER_SRC),
}

USE_FIELDS = (
    "ok", "err_start", "err_end", "module_start", "module_end",
    "alias_start", "alias_end", "has_alias", "cursor",
)
# The real uses_step passes the end of the use token, before following trivia.
USE_CASES = (
    ("bare", b"use alpha;", 32, 3, (True, 0, 0, 4, 9, 9, 9, False, 10)),
    ("aliased", b"use mncs.alpha.beta as beta;", 32, 3, (True, 0, 0, 4, 19, 23, 27, True, 28)),
    ("trivia", b"/*c*/ use /*x*/ dotted . path /*y*/ as tag ;", 16, 9, (True, 0, 0, 16, 29, 39, 42, True, 44)),
    ("split_name", b"use alphabet.part as alias;", 9, 3, (True, 0, 0, 4, 17, 21, 26, True, 27)),
    ("same_input_other_layout", b"use alphabet.part as alias;", 16, 3, (True, 0, 0, 4, 17, 21, 26, True, 27)),
    ("bad_first_segment", b"use .bad;", 32, 3, (False, 4, 5, 3, 3, 0, 0, False, 3)),
    ("bad_dotted_segment", b"use foo. .bar;", 32, 3, (False, 9, 10, 3, 3, 0, 0, False, 3)),
    ("double_dot_token", b"use foo..bar;", 32, 3, (False, 7, 9, 4, 7, 0, 0, False, 3)),
    ("keyword_alias", b"use foo as mncs;", 32, 3, (False, 11, 15, 4, 7, 0, 0, False, 3)),
    ("missing_alias", b"use foo as ;", 32, 3, (False, 11, 12, 4, 7, 0, 0, False, 3)),
    ("missing_semicolon", b"use alpha as beta", 32, 3, (False, 17, 17, 4, 9, 0, 0, False, 3)),
)
UNIT_CASES = (
    (
        "valid_unit",
        b"mncs 0.19; module m; use a.b as c; record R { value: u64 } enum E { A, B { n: u64 } } fn f(x: R) -> (r: E) { return E.A; }",
        32,
    ),
    (
        "qualified_imported_type",
        b"mncs 0.19; module m; use p.h as h; record U { k: h.Hdr } enum W { S { v: h.V } } fn g(x: h.Hdr) -> (r: U) { return x; }",
        32,
    ),
    (
        "page_split_inside_decl",
        b"mncs 0.19; module m; record Box { item: u64 } fn id(x: Box) -> (r: Box) { return x; }",
        23,
    ),
    (
        "malformed_declaration",
        b"mncs 0.19; module m; fn f(x: ) -> (r: u64) { return 0; }",
        32,
    ),
    (
        "malformed_split_decl",
        b"mncs 0.19; module m; record Bad { x: }",
        15,
    ),
)


PARSE_UNIT_CASES = (
    (
        "valid_nonempty_body",
        b"mncs 0.19; module m; fn add1(x: u64) -> (r: u64) { let y: u64 = x + 1; return y; }",
        32,
    ),
    (
        "page_split_inside_expr",
        b"mncs 0.19; module m; fn calc(x: u64) -> (r: u64) { let y: u64 = (x + 10) * 2; return y; }",
        23,
    ),
    (
        "malformed_body",
        b"mncs 0.19; module m; fn bad(x: u64) -> (r: u64) { let y: u64 = x + ; return y; }",
        32,
    ),
)
CHECK_UNIT_VALID_LOCAL_CALL_RAW = (
    b"mncs 0.19; module m; fn inc(x: u64) -> (r: u64) { return x + 1; } "
    b"fn main(a: u64) -> (r: u64) { return inc(a); }"
)
CHECK_UNIT_CASES = (
    ("valid_local_call", CHECK_UNIT_VALID_LOCAL_CALL_RAW, 32),
    ("valid_local_call_layout_2", CHECK_UNIT_VALID_LOCAL_CALL_RAW, 28),
    (
        "duplicate_fn_name",
        b"mncs 0.19; module m; fn f(x: u64) -> (r: u64) { return x; } fn f(y: u64) -> (r: u64) { return y; }",
        32,
    ),
    (
        "unresolved_reference",
        b"mncs 0.19; module m; fn main(a: u64) -> (r: u64) { return missing(a); }",
        32,
    ),
    (
        "malformed_syntax",
        b"mncs 0.19; module m; fn bad(x: u64) -> (r: u64) { return x + ; }",
        32,
    ),
)
PROVE_UNIT_VALID_RAW = b"mncs 0.19; module m; fn id(x: u64) -> (r: u64) { return x; }"
PROVE_UNIT_CASES = (
    ("valid_unit", PROVE_UNIT_VALID_RAW, 32),
    ("valid_unit_layout_2", PROVE_UNIT_VALID_RAW, 20),
    (
        "semantically_invalid_unit",
        b"mncs 0.19; module m; fn bad(x: u64) -> (r: bool) { return x; }",
        32,
    ),
    (
        "finite_enum_unit",
        b"mncs 0.19; module m; enum Color { Red, Blue } fn pick() -> (r: Color) { return Color.Red; }",
        32,
    ),
)
CANONICAL_FINITE_ID_RAW = b"mncs 0.19; module alpha.beta.v1; enum TrafficLight { Go, Stop }"
# module_start..module_end = "alpha.beta.v1" (18..31), name_start..name_end = "TrafficLight" (38..50)
CANONICAL_FINITE_ID_CASES = (
    ("distinct_module_and_type", CANONICAL_FINITE_ID_RAW, 32, (18, 31, 38, 50)),
    ("page_split_module_and_type", CANONICAL_FINITE_ID_RAW, 21, (18, 31, 38, 50)),
)
FLOW_LOWER_UNIT_CASES = (
    ("valid_unit", PROVE_UNIT_VALID_RAW, 32),
    (
        "semantically_invalid_unit",
        b"mncs 0.19; module m; fn bad(x: u64) -> (r: bool) { return x; }",
        32,
    ),
)
SSA_FRONTIER_EXPECTATIONS = {
    name: (shape, expected_iters)
    for name, _, shape, expected_iters in flow_ssa_frontier.CASES
}
SSA_SCALAR_CALL_RAW = b"mncs 0.19;module m;fn inc(x:u64)->(r:u64){return x+1;}fn main(a:u64)->(r:u64){return inc(a+2);}"
SSA_LOWER_FLOW_UNIT_CASES = (
    (
        "fallthrough_join",
        flow_ssa_frontier.CASES[0][1].encode(),
        32,
    ),
    ("scalar_and_direct_call", SSA_SCALAR_CALL_RAW, 32),
    ("scalar_and_direct_call_layout_2", SSA_SCALAR_CALL_RAW, 25),
    (
        "record_construct_and_project",
        b"mncs 0.19;module m;record P{x:u64,y:u64}fn f(a:u64)->(r:u64){let p:P=P{x:a,y:a+1};return p.x+p.y;}",
        32,
    ),
    (
        "finite_enum_match",
        b"mncs 0.19;module m;enum C{R,B}fn f(c:C)->(r:u64){return match c{R=>1,B=>2};}",
        32,
    ),
    (
        "conditional_return_counted_iteration",
        # Compact identifier spelling keeps flow_ssa_frontier.CASES[1] within 4x32 = 128 bytes.
        b"mncs 0.18;module p;fn f(b:bool)->(r:u64){iterate i up_to 4 carrying s:u64=0{if b{return s;}next s=s+1;}return s;}",
        32,
    ),
    (
        "semantically_invalid_unit",
        b"mncs 0.19;module m;fn bad(x:u64)->(r:bool){return x;}",
        32,
    ),
)
UNIT_CHECK_FIELDS = ("ok", "stage", "err_start", "err_end", "fn_count", "expr_count", "ir_ok")


def expected_record_name(case):
    if case == "parse_use":
        return "UseDecl"
    if case in ("parse_unit_signatures", "parse_unit"):
        return "Unit"
    if case == "check_unit":
        return "UnitCheck"
    if case == "canonical_finite_type_identity":
        return "SemanticIdentity"
    if case == "prove_unit":
        return "UnitProve"
    if case == "flow_lower_unit":
        return "LoweredUnit"
    if case == "ssa_lower_flow_unit":
        return "ValueSsaSummary"
    raise ValueError(f"unsupported declaration case {case}")


def oracle_module_and_function(case):
    if case == "flow_lower_unit":
        return "mncs.compiler.flow.v1", "lower_unit"
    if case == "ssa_lower_flow_unit":
        return "mncs.compiler.ssa.v1", "lower_flow_unit_value_ssa"
    return DECL_MODULE, case


def execution_cases(case):
    if case == "parse_use":
        fixtures = (
            (label, raw, stride, [cursor], dict(zip(USE_FIELDS, values)))
            for label, raw, stride, cursor, values in USE_CASES
        )
    elif case == "parse_unit_signatures":
        fixtures = ((label, raw, stride, [], None) for label, raw, stride in UNIT_CASES)
    elif case == "parse_unit":
        fixtures = ((label, raw, stride, [], None) for label, raw, stride in PARSE_UNIT_CASES)
    elif case == "check_unit":
        fixtures = ((label, raw, stride, [], None) for label, raw, stride in CHECK_UNIT_CASES)
    elif case == "canonical_finite_type_identity":
        fixtures = ((label, raw, stride, list(spans), None) for label, raw, stride, spans in CANONICAL_FINITE_ID_CASES)
    elif case == "prove_unit":
        fixtures = ((label, raw, stride, [], None) for label, raw, stride in PROVE_UNIT_CASES)
    elif case == "flow_lower_unit":
        fixtures = ((label, raw, stride, [], None) for label, raw, stride in FLOW_LOWER_UNIT_CASES)
    elif case == "ssa_lower_flow_unit":
        fixtures = ((label, raw, stride, [], None) for label, raw, stride in SSA_LOWER_FLOW_UNIT_CASES)
    else:
        raise ValueError(f"unsupported declaration case {case}")
    for label, raw, stride, extra, expected in fixtures:
        pages = project.chunk(raw, stride)
        assert len(pages) <= 4 and stride <= 32, (label, len(pages), stride, len(raw))
        args = [project.pages_value(pages), project.integer(stride), project.integer(len(raw))]
        yield label, stride, args + [project.integer(value) for value in extra], expected


def stage0_results(case):
    type_args = [sem.nat_arg(4), sem.nat_arg(32)]
    oracle_mod, oracle_fn = oracle_module_and_function(case)
    oracle = batch.single_emit.Stage0ModuleOracle(
        oracle_mod,
        [ROOT / "src", batch.single_emit.STDLIB_ROOT / "library"],
        generic_seeds=[(oracle_fn, type_args)],
        probe_bin=LOCKED_ORACLE_PROBE,
        toolchain_identity_root=LOCKED_ORACLE_ROOT,
    )
    try:
        results = {}
        for label, _, args, expected in execution_cases(case):
            response = oracle.call(oracle_fn, args, type_arguments=type_args)
            assert response["status"] == "returned", (label, response)
            record = response["returned"][0]
            value = sem.decode(record)
            if expected is not None:
                assert value == expected, (label, value, expected)
            elif case in ("parse_unit_signatures", "parse_unit"):
                assert value["ok"] == (not label.startswith("malformed_")), (label, value)
            elif case == "check_unit":
                assert set(UNIT_CHECK_FIELDS).issubset(value.keys()), (label, value)
                assert value["ok"] == label.startswith("valid_"), (label, value)
            elif case == "canonical_finite_type_identity":
                assert value["valid"] is True and len(value["bytes"]) == 512, (label, value)
            elif case == "prove_unit":
                assert value["unit"]["ok"] is True, (label, value["unit"])
                assert value["ok"] == (label != "semantically_invalid_unit"), (label, value)
            elif case == "ssa_lower_flow_unit":
                loop_resp = oracle.call("value_ssa_loop_instruction_counts", [record])
                assert loop_resp["status"] == "returned", (label, loop_resp)
                loop_counts = sem.decode(loop_resp["returned"][0])
                assert loop_counts["complete"] is True, (label, loop_counts)
                st_resp = oracle.call("value_ssa_function_status", [record, project.integer(0)])
                assert st_resp["status"] == "returned", (label, st_resp)
                fn_status = sem.decode(st_resp["returned"][0])
                at_resp = oracle.call("value_ssa_function_at", [record, project.integer(0)])
                assert at_resp["status"] == "returned", (label, at_resp)
                fn_lookup = sem.decode(at_resp["returned"][0])
                if label == "semantically_invalid_unit":
                    assert value["valid"] is False, (label, value)
                else:
                    assert value["valid"] is True and value["function_count"] >= 1, (label, value)
                    assert value["verified_function_count"] == value["function_count"], (label, value)
                    assert value["supported_function_count"] == value["function_count"], (label, value)
                    assert fn_status["found"] and fn_status["verified"], (label, fn_status)
                    assert fn_lookup["found"] and fn_lookup["function"]["verified"], (label, fn_lookup)
                    if label == "conditional_return_counted_iteration":
                        assert loop_counts["index_init_count"] == 1, (label, loop_counts)
                        assert loop_counts["guard_count"] == 1, (label, loop_counts)
                        assert loop_counts["index_advance_count"] == 1, (label, loop_counts)
                        assert value["branch_count"] == 2 and value["jump_count"] == 3, (label, value)
                        assert value["return_count"] == 2 and value["failure_count"] == 1, (label, value)
                    elif label == "fallthrough_join":
                        assert loop_counts["index_init_count"] == 0, (label, loop_counts)
                        assert value["branch_count"] == 1 and value["jump_count"] == 1, (label, value)
                        assert value["return_count"] == 2 and value["failure_count"] == 1, (label, value)
                    elif label == "scalar_and_direct_call":
                        assert value["has_call"] is True and value["call_count"] >= 1, (label, value)
            results[label] = (record["record"]["type_identity"], value)
        return results
    finally:
        oracle.close()


def _iter_cons(node):
    cur = node
    while isinstance(cur, dict) and cur.get("$v") == 1:
        payload = cur.get("$p") or {}
        yield payload["head"]
        cur = payload.get("tail")


def _decoded_ssa_loop_counts(value):
    counts = {"index_init": 0, "guard": 0, "index_advance": 0}
    for fn_item in _iter_cons(value.get("functions")):
        for blk in _iter_cons(fn_item.get("blocks")):
            for inst in _iter_cons(blk.get("instructions")):
                kind = inst.get("kind")
                if kind == 15:
                    counts["index_init"] += 1
                elif kind == 13:
                    counts["guard"] += 1
                elif kind == 14:
                    counts["index_advance"] += 1
    return counts


def run_execution(case, target, artifact_path):
    oracle = stage0_results(case)
    module, function = target.removeprefix("mncs:0.2:function:").split("::")
    vm = batch.single_emit.FreshVmSession(artifact_path)
    results = {}
    expected_labels = []
    try:
        for label, stride, args, _ in execution_cases(case):
            expected_labels.append(label)
            response = vm.call(module, function, args)
            assert response["outcome"]["kind"] == "completed", (label, response)
            returned = response["execution"]["returned"][0]
            name = expected_record_name(case)
            assert returned["record"]["name"] == name, (label, returned)
            oracle_identity, oracle_value = oracle[label]
            assert returned["record"]["type_identity"] == oracle_identity, (label, returned, oracle_identity)
            value = sem.decode(returned)
            assert value == oracle_value, (label, value, oracle_value)
            entry = {
                "stride": stride,
                "type_identity": returned["record"]["type_identity"],
                "returned": value,
                "matches_stage0": True,
                "steps": response["record"]["usage"]["steps"],
                "max_memory_cells": response["record"]["usage"]["max_memory_cells"],
            }
            if case == "ssa_lower_flow_unit" and label in SSA_FRONTIER_EXPECTATIONS:
                expected_shape, expected_iters = SSA_FRONTIER_EXPECTATIONS[label]
                assert value["function_count"] == expected_shape["functions"], (label, value, expected_shape)
                assert value["branch_count"] == expected_shape["branches"], (label, value, expected_shape)
                assert value["jump_count"] == expected_shape["jumps"], (label, value, expected_shape)
                assert value["return_count"] == expected_shape["returns"], (label, value, expected_shape)
                assert value["failure_count"] == expected_shape["failures"], (label, value, expected_shape)
                assert value["switch_count"] == expected_shape["switches"], (label, value, expected_shape)
                terminator_total = (
                    value["branch_count"] + value["jump_count"] + value["return_count"]
                    + value["failure_count"] + value["switch_count"]
                )
                assert terminator_total == value["block_count"], (label, terminator_total, value)
                loop_counts = _decoded_ssa_loop_counts(value)
                assert all(c == expected_iters for c in loop_counts.values()), (label, loop_counts, expected_iters)
                entry["loop_instruction_counts"] = loop_counts
            results[label] = entry
        if case == "check_unit":
            assert results["valid_local_call"]["returned"] == results["valid_local_call_layout_2"]["returned"], (
                results["valid_local_call"]["returned"],
                results["valid_local_call_layout_2"]["returned"],
            )
        elif case == "prove_unit":
            assert results["valid_unit"]["returned"] == results["valid_unit_layout_2"]["returned"], (
                results["valid_unit"]["returned"],
                results["valid_unit_layout_2"]["returned"],
            )
        elif case == "canonical_finite_type_identity":
            assert results["distinct_module_and_type"]["returned"] == results["page_split_module_and_type"]["returned"], (
                results["distinct_module_and_type"]["returned"],
                results["page_split_module_and_type"]["returned"],
            )
        elif case == "ssa_lower_flow_unit":
            assert results["scalar_and_direct_call"]["returned"] == results["scalar_and_direct_call_layout_2"]["returned"], (
                results["scalar_and_direct_call"]["returned"],
                results["scalar_and_direct_call_layout_2"]["returned"],
            )
    finally:
        vm.close()
    case_summary = differential_evidence.summarize_differential_cases(expected_labels, results)
    assert case_summary["all_expected_match_stage0"], case_summary
    return {
        "status": "returned",
        "cases": results,
        "case_summary": case_summary,
        "vm_peak_rss_bytes": vm.peak_rss_bytes,
        "max_memory_cells": max(result["max_memory_cells"] for result in results.values()),
    }


def run_control(artifact_path):
    module = "mncs.test.small_finite.v1"
    color = f"mncs:0.2:finite-type:{module}::Color"
    variant = f"mncs:0.2:finite-variant:{module}::Color"
    cases = [
        ("Empty", 0, [], 6),
        ("Rgb", 1, [[field, project.integer(value)] for field, value in zip("rgb", (4, 5, 6))], 654),
    ]
    vm = batch.single_emit.FreshVmSession(artifact_path)
    results = {}
    try:
        for name, disc, payload, expected in cases:
            result = vm.call(module, "score", [{"finite": {
                "type_identity": color, "variant_identity": f"{variant}::{name}",
                "discriminant": disc, "payload": payload,
            }}])
            assert result["outcome"]["kind"] == "completed", result
            actual = sem.decode(result["execution"]["returned"][0])
            assert actual == expected, (name, actual, expected)
            usage = result["record"]["usage"]
            results[name] = {
                "returned": actual, "steps": usage["steps"],
                "max_memory_cells": usage["max_memory_cells"],
            }
    finally:
        vm.close()
    return {
        "status": "returned", "cases": results, "vm_peak_rss_bytes": vm.peak_rss_bytes,
        "max_memory_cells": max(result["max_memory_cells"] for result in results.values()),
    }


def assert_callable_specialization(emitted, case):
    mod, fn = oracle_module_and_function(case)
    identity = f"mncs:0.2:function:{mod}::{fn}"
    specializations = [
        [arg["value"] for arg in native_vm_artifact.flist(function["nat_args"])]
        for function in native_vm_artifact.flist(emitted["functions"])
        if compiler_diagnosis.decode_semantic_id(function["identity"]) == identity
    ]
    assert specializations == [[4, 32]], (identity, specializations)


def select_sources(case):
    if case == "control":
        return CONTROL_CALLABLE, [
            ("00_small_finite.mncs", Path("00_small_finite.mncs"), batch.SMALL_FINITE_SRC.encode()),
        ]
    target, wrapper = DECLARATION_CASES[case]
    sources = batch._segment_sources(batch._lexer_sources())
    extra_mods = (
        ((4, "parser"), (5, "decl"), (6, "flow"), (7, "ssa"))
        if case == "ssa_lower_flow_unit"
        else ((4, "parser"), (5, "decl"), (6, "flow"))
        if case == "flow_lower_unit"
        else ((4, "parser"), (5, "decl"))
    )
    for number, name in extra_mods:
        path = ROOT / "src" / "compiler" / f"{name}.mncs"
        sources.append((f"{number:02}_{path.name}", path, path.read_bytes()))
    wrapper_num = 8 if case == "ssa_lower_flow_unit" else 7 if case == "flow_lower_unit" else 6
    wrapper_id = f"{wrapper_num:02}_{case}_caller.mncs"
    sources.append((wrapper_id, Path(wrapper_id), wrapper.encode()))
    sources.sort(key=lambda entry: entry[0].encode())
    return target, sources


def source_manifest(sources):
    return [
        {
            "source_id": source_id,
            "bytes": len(raw),
            "pages": len(project.chunk(raw, PROJECT_STRIDE)),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        for source_id, _, raw in sources
    ]


def run_request(target, sources, page_bound, step_budget):
    targs = [sem.nat_arg(page_bound), sem.nat_arg(PROJECT_STRIDE)]
    env = {
        "MNCS_LIBRARY_PATH": f"{ROOT / 'src'}:{batch.single_emit.STDLIB_ROOT / 'library'}",
        "MNCS_PROBE_BIN": str(batch.single_emit.PROBE_BIN),
        "MNCS_PROBE_TOOLCHAIN_IDENTITY_ROOT": str(project.probe_toolchain_identity_root(ROOT)),
        "MNCS_PROBE_COMPACT_RESPONSE": os.environ.get("MNCS_PROBE_COMPACT_RESPONSE", "1"),
        "MNCS_PROBE_MODULES": "source,lexer,parser,segment,decl,flow,ssa,project",
        "MNCS_PROBE_LIBRARY_MODULES": f"{EMITTER},mncs.std.text_utf8.v1",
        "MNCS_PROBE_EXECUTION_MODULES": EMITTER,
        "MNCS_PROBE_BACKEND": batch.backend_policy.resolve("project"),
        "MNCS_PROBE_BACKEND_ROOTS": PRODUCER,
        "MNCS_PROBE_CACHE_DIR": str(batch.single_emit.CACHE_DIR),
        "MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES": str(MEMORY_LIMIT),
        "MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES": os.environ.get(
            "MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES", "1",
        ),
        "MNCS_PROBE_GENERIC_SEEDS": json.dumps([
            {"module": EMITTER, "function": PRODUCER, "type_arguments": targs},
        ]),
        "MNCS_PROBE_TELEMETRY": "1",
    }
    started = time.monotonic()
    with patch.dict(os.environ, env):
        probe = project.Probe()
        transport_failed = False
        phase = "stage0_probe_startup"
        try:
            status_doc = probe.send({"execution_status": True})
            compact_req = bool(
                status_doc.get("compact_encoding_supported")
                and os.environ.get("MNCS_PROBE_COMPACT_REQUEST", "1") == "1"
            )
            records = {r["name"]: r for r in probe.send({"record_types": EMITTER})}
            encoded = project.encode_project_request_bytes(
                records, sources, page_bound=page_bound, target_index=len(sources) - 1,
                target_module=EMITTER, target_function=PRODUCER,
                extra_arguments=[batch.punct._make_semantic_id_wire(records, target)],
                step_budget=step_budget,
                compact_request=compact_req,
            )
            phase = "stage0_probe_request"
            response = probe.send_encoded_decoded(encoded, module=EMITTER, record_digest=False)
        except (project.ProbeExited, project.ProbeTimeout) as exc:
            transport_failed = True
            child = next(iter(probe._procs.values()))
            observation = exc.observation
            return None, {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "backend": env["MNCS_PROBE_BACKEND"],
                "child_peak_rss_bytes": (observation.get("resource_summary") or {}).get("max_kernel_rss_high_water_bytes"),
                "failure": {
                    "phase": phase,
                    "rejecting_component": "mncs-compiler:tools/stage0-probe",
                    "detail": str(exc),
                    "termination": observation.get("termination"),
                    "classification": observation.get("status"),
                    "return_code": observation.get("return_code"),
                    "stderr_tail": probe._stderr_tail(child).decode(errors="replace")[-3000:],
                },
            }
        finally:
            try:
                probe.close()
            except AssertionError:
                if not transport_failed:
                    raise
    observation = probe.request_observations[-1]
    metrics = {
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "request_bytes": observation["request_bytes"],
        "response_bytes": observation["reply_bytes"],
        "response_encoding": observation.get("response_encoding"),
        "wait_elapsed_seconds": round(observation["wait_elapsed_ns"] / 1e9, 4) if "wait_elapsed_ns" in observation else None,
        "response_parse_seconds": round(observation["response_parse_ns"] / 1e9, 4) if "response_parse_ns" in observation else None,
        "reported_execution_steps": observation.get("execution_steps"),
        "child_peak_rss_bytes": (observation.get("final_process") or {}).get("rss_high_water_bytes"),
        "backend": env["MNCS_PROBE_BACKEND"],
        "program_released_after_identities": env["MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES"] == "1",
    }
    return response, metrics


def run_attempt(case, page_bound, artifact_path, *, step_budget=DEFAULT_STEP_BUDGET, verify_determinism=False):
    target, sources = select_sources(case)
    selected_bound, pages = frontier._project_page_capacity(sources)
    page_bound = selected_bound if page_bound is None else page_bound
    if page_bound not in frontier.PROJECT_PROBE_PAGE_TIERS:
        raise ValueError(f"unsupported project page tier {page_bound}")
    manifest = source_manifest(sources)
    oracle_mod, oracle_fn = oracle_module_and_function(case) if case != "control" else (None, None)
    report = {
        "target": target,
        "specialization": f"{oracle_mod}::{oracle_fn}<4,32>" if case != "control" else None,
        "compiler_revision": batch.single_emit._git_head(ROOT),
        "emitter_source_sha256": compiler_diagnosis.file_sha256(ROOT / compiler_diagnosis.EMITTER_REL_PATH),
        "project_source_sha256": compiler_diagnosis.file_sha256(ROOT / "src/compiler/project.mncs"),
        "stage0_revision": batch.single_emit._stage0_lock_rev(),
        "emitter_probe_sha256": compiler_diagnosis.file_sha256(batch.single_emit.PROBE_BIN),
        "locked_oracle_sha256": compiler_diagnosis.file_sha256(LOCKED_ORACLE_PROBE),
        "vm_revision": batch.single_emit._git_head(batch.single_emit.VM_REPO),
        "producer": f"{EMITTER}::{PRODUCER}",
        "sources": manifest,
        "source_closure_sha256": compiler_diagnosis.canonical_digest(manifest),
        "total_pages": pages,
        "selected_emitter_specialization": [page_bound, PROJECT_STRIDE],
        "minimum_supported_emitter_specialization": [selected_bound, PROJECT_STRIDE],
        "memory_guard_bytes": MEMORY_LIMIT,
        "emitter_step_budget": step_budget,
        "emission": {"status": "not_started"},
        "admission": {"status": "not_started"},
        "execution": {"status": "not_started"},
    }
    emitted = None
    request_failure = None
    if pages > page_bound:
        request_failure = {
            "phase": "project_request_capacity",
            "rejecting_component": "mncs-compiler:tools/test_native_decl_vm_emit.py",
            "supplied_capacity": page_bound,
            "required_capacity": pages,
        }
    else:
        response, metrics = run_request(target, sources, page_bound, step_budget)
        report["emitter_probe"] = metrics
        if response is None:
            request_failure = metrics["failure"]
        else:
            report["probe_status"] = response["status"]
            if response["status"] == "returned":
                emitted = response["returned"][0]
            else:
                request_failure = {
                    "phase": "stage0_probe_execution",
                    "rejecting_component": "mncs-compiler:tools/stage0-probe",
                    "response": response,
                }
    if request_failure is not None:
        report["emission"] = {"status": "refused", **request_failure}
    else:
        report["emission"] = {
            "status": "emitted" if emitted["ok"] else "refused",
            "detail": batch._emit_detail_summary(emitted),
        }

    if emitted and emitted["ok"]:
        report["emission"].update({key: emitted[key] for key in (
            "function_count", "record_type_count", "finite_type_count", "block_count", "instruction_count",
        )})
        try:
            if case != "control":
                assert_callable_specialization(emitted, case)
            artifact = batch._package(emitted, sources, len(sources) - 1)
            artifact_bytes = json.dumps(artifact, separators=(",", ":")).encode()
            artifact_path.write_bytes(artifact_bytes)
        except (AssertionError, KeyError, OSError, ValueError) as exc:
            report["seal"] = {"status": "failed", "detail": str(exc)[:2000]}
        else:
            report["emission"].update({
                "artifact_id": artifact["artifact_id"],
                "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                "artifact_bytes": len(artifact_bytes),
            })
            admitted = batch._admit_artifact(artifact_path)
            report["admission"] = {"status": "admitted" if admitted["ok"] else "refused", **admitted}
            if admitted["ok"]:
                try:
                    report["execution"] = (
                        run_control(artifact_path) if case == "control"
                        else run_execution(case, target, artifact_path)
                    )
                except (AssertionError, KeyError, OSError, ValueError) as exc:
                    report["execution"] = {"status": "failed", "detail": str(exc)[:4000]}
            if verify_determinism:
                resp2, metrics2 = run_request(target, sources, page_bound, step_budget)
                if resp2 is None or resp2["status"] != "returned" or not resp2["returned"][0]["ok"]:
                    report["determinism"] = {
                        "status": "failed",
                        "emitter_probe_run2": metrics2,
                        "detail": "second fresh emission failed",
                    }
                else:
                    emitted2 = resp2["returned"][0]
                    artifact2 = batch._package(emitted2, sources, len(sources) - 1)
                    artifact2_bytes = json.dumps(artifact2, separators=(",", ":")).encode()
                    sha2 = hashlib.sha256(artifact2_bytes).hexdigest()
                    same_raw = emitted == emitted2
                    same_artifact = artifact_bytes == artifact2_bytes
                    report["determinism"] = {
                        "status": "passed" if (same_raw and same_artifact) else "failed",
                        "deterministic_across_fresh_processes": bool(same_raw and same_artifact),
                        "raw_emit_equal": same_raw,
                        "artifact_bytes_equal": same_artifact,
                        "artifact_sha256_run1": report["emission"]["artifact_sha256"],
                        "artifact_sha256_run2": sha2,
                        "emitter_probe_run2": metrics2,
                    }
                    assert same_raw and same_artifact, report["determinism"]
    if case != "control":
        diagnosis = compiler_diagnosis.build_diagnosis_document(
            ROOT,
            target_callable=target,
            target_specialization=report["specialization"],
            declaration_identity=f"mncs:0.2:function:{oracle_mod}::{oracle_fn}",
            emit_res=emitted,
            request_failure=request_failure,
            source_paths=[path for _, path, _ in sources] + [
                ROOT / "src" / "compiler" / f"{module}.mncs"
                for module in ("flow", "ssa", "project")
            ],
            admitted=report["admission"]["status"] == "admitted",
            executed=report["execution"]["status"] == "returned",
            artifact_id=report["emission"].get("artifact_id"),
        )
        compiler_diagnosis.write_cached_diagnosis(ROOT, diagnosis)
    return report


def guard_host_memory():
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = MEMORY_LIMIT if soft == resource.RLIM_INFINITY else min(soft, MEMORY_LIMIT)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("control", *DECLARATION_CASES))
    parser.add_argument("--page-bound", type=int)
    parser.add_argument("--step-budget", type=int, default=DEFAULT_STEP_BUDGET)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--artifact-out", type=Path)
    parser.add_argument("--verify-determinism", action="store_true")
    args = parser.parse_args()
    if args.step_budget <= 0:
        parser.error("--step-budget must be positive")

    guard_host_memory()
    with tempfile.TemporaryDirectory(prefix="mncs-decl-emit-") as scratch:
        artifact_path = args.artifact_out or Path(scratch) / "artifact.json"
        report = run_attempt(
            args.case,
            args.page_bound,
            artifact_path,
            step_budget=args.step_budget,
            verify_determinism=args.verify_determinism,
        )
    report["host_peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    rendered = json.dumps(report, indent=2) + "\n"
    if args.json_out:
        args.json_out.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
