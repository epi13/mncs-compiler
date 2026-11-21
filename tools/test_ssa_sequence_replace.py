#!/usr/bin/env python3
"""Differential, anti-tamper, project, and canonical-VM proof for SequenceReplace SSA."""
import copy
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import tempfile
import time

TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parents[0]
sys.path.insert(0, str(TOOLS_DIR))


def _configure_env() -> None:
    os.environ.setdefault("MNCS_PROBE_LIBRARY_MODULES", "mncs.std.text_utf8.v1")
    if not os.environ.get("MNCS_LIBRARY_PATH"):
        for base in (ROOT.parent, ROOT.parent.parent.parent):
            candidate = base / "mncs-stdlib" / "library"
            if candidate.is_dir():
                os.environ["MNCS_LIBRARY_PATH"] = str(candidate)
                break
    os.environ.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build" / "probe-cache"))
    if not os.environ.get("MNCS_PROBE_BIN"):
        for base in (ROOT, ROOT.parent.parent):
            candidate = base / ".bootstrap" / "target" / "release" / "mncs-compiler-stage0-probe"
            if candidate.is_file():
                os.environ["MNCS_PROBE_BIN"] = str(candidate)
                break


_configure_env()

import test_project as project
import test_sem as sem
from test_ssa_sequence_length import (
    SSA_MODULE, TYPE_ARGS, STEP_BUDGET, Probe, call, decode, field,
    integer, linked, logical_module_args, run_batch, vm_to_wire, wire_linked,
)

DECL_MODULE = "mncs.compiler.decl.v1"
FLOW_MODULE = "mncs.compiler.flow.v1"
STRIDE = TYPE_ARGS[1]["value"]
SOURCE_MODULE_PATH = ROOT / "src" / "compiler" / "source.mncs"
MAX_EXPANDED_NODES = 8_500_000

SMALL_CASES = [
    (
        "replace-exact-lit",
        "mncs 0.18; module frontier.rep; "
        "fn set_lit(s: [u64; 3]) -> (r: [u64; 3]) { return replace(s, 1, 9); }",
        1,
        1,
    ),
    (
        "replace-exact-dyn",
        "mncs 0.18; module frontier.rep; "
        "fn set_dyn(s: [u64; 3], i: u64, v: u64) -> (r: [u64; 3]) { return replace(s, i, v); }",
        1,
        1,
    ),
    (
        "replace-generic-exact",
        "mncs 0.18; module frontier.rep; "
        "fn set_gen<N: Nat>(s: [u64; N], i: u64, v: u64) -> (r: [u64; N]) { return replace(s, i, v); }",
        1,
        0,
    ),
    (
        "replace-generic-called",
        "mncs 0.18; module frontier.rep; "
        "fn set_gen<N: Nat>(s: [u64; N], i: u64, v: u64) -> (r: [u64; N]) { return replace(s, i, v); } "
        "fn main(s: [u64; 3], i: u64, v: u64) -> (r: [u64; 3]) { return set_gen<3>(s, i, v); }",
        1,
        1,
    ),
    (
        "replace-nested-seq",
        "mncs 0.18; module frontier.rep; "
        "fn set_row(s: [[byte; 2]; 2]) -> (r: [[byte; 2]; 2]) { return replace(s, 0, [65 as byte, 66 as byte]); }",
        1,
        1,
    ),
    (
        "replace-match-arm",
        "mncs 0.18; module frontier.rep; "
        "fn set_arm(k: u64, s: [u64; 2]) -> (r: [u64; 2]) { return match k { 0 => replace(s, 0, 7), _ => replace(s, 1, 8) }; }",
        2,
        2,
    ),
    (
        "replace-chained",
        "mncs 0.18; module frontier.rep; "
        "fn set_two(s: [u64; 3]) -> (r: [u64; 3]) { return replace(replace(s, 0, 7), 2, 9); }",
        2,
        2,
    ),
    (
        "replace-nominal-record",
        "mncs 0.18; module frontier.rep; record Cell { line: u64, col: u64 } "
        "fn put_cell(cells: [Cell; 4], idx: u64, pos: Cell) -> (r: [Cell; 4]) { return replace(cells, idx, pos); }",
        1,
        1,
    ),
]

DECL_VM_CASES = [
    ("vm-decl-clean-lit", "mncs 0.18; module t; fn f(s: [u64; 3]) -> (r: [u64; 3]) { return replace(s, 1, 9); }", True),
    ("vm-decl-clean-dyn", "mncs 0.18; module t; fn f(s: [u64; 3], i: u64, v: u64) -> (r: [u64; 3]) { return replace(s, i, v); }", True),
    ("vm-decl-gen-exact", "mncs 0.18; module t; fn f<N: Nat>(s: [u64; N], i: u64, v: u64) -> (r: [u64; N]) { return replace(s, i, v); }", True),
    ("vm-decl-non-seq", "mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return replace(x, 0, 1); }", False),
    ("vm-decl-view-lit", "mncs 0.18; module t; fn f(s: [u64; up_to 4]) -> (r: [u64; up_to 4]) { return replace(s, 0, 1); }", False),
    ("vm-decl-bad-idx-var", "mncs 0.18; module t; fn f(s: [u64; 4], b: bool) -> (r: [u64; 4]) { return replace(s, b, 1); }", False),
    ("vm-decl-bad-idx-true", "mncs 0.18; module t; fn f(s: [u64; 4]) -> (r: [u64; 4]) { return replace(s, true, 1); }", False),
    ("vm-decl-bad-idx-bin", "mncs 0.18; module t; fn f(s: [u64; 4], a: bool, b: bool) -> (r: [u64; 4]) { return replace(s, a && b, 1); }", False),
    ("vm-decl-bad-elem-var", "mncs 0.18; module t; fn f(s: [u64; 4], b: bool) -> (r: [u64; 4]) { return replace(s, 0, b); }", False),
    ("vm-decl-bad-elem-call", "mncs 0.18; module t; fn g() -> (r: bool) { return true; } fn f(s: [u64; 4]) -> (r: [u64; 4]) { return replace(s, 0, g()); }", False),
    ("vm-decl-oob-lit", "mncs 0.18; module t; fn f(s: [u64; 3]) -> (r: [u64; 3]) { return replace(s, 3, 9); }", False),
    ("vm-decl-ret-mismatch", "mncs 0.18; module t; fn f(s: [u64; 3]) -> (r: [byte; 3]) { return replace(s, 0, 9); }", False),
    ("vm-decl-ret-borrow", "mncs 0.18; module t; fn f(s: [u64; 3]) -> (r: [u64; up_to 4]) { return replace(s, 0, 9); }", True),
    ("vm-decl-old-profile", "mncs 0.7; module t; fn f(s: [u64; 3]) -> (r: [u64; 3]) { return replace(s, 0, 9); }", False),
    ("vm-decl-type-args", "mncs 0.18; module t; fn f(s: [u64; 3]) -> (r: [u64; 3]) { return replace<u64>(s, 0, 9); }", False),
    ("vm-decl-arity-2-parse", "mncs 0.18; module t; fn f(s: [u64; 3]) -> (r: [u64; 3]) { return replace(s, 0); }", False),
    ("vm-decl-arity-0-parse", "mncs 0.18; module t; fn f() -> (r: [u64; 3]) { return replace(); }", False),
    ("vm-decl-arity-4-parse", "mncs 0.18; module t; fn f(s: [u64; 3]) -> (r: [u64; 3]) { return replace(s, 0, 1, 2); }", False),
    ("vm-decl-old-arity-2", "mncs 0.7; module t; fn f(s: [u64; 3]) -> (r: [u64; 3]) { return replace(s, 0); }", False),
]

NATIVE_ONLY_DECL_CASES = [
    ("native-decl-view-generic", "mncs 0.18; module t; fn f<N: Nat>(s: [u64; up_to N]) -> (r: [u64; up_to N]) { return replace(s, 0, 1); }", [("MNE198", 85, 101)]),
]

DEP_MODULE_SRC = (
    "mncs 0.18; module demo.repdep; "
    "record Entry { code: u64, ok: bool } "
    "fn mk(code: u64, ok: bool) -> (r: Entry) { return Entry { code: code, ok: ok }; }"
)
ROOT_MODULE_SRC = (
    "mncs 0.18; module demo.reproot; use demo.repdep as dep; "
    "fn update(table: [dep.Entry; 4], idx: u64, code: u64) -> (r: [dep.Entry; 4]) { "
    "return replace(table, idx, dep.mk(code, true)); }"
)
BAD_VIEW_ROOT_SRC = (
    "mncs 0.18; module demo.reproot; use demo.repdep as dep; "
    "fn update(table: [dep.Entry; up_to 4], idx: u64, code: u64) -> (r: [dep.Entry; up_to 4]) { "
    "return replace(table, idx, dep.mk(code, true)); }"
)
BAD_ELEM_ROOT_SRC = (
    "mncs 0.18; module demo.reproot; use demo.repdep as dep; "
    "fn update(table: [dep.Entry; 4], idx: u64, bad: bool) -> (r: [dep.Entry; 4]) { "
    "return replace(table, idx, bad); }"
)


def measure_dag_artifact(artifact: dict) -> dict:
    """Compute expanded DAG node count & depth using vm-artifact-codec rules."""
    graph = artifact["code"]["mncs-selected-ssa"]["module"]
    nodes = graph["nodes"]
    root = graph["root"]
    assert root == len(nodes) - 1, (root, len(nodes))
    sizes = [0] * len(nodes)
    depths = [0] * len(nodes)
    for i, node in enumerate(nodes):
        if isinstance(node, dict) and "A" in node:
            size, depth = 1, 1
            for ref in node["A"]:
                assert ref < i
                size += sizes[ref]
                depth = max(depth, depths[ref] + 1)
                assert depth <= 128 and size <= MAX_EXPANDED_NODES, (depth, size)
            sizes[i], depths[i] = size, depth
        elif isinstance(node, dict) and "O" in node:
            size, depth = 1, 1
            for key_ref, val_ref in node["O"]:
                assert key_ref < i and val_ref < i
                size += sizes[key_ref] + sizes[val_ref]
                depth = max(depth, depths[key_ref] + 1, depths[val_ref] + 1)
                assert depth <= 128 and size <= MAX_EXPANDED_NODES, (depth, size)
            sizes[i], depths[i] = size, depth
        else:
            sizes[i], depths[i] = 1, 1
    return {
        "dag_nodes": len(nodes),
        "expanded_nodes": sizes[root],
        "depth": depths[root],
        "max_expanded_limit": MAX_EXPANDED_NODES,
    }


def paged_args(raw: bytes) -> list:
    return logical_module_args(raw if raw else b" ") if not raw else logical_module_args(raw)


def mutate_field(record: dict, key: str, transform) -> None:
    for name, value in record["record"]["fields"]:
        if name == key:
            transform(value)
            return
    raise AssertionError(f"record missing field {key!r}")


def find_first_kind19(function_wire: dict) -> dict:
    for block in wire_linked(field(function_wire, "blocks")):
        for inst in wire_linked(field(block, "instructions")):
            if field(inst, "kind")["integer"]["value"] == 19:
                return inst
    raise AssertionError("expected at least one kind-19 SequenceReplace instruction")


def find_value_wire(function_wire: dict, value_id: int) -> dict:
    for value in wire_linked(field(function_wire, "values")):
        if field(value, "id")["integer"]["value"] == value_id:
            return value
    raise AssertionError(f"value {value_id} missing from function.values")


def wire_bool_ty() -> dict:
    return {
        "finite": {
            "discriminant": 0,
            "payload": [],
            "type_identity": "mncs:0.2:finite-type:mncs.compiler.decl.v1::SemType",
            "variant_identity": "mncs:0.2:finite-variant:mncs.compiler.decl.v1::SemType::TBool",
        }
    }


def make_tamper_variants(prepared: dict, source_fn_wire: dict, source_args: list) -> list[tuple[str, list, dict]]:
    variants: list[tuple[str, list, dict]] = []
    dyn_case = prepared["replace-exact-dyn"]
    dyn_fn = dyn_case["function_wires"][0]
    dyn_args = dyn_case["args"]
    lit_case = prepared["replace-exact-lit"]
    lit_fn = lit_case["function_wires"][0]
    lit_args = lit_case["args"]

    def add(label: str, base_args: list, base_fn: dict, mutator) -> None:
        mutated = copy.deepcopy(base_fn)
        mutator(mutated)
        variants.append((label, base_args, mutated))

    add("tamper-kind-to-select", dyn_args, dyn_fn,
        lambda fn: mutate_field(find_first_kind19(fn), "kind", lambda v: v["integer"].__setitem__("value", 9)))
    add("tamper-operator", dyn_args, dyn_fn,
        lambda fn: mutate_field(find_first_kind19(fn), "operator", lambda v: v["integer"].__setitem__("value", 9)))
    add("tamper-argc", dyn_args, dyn_fn,
        lambda fn: mutate_field(find_first_kind19(fn), "argc", lambda v: v["integer"].__setitem__("value", 2)))
    add("tamper-binding-start", dyn_args, dyn_fn,
        lambda fn: mutate_field(find_first_kind19(fn), "binding_start", lambda v: v["integer"].__setitem__("value", v["integer"]["value"] + 1)))
    add("tamper-start", dyn_args, dyn_fn,
        lambda fn: mutate_field(find_first_kind19(fn), "start", lambda v: v["integer"].__setitem__("value", v["integer"]["value"] + 1)))
    add("tamper-end", dyn_args, dyn_fn,
        lambda fn: mutate_field(find_first_kind19(fn), "end", lambda v: v["integer"].__setitem__("value", v["integer"]["value"] - 1)))
    add("tamper-nominal-valid", dyn_args, dyn_fn,
        lambda fn: mutate_field(field(find_first_kind19(fn), "nominal_identity"), "valid", lambda v: v["boolean"].__setitem__("value", True)))
    add("tamper-identity-byte", dyn_args, dyn_fn,
        lambda fn: field(find_first_kind19(fn), "identity")["record"]["fields"][0][1]["sequence"]["values"][0]["byte"].__setitem__("value", 88))

    def _drop_elem_input(fn: dict) -> None:
        inst = find_first_kind19(fn)
        inputs = field(inst, "inputs")
        second = dict(inputs["finite"]["payload"])["tail"]
        second["finite"]["payload"] = [
            (k, {"finite": {"discriminant": 0, "payload": [],
                            "type_identity": inputs["finite"]["type_identity"],
                            "variant_identity": "mncs:0.2:finite-variant:mncs.compiler.ssa.v1::IdList::INil"}}
             if k == "tail" else val)
            for k, val in second["finite"]["payload"]
        ]

    add("tamper-drop-elem-input", dyn_args, dyn_fn, _drop_elem_input)

    def _swap_base_and_index(fn: dict) -> None:
        inst = find_first_kind19(fn)
        inputs = field(inst, "inputs")
        first = dict(inputs["finite"]["payload"])["head"]
        second = dict(dict(inputs["finite"]["payload"])["tail"]["finite"]["payload"])["head"]
        id0 = field(first, "value")["integer"]["value"]
        id1 = field(second, "value")["integer"]["value"]
        mutate_field(first, "value", lambda v: v["integer"].__setitem__("value", id1))
        mutate_field(second, "value", lambda v: v["integer"].__setitem__("value", id0))

    add("tamper-swap-base-index", dyn_args, dyn_fn, _swap_base_and_index)

    def _wrong_elem_input(fn: dict) -> None:
        inst = find_first_kind19(fn)
        inputs = field(inst, "inputs")
        first = dict(inputs["finite"]["payload"])["head"]
        base_id = field(first, "value")["integer"]["value"]
        third = dict(dict(dict(inputs["finite"]["payload"])["tail"]["finite"]["payload"])["tail"]["finite"]["payload"])["head"]
        mutate_field(third, "value", lambda v: v["integer"].__setitem__("value", base_id))

    add("tamper-wrong-elem-input", dyn_args, dyn_fn, _wrong_elem_input)

    def _view_result_ty(fn: dict) -> None:
        inst = find_first_kind19(fn)
        res_id = field(inst, "result")["integer"]["value"]
        res_val = find_value_wire(fn, res_id)
        for container in (inst, res_val):
            ty = field(container, "ty")
            for k, val in ty["finite"]["payload"]:
                if k == "length_valid":
                    val["boolean"]["value"] = False

    add("tamper-result-view-type", lit_args, lit_fn, _view_result_ty)

    def _wrong_result_elem(fn: dict) -> None:
        inst = find_first_kind19(fn)
        res_id = field(inst, "result")["integer"]["value"]
        res_val = find_value_wire(fn, res_id)
        for container in (inst, res_val):
            ty = field(container, "ty")
            ty["finite"]["payload"] = [
                (k, wire_bool_ty() if k == "element" else val)
                for k, val in ty["finite"]["payload"]
            ]

    add("tamper-result-element-type", lit_args, lit_fn, _wrong_result_elem)

    # Anti-tamper directly against real compiler module line_cols_step!
    add("tamper-source-line-cols-step-span", source_args, source_fn_wire,
        lambda fn: mutate_field(find_first_kind19(fn), "binding_end", lambda v: v["integer"].__setitem__("value", v["integer"]["value"] - 1)))
    return variants


def test_decl_and_vm() -> dict:
    raw_src = SOURCE_MODULE_PATH.read_bytes()
    src_text = raw_src.decode("utf-8")
    src_args = logical_module_args(raw_src)

    decl = Probe(
        DECL_MODULE, ["prove_unit", "local_nominal_type_identities"],
        backend="cranelift", backend_roots=["prove_unit", "local_nominal_type_identities"],
        timeout_seconds=420.0,
    )
    vm_prep_cases = []
    nominals_by_name = {}
    t_decl = time.monotonic()
    try:
        for case_id, src, want_ok in DECL_VM_CASES:
            args = paged_args(src.encode("utf-8"))
            oracle_diags = decl.send({"elaborate": src})
            ref_mne = [
                (d["code"], d["span"]["start"], d["span"]["end"])
                for d in oracle_diags if d.get("code", "").startswith("MNE")
            ]
            ref_mnp = [
                (d["code"], d["span"]["start"], d["span"]["end"])
                for d in oracle_diags if d.get("code", "").startswith("MNP")
            ]
            wire = call(decl, DECL_MODULE, "prove_unit", args)["returned"][0]
            got = decode(wire)
            assert got["ok"] is want_ok, (case_id, got["ok"], want_ok, oracle_diags)
            if ref_mnp:
                assert got["unit"]["ok"] is False, (case_id, got["unit"])
            else:
                assert got["unit"]["ok"] is True, (case_id, got["unit"])
                native_diags = [
                    (sem.KIND_TO_MNE[o["kind"]], o["start"], o["end"])
                    for o in sem.flist(got["obls"], 1) if o["status"] == 1
                ]
                assert native_diags == ref_mne, (case_id, native_diags, ref_mne)
            vm_prep_cases.append((case_id, args, got))

        for case_id, src, expected in NATIVE_ONLY_DECL_CASES:
            args = paged_args(src.encode("utf-8"))
            wire = call(decl, DECL_MODULE, "prove_unit", args)["returned"][0]
            got = decode(wire)
            native_diags = [
                (sem.KIND_TO_MNE[o["kind"]], o["start"], o["end"])
                for o in sem.flist(got["obls"], 1) if o["status"] == 1
            ]
            assert got["ok"] is False and native_diags == expected, (case_id, native_diags, expected)
            vm_prep_cases.append((case_id, args, got))

        for case_id, src, _, _ in SMALL_CASES:
            args = paged_args(src.encode("utf-8"))
            prove_wire = call(decl, DECL_MODULE, "prove_unit", args)["returned"][0]
            prove_dec = decode(prove_wire)
            assert prove_dec["ok"] is True, (case_id, prove_dec)
            unit_wire = field(prove_wire, "unit")
            nom_wire = call(decl, DECL_MODULE, "local_nominal_type_identities", [*args, unit_wire])["returned"][0]
            nominals_by_name[case_id] = (args, nom_wire)

        # Real compiler module src/compiler/source.mncs
        src_ref = [d for d in decl.send({"elaborate": src_text}) if d.get("code", "").startswith(("MNE", "MNP"))]
        assert src_ref == [], src_ref
        t0 = time.monotonic()
        src_prove1 = call(decl, DECL_MODULE, "prove_unit", src_args)
        src_decl_ms_1 = (time.monotonic() - t0) * 1000.0
        t0 = time.monotonic()
        src_prove2 = call(decl, DECL_MODULE, "prove_unit", src_args)
        src_decl_ms_2 = (time.monotonic() - t0) * 1000.0
        assert src_prove1["returned"] == src_prove2["returned"], "non-deterministic source.mncs prove_unit"
        src_dec = decode(src_prove1["returned"][0])
        assert src_dec["ok"] is True and src_dec["fn_count"] == 55, src_dec
        src_obls = sem.flist(src_dec["obls"], 1)
        assert all(o["status"] != 1 for o in src_obls), src_obls
        assert {o["kind"] for o in src_obls if o["status"] == 2} <= sem.UNKNOWN_KINDS, src_obls
        src_nom = call(decl, DECL_MODULE, "local_nominal_type_identities", [*src_args, field(src_prove1["returned"][0], "unit")])["returned"][0]
        decl_artifact = decl.send({"emit_vm_artifact": {"module": DECL_MODULE}})["artifact"]
    finally:
        decl_stderr = decl.close() if not decl.stderr_file.closed else ""

    vm_batch = [
        {"id": case_id, "function": "prove_unit", "args": args, "type_args": TYPE_ARGS, "step_budget": STEP_BUDGET}
        for case_id, args, _ in vm_prep_cases
    ]
    with tempfile.TemporaryDirectory(prefix="mncs-vm-decl-seqrep-") as tmp:
        art_path = Path(tmp) / "decl.json"
        art_path.write_text(json.dumps(decl_artifact, sort_keys=True) + "\n")
        vm_doc, vm_wall, vm_rss, _, _ = run_batch(art_path, vm_batch, Path(tmp), module=DECL_MODULE)
    for (case_id, _, cranelift_dec), vm_row in zip(vm_prep_cases, vm_doc["results"], strict=True):
        assert vm_row["outcome"] == {"kind": "completed"}, (case_id, vm_row)
        vm_dec = decode(vm_to_wire(vm_row["record"]["returned"][0]))
        assert vm_dec == cranelift_dec, (case_id, "Cranelift vs Canonical VM mismatch")

    return {
        "nominals_by_name": nominals_by_name,
        "src_args": src_args,
        "src_nominal_wire": src_nom,
        "src_decl": {
            "ok": src_dec["ok"],
            "fn_count": src_dec["fn_count"],
            "obligations": len(src_obls),
            "unknown_kinds": sorted({o["kind"] for o in src_obls if o["status"] == 2}),
            "steps": src_prove1["steps"],
            "call_ms": [round(src_decl_ms_1, 3), round(src_decl_ms_2, 3)],
            "elapsed_seconds": round(time.monotonic() - t_decl, 3),
        },
        "decl_vm": {
            "cases": len(vm_prep_cases),
            "vm_wall_seconds": round(vm_wall, 6),
            "vm_peak_rss_kb": vm_rss,
            "agreement": True,
        },
        "decl_stderr": decl_stderr,
    }


def test_flow_step(nominals_by_name: dict, src_args: list) -> dict:
    flow = Probe(FLOW_MODULE, ["lower_unit"], backend="cranelift", backend_roots=["lower_unit"], timeout_seconds=420.0)
    flows = {}
    try:
        for case_id, _, _, _ in SMALL_CASES:
            args, nom = nominals_by_name[case_id]
            flow_wire = call(flow, FLOW_MODULE, "lower_unit", args)["returned"][0]
            flow_dec = decode(flow_wire)
            assert flow_dec["valid"] and flow_dec["graph_ok"] and flow_dec["proof"]["ok"], (case_id, flow_dec)
            flows[case_id] = (args, flow_wire, nom)
        t0 = time.monotonic()
        src_flow = call(flow, FLOW_MODULE, "lower_unit", src_args)
        src_flow_ms = (time.monotonic() - t0) * 1000.0
        src_flow_wire = src_flow["returned"][0]
        src_flow_dec = decode(src_flow_wire)
        flow_fns = linked(src_flow_dec["functions"])
        assert src_flow_dec["valid"] and src_flow_dec["proof"]["ok"], src_flow_dec
        assert len(flow_fns) == 55 and all(fn["verified"] for fn in flow_fns), src_flow_dec
    finally:
        flow.close()
    return {
        "flows": flows,
        "src_flow_wire": src_flow_wire,
        "src_flow_ms": round(src_flow_ms, 3),
        "src_flow_steps": src_flow["steps"],
        "src_flow_verified_functions": len(flow_fns),
    }


def test_ssa_and_vm(flow_res: dict, src_nominal_wire: dict, src_args: list) -> dict:
    flows = flow_res["flows"]
    ssa = Probe(
        SSA_MODULE, ["lower_value_ssa", "verify_function"],
        backend="cranelift", backend_roots=["lower_value_ssa", "verify_function"],
        timeout_seconds=420.0,
    )
    prepared = {}
    oracle_total = 0
    native_total = 0
    try:
        for case_id, src, expected_replaces, expected_oracle in SMALL_CASES:
            args, flow_wire, nom_wire = flows[case_id]
            oracle = ssa.send({"ssa": src})
            assert oracle.get("ssa") is not None and not oracle.get("diagnostics"), (case_id, oracle)
            oracle_count = sum(
                1
                for fn in oracle["ssa"]["functions"]
                for blk in fn["blocks"]
                for ins in blk["instructions"]
                if "SequenceReplace" in ins["kind"]
            )
            assert oracle_count == expected_oracle, (case_id, oracle_count, expected_oracle)
            oracle_total += oracle_count

            ssa_raw = call(ssa, SSA_MODULE, "lower_value_ssa", [*args, flow_wire, nom_wire])
            ssa_wire = ssa_raw["returned"][0]
            ssa_dec = decode(ssa_wire)
            assert ssa_dec["valid"] and ssa_dec["unsupported_block_count"] == 0, (case_id, ssa_dec)
            assert ssa_dec["function_count"] == ssa_dec["verified_function_count"] >= 1, (case_id, ssa_dec)
            function_wires = wire_linked(field(ssa_wire, "functions"))
            native_count = 0
            for fn_wire in function_wires:
                ver = call(ssa, SSA_MODULE, "verify_function", [*args, fn_wire])["returned"][0]
                assert ver == {"boolean": {"value": True}}, (case_id, ver)
                for blk in wire_linked(field(fn_wire, "blocks")):
                    for ins in wire_linked(field(blk, "instructions")):
                        if field(ins, "kind")["integer"]["value"] == 19:
                            native_count += 1
                            assert field(ins, "operator")["integer"]["value"] == 10
                            assert field(ins, "argc")["integer"]["value"] == 3
            assert native_count == expected_replaces, (case_id, native_count, expected_replaces)
            native_total += native_count
            prepared[case_id] = {"args": args, "ssa_dec": ssa_dec, "function_wires": function_wires}

        # Real compiler module src/compiler/source.mncs
        t0 = time.monotonic()
        src_ssa1 = call(ssa, SSA_MODULE, "lower_value_ssa", [*src_args, flow_res["src_flow_wire"], src_nominal_wire])
        src_ssa_ms_1 = (time.monotonic() - t0) * 1000.0
        t0 = time.monotonic()
        src_ssa2 = call(ssa, SSA_MODULE, "lower_value_ssa", [*src_args, flow_res["src_flow_wire"], src_nominal_wire])
        src_ssa_ms_2 = (time.monotonic() - t0) * 1000.0
        assert src_ssa1["returned"] == src_ssa2["returned"], "non-deterministic source.mncs lower_value_ssa"
        src_ssa_dec = decode(src_ssa1["returned"][0])
        assert src_ssa_dec["function_count"] == 55, src_ssa_dec
        assert src_ssa_dec["supported_function_count"] == 55, src_ssa_dec
        assert src_ssa_dec["unsupported_block_count"] == 0, src_ssa_dec
        assert src_ssa_dec["verified_function_count"] == 55, src_ssa_dec

        src_fn_wires = wire_linked(field(src_ssa1["returned"][0], "functions"))
        line_cols_step_wire = src_fn_wires[43]
        line_cols_global_wire = src_fn_wires[42]
        step_id = decode(field(line_cols_step_wire, "identity"))
        step_name = bytes(step_id["bytes"][:step_id["len"]]).decode()
        global_id = decode(field(line_cols_global_wire, "identity"))
        global_name = bytes(global_id["bytes"][:global_id["len"]]).decode()
        assert step_name.endswith("::line_cols_step") and global_name.endswith("::line_cols_global"), (step_name, global_name)
        assert field(line_cols_step_wire, "verified")["boolean"]["value"] is True
        assert field(line_cols_global_wire, "verified")["boolean"]["value"] is True
        assert call(ssa, SSA_MODULE, "verify_function", [*src_args, line_cols_step_wire])["returned"][0] == {"boolean": {"value": True}}
        assert call(ssa, SSA_MODULE, "verify_function", [*src_args, line_cols_global_wire])["returned"][0] == {"boolean": {"value": True}}

        step_k19 = sum(
            1
            for blk in wire_linked(field(line_cols_step_wire, "blocks"))
            for ins in wire_linked(field(blk, "instructions"))
            if field(ins, "kind")["integer"]["value"] == 19
        )
        assert step_k19 == 1, step_k19

        # Anti-tamper verification on Cranelift
        tampers = make_tamper_variants(prepared, line_cols_step_wire, src_args)
        for label, t_args, t_fn in tampers:
            rej = call(ssa, SSA_MODULE, "verify_function", [*t_args, t_fn])["returned"][0]
            assert rej == {"boolean": {"value": False}}, (label, rej)

        ssa_artifact = ssa.send({"emit_vm_artifact": {"module": SSA_MODULE}})["artifact"]
    finally:
        ssa_stderr = ssa.close() if not ssa.stderr_file.closed else ""

    dag_stats = measure_dag_artifact(ssa_artifact)

    # Canonical VM execution on mncs-vm for SMALL_CASES + valid/tamper verify_function!
    # Whole-module source wires exceed serde_json's default 128-level recursion limit
    # on mncs-vm calls.json due to deeply nested linked-list ValueList encoding, so
    # source.mncs verification is enforced on Cranelift above while all small-fixture
    # lower/verify/anti-tamper cases run on both Cranelift and mncs-vm.
    vm_tampers = [(label, t_args, t_fn) for label, t_args, t_fn in tampers if not label.startswith("tamper-source-")]
    vm_cases = []
    for case_id, _, _, _ in SMALL_CASES:
        args, flow_wire, nom_wire = flows[case_id]
        vm_cases.append({"id": f"lower:{case_id}", "function": "lower_value_ssa", "args": [*args, flow_wire, nom_wire], "type_args": TYPE_ARGS, "step_budget": STEP_BUDGET})
        vm_cases.append({"id": f"verify:{case_id}", "function": "verify_function", "args": [*args, prepared[case_id]["function_wires"][0]], "type_args": TYPE_ARGS, "step_budget": STEP_BUDGET})
    for label, t_args, t_fn in vm_tampers:
        vm_cases.append({"id": f"tamper:{label}", "function": "verify_function", "args": [*t_args, t_fn], "type_args": TYPE_ARGS, "step_budget": STEP_BUDGET})

    with tempfile.TemporaryDirectory(prefix="mncs-vm-ssa-seqrep-") as tmp:
        art_path = Path(tmp) / "ssa.json"
        art_path.write_text(json.dumps(ssa_artifact, sort_keys=True) + "\n")
        vm_doc, vm_wall, vm_rss, _, _ = run_batch(art_path, vm_cases, Path(tmp), module=SSA_MODULE)

    idx = 0
    for case_id, _, _, _ in SMALL_CASES:
        low_row = vm_doc["results"][idx]; idx += 1
        assert low_row["outcome"] == {"kind": "completed"}, (case_id, low_row)
        assert decode(vm_to_wire(low_row["record"]["returned"][0])) == prepared[case_id]["ssa_dec"], case_id
        ver_row = vm_doc["results"][idx]; idx += 1
        assert ver_row["outcome"] == {"kind": "completed"} and ver_row["record"]["returned"] == [{"Boolean": True}], (case_id, ver_row)
    for label, _, _ in vm_tampers:
        row = vm_doc["results"][idx]; idx += 1
        assert row["outcome"] == {"kind": "completed"} and row["record"]["returned"] == [{"Boolean": False}], (label, row)

    return {
        "small_cases": len(SMALL_CASES),
        "small_stage0_seq_replaces": oracle_total,
        "small_native_seq_replaces": native_total,
        "tamper_rejections": [label for label, _, _ in tampers],
        "source_module": {
            "function_count": src_ssa_dec["function_count"],
            "supported_function_count": src_ssa_dec["supported_function_count"],
            "verified_function_count": src_ssa_dec["verified_function_count"],
            "unsupported_block_count": src_ssa_dec["unsupported_block_count"],
            "instruction_count": src_ssa_dec["instruction_count"],
            "block_count": src_ssa_dec["block_count"],
            "line_cols_step_verified": True,
            "line_cols_global_verified": True,
            "line_cols_step_kind19_count": step_k19,
            "steps": src_ssa1["steps"],
            "call_ms": [round(src_ssa_ms_1, 3), round(src_ssa_ms_2, 3)],
        },
        "ssa_vm": {
            "artifact_id": ssa_artifact["artifact_id"],
            "dag_footprint": dag_stats,
            "batch_cases": len(vm_cases),
            "vm_wall_seconds": round(vm_wall, 6),
            "vm_peak_rss_kb": vm_rss,
            "agreement": True,
        },
        "ssa_stderr": ssa_stderr,
    }


def decode_project_object(obj: dict):
    """Decode typed wire values as the JSON parser finishes each object."""
    if len(obj) == 1:
        tag, payload = next(iter(obj.items()))
        if tag == "record":
            return dict(payload["fields"])
        if tag == "finite":
            return {"$v": payload["discriminant"], "$p": dict(payload.get("payload", []))}
        if tag == "sequence":
            return payload["values"]
        if tag in ("byte", "integer", "boolean"):
            return payload["value"]
    return obj


def compile_source_project(probe, identities, source, *, compare_digest=None):
    # The full compiler module returns deeply nested proof, flow and SSA values.
    # Digest its complete wire reply for the repeat check. Decode directly into
    # the semantic representation to avoid retaining a second tree of wire tags.
    path = Path("compiler/source.mncs")
    request = project.request_value(identities, [(str(path), path, source)])
    proc = probe._route(request)
    proc.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
    proc.stdin.flush()
    if probe._request_timeout_s:
        line = probe._readline(proc, probe._request_timeout_s, {})
    else:
        line = proc.stdout.readline()
        if not line.endswith(b"\n"):
            raise RuntimeError("incomplete full-module project reply")
    digest = hashlib.sha256(line).digest()
    if compare_digest is not None:
        assert digest == compare_digest, "non-deterministic source.mncs project result"
        return None, digest
    response = json.loads(line, object_hook=decode_project_object)
    assert response["status"] == "returned", response.get("failure")
    return response, digest


def test_project_pipeline() -> dict:
    os.environ["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa,project"
    # Project calls lower SSA internally. Only compile_project is called from the host.
    os.environ["MNCS_PROBE_EXECUTION_MODULES"] = project.PROJECT_MODULE
    os.environ["MNCS_PROBE_GENERIC_SEEDS"] = project._filtered_seeds(
        os.environ["MNCS_PROBE_GENERIC_SEEDS"], project.PROJECT_MODULE)
    os.environ["MNCS_PROBE_BACKEND_ROOTS"] = "compile_project"
    os.environ["MNCS_PROBE_BACKEND"] = project.backend_policy.resolve("project")
    t0 = time.monotonic()
    probe = project.Probe()
    try:
        identities = project.identity_map(probe)

        # 1. Multi-module imported record + SequenceReplace
        pos_sources = [
            ("a-dep.mncs", Path("a-dep.mncs"), DEP_MODULE_SRC.encode("utf-8")),
            ("b-root.mncs", Path("b-root.mncs"), ROOT_MODULE_SRC.encode("utf-8")),
        ]
        t_pos = time.monotonic()
        pos_resp = probe.send(project.request_value(identities, pos_sources))
        pos_ms = (time.monotonic() - t_pos) * 1000.0
        assert pos_resp.get("status") == "returned", pos_resp
        pos_native = project.decode(pos_resp["returned"][0])
        pos_oracle = probe.send({"project_oracle": {"root": ROOT_MODULE_SRC, "modules": {"demo.repdep": DEP_MODULE_SRC}}})
        assert pos_native["valid"] is True and pos_native["value_ssa_valid"] is True, pos_native["diagnostics"]
        assert pos_oracle["valid"] is True, pos_oracle["diagnostics"]
        root_mod = next(m for m in project.flist(pos_native["modules"]) if m["source_index"] == 1)
        assert root_mod["value_ssa"]["verified_function_count"] == 1
        root_k19 = sum(
            1
            for fn in project.flist(root_mod["value_ssa"]["functions"])
            for blk in project.flist(fn["blocks"])
            for ins in project.flist(blk["instructions"])
            if ins["kind"] == 19
        )
        assert root_k19 == 1, root_k19

        # 2. Negative multi-module MNE198 (view refused) & MNE200 (wrong element)
        for neg_label, bad_src, want_code, snippet in (
            ("MNE198", BAD_VIEW_ROOT_SRC, "MNE198", "replace(table, idx, dep.mk(code, true))"),
            ("MNE200", BAD_ELEM_ROOT_SRC, "MNE200", "bad)"),
        ):
            target_span = bad_src.rindex("bad") if want_code == "MNE200" else bad_src.index(snippet)
            target_end = target_span + (3 if want_code == "MNE200" else len(snippet))
            bad_sources = [
                ("a-dep.mncs", Path("a-dep.mncs"), DEP_MODULE_SRC.encode("utf-8")),
                ("b-root.mncs", Path("b-root.mncs"), bad_src.encode("utf-8")),
            ]
            bad_native = project.decode(probe.send(project.request_value(identities, bad_sources))["returned"][0])
            bad_oracle = probe.send({"project_oracle": {"root": bad_src, "modules": {"demo.repdep": DEP_MODULE_SRC}}})
            assert bad_native["valid"] is False and bad_oracle["valid"] is False, (neg_label, bad_native, bad_oracle)
            bad_proof = next(m for m in project.flist(bad_native["modules"]) if m["source_index"] == 1)["flow"]["proof"]
            assert any(
                d.get("code") == want_code and (d.get("span", {}).get("start"), d.get("span", {}).get("end")) == (target_span, target_end)
                for d in bad_oracle["diagnostics"]
            ), (neg_label, bad_oracle["diagnostics"])
            assert any(
                sem.KIND_TO_MNE.get(o["kind"]) == want_code and (o["start"], o["end"]) == (target_span, target_end)
                for o in project.flist(bad_proof["obls"]) if o["status"] == 1
            ), (neg_label, bad_proof["obls"])

        # 3. Real src/compiler/source.mncs via project.compile_project + comment-edit incremental check
        raw_src = SOURCE_MODULE_PATH.read_bytes()
        commented_src = raw_src + b"\n// incremental comment check for source.mncs\n"
        t_s1 = time.monotonic()
        src_resp1, digest = compile_source_project(probe, identities, raw_src)
        src_proj_ms_1 = (time.monotonic() - t_s1) * 1000.0
        t_s2 = time.monotonic()
        compile_source_project(probe, identities, raw_src, compare_digest=digest)
        src_proj_ms_2 = (time.monotonic() - t_s2) * 1000.0
        src_proj_dec = src_resp1["returned"][0]
        src_oracle = probe.send({"project_oracle": {"root": raw_src.decode("utf-8"), "modules": {}}})
        assert src_proj_dec["valid"] is True and src_oracle["valid"] is True, (src_proj_dec["diagnostics"], src_oracle["diagnostics"])
        src_mod = project.flist(src_proj_dec["modules"])[0]
        assert src_mod["flow"]["proof"]["ok"] is True and src_mod["flow"]["valid"] is True
        assert src_mod["value_ssa"]["supported_function_count"] == 55 and src_mod["value_ssa"]["verified_function_count"] == 55
        proj_k19 = sum(
            1
            for fn in project.flist(src_mod["value_ssa"]["functions"])
            for blk in project.flist(fn["blocks"])
            for ins in project.flist(blk["instructions"])
            if ins["kind"] == 19
        )
        assert proj_k19 == 1, proj_k19

        t_inc = time.monotonic()
        inc_resp, _ = compile_source_project(probe, identities, commented_src)
        src_inc_ms = (time.monotonic() - t_inc) * 1000.0
        inc_dec = inc_resp["returned"][0]
        assert inc_dec["valid"] is True
        inc_mod = project.flist(inc_dec["modules"])[0]
        assert inc_mod["value_ssa"]["verified_function_count"] == 55 and inc_mod["value_ssa"]["supported_function_count"] == 55
    finally:
        probe_wall_s = time.monotonic() - t0
        probe.close()

    return {
        "project_probe_wall_seconds": round(probe_wall_s, 3),
        "multi_module_verified_functions": root_mod["value_ssa"]["verified_function_count"],
        "multi_module_kind19_count": root_k19,
        "multi_module_call_ms": round(pos_ms, 3),
        "negative_multi_module_matched": ["MNE198", "MNE200"],
        "source_module_project": {
            "valid": src_proj_dec["valid"],
            "value_ssa_valid": src_proj_dec["value_ssa_valid"],
            "supported_functions": src_mod["value_ssa"]["supported_function_count"],
            "verified_functions": src_mod["value_ssa"]["verified_function_count"],
            "kind19_count": proj_k19,
            "steps": src_resp1["steps"],
            "call_ms": [round(src_proj_ms_1, 3), round(src_proj_ms_2, 3)],
            "incremental_comment_edit_ms": round(src_inc_ms, 3),
        },
    }


def run_module_phases() -> dict:
    decl_res = test_decl_and_vm()
    flow_res = test_flow_step(decl_res["nominals_by_name"], decl_res["src_args"])
    ssa_res = test_ssa_and_vm(flow_res, decl_res["src_nominal_wire"], decl_res["src_args"])
    return {
        "schema_version": 1,
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "decl": decl_res["src_decl"],
        "decl_vm": decl_res["decl_vm"],
        "flow": {
            "src_flow_ms": flow_res["src_flow_ms"],
            "src_flow_steps": flow_res["src_flow_steps"],
            "src_flow_verified_functions": flow_res["src_flow_verified_functions"],
        },
        "ssa": ssa_res,
    }


def main() -> dict:
    # Bound this test and its descendants even when the runner restarts it.
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    budget = 8 * 1024**3
    for limit in (soft, hard):
        if limit != resource.RLIM_INFINITY:
            budget = min(budget, limit)
    resource.setrlimit(resource.RLIMIT_AS, (budget, hard))
    started = time.monotonic()
    result = run_module_phases()
    return {
        **result,
        "project": test_project_pipeline(),
        "max_child_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


if __name__ == "__main__":
    os.chdir(ROOT)
    print(json.dumps(main(), indent=2))
