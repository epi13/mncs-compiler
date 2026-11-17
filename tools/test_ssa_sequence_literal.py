#!/usr/bin/env python3
"""Differential, anti-tamper, project, and canonical-VM proof for SequenceLiteral SSA."""
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
    integer, linked, run_batch, vm_to_wire, wire_linked,
)

DECL_MODULE = "mncs.compiler.decl.v1"
FLOW_MODULE = "mncs.compiler.flow.v1"
STRIDE = TYPE_ARGS[1]["value"]
BACKEND_POLICY_PATH = ROOT / "src" / "compiler" / "backend_policy.mncs"

SMALL_CASES = [
    (
        "seq-empty-view",
        "mncs 0.18; module frontier.seq; fn empty_view() -> (r: [byte; up_to 4]) { return []; }",
        1,
    ),
    (
        "seq-empty-exact-zero",
        "mncs 0.18; module frontier.seq; fn empty_exact() -> (r: [byte; 0]) { return []; }",
        1,
    ),
    (
        "seq-exact-scalars",
        "mncs 0.18; module frontier.seq; fn exact_scalars(flag: bool, x: u64) -> (r: [u64; 3]) { "
        "if flag { return [1, x, x + 2]; } return [3, x, x + 4]; }",
        2,
    ),
    (
        "seq-bytes-view",
        "mncs 0.18; module frontier.seq; fn bytes_view() -> (r: [byte; up_to 8]) { return [67, 80, 45]; }",
        1,
    ),
    (
        "seq-nested-exact",
        "mncs 0.18; module frontier.seq; "
        "fn nested(a: [byte; 2], b: [byte; 2]) -> (r: [[byte; 2]; 2]) { return [a, b]; }",
        1,
    ),
    (
        "seq-match-arms",
        "mncs 0.18; module frontier.seq; "
        "fn armed(k: u64) -> (r: [u64; 2]) { return match k { 0 => [1, 2], _ => [3, 4] }; }",
        2,
    ),
    (
        "seq-record-call-elements",
        "mncs 0.18; module frontier.seq; record R { x: u64, ok: bool } "
        "fn make(x: u64) -> (r: R) { return R { x: x, ok: x == 0 }; } "
        "fn all(flag: bool) -> (r: [R; 2]) { if flag { return [make(0), make(1)]; } return [make(2), make(3)]; }",
        2,
    ),
]

DECL_VM_CASES = [
    ("vm-decl-missing-expected", "mncs 0.18; module t; fn f() -> (r: u64) { return [1, 2].len; }"),
    ("vm-decl-len-mismatch", "mncs 0.18; module t; fn f() -> (r: [u64; 3]) { return [1, 2]; }"),
    ("vm-decl-view-overflow", "mncs 0.18; module t; fn f() -> (r: [u64; up_to 1]) { return [1, 2]; }"),
    ("vm-decl-elem-mismatch", "mncs 0.18; module t; fn f(b: bool) -> (r: [u64; 2]) { return [1, b]; }"),
    ("vm-decl-empty-view", "mncs 0.18; module t; fn f() -> (r: [byte; up_to 4]) { return []; }"),
    ("vm-decl-nested-exact", "mncs 0.18; module t; fn f(a: [byte; 2], b: [byte; 2]) -> (r: [[byte; 2]; 2]) { return [a, b]; }"),
]

DEP_MODULE_SRC = (
    "mncs 0.18; module demo.seqdep; "
    "record Entry { code: u64, tag: byte } "
    "fn mk(code: u64, tag: byte) -> (r: Entry) { return Entry { code: code, tag: tag }; }"
)
ROOT_MODULE_SRC = (
    "mncs 0.18; module demo.seqroot; use demo.seqdep as dep; "
    "fn items(x: u64) -> (r: [dep.Entry; 2]) { return [dep.mk(x, 65), dep.mk(x + 1, 66)]; } "
    "fn tags() -> (r: [byte; up_to 4]) { return [65, 66]; }"
)
BAD_ROOT_MODULE_SRC = (
    "mncs 0.18; module demo.seqroot; use demo.seqdep as dep; "
    "fn items(x: u64) -> (r: [dep.Entry; 2]) { return [dep.mk(x, 65)]; }"
)


def paged_args(raw: bytes) -> list:
    chunks = [raw[i:i + STRIDE] for i in range(0, len(raw), STRIDE)] or [b""]
    pages = {
        "sequence": {
            "values": [
                {"sequence": {"values": [{"byte": {"value": b}} for b in chunk]}}
                for chunk in chunks
            ]
        }
    }
    return [pages, integer(STRIDE), integer(len(raw))]


def mutate_field(record: dict, key: str, transform) -> None:
    for name, value in record["record"]["fields"]:
        if name == key:
            transform(value)
            return
    raise AssertionError(f"record missing field {key!r}")


def find_first_kind18(function_wire: dict) -> dict:
    for block in wire_linked(field(function_wire, "blocks")):
        for inst in wire_linked(field(block, "instructions")):
            if field(inst, "kind")["integer"]["value"] == 18:
                return inst
    raise AssertionError("expected at least one kind-18 SequenceConstruct instruction")


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


def make_tamper_variants(prepared: dict) -> list[tuple[str, list, dict]]:
    variants: list[tuple[str, list, dict]] = []
    scalar_case = prepared["seq-exact-scalars"]
    scalar_fn = scalar_case["function_wires"][0]
    scalar_args = scalar_case["args"]

    def add(label: str, base_args: list, base_fn: dict, mutator) -> None:
        mutated = copy.deepcopy(base_fn)
        mutator(mutated)
        variants.append((label, base_args, mutated))

    add("tamper-operator", scalar_args, scalar_fn,
        lambda fn: mutate_field(find_first_kind18(fn), "operator", lambda v: v["integer"].__setitem__("value", 2)))
    add("tamper-argc", scalar_args, scalar_fn,
        lambda fn: mutate_field(find_first_kind18(fn), "argc", lambda v: v["integer"].__setitem__("value", 2)))
    add("tamper-start", scalar_args, scalar_fn,
        lambda fn: mutate_field(find_first_kind18(fn), "start", lambda v: v["integer"].__setitem__("value", v["integer"]["value"] + 1)))
    add("tamper-end", scalar_args, scalar_fn,
        lambda fn: mutate_field(find_first_kind18(fn), "end", lambda v: v["integer"].__setitem__("value", v["integer"]["value"] - 1)))

    def _drop_input(fn: dict) -> None:
        inst = find_first_kind18(fn)
        inputs = field(inst, "inputs")
        # inputs is ICons(id0, ICons(id1, ICons(id2, INil))); drop the tail node.
        second = dict(inputs["finite"]["payload"])["tail"]
        third = dict(second["finite"]["payload"])["tail"]
        second["finite"]["payload"] = [
            (k, {"finite": {"discriminant": 0, "payload": [],
                            "type_identity": inputs["finite"]["type_identity"],
                            "variant_identity": "mncs:0.2:finite-variant:mncs.compiler.ssa.v1::IdList::INil"}}
             if k == "tail" else val)
            for k, val in second["finite"]["payload"]
        ]
        assert third["finite"]["discriminant"] == 1

    add("tamper-drop-input", scalar_args, scalar_fn, _drop_input)

    def _wrong_input_type(fn: dict) -> None:
        inst = find_first_kind18(fn)
        # First param (id=0) in exact_scalars(flag: bool, x: u64) is bool.
        inputs = field(inst, "inputs")
        first_id = dict(inputs["finite"]["payload"])["head"]
        mutate_field(first_id, "value", lambda v: v["integer"].__setitem__("value", 0))

    add("tamper-wrong-input-type", scalar_args, scalar_fn, _wrong_input_type)

    def _wrong_result_elem(fn: dict) -> None:
        inst = find_first_kind18(fn)
        res_id = field(inst, "result")["integer"]["value"]
        res_val = find_value_wire(fn, res_id)
        for container in (inst, res_val):
            ty = field(container, "ty")
            mutate_field_list = ty["finite"]["payload"]
            ty["finite"]["payload"] = [
                (k, wire_bool_ty() if k == "element" else val)
                for k, val in mutate_field_list
            ]

    add("tamper-result-element-type", scalar_args, scalar_fn, _wrong_result_elem)

    def _wrong_exact_length(fn: dict) -> None:
        inst = find_first_kind18(fn)
        res_id = field(inst, "result")["integer"]["value"]
        res_val = find_value_wire(fn, res_id)
        for container in (inst, res_val):
            ty = field(container, "ty")
            for k, val in ty["finite"]["payload"]:
                if k == "length":
                    val["integer"]["value"] = 4

    add("tamper-exact-bound", scalar_args, scalar_fn, _wrong_exact_length)

    view_case = prepared["seq-bytes-view"]
    view_fn = view_case["function_wires"][0]
    view_args = view_case["args"]

    def _generic_view_sentinel(fn: dict) -> None:
        inst = find_first_kind18(fn)
        res_id = field(inst, "result")["integer"]["value"]
        res_val = find_value_wire(fn, res_id)
        for container in (inst, res_val):
            ty = field(container, "ty")
            for k, val in ty["finite"]["payload"]:
                if k == "length":
                    val["integer"]["value"] = 18446744073709551614

    add("tamper-view-generic-sentinel", view_args, view_fn, _generic_view_sentinel)

    add("tamper-metadata-binding-start", scalar_args, scalar_fn,
        lambda fn: mutate_field(find_first_kind18(fn), "binding_start", lambda v: v["integer"].__setitem__("value", 1)))
    add("tamper-metadata-nominal-valid", scalar_args, scalar_fn,
        lambda fn: mutate_field(field(find_first_kind18(fn), "nominal_identity"), "valid", lambda v: v["boolean"].__setitem__("value", True)))

    empty_case = prepared["seq-empty-view"]
    empty_fn = empty_case["function_wires"][0]
    empty_args = empty_case["args"]
    sample_id_wire = dict(field(find_first_kind18(scalar_fn), "inputs")["finite"]["payload"])["head"]

    def _inject_empty_input(fn: dict) -> None:
        inst = find_first_kind18(fn)
        res_id = field(inst, "result")["integer"]["value"]
        injected_id = copy.deepcopy(sample_id_wire)
        mutate_field(injected_id, "value", lambda v: v["integer"].__setitem__("value", res_id))
        inputs = field(inst, "inputs")
        inputs["finite"] = {
            "discriminant": 1,
            "payload": [
                ("head", injected_id),
                ("tail", {"finite": {"discriminant": 0, "payload": [],
                                     "type_identity": inputs["finite"]["type_identity"],
                                     "variant_identity": "mncs:0.2:finite-variant:mncs.compiler.ssa.v1::IdList::INil"}}),
            ],
            "type_identity": inputs["finite"]["type_identity"],
            "variant_identity": "mncs:0.2:finite-variant:mncs.compiler.ssa.v1::IdList::ICons",
        }

    add("tamper-empty-extra-input", empty_args, empty_fn, _inject_empty_input)
    return variants


def test_decl_and_vm() -> dict:
    raw_bp = BACKEND_POLICY_PATH.read_bytes()
    bp_text = raw_bp.decode("utf-8")
    bp_args = paged_args(raw_bp)

    decl = Probe(
        DECL_MODULE, ["prove_unit", "local_nominal_type_identities"],
        backend="cranelift", backend_roots=["prove_unit", "local_nominal_type_identities"],
        timeout_seconds=300.0,
    )
    vm_prep_cases = []
    nominals_by_name = {}
    t_decl = time.monotonic()
    try:
        # 1. Stage-0 differential on small DECL VM cases
        for case_id, src in DECL_VM_CASES:
            args = paged_args(src.encode("utf-8"))
            ref_diags = [
                (d["code"], d["span"]["start"], d["span"]["end"])
                for d in decl.send({"elaborate": src}) if d.get("code", "").startswith("MNE")
            ]
            wire = call(decl, DECL_MODULE, "prove_unit", args)["returned"][0]
            got = decode(wire)
            native_diags = [
                (sem.KIND_TO_MNE[o["kind"]], o["start"], o["end"])
                for o in sem.flist(got["obls"], 1) if o["status"] == 1
            ]
            assert native_diags == ref_diags, (case_id, native_diags, ref_diags)
            assert got["ok"] == (not ref_diags), (case_id, got["ok"], ref_diags)
            vm_prep_cases.append((case_id, args, got))

        # 2. Small SSA cases nominal derivation
        for case_id, src, _ in SMALL_CASES:
            args = paged_args(src.encode("utf-8"))
            prove_wire = call(decl, DECL_MODULE, "prove_unit", args)["returned"][0]
            prove_dec = decode(prove_wire)
            assert prove_dec["ok"] is True, (case_id, prove_dec)
            unit_wire = field(prove_wire, "unit")
            nom_wire = call(decl, DECL_MODULE, "local_nominal_type_identities", [*args, unit_wire])["returned"][0]
            nominals_by_name[case_id] = (args, nom_wire)

        # 3. backend_policy.mncs real compiler self-ingestion proof + deterministic repeat
        bp_ref = [
            d for d in decl.send({"elaborate": bp_text}) if d.get("code", "").startswith("MNE")
        ]
        assert bp_ref == [], bp_ref
        t0 = time.monotonic()
        bp_prove1 = call(decl, DECL_MODULE, "prove_unit", bp_args)
        bp_decl_ms_1 = (time.monotonic() - t0) * 1000.0
        t0 = time.monotonic()
        bp_prove2 = call(decl, DECL_MODULE, "prove_unit", bp_args)
        bp_decl_ms_2 = (time.monotonic() - t0) * 1000.0
        assert bp_prove1["returned"] == bp_prove2["returned"], "non-deterministic backend_policy prove_unit"
        bp_dec = decode(bp_prove1["returned"][0])
        assert bp_dec["ok"] is True and bp_dec["fn_count"] == 7, bp_dec
        bp_obls = sem.flist(bp_dec["obls"], 1)
        assert all(o["status"] != 1 for o in bp_obls), bp_obls
        assert {o["kind"] for o in bp_obls if o["status"] == 2} <= sem.UNKNOWN_KINDS, bp_obls
        bp_nom = call(decl, DECL_MODULE, "local_nominal_type_identities", [*bp_args, field(bp_prove1["returned"][0], "unit")])["returned"][0]
        decl_artifact = decl.send({"emit_vm_artifact": {"module": DECL_MODULE}})["artifact"]
    finally:
        decl_stderr = decl.close() if not decl.stderr_file.closed else ""

    # Canonical VM check for decl.prove_unit
    vm_batch = [
        {"id": case_id, "function": "prove_unit", "args": args, "type_args": TYPE_ARGS, "step_budget": STEP_BUDGET}
        for case_id, args, _ in vm_prep_cases
    ]
    with tempfile.TemporaryDirectory(prefix="mncs-vm-decl-seqlit-") as tmp:
        art_path = Path(tmp) / "decl.json"
        art_path.write_text(json.dumps(decl_artifact, sort_keys=True) + "\n")
        vm_doc, vm_wall, vm_rss, _, _ = run_batch(art_path, vm_batch, Path(tmp), module=DECL_MODULE)
    for (case_id, _, cranelift_dec), vm_row in zip(vm_prep_cases, vm_doc["results"], strict=True):
        assert vm_row["outcome"] == {"kind": "completed"}, (case_id, vm_row)
        vm_dec = decode(vm_to_wire(vm_row["record"]["returned"][0]))
        assert vm_dec == cranelift_dec, (case_id, "Cranelift vs Canonical VM mismatch")

    return {
        "nominals_by_name": nominals_by_name,
        "bp_args": bp_args,
        "bp_nominal_wire": bp_nom,
        "bp_decl": {
            "ok": bp_dec["ok"],
            "fn_count": bp_dec["fn_count"],
            "obligations": len(bp_obls),
            "steps": bp_prove1["steps"],
            "call_ms": [round(bp_decl_ms_1, 3), round(bp_decl_ms_2, 3)],
            "elapsed_seconds": round(time.monotonic() - t_decl, 3),
        },
        "decl_vm": {
            "cases": len(vm_prep_cases),
            "vm_wall_seconds": round(vm_wall, 6),
            "vm_peak_rss_kb": vm_rss,
            "steps": {r["id"]: r["record"]["usage"]["steps"] for r in vm_doc["results"]},
            "agreement": True,
        },
        "decl_stderr": decl_stderr,
    }


def test_flow_step(nominals_by_name: dict, bp_args: list) -> dict:
    flow = Probe(FLOW_MODULE, ["lower_unit"], backend="cranelift", backend_roots=["lower_unit"], timeout_seconds=300.0)
    flows = {}
    try:
        for case_id, _, _ in SMALL_CASES:
            args, nom = nominals_by_name[case_id]
            flow_wire = call(flow, FLOW_MODULE, "lower_unit", args)["returned"][0]
            flow_dec = decode(flow_wire)
            assert flow_dec["valid"] and flow_dec["graph_ok"] and flow_dec["proof"]["ok"], (case_id, flow_dec)
            flows[case_id] = (args, flow_wire, nom)
        t0 = time.monotonic()
        bp_flow = call(flow, FLOW_MODULE, "lower_unit", bp_args)
        bp_flow_ms = (time.monotonic() - t0) * 1000.0
        bp_flow_dec = decode(bp_flow["returned"][0])
        assert bp_flow_dec["valid"] and bp_flow_dec["graph_ok"] and bp_flow_dec["proof"]["ok"], bp_flow_dec
    finally:
        flow_stderr = flow.close() if not flow.stderr_file.closed else ""
    return {
        "small_flows": flows,
        "bp_flow_wire": bp_flow["returned"][0],
        "bp_flow_ms": round(bp_flow_ms, 3),
        "bp_flow_steps": bp_flow["steps"],
        "flow_stderr": flow_stderr,
    }


def test_ssa_and_vm(flow_out: dict, bp_nominal_wire: dict, bp_args: list) -> dict:
    ssa = Probe(
        SSA_MODULE, ["lower_value_ssa", "verify_function"],
        backend="cranelift", backend_roots=["lower_value_ssa", "verify_function"],
        timeout_seconds=300.0,
    )
    prepared = {}
    oracle_total = 0
    native_total = 0
    try:
        # 1. Small SSA cases differential & per-function verify_function
        for case_id, src, expected_seq_count in SMALL_CASES:
            args, flow_wire, nom_wire = flow_out["small_flows"][case_id]
            oracle = ssa.send({"ssa": src})
            assert oracle.get("ssa") is not None and not oracle.get("diagnostics"), (case_id, oracle)
            stage0_seq = sum(
                1
                for fn in oracle["ssa"]["functions"]
                for blk in fn["blocks"]
                for ins in blk["instructions"]
                if "SequenceConstruct" in ins["kind"]
            )
            oracle_total += stage0_seq
            assert stage0_seq == expected_seq_count, (case_id, stage0_seq, expected_seq_count)

            res1 = call(ssa, SSA_MODULE, "lower_value_ssa", [*args, flow_wire, nom_wire])
            res2 = call(ssa, SSA_MODULE, "lower_value_ssa", [*args, flow_wire, nom_wire])
            assert res1["returned"] == res2["returned"], (case_id, "non-deterministic SSA")
            native_wire = res1["returned"][0]
            native = decode(native_wire)
            assert native["valid"] is True, (case_id, native)
            assert native["function_count"] == native["supported_function_count"] == native["verified_function_count"] == len(oracle["ssa"]["functions"]), (case_id, native)

            fn_wires = wire_linked(field(native_wire, "functions"))
            seq_count = 0
            for fn_w in fn_wires:
                ver = call(ssa, SSA_MODULE, "verify_function", [*args, fn_w])
                assert ver["returned"] == [{"boolean": {"value": True}}], (case_id, ver)
                for blk_w in wire_linked(field(fn_w, "blocks")):
                    for ins_w in wire_linked(field(blk_w, "instructions")):
                        if field(ins_w, "kind")["integer"]["value"] == 18:
                            seq_count += 1
            assert seq_count == stage0_seq, (case_id, seq_count, stage0_seq)
            native_total += seq_count
            prepared[case_id] = {
                "args": args,
                "flow_wire": flow_wire,
                "nominal_wire": nom_wire,
                "native_wire": native_wire,
                "native_summary": native,
                "function_wires": fn_wires,
                "seq_count": seq_count,
            }

        # 2. Anti-tamper negative tests on Cranelift
        tampers = make_tamper_variants(prepared)
        for label, t_args, t_fn in tampers:
            ver = call(ssa, SSA_MODULE, "verify_function", [*t_args, t_fn])
            assert ver["returned"] == [{"boolean": {"value": False}}], (label, ver)

        # 3. backend_policy.mncs SSA differential & per-function verify + tamper
        bp_text = BACKEND_POLICY_PATH.read_text()
        bp_oracle_ast = ssa.send({"oracle": bp_text})
        bp_oracle_ssa = ssa.send({"ssa": bp_text})
        assert bp_oracle_ssa.get("ssa") is not None and not bp_oracle_ssa.get("diagnostics"), bp_oracle_ssa
        bp_stage0_per_fn = [
            sum(1 for blk in fn["blocks"] for ins in blk["instructions"] if "SequenceConstruct" in ins["kind"])
            for fn in bp_oracle_ssa["ssa"]["functions"]
        ]
        bp_fn_names = [f["name"]["text"] for f in bp_oracle_ast["ast"]["functions"]]

        t0 = time.monotonic()
        bp_ssa1 = call(ssa, SSA_MODULE, "lower_value_ssa", [*bp_args, flow_out["bp_flow_wire"], bp_nominal_wire])
        bp_ssa_ms_1 = (time.monotonic() - t0) * 1000.0
        t0 = time.monotonic()
        bp_ssa2 = call(ssa, SSA_MODULE, "lower_value_ssa", [*bp_args, flow_out["bp_flow_wire"], bp_nominal_wire])
        bp_ssa_ms_2 = (time.monotonic() - t0) * 1000.0
        assert bp_ssa1["returned"] == bp_ssa2["returned"], "non-deterministic backend_policy SSA"
        bp_ssa_wire = bp_ssa1["returned"][0]
        bp_ssa_dec = decode(bp_ssa_wire)
        assert bp_ssa_dec["valid"] is True, bp_ssa_dec
        assert bp_ssa_dec["function_count"] == bp_ssa_dec["supported_function_count"] == bp_ssa_dec["verified_function_count"] == 7, bp_ssa_dec
        assert bp_ssa_dec["unsupported_block_count"] == 0, bp_ssa_dec

        bp_fn_wires = wire_linked(field(bp_ssa_wire, "functions"))
        bp_native_per_fn = {}
        for name, fn_w in zip(bp_fn_names, bp_fn_wires, strict=True):
            ident = decode(field(fn_w, "identity"))
            fn_ident_name = bytes(ident["bytes"][:ident["len"]]).decode("utf-8").rsplit("::", 1)[-1]
            assert fn_ident_name == name, (fn_ident_name, name)
            ver = call(ssa, SSA_MODULE, "verify_function", [*bp_args, fn_w])
            assert ver["returned"] == [{"boolean": {"value": True}}], (name, ver)
            bp_native_per_fn[name] = sum(
                1
                for blk in wire_linked(field(fn_w, "blocks"))
                for ins in wire_linked(field(blk, "instructions"))
                if field(ins, "kind")["integer"]["value"] == 18
            )
        # Stage-0 emits lowered SSA functions sorted by function name, whereas
        # native lower_value_ssa preserves AST declaration order.
        assert [bp_native_per_fn[n] for n in sorted(bp_fn_names)] == bp_stage0_per_fn, (
            bp_native_per_fn, bp_stage0_per_fn,
        )
        assert sum(bp_stage0_per_fn) == 39, bp_native_per_fn

        # Corrupt one kind-18 instruction in backend_policy.mncs (vm_driver_for_id) and confirm rejection.
        bp_tampered = copy.deepcopy(bp_fn_wires[1])
        mutate_field(find_first_kind18(bp_tampered), "operator", lambda v: v["integer"].__setitem__("value", 99))
        bp_rejected = call(ssa, SSA_MODULE, "verify_function", [*bp_args, bp_tampered])
        assert bp_rejected["returned"] == [{"boolean": {"value": False}}], bp_rejected

        ssa_artifact = ssa.send({"emit_vm_artifact": {"module": SSA_MODULE}})["artifact"]
    finally:
        ssa_stderr = ssa.close() if not ssa.stderr_file.closed else ""

    # 4. Canonical VM parity for lower_value_ssa + verify_function (positive & tamper)
    vm_cases = []
    for case_id, _, _ in SMALL_CASES:
        item = prepared[case_id]
        vm_cases.append({
            "id": f"lower:{case_id}",
            "function": "lower_value_ssa",
            "args": [*item["args"], item["flow_wire"], item["nominal_wire"]],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        })
    vm_cases.append({
        "id": "verify:seq-record-call-ok",
        "function": "verify_function",
        "args": [*prepared["seq-record-call-elements"]["args"], prepared["seq-record-call-elements"]["function_wires"][1]],
        "type_args": TYPE_ARGS,
        "step_budget": STEP_BUDGET,
    })
    for label, t_args, t_fn in tampers:
        vm_cases.append({
            "id": f"reject:{label}",
            "function": "verify_function",
            "args": [*t_args, t_fn],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        })

    with tempfile.TemporaryDirectory(prefix="mncs-vm-ssa-seqlit-") as tmp:
        art_path = Path(tmp) / "ssa.json"
        art_path.write_text(json.dumps(ssa_artifact, sort_keys=True) + "\n")
        vm_doc, vm_wall, vm_rss, _, _ = run_batch(art_path, vm_cases, Path(tmp), module=SSA_MODULE)

    vm_res = {row["id"]: row for row in vm_doc["results"]}
    for case_id, _, _ in SMALL_CASES:
        row = vm_res[f"lower:{case_id}"]
        assert row["outcome"] == {"kind": "completed"}, (case_id, row)
        assert decode(vm_to_wire(row["record"]["returned"][0])) == prepared[case_id]["native_summary"], case_id
    assert vm_res["verify:seq-record-call-ok"]["outcome"] == {"kind": "completed"}
    assert decode(vm_to_wire(vm_res["verify:seq-record-call-ok"]["record"]["returned"][0])) is True
    for label, _, _ in tampers:
        row = vm_res[f"reject:{label}"]
        assert row["outcome"] == {"kind": "completed"}, (label, row)
        assert decode(vm_to_wire(row["record"]["returned"][0])) is False, label

    return {
        "small_cases": len(SMALL_CASES),
        "small_stage0_seq_constructs": oracle_total,
        "small_native_seq_constructs": native_total,
        "tamper_rejections": [label for label, _, _ in tampers],
        "backend_policy": {
            "valid": bp_ssa_dec["valid"],
            "functions": bp_ssa_dec["verified_function_count"],
            "instructions": bp_ssa_dec["instruction_count"],
            "blocks": bp_ssa_dec["block_count"],
            "per_function_seq_constructs": bp_native_per_fn,
            "total_seq_constructs": sum(bp_native_per_fn.values()),
            "tamper_rejected": True,
            "steps": bp_ssa1["steps"],
            "call_ms": [round(bp_ssa_ms_1, 3), round(bp_ssa_ms_2, 3)],
        },
        "ssa_vm": {
            "artifact_id": ssa_artifact["artifact_id"],
            "batch_cases": len(vm_cases),
            "vm_wall_seconds": round(vm_wall, 6),
            "vm_peak_rss_kb": vm_rss,
            "agreement": True,
        },
        "ssa_stderr": ssa_stderr,
    }


def test_project_pipeline() -> dict:
    # Enforce single-session Cranelift project execution using the seeded compile_project entry.
    os.environ["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa,project"
    os.environ["MNCS_PROBE_EXECUTION_MODULES"] = f"{project.PROJECT_MODULE},{project.SSA_MODULE}"
    os.environ["MNCS_PROBE_BACKEND"] = project.backend_policy.resolve("project")
    t0 = time.monotonic()
    probe = project.Probe()
    try:
        identities = project.identity_map(probe)

        # 1. Multi-module imported record + SequenceLiteral + view
        sources = [
            ("a-dep.mncs", Path("a-dep.mncs"), DEP_MODULE_SRC.encode("utf-8")),
            ("b-root.mncs", Path("b-root.mncs"), ROOT_MODULE_SRC.encode("utf-8")),
        ]
        t_pos = time.monotonic()
        pos_resp = probe.send(project.request_value(identities, sources))
        pos_ms = (time.monotonic() - t_pos) * 1000.0
        assert pos_resp.get("status") == "returned", pos_resp
        pos_native = project.decode(pos_resp["returned"][0])
        pos_oracle = probe.send({"project_oracle": {
            "root": ROOT_MODULE_SRC,
            "modules": {"demo.seqdep": DEP_MODULE_SRC},
        }})
        assert pos_native["valid"] is True and pos_native["value_ssa_valid"] is True, pos_native["diagnostics"]
        assert pos_oracle["valid"] is True, pos_oracle["diagnostics"]
        root_mod = next(m for m in project.flist(pos_native["modules"]) if m["source_index"] == 1)
        assert root_mod["flow"]["proof"]["ok"] is True
        assert root_mod["value_ssa"]["verified_function_count"] == 2
        root_seq_count = sum(
            1
            for fn in project.flist(root_mod["value_ssa"]["functions"])
            for blk in project.flist(fn["blocks"])
            for ins in project.flist(blk["instructions"])
            if ins["kind"] == 18
        )
        assert root_seq_count == 2, root_seq_count

        # 2. Negative multi-module SequenceLiteral length mismatch (MNE184 -> kind 102)
        bad_sources = [
            ("a-dep.mncs", Path("a-dep.mncs"), DEP_MODULE_SRC.encode("utf-8")),
            ("b-root.mncs", Path("b-root.mncs"), BAD_ROOT_MODULE_SRC.encode("utf-8")),
        ]
        bad_resp = probe.send(project.request_value(identities, bad_sources))
        assert bad_resp.get("status") == "returned", bad_resp
        bad_native = project.decode(bad_resp["returned"][0])
        bad_oracle = probe.send({"project_oracle": {
            "root": BAD_ROOT_MODULE_SRC,
            "modules": {"demo.seqdep": DEP_MODULE_SRC},
        }})
        assert bad_native["valid"] is False and bad_oracle["valid"] is False
        bad_proof = next(m for m in project.flist(bad_native["modules"]) if m["source_index"] == 1)["flow"]["proof"]
        bad_lit_start = BAD_ROOT_MODULE_SRC.index("[dep.mk(x, 65)]")
        bad_lit_end = bad_lit_start + len("[dep.mk(x, 65)]")
        assert (bad_proof["err_start"], bad_proof["err_end"]) == (bad_lit_start, bad_lit_end), bad_proof
        assert any(
            d.get("code") == "MNE184"
            and (d.get("span", {}).get("start"), d.get("span", {}).get("end")) == (bad_lit_start, bad_lit_end)
            for d in bad_oracle["diagnostics"]
        ), bad_oracle["diagnostics"]

        # 3. Real src/compiler/backend_policy.mncs via project.compile_project
        bp_raw = BACKEND_POLICY_PATH.read_bytes()
        bp_sources = [("compiler/backend_policy.mncs", Path("compiler/backend_policy.mncs"), bp_raw)]
        t_bp1 = time.monotonic()
        bp_resp1 = probe.send(project.request_value(identities, bp_sources))
        bp_proj_ms_1 = (time.monotonic() - t_bp1) * 1000.0
        t_bp2 = time.monotonic()
        bp_resp2 = probe.send(project.request_value(identities, bp_sources))
        bp_proj_ms_2 = (time.monotonic() - t_bp2) * 1000.0
        assert bp_resp1.get("status") == "returned" and bp_resp1["returned"] == bp_resp2["returned"], bp_resp1
        bp_native = project.decode(bp_resp1["returned"][0])
        bp_oracle = probe.send({"project_oracle": {"root": bp_raw.decode("utf-8"), "modules": {}}})
        assert bp_native["valid"] is True and bp_native["value_ssa_valid"] is True, bp_native
        assert bp_oracle["valid"] is True, bp_oracle["diagnostics"]
        bp_mod = project.flist(bp_native["modules"])[0]
        assert bp_mod["value_ssa"]["verified_function_count"] == 7
        bp_seq_count = sum(
            1
            for fn in project.flist(bp_mod["value_ssa"]["functions"])
            for blk in project.flist(fn["blocks"])
            for ins in project.flist(blk["instructions"])
            if ins["kind"] == 18
        )
        assert bp_seq_count == 39, bp_seq_count
    finally:
        probe_wall_s = time.monotonic() - t0
        probe.close()

    return {
        "project_probe_wall_seconds": round(probe_wall_s, 3),
        "multi_module_verified_functions": root_mod["value_ssa"]["verified_function_count"],
        "multi_module_kind18_count": root_seq_count,
        "multi_module_call_ms": round(pos_ms, 3),
        "negative_multi_module_mne184_matched": True,
        "backend_policy_project": {
            "valid": bp_native["valid"],
            "value_ssa_valid": bp_native["value_ssa_valid"],
            "verified_functions": bp_mod["value_ssa"]["verified_function_count"],
            "kind18_count": bp_seq_count,
            "steps": bp_resp1["steps"],
            "call_ms": [round(bp_proj_ms_1, 3), round(bp_proj_ms_2, 3)],
        },
    }


def main() -> dict:
    started = time.monotonic()
    decl_res = test_decl_and_vm()
    flow_res = test_flow_step(decl_res["nominals_by_name"], decl_res["bp_args"])
    ssa_res = test_ssa_and_vm(flow_res, decl_res["bp_nominal_wire"], decl_res["bp_args"])
    project_res = test_project_pipeline()
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return {
        "schema_version": 1,
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "decl": decl_res["bp_decl"],
        "decl_vm": decl_res["decl_vm"],
        "flow": {"bp_ms": flow_res["bp_flow_ms"], "bp_steps": flow_res["bp_flow_steps"]},
        "ssa": ssa_res,
        "project": project_res,
        "max_child_rss_kib": usage.ru_maxrss,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


if __name__ == "__main__":
    os.chdir(ROOT)
    print(json.dumps(main(), indent=2))
