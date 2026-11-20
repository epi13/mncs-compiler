#!/usr/bin/env python3
"""Focused verification and tamper-rejection suite for scoped `if`-join
environment projection and the raised 8,192-value SSA verifier envelope in
`mncs.compiler.ssa.v1`.

Proves:
1. Nested `if` without `else` containing branch-local `let` bindings falling
   through to outer join blocks (the `lexer.mncs::keyword_global` pattern)
   lowers and verifies cleanly, projecting join-edge environments back to the
   outer scope.
2. `if { let ... } else { let ... }` with distinct branch-local `let` bindings
   lowers and verifies cleanly.
3. Shadowing an outer binding inside an `if` branch (both same-type and
   different-type shadows) preserves the outer binding on the join edge via
   exact declaration-span projection rather than leaking the shadowed inner
   binding.
4. High-value-count functions exceeding the prior 2,048-value ceiling
   (`2048 < value_count <= 8192`, the `lexer.mncs::next_token_global` verifier
   rule) lower and verify cleanly.
5. Tamper cases fail closed in `verify_function_with_flow`:
   - appending an extra inner-branch argument onto an `if`-join Jump edge
   - substituting a shadowed inner `let` value of the wrong type on the join edge
   - inflating `value_count` above the 8,192 envelope
   - corrupting a value entry with ID > 2048
"""

import argparse
import copy
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import test_sem as sem
from test_ssa_post_match_loop_advance import (
    SingleModuleProbe,
    _ident_str,
    _paged_args,
    _set_wire_field,
    _walk_wire_flist,
    _wire_field,
)

sys.setrecursionlimit(max(sys.getrecursionlimit(), 50000))


SCOPED_IF_JOIN_SRC = """\
mncs 0.18;
module demo.scoped_if_join;

fn nested_if_fallthrough(a: u64, b: u64, c: u64) -> (result: u64) {
    let base: u64 = a +% b;
    if a > 0 {
        let step1: u64 = base +% 1;
        if b > 0 {
            let step2: u64 = step1 +% 2;
            if c > 0 {
                if step2 == 42 {
                    return step2;
                }
            }
        }
    }
    if base == 10 {
        let t0: u64 = a +% 1;
        let t1: u64 = b +% 2;
        if t0 == t1 { return 100; }
    }
    if base == 20 {
        let u0: u64 = a +% 3;
        let u1: u64 = b +% 4;
        if u0 == u1 { return 200; }
    }
    return base;
}

fn if_else_distinct_lets(flag: bool, x: u64) -> (result: u64) {
    let outer: u64 = x +% 5;
    if flag {
        let left_only: u64 = outer +% 1;
        if left_only == 99 { return left_only; }
    } else {
        let right_a: bool = x > 10;
        let right_b: u64 = select(right_a, 7, 8);
        if right_b == 77 { return right_b; }
    }
    return outer;
}

fn shadow_same_and_diff_type(cond: bool, flag: bool, base: u64) -> (result: u64) {
    let x: u64 = base +% 1;
    if cond {
        let x: u64 = base +% 99;
        let y: u64 = x +% 1;
        if y == 0 { return y; }
    }
    if flag {
        let x: bool = cond;
        if x && !cond { return 7; }
    }
    return x;
}
"""


def _build_large_value_source(local_count: int = 24, branch_count: int = 27) -> str:
    """Build a valid MNCS function whose verified SSA exceeds 2,048 values
    within ~120 CFG blocks (mirroring `lexer.mncs::next_token_global`)."""
    lines = [
        "mncs 0.18;",
        "module demo.large_value_envelope;",
        "",
        "fn many_values(x: u64, y: u64, z: u64) -> (result: u64) {",
        "    let a0: u64 = x +% 1;",
        "    let a1: u64 = y +% 2;",
        "    let a2: u64 = z +% 3;",
    ]
    for i in range(3, local_count):
        lines.append(f"    let a{i}: u64 = a{i - 1} +% a{i - 2};")
    for idx in range(branch_count):
        lines.append(f"    if x == {idx} {{ return a{idx % local_count}; }}")
    lines.append(f"    return a{local_count - 1};")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _flist(val, cons=1):
    items = []
    while val["$v"] == cons:
        items.append(val["$p"]["head"])
        val = val["$p"]["tail"]
    return items


def _wire_flist_len(node) -> int:
    count = 0
    for _ in _walk_wire_flist(node):
        count += 1
    return count


def _find_join_jump_block(fn_wire, target_block_id):
    for blk in _walk_wire_flist(_wire_field(fn_wire, "blocks")):
        term = _wire_field(blk, "terminator")["finite"]
        if term["discriminant"] == 1:  # Jump
            payload = {k: v for k, v in term["payload"]}
            if payload["target"]["integer"]["value"] == target_block_id:
                return blk, payload
    return None, None


def _id_list_duplicate_head(id_list_wire):
    dup = copy.deepcopy(id_list_wire)
    for idx, (k, _) in enumerate(dup["finite"]["payload"]):
        if k == "tail":
            dup["finite"]["payload"][idx] = [k, id_list_wire]
            return dup
    raise AssertionError("expected non-empty IdList")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    started = time.monotonic()
    scoped_bytes = SCOPED_IF_JOIN_SRC.encode("utf-8")
    scoped_args = _paged_args(scoped_bytes)

    large_src = _build_large_value_source()
    large_bytes = large_src.encode("utf-8")
    large_args = _paged_args(large_bytes)

    # Fetch Flow CFGs sequentially before spawning the SSA probe.
    flow_probe = SingleModuleProbe(
        "source,lexer,parser,segment,decl,flow",
        "mncs.compiler.flow.v1",
        ["lower_unit"],
    )
    flow_ok = False
    try:
        scoped_flow_wire = flow_probe.call_raw("lower_unit", scoped_args)
        large_flow_wire = flow_probe.call_raw("lower_unit", large_args)
        flow_ok = True
    finally:
        flow_probe.close(check=flow_ok)

    probe = SingleModuleProbe(
        "source,lexer,parser,segment,decl,flow,ssa",
        "mncs.compiler.ssa.v1",
        ["lower_flow_unit_value_ssa", "verify_function_with_flow"],
    )
    ssa_ok = False
    try:
        # 1-3: Scoped if-join fixtures
        scoped_ssa_wire = probe.call_raw("lower_flow_unit_value_ssa", scoped_args)
        scoped_ssa = sem.decode(scoped_ssa_wire)
        assert scoped_ssa["valid"] is True, scoped_ssa
        assert scoped_ssa["function_count"] == 3, scoped_ssa
        assert scoped_ssa["supported_function_count"] == 3, scoped_ssa
        assert scoped_ssa["verified_function_count"] == 3, scoped_ssa

        scoped_flow_fns = list(_walk_wire_flist(_wire_field(scoped_flow_wire, "functions")))
        scoped_ssa_fns = list(_walk_wire_flist(_wire_field(scoped_ssa_wire, "functions")))
        scoped_fn_reports = []
        for idx, (ssa_fn_w, flow_fn_w) in enumerate(zip(scoped_ssa_fns, scoped_flow_fns)):
            ver = sem.decode(
                probe.call_raw("verify_function_with_flow", scoped_args + [ssa_fn_w, flow_fn_w])
            )
            assert ver is True, f"function {idx} failed verify_function_with_flow"
            ssa_fn_dec = sem.decode(ssa_fn_w)
            scoped_fn_reports.append({
                "index": idx,
                "identity": _ident_str(ssa_fn_dec["identity"]),
                "block_count": ssa_fn_dec["block_count"],
                "value_count": ssa_fn_dec["value_count"],
                "verified": ssa_fn_dec["verified"],
            })

        # Verify shadowing function passes the outer `x` (u64) on the join edge,
        # not the inner `x: bool` or shadowed `x: u64`.
        shadow_fn_wire = scoped_ssa_fns[2]
        shadow_flow_wire = scoped_flow_fns[2]
        shadow_fn_dec = sem.decode(shadow_fn_wire)
        # Entry block 0 branches to yes=1, no=2, join=3 (first `if cond`);
        # block 3 branches to yes=4, no=5, join=6 (second `if flag`).
        # Confirm join block 3 has exactly 4 parameters (cond, flag, base, outer x),
        # and join block 6 has exactly 4 parameters (not inner `x: bool`!).
        blocks_by_id = {b["id"]: b for b in _flist(shadow_fn_dec["blocks"])}
        # First outer `if cond` is in block 0 -> join1 = no + 1;
        # second outer `if flag` is in join1 -> join2 = no + 1.
        join1_id = blocks_by_id[0]["terminator"]["$p"]["no"] + 1
        else1_id = blocks_by_id[0]["terminator"]["$p"]["no"]
        join2_id = blocks_by_id[join1_id]["terminator"]["$p"]["no"] + 1
        assert len(_flist(blocks_by_id[join1_id]["parameters"])) == 4, blocks_by_id[join1_id]
        assert len(_flist(blocks_by_id[join2_id]["parameters"])) == 4, blocks_by_id[join2_id]
        # In block 1 (inside `if cond`), inner `let x: u64 = base +% 99` defines an
        # instruction result; check that the `then`-branch Jump to join1 does NOT
        # pass any instruction defined inside block 1 (it passes outer `x`!).
        b1_inst_results = {inst["result"] for inst in _flist(blocks_by_id[1]["instructions"])}
        then_to_join1_args = None
        for b in blocks_by_id.values():
            if b["id"] == else1_id:
                continue
            term = b["terminator"]
            if term["$v"] == 1 and term["$p"]["target"] == join1_id:
                then_to_join1_args = [a["value"] for a in _flist(term["$p"]["arguments"])]
                break
        assert then_to_join1_args is not None and len(then_to_join1_args) == 4
        assert not (set(then_to_join1_args) & b1_inst_results), (
            f"shadowed inner `x` leaked onto join-{join1_id} edge: {then_to_join1_args} vs {b1_inst_results}"
        )

        # Tamper 1: Append an extra argument onto a Jump targeting join1
        tamper_extra_arg = copy.deepcopy(shadow_fn_wire)
        for blk in _walk_wire_flist(_wire_field(tamper_extra_arg, "blocks")):
            term = _wire_field(blk, "terminator")["finite"]
            if term["discriminant"] == 1:
                payload = {k: v for k, v in term["payload"]}
                if payload["target"]["integer"]["value"] == join1_id:
                    for idx_p, (k, v) in enumerate(term["payload"]):
                        if k == "arguments":
                            term["payload"][idx_p] = [k, _id_list_duplicate_head(v)]
                            break
                    break
        t1_ok = sem.decode(
            probe.call_raw("verify_function_with_flow", scoped_args + [tamper_extra_arg, shadow_flow_wire])
        )
        assert t1_ok is False, "tamper_extra_arg was unexpectedly accepted"

        # Tamper 2: Replace arguments on Jump -> join2 with `cond: bool` (id 0), breaking u64 param types
        tamper_wrong_type = copy.deepcopy(shadow_fn_wire)
        for blk in _walk_wire_flist(_wire_field(tamper_wrong_type, "blocks")):
            term = _wire_field(blk, "terminator")["finite"]
            if term["discriminant"] == 1:
                payload = {k: v for k, v in term["payload"]}
                if payload["target"]["integer"]["value"] == join2_id:
                    cur = payload["arguments"]
                    while cur["finite"]["discriminant"] == 1:
                        p = {k: v for k, v in cur["finite"]["payload"]}
                        _set_wire_field(p["head"], "value", sem.integer(0))
                        cur = p["tail"]
                    break
        t2_ok = sem.decode(
            probe.call_raw("verify_function_with_flow", scoped_args + [tamper_wrong_type, shadow_flow_wire])
        )
        assert t2_ok is False, "tamper_wrong_type was unexpectedly accepted"

        # 4: High value-count envelope (> 2048 values, <= 8192 values)
        large_ssa_wire = probe.call_raw("lower_flow_unit_value_ssa", large_args)
        large_ssa = sem.decode(large_ssa_wire)
        assert large_ssa["valid"] is True, large_ssa
        assert large_ssa["verified_function_count"] == 1, large_ssa
        assert large_ssa["value_count"] > 2048, f"expected > 2048 values, got {large_ssa['value_count']}"
        assert large_ssa["value_count"] <= 8192, large_ssa["value_count"]

        large_flow_fn = next(_walk_wire_flist(_wire_field(large_flow_wire, "functions")))
        large_ssa_fn = next(_walk_wire_flist(_wire_field(large_ssa_wire, "functions")))
        large_pos_ver = sem.decode(
            probe.call_raw("verify_function_with_flow", large_args + [large_ssa_fn, large_flow_fn])
        )
        assert large_pos_ver is True, "large value-count function failed verify_function_with_flow"

        # Tamper 3: Inflate value_count above the 8192 ceiling (in-place, then restore)
        orig_value_count = _wire_field(large_ssa_fn, "value_count")
        _set_wire_field(large_ssa_fn, "value_count", sem.integer(8193))
        t3_ok = sem.decode(
            probe.call_raw("verify_function_with_flow", large_args + [large_ssa_fn, large_flow_fn])
        )
        _set_wire_field(large_ssa_fn, "value_count", orig_value_count)
        assert t3_ok is False, "tamper_above_8192 was unexpectedly accepted"

        # Tamper 4: Corrupt a high-ID value (> 2048) at the head of function.values
        head_val = {k: v for k, v in _wire_field(large_ssa_fn, "values")["finite"]["payload"]}["head"]
        head_id = _wire_field(head_val, "id")["integer"]["value"]
        assert head_id > 2048, f"expected head value id > 2048, got {head_id}"
        orig_owner = _wire_field(head_val, "owner_block")
        _set_wire_field(head_val, "owner_block", sem.integer(999))
        t4_ok = sem.decode(
            probe.call_raw("verify_function_with_flow", large_args + [large_ssa_fn, large_flow_fn])
        )
        _set_wire_field(head_val, "owner_block", orig_owner)
        assert t4_ok is False, "tamper_high_id_value was unexpectedly accepted"

        ssa_ok = True
    finally:
        probe.close(check=ssa_ok)

    report = {
        "schema_version": 1,
        "kind": "ssa-scoped-if-join-and-value-envelope-regression",
        "elapsed_s": round(time.monotonic() - started, 3),
        "scoped_if_join_functions": scoped_fn_reports,
        "shadow_outer_binding_preserved": True,
        "large_value_envelope": {
            "block_count": large_ssa["block_count"],
            "value_count": large_ssa["value_count"],
            "instruction_count": large_ssa["instruction_count"],
            "verified": large_pos_ver,
        },
        "tamper_rejections": {
            "extra_if_join_argument_rejected": not t1_ok,
            "wrong_type_shadow_on_if_join_rejected": not t2_ok,
            "value_count_above_8192_rejected": not t3_ok,
            "corrupted_high_id_value_rejected": not t4_ok,
        },
        "all_passed": True,
    }
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
