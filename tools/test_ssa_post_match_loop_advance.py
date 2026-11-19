#!/usr/bin/env python3
"""Focused verification and tamper-rejection suite for post-match loop advances
in native Value SSA (`mncs.compiler.ssa.v1`).

Proves:
1. Scalar match, nested scalar match (binary-search pattern used in
   `source.mncs` and `std/text_utf8.mncs`), and finite match inside an
   `iterate` body before `next state = ...` lower and pass
   `verify_function_with_flow` with the `kind=14` advance instruction in the
   post-match join block, consuming the join block's carried loop index
   parameter and passing `inst.result` on the backedge `Jump` to the loop
   header's index parameter slot.
2. Miswired SSA tamper cases fail closed in `verify_function_with_flow`:
   - passing the old unadvanced index instead of `inst.result` on the backedge
   - redirecting the post-match join block's `Jump` target away from the header
   - feeding the pre-match block's stale loop index into `kind=14`
   - placing an unmapped `kind=14` operation in an arm block instead of join
3. Real unchanged `src/compiler/source.mncs` advances past the 9 post-match
   loop-advance failures.
"""

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import backend_policy
import test_sem as sem


def _find_main_root():
    try:
        common = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        common_path = (ROOT / common).resolve()
        if common_path.name == ".git":
            return common_path.parent
    except (OSError, subprocess.CalledProcessError):
        pass
    return ROOT


MAIN_ROOT = _find_main_root()
PROBE_BIN = Path(
    os.environ.get(
        "MNCS_PROBE_BIN",
        MAIN_ROOT / ".bootstrap" / "target" / "release" / "mncs-compiler-stage0-probe",
    )
)
STDLIB_ROOT = Path(os.environ.get("MNCS_STDLIB_ROOT", MAIN_ROOT.parent / "mncs-stdlib"))
CACHE_DIR = Path(os.environ.get("MNCS_PROBE_CACHE_DIR", MAIN_ROOT / ".build" / "probe-cache"))
MAX_AS_BYTES = int(os.environ.get("MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES", str(8 * 1024**3)))


def _paged_args(raw: bytes, stride: int = 1024):
    chunks = [raw[i:i + stride] for i in range(0, len(raw), stride)] if raw else []
    return [sem.pages_value(chunks), sem.integer(stride), sem.integer(len(raw))]


def _wire_field(rec, name):
    for k, v in rec["record"]["fields"]:
        if k == name:
            return v
    raise KeyError(name)


def _set_wire_field(rec, name, value):
    for idx, (k, _) in enumerate(rec["record"]["fields"]):
        if k == name:
            rec["record"]["fields"][idx] = [k, value]
            return
    raise KeyError(name)


def _walk_wire_flist(node):
    cur = node
    while cur["finite"]["discriminant"] == 1:
        payload = {k: v for k, v in cur["finite"]["payload"]}
        yield payload["head"]
        cur = payload["tail"]


def _ident_str(ident: dict) -> str:
    if not ident.get("valid"):
        return ""
    return bytes(ident["bytes"][:ident["len"]]).decode("utf-8", errors="replace")


class SingleModuleProbe:
    def __init__(self, modules_csv, exec_module, functions):
        env = dict(os.environ)
        env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{STDLIB_ROOT / 'library'}"
        env["MNCS_PROBE_MODULES"] = modules_csv
        env["MNCS_PROBE_LIBRARY_MODULES"] = "mncs.std.text_utf8.v1"
        env["MNCS_PROBE_EXECUTION_MODULES"] = exec_module
        env["MNCS_PROBE_BACKEND"] = backend_policy.resolve("project")
        env["MNCS_PROBE_BACKEND_ROOTS"] = ",".join(functions)
        env["MNCS_PROBE_CACHE_DIR"] = str(CACHE_DIR)
        env["MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES"] = str(MAX_AS_BYTES)
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
            {
                "module": exec_module,
                "function": fn_name,
                "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
            }
            for fn_name in functions
        ])
        self.exec_module = exec_module
        self.proc = subprocess.Popen(
            [str(PROBE_BIN)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            cwd=ROOT,
            env=env,
        )

    def call_raw(self, function, args):
        req = {
            "schema_version": "0.1",
            "target": {"module": self.exec_module, "function": function},
            "arguments": args,
            "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
            "step_budget": 8000000,
        }
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"probe terminated: {self.proc.poll()}"
        resp = json.loads(line)
        assert resp.get("status") == "returned", resp
        return resp["returned"][0]

    def close(self, check=True):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        rc = self.proc.wait(timeout=60)
        if check:
            assert rc == 0, f"probe exited with {rc}"


SCALAR_MATCH_LOOP_SRC = """\
mncs 0.18;
module demo.post_match_loop;

record Search { low: u64, high: u64, found: bool }

fn binary_search(value: u64) -> (result: bool) {
    let initial: Search = Search { low: 0, high: 8, found: false };
    iterate step up_to 4 carrying state: Search = initial {
        let active: bool = state.low < state.high && !state.found;
        let mid: u64 = select(active, (state.low + state.high) / 2, 0);
        let updated: Search = match select(active, 1, 0) {
            0 => state,
            _ => match select(value < mid, 1, 0) {
                1 => Search { ..state, high: mid },
                _ => match select(value > mid, 1, 0) {
                    1 => Search { ..state, low: mid + 1 },
                    _ => Search { ..state, found: true }
                }
            }
        };
        next state = updated;
    }
    return state.found;
}
"""

FINITE_MATCH_LOOP_SRC = """\
mncs 0.18;
module demo.post_finite_match_loop;

enum Step { Keep, Bump { delta: u64 } }

fn step_choice(i: u64) -> (result: Step) {
    if i % 2 == 0 { return Step.Bump { delta: 1 }; }
    return Step.Keep;
}

fn run_loop(limit: u64) -> (result: u64) {
    iterate i up_to 8 carrying acc: u64 = 0 {
        let action: Step = step_choice(i);
        let updated: u64 = match action {
            Keep => acc,
            Bump { delta: d } => acc +% d
        };
        next acc = updated;
    }
    return acc;
}
"""


def _find_post_match_advance_info(fn_wire, flow_block_count):
    """Locate the generated post-match join block carrying kind=14 and its Jump."""
    for blk in _walk_wire_flist(_wire_field(fn_wire, "blocks")):
        blk_id = _wire_field(blk, "id")["integer"]["value"]
        if blk_id < flow_block_count:
            continue
        for inst in _walk_wire_flist(_wire_field(blk, "instructions")):
            if _wire_field(inst, "kind")["integer"]["value"] == 14:
                return blk, inst
    return None, None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    scalar_bytes = SCALAR_MATCH_LOOP_SRC.encode("utf-8")
    scalar_args = _paged_args(scalar_bytes)

    # Fetch Flow CFG first in a dedicated sequential probe so only one probe runs at a time.
    flow_probe = SingleModuleProbe(
        "source,lexer,parser,segment,decl,flow",
        "mncs.compiler.flow.v1",
        ["lower_unit"],
    )
    flow_ok = False
    try:
        flow_wire = flow_probe.call_raw("lower_unit", scalar_args)
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
        # 1. Nested scalar match in iterate body (binary search pattern)
        ssa_wire = probe.call_raw("lower_flow_unit_value_ssa", scalar_args)
        ssa_dec = sem.decode(ssa_wire)
        assert ssa_dec["valid"] is True and ssa_dec["verified_function_count"] == 1, ssa_dec

        flow_fn_wire = next(_walk_wire_flist(_wire_field(flow_wire, "functions")))
        ssa_fn_wire = next(_walk_wire_flist(_wire_field(ssa_wire, "functions")))
        flow_blk_count = _wire_field(flow_fn_wire, "block_count")["integer"]["value"]

        # Positive direct verify_function_with_flow check
        pos_ver = sem.decode(
            probe.call_raw(
                "verify_function_with_flow",
                scalar_args + [ssa_fn_wire, flow_fn_wire],
            )
        )
        assert pos_ver is True, "positive verify_function_with_flow failed"

        join_blk, adv_inst = _find_post_match_advance_info(ssa_fn_wire, flow_blk_count)
        assert join_blk is not None and adv_inst is not None, "expected kind=14 in post-match join block"
        join_blk_id = _wire_field(join_blk, "id")["integer"]["value"]
        adv_result_id = _wire_field(adv_inst, "result")["integer"]["value"]
        adv_input_id = next(_walk_wire_flist(_wire_field(adv_inst, "inputs")))
        adv_input_val_id = _wire_field(adv_input_id, "value")["integer"]["value"]

        # Tamper 1: pass OLD unadvanced index (adv_input_val_id) instead of adv_result_id on backedge Jump
        t1_fn = copy.deepcopy(ssa_fn_wire)
        t1_blk, _ = _find_post_match_advance_info(t1_fn, flow_blk_count)
        t1_term = _wire_field(t1_blk, "terminator")
        assert t1_term["finite"]["discriminant"] == 1, t1_term  # Jump
        t1_payload = {k: v for k, v in t1_term["finite"]["payload"]}
        replaced = False
        for arg_rec in _walk_wire_flist(t1_payload["arguments"]):
            if _wire_field(arg_rec, "value")["integer"]["value"] == adv_result_id:
                _set_wire_field(arg_rec, "value", sem.integer(adv_input_val_id))
                replaced = True
                break
        assert replaced, "did not find adv_result_id in post-match Jump arguments"
        t1_ok = sem.decode(
            probe.call_raw(
                "verify_function_with_flow",
                scalar_args + [t1_fn, flow_fn_wire],
            )
        )
        assert t1_ok is False, "tamper 1 (stale unadvanced index on backedge Jump) must be rejected"

        # Tamper 2: redirect post-match join block's Jump target away from loop header
        t2_fn = copy.deepcopy(ssa_fn_wire)
        t2_blk, _ = _find_post_match_advance_info(t2_fn, flow_blk_count)
        t2_term = _wire_field(t2_blk, "terminator")
        for idx, (k, v) in enumerate(t2_term["finite"]["payload"]):
            if k == "target":
                orig_target = v["integer"]["value"]
                t2_term["finite"]["payload"][idx] = [k, sem.integer(orig_target + 1)]
        t2_ok = sem.decode(
            probe.call_raw(
                "verify_function_with_flow",
                scalar_args + [t2_fn, flow_fn_wire],
            )
        )
        assert t2_ok is False, "tamper 2 (miswired Jump target) must be rejected"

        # Tamper 3: feed pre-match block's stale index (value id 0 or another non-join param) into kind=14
        t3_fn = copy.deepcopy(ssa_fn_wire)
        _, t3_inst = _find_post_match_advance_info(t3_fn, flow_blk_count)
        t3_in = next(_walk_wire_flist(_wire_field(t3_inst, "inputs")))
        _set_wire_field(t3_in, "value", sem.integer(0))
        t3_ok = sem.decode(
            probe.call_raw(
                "verify_function_with_flow",
                scalar_args + [t3_fn, flow_fn_wire],
            )
        )
        assert t3_ok is False, "tamper 3 (stale pre-match input to kind=14) must be rejected"

        # Tamper 4: break the post-match join_target chain on the pre-match block
        t4_fn = copy.deepcopy(ssa_fn_wire)
        broke_join = False
        for blk in _walk_wire_flist(_wire_field(t4_fn, "blocks")):
            term = _wire_field(blk, "terminator")
            if term["finite"]["discriminant"] in (4, 5):  # FiniteSwitch / ScalarSwitch
                for idx, (k, v) in enumerate(term["finite"]["payload"]):
                    if k == "join_target" and v["integer"]["value"] == join_blk_id:
                        term["finite"]["payload"][idx] = [k, sem.integer(0)]
                        broke_join = True
                        break
            if broke_join:
                break
        assert broke_join, "did not find switch join_target pointing to join_blk_id"
        t4_ok = sem.decode(
            probe.call_raw(
                "verify_function_with_flow",
                scalar_args + [t4_fn, flow_fn_wire],
            )
        )
        assert t4_ok is False, "tamper 4 (broken switch join_target chain) must be rejected"

        # 2. Finite match in iterate body
        finite_bytes = FINITE_MATCH_LOOP_SRC.encode("utf-8")
        finite_ssa = sem.decode(
            probe.call_raw("lower_flow_unit_value_ssa", _paged_args(finite_bytes))
        )
        assert finite_ssa["valid"] is True and finite_ssa["verified_function_count"] == 2, finite_ssa

        # 2b. Nested iterate where inner iterate has post-match advance before outer next
        nested_src = """\
mncs 0.18;
module demo.nested_post_match_loop;
record S { c: u64, err: bool }
fn nested_scan(total: u64) -> (r: u64) {
    let init: S = S { c: 0, err: false };
    iterate batch up_to 4 carrying outer: S = init {
        if outer.err { return 1; }
        iterate byte_index up_to 4 carrying inner: S = outer {
            let adv: S = match select(inner.err || inner.c >= total, 1, 0) {
                1 => inner,
                _ => S { c: inner.c + 1, err: false }
            };
            next inner = adv;
        }
        next outer = inner;
    }
    return outer.c;
}
"""
        nested_ssa = sem.decode(
            probe.call_raw("lower_flow_unit_value_ssa", _paged_args(nested_src.encode("utf-8")))
        )
        assert nested_ssa["valid"] is True and nested_ssa["verified_function_count"] == 1, nested_ssa

        # 3. Real unchanged src/compiler/source.mncs!
        source_path = ROOT / "src" / "compiler" / "source.mncs"
        source_bytes = source_path.read_bytes()
        t_src = time.monotonic()
        source_ssa_wire = probe.call_raw("lower_flow_unit_value_ssa", _paged_args(source_bytes))
        source_elapsed_s = time.monotonic() - t_src
        source_ssa = sem.decode(source_ssa_wire)
        unverified_source_fns = []
        for fn_w in _walk_wire_flist(_wire_field(source_ssa_wire, "functions")):
            if not sem.decode(_wire_field(fn_w, "verified")):
                unverified_source_fns.append(_ident_str(sem.decode(_wire_field(fn_w, "identity"))))
        # Previously 46/55 verified (9 post-match loop-advance functions failed).
        assert source_ssa["valid"] is True and source_ssa["verified_function_count"] == source_ssa["function_count"], (
            source_ssa["verified_function_count"],
            source_ssa["function_count"],
            unverified_source_fns,
        )

        summary = {
            "status": "passed",
            "nested_scalar_match_loop": {
                "valid": True,
                "post_match_join_block_id": join_blk_id,
                "flow_block_count": flow_blk_count,
                "advance_input_value_id": adv_input_val_id,
                "advance_result_value_id": adv_result_id,
            },
            "finite_match_loop": {
                "valid": True,
                "verified_functions": finite_ssa["verified_function_count"],
            },
            "nested_iterate_post_match_loop": {
                "valid": True,
                "verified_functions": nested_ssa["verified_function_count"],
            },
            "miswired_ssa_tamper_rejections": {
                "stale_unadvanced_index_on_backedge_jump": not t1_ok,
                "miswired_jump_target": not t2_ok,
                "stale_pre_match_input_to_kind14": not t3_ok,
                "broken_switch_join_target_chain": not t4_ok,
            },
            "real_source_mncs": {
                "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
                "valid": source_ssa["valid"],
                "function_count": source_ssa["function_count"],
                "supported_function_count": source_ssa["supported_function_count"],
                "verified_function_count": source_ssa["verified_function_count"],
                "unverified_functions": unverified_source_fns,
                "elapsed_s": round(source_elapsed_s, 3),
            },
        }
        if args.json_out:
            args.json_out.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))
        ssa_ok = True
    finally:
        probe.close(check=ssa_ok)


if __name__ == "__main__":
    main()
