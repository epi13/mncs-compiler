#!/usr/bin/env python3
"""End-to-end differential, determinism, callable-selection, and fail-closed proof
for compiling and emitting unchanged `mncs.compiler.lexer.v1::punctuation` from
native Project SSA (`mncs.compiler.project.v1` -> `mncs.compiler.vm_emit.v1`) and
independently admitting/executing it in a fresh canonical VM (`mncs-vm`).

Provenance boundary:
- Project proof/CFG/Value SSA (`compile_project_target`), targeted callable
  selection, ScalarSwitch -> Constant/IntegerCompare/ConditionalBranch lowering,
  CFG/join verification, and closure checks:
      100% MNCS (`mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable`).
- DAG transport encoding & SHA-256 sealing:
      `tools/native_vm_artifact.py` (no lowering or closure selection).
- Independent admission & execution:
      fresh `mncs-vm` release binary (`da99dee`).
- Behavioral oracle:
      locked Stage-0 `mncs-compiler-stage0-probe` (`6a3f6907`).
"""

import argparse
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import backend_policy
import native_vm_artifact
import test_native_project_vm_emit as proj_emit
import test_native_vm_emit as single_emit
import test_project as project
import test_sem as sem


# Exact 24 x 8 = 192 corpus from tools/test_vm_lexer_nested_match.py.
OUTER_VALUES = [
    45, 61, 38, 124, 33, 60, 62, 46, 43, 42, 47, 37,
    94, 58, 59, 44, 40, 41, 123, 125, 91, 93, 0, 255,
]
INNER_VALUES = [0, 37, 38, 46, 61, 62, 124, 255]

TARGET_PUNCTUATION_ID = "mncs:0.2:function:mncs.compiler.lexer.v1::punctuation"
TARGET_MISSING_ID = "mncs:0.2:function:mncs.compiler.lexer.v1::does_not_exist"
TARGET_GENERIC_ID = "mncs:0.2:function:mncs.compiler.lexer.v1::keyword"
TARGET_IS_TRIVIA_ID = "mncs:0.2:function:mncs.compiler.lexer.v1::is_trivia"

WHOLE_MODULE_SWITCH_SOURCE = """\
mncs 0.19;
module mncs.test.scalar_switch_whole.v1;

fn tag_code(c: u64) -> (result: u64) {
    return match c {
        43 => 10,
        45 => 20,
        61 => 30,
        _ => 99
    };
}

fn dispatch_pair(a: u64, c: u64) -> (result: u64) {
    let base: u64 = match a {
        0 => 100,
        1 => 200,
        _ => 300
    };
    return base +% tag_code(c);
}
"""

CALLABLE_SELECT_SOURCE = """\
mncs 0.19;
module mncs.test.callable_select_switch.v1;

enum UnusedEnum { EA, EB }

fn helper_switch(x: u64) -> (result: u64) {
    return match x {
        1 => 11,
        2 => 22,
        _ => 33
    };
}

fn selected_entry(x: u64, y: u64) -> (result: u64) {
    return helper_switch(x) +% y;
}

fn unselected_finite_switch(e: UnusedEnum) -> (result: u64) {
    return match e {
        EA => 1,
        EB => 2
    };
}
"""

SMALL_TAMPER_SWITCH_SOURCE = """\
mncs 0.19;
module mncs.test.tamper_switch.v1;

fn classify(x: u64) -> (result: u64) {
    return match x {
        10 => 100,
        20 => 200,
        _ => 300
    };
}
"""


def _u64_val(v):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": v}}


def _byte_val(v):
    return {"byte": {"value": v}}


def _make_semantic_id_wire(recs, text: str):
    rec = recs["SemanticIdentity"]
    raw = text.encode("utf-8")
    assert len(raw) <= 512
    padded = [{"byte": {"value": b}} for b in raw] + [{"byte": {"value": 0}}] * (512 - len(raw))
    return {
        "record": {
            "type_identity": rec["identity"],
            "name": rec["name"],
            "fields": [
                ["bytes", {"sequence": {"values": padded}}],
                ["len", sem.integer(len(raw))],
                ["valid", {"boolean": {"value": True}}],
            ],
        }
    }


def _extract_dag_module(artifact):
    dag = artifact["code"]["mncs-selected-ssa"]["module"]
    nodes = dag["nodes"]
    cache = {}

    def decode(nid):
        if nid in cache:
            return cache[nid]
        n = nodes[nid]
        if n == "N":
            val = None
        elif "B" in n:
            val = n["B"]
        elif "I" in n:
            val = n["I"]
        elif "S" in n:
            val = n["S"]
        elif "A" in n:
            val = [decode(c) for c in n["A"]]
        elif "O" in n:
            val = {decode(k): decode(v) for k, v in n["O"]}
        else:
            raise ValueError(f"unknown DAG node: {n}")
        cache[nid] = val
        return val

    return decode(dag["root"])


def _assert_no_symbolic_bounds(node, path="root"):
    if isinstance(node, dict):
        for k, v in node.items():
            assert k not in ("param", "up_to_param", "generic_param"), (
                f"symbolic generic survived at {path}.{k}: {v}"
            )
            _assert_no_symbolic_bounds(v, f"{path}.{k}")
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _assert_no_symbolic_bounds(item, f"{path}[{idx}]")


def _run_admit_check(artifact_path):
    res = subprocess.run(
        [str(single_emit.VM_BIN), "admit", "--artifact", str(artifact_path)],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, f"mncs-vm admit failed: {res.stdout} {res.stderr}"
    payload = json.loads(res.stdout)
    assert payload["status"] == "admitted", payload
    return payload


class LexerCallableEmitterProbe:
    """Focused Stage-0 host probe seeding M=256, N=1024 for lexer callable emission."""

    def __init__(self, roots=None, seeds=None):
        env = dict(os.environ)
        env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{single_emit.STDLIB_ROOT / 'library'}"
        env["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa,project"
        env["MNCS_PROBE_LIBRARY_MODULES"] = "mncs.compiler.vm_emit.v1,mncs.std.text_utf8.v1"
        env["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.vm_emit.v1"
        env["MNCS_PROBE_BACKEND"] = backend_policy.resolve("project")
        env["MNCS_PROBE_BACKEND_ROOTS"] = ",".join(
            roots or ["compile_and_emit_project_vm_callable"]
        )
        env["MNCS_PROBE_CACHE_DIR"] = str(single_emit.CACHE_DIR)
        env["MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES"] = str(single_emit.MAX_AS_BYTES)
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(
            seeds
            or [
                {
                    "module": "mncs.compiler.vm_emit.v1",
                    "function": "compile_and_emit_project_vm_callable",
                    "type_arguments": [sem.nat_arg(256), sem.nat_arg(1024)],
                }
            ]
        )
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(
            [str(single_emit.PROBE_BIN)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            cwd=ROOT,
            env=env,
        )
        self.peak_rss_bytes = None

    def send(self, req):
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"emitter probe terminated unexpectedly: {self.proc.poll()}"
        return json.loads(line)

    def record_types(self, module_name):
        return {r["name"]: r for r in self.send({"record_types": module_name})}

    def call_raw(self, function, args, type_arguments, module="mncs.compiler.vm_emit.v1"):
        resp = self.send({
            "schema_version": "0.1",
            "target": {"module": module, "function": function},
            "arguments": args,
            "type_arguments": type_arguments,
            "step_budget": 8_000_000,
        })
        assert resp.get("status") == "returned", resp
        self.peak_rss_bytes = single_emit._read_proc_vmhwm_bytes(self.proc.pid) or self.peak_rss_bytes
        return resp["returned"][0]

    def call(self, function, args, type_arguments, module="mncs.compiler.vm_emit.v1"):
        return sem.decode(self.call_raw(function, args, type_arguments, module=module))

    def close(self):
        self.peak_rss_bytes = single_emit._read_proc_vmhwm_bytes(self.proc.pid) or self.peak_rss_bytes
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        rc = self.proc.wait(timeout=60)
        assert rc == 0, f"emitter probe exited with {rc}"


def _wire_field(rec, name):
    return proj_emit._wire_field(rec, name)


def _set_wire_field(rec, name, value):
    return proj_emit._set_wire_field(rec, name, value)


def _find_first_scalar_switch_block(proj_result_wire):
    mod = next(proj_emit._walk_wire_flist(_wire_field(proj_result_wire, "modules")))
    ssa_summary_wire = _wire_field(mod, "value_ssa")
    fns = _wire_field(ssa_summary_wire, "functions")
    fn = next(proj_emit._walk_wire_flist(fns))
    for blk in proj_emit._walk_wire_flist(_wire_field(fn, "blocks")):
        term = _wire_field(blk, "terminator")
        if term["finite"]["discriminant"] == 5:
            return fn, blk, term
    raise AssertionError("no ScalarSwitch block found")


def _term_payload_get(term, key):
    for k, v in term["finite"]["payload"]:
        if k == key:
            return v
    raise KeyError(key)


def _term_payload_set(term, key, value):
    for idx, (k, _) in enumerate(term["finite"]["payload"]):
        if k == key:
            term["finite"]["payload"][idx] = [k, value]
            return
    raise KeyError(key)


def _run_tamper_checks(small_probe, recs):
    """Exercise fail-closed rejections on malformed ScalarSwitch edges & join args."""
    raw = SMALL_TAMPER_SWITCH_SOURCE.encode("utf-8")
    targs64 = [sem.nat_arg(64), sem.nat_arg(1024)]
    sources = [("00_tamper_switch.mncs", Path("00_tamper_switch.mncs"), raw)]

    proj_wire = small_probe.call_raw(
        "compile_project_for_emit",
        proj_emit._build_compile_only_args(recs, sources),
        targs64,
    )
    baseline = small_probe.call("emit_verified_project_ssa", [proj_wire, sem.integer(0)], [])
    assert baseline["ok"] is True and baseline["function_count"] == 1, baseline

    results = {}

    def check_tamper(label, mutator, expected_codes=(6, 7)):
        mut = copy.deepcopy(proj_wire)
        mutator(mut)
        out = small_probe.call("emit_verified_project_ssa", [mut, sem.integer(0)], [])
        assert out["ok"] is False and out["error_code"] in expected_codes, (label, out)
        results[label] = {"ok": out["ok"], "error_code": out["error_code"], "error_block": out["error_block"]}

    # 1. Mismatched default_target
    def tamper_default_target(w):
        _, _, term = _find_first_scalar_switch_block(w)
        _term_payload_set(term, "default_target", sem.integer(999))
    check_tamper("mismatched_default_target", tamper_default_target, (7,))

    # 2. Missing default case (flip is_default -> false)
    def tamper_no_default(w):
        _, _, term = _find_first_scalar_switch_block(w)
        for cs in proj_emit._walk_wire_flist(_term_payload_get(term, "cases")):
            if _wire_field(cs, "is_default")["boolean"]["value"]:
                _set_wire_field(cs, "is_default", {"boolean": {"value": False}})
                _set_wire_field(cs, "value", sem.integer(30))
    check_tamper("missing_default_case", tamper_no_default, (7,))

    # 3. Arm count mismatch
    def tamper_arm_count(w):
        _, _, term = _find_first_scalar_switch_block(w)
        _term_payload_set(term, "arm_count", sem.integer(2))
    check_tamper("arm_count_mismatch", tamper_arm_count, (7,))

    # 4. Duplicate literal value across two non-default arms
    def tamper_dup_literal(w):
        _, _, term = _find_first_scalar_switch_block(w)
        cases = list(proj_emit._walk_wire_flist(_term_payload_get(term, "cases")))
        non_defs = [c for c in cases if not _wire_field(c, "is_default")["boolean"]["value"]]
        assert len(non_defs) >= 2
        v0 = _wire_field(non_defs[0], "value")
        _set_wire_field(non_defs[1], "value", copy.deepcopy(v0))
    check_tamper("duplicate_literal_arm", tamper_dup_literal, (7,))

    # 5. Missing switch target block
    def tamper_missing_target(w):
        _, _, term = _find_first_scalar_switch_block(w)
        first = next(proj_emit._walk_wire_flist(_term_payload_get(term, "cases")))
        _set_wire_field(first, "target", sem.integer(777))
    check_tamper("missing_switch_target", tamper_missing_target, (7,))

    # 6. Overflow literal case
    def tamper_overflow_case(w):
        _, _, term = _find_first_scalar_switch_block(w)
        first = next(proj_emit._walk_wire_flist(_term_payload_get(term, "cases")))
        _set_wire_field(first, "overflow", {"boolean": {"value": True}})
    check_tamper("overflow_literal_case", tamper_overflow_case, (7,))

    # 7. Arm jumping to wrong target (not join_target)
    def tamper_wrong_arm_join(w):
        fn, sw_blk, term = _find_first_scalar_switch_block(w)
        first = next(proj_emit._walk_wire_flist(_term_payload_get(term, "cases")))
        arm_tid = _wire_field(first, "target")["integer"]["value"]
        sw_id = _wire_field(sw_blk, "id")["integer"]["value"]
        for blk in proj_emit._walk_wire_flist(_wire_field(fn, "blocks")):
            if _wire_field(blk, "id")["integer"]["value"] == arm_tid:
                _term_payload_set(_wire_field(blk, "terminator"), "target", sem.integer(sw_id))
                return
        raise AssertionError("arm block not found")
    check_tamper("arm_wrong_join_target", tamper_wrong_arm_join, (7,))

    # 8. Mismatched join argument arity (drop first arg on arm Jump to join)
    def tamper_join_arg_arity(w):
        fn, _, term = _find_first_scalar_switch_block(w)
        first = next(proj_emit._walk_wire_flist(_term_payload_get(term, "cases")))
        arm_tid = _wire_field(first, "target")["integer"]["value"]
        for blk in proj_emit._walk_wire_flist(_wire_field(fn, "blocks")):
            if _wire_field(blk, "id")["integer"]["value"] == arm_tid:
                arm_term = _wire_field(blk, "terminator")
                args = _term_payload_get(arm_term, "arguments")
                # Drop head of IdList (ICons -> tail)
                tail = {k: v for k, v in args["finite"]["payload"]}["tail"]
                _term_payload_set(arm_term, "arguments", tail)
                return
        raise AssertionError("arm block not found")
    check_tamper("mismatched_join_arg_arity", tamper_join_arg_arity, (7,))

    # 9. Mismatched join parameter type vs switch result_type (u32 vs u64)
    def tamper_join_param_type(w):
        _, _, term = _find_first_scalar_switch_block(w)
        res_ty = copy.deepcopy(_term_payload_get(term, "result_type"))
        for idx, (k, _) in enumerate(res_ty["finite"]["payload"]):
            if k == "width":
                res_ty["finite"]["payload"][idx] = ["width", sem.integer(32)]
        _term_payload_set(term, "result_type", res_ty)
    check_tamper("mismatched_join_result_type", tamper_join_param_type, (7,))

    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    assert single_emit.PROBE_BIN.exists(), f"probe missing: {single_emit.PROBE_BIN}"
    assert single_emit.VM_BIN.exists(), f"mncs-vm missing: {single_emit.VM_BIN}"

    stdlib_dir = single_emit.STDLIB_ROOT / "library" / "std"
    prim_path = stdlib_dir / "utf8_primitives.mncs"
    scal_path = stdlib_dir / "utf8_scalar.mncs"
    text_path = stdlib_dir / "text_utf8.mncs"
    source_path = ROOT / "src" / "compiler" / "source.mncs"
    lexer_path = ROOT / "src" / "compiler" / "lexer.mncs"

    lexer_bytes = lexer_path.read_bytes()
    lexer_sha256 = hashlib.sha256(lexer_bytes).hexdigest()
    sources_lexer = [
        ("00_utf8_primitives.mncs", prim_path, prim_path.read_bytes()),
        ("01_utf8_scalar.mncs", scal_path, scal_path.read_bytes()),
        ("02_text_utf8.mncs", text_path, text_path.read_bytes()),
        ("03_source.mncs", source_path, source_path.read_bytes()),
        ("04_lexer.mncs", lexer_path, lexer_bytes),
    ]
    lexer_target_idx = 4
    closure_digests = [(sid, hashlib.sha256(raw).hexdigest()) for sid, _, raw in sources_lexer]

    emitter_src_sha256 = hashlib.sha256((ROOT / "src" / "compiler" / "vm_emit.mncs").read_bytes()).hexdigest()
    compiler_rev = single_emit._git_head(ROOT)
    vm_rev = single_emit._git_head(single_emit.VM_REPO)
    stage0_rev = single_emit._stage0_lock_rev()
    targs256 = [sem.nat_arg(256), sem.nat_arg(1024)]

    with tempfile.TemporaryDirectory(prefix="mncs-lexer-punct-vm-") as tmpdir:
        tmp = Path(tmpdir)

        # ------------------------------------------------------------------
        # 1. Process 1: Emit unchanged mncs.compiler.lexer.v1::punctuation
        #    + negative callable-selection checks + next blocker (is_trivia).
        # ------------------------------------------------------------------
        t1 = time.monotonic()
        p1 = LexerCallableEmitterProbe()
        try:
            recs1 = p1.record_types("mncs.compiler.vm_emit.v1")
            base_args = proj_emit._build_project_args(recs1, sources_lexer, lexer_target_idx)
            emit1 = p1.call(
                "compile_and_emit_project_vm_callable",
                base_args + [_make_semantic_id_wire(recs1, TARGET_PUNCTUATION_ID)],
                targs256,
            )
            assert emit1["ok"] is True, emit1
            assert emit1["function_count"] == 1, emit1

            neg_missing = p1.call(
                "compile_and_emit_project_vm_callable",
                base_args + [_make_semantic_id_wire(recs1, TARGET_MISSING_ID)],
                targs256,
            )
            assert neg_missing["ok"] is False and neg_missing["error_code"] == 9, neg_missing

            neg_generic = p1.call(
                "compile_and_emit_project_vm_callable",
                base_args + [_make_semantic_id_wire(recs1, TARGET_GENERIC_ID)],
                targs256,
            )
            assert neg_generic["ok"] is False and neg_generic["error_code"] == 11, neg_generic
        finally:
            p1.close()
        emit1_wall_s = time.monotonic() - t1

        # ------------------------------------------------------------------
        # 2. Process 2: Fresh independent process for bit-for-bit determinism.
        # ------------------------------------------------------------------
        t2 = time.monotonic()
        p2 = LexerCallableEmitterProbe()
        try:
            recs2 = p2.record_types("mncs.compiler.vm_emit.v1")
            base_args2 = proj_emit._build_project_args(recs2, sources_lexer, lexer_target_idx)
            emit2 = p2.call(
                "compile_and_emit_project_vm_callable",
                base_args2 + [_make_semantic_id_wire(recs2, TARGET_PUNCTUATION_ID)],
                targs256,
            )
            assert emit2["ok"] is True, emit2
        finally:
            p2.close()
        emit2_wall_s = time.monotonic() - t2

        art1 = native_vm_artifact.package_native_vm_artifact(
            emit1, lexer_bytes,
            allow_multi_module=True,
            producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            ssa_producer_entry="mncs.compiler.project.v1::compile_project_target",
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
            closure_source_digests=closure_digests,
        )
        art2 = native_vm_artifact.package_native_vm_artifact(
            emit2, lexer_bytes,
            allow_multi_module=True,
            producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            ssa_producer_entry="mncs.compiler.project.v1::compile_project_target",
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
            closure_source_digests=closure_digests,
        )
        art1_bytes = (json.dumps(art1, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        art2_bytes = (json.dumps(art2, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        assert art1_bytes == art2_bytes, "non-deterministic lexer::punctuation artifact emission"
        assert art1["artifact_id"] == art2["artifact_id"]
        art_sha256 = hashlib.sha256(art1_bytes).hexdigest()

        ssa_mod = _extract_dag_module(art1)
        _assert_no_symbolic_bounds(ssa_mod)
        assert len(art1["callables"]) == 1
        assert art1["callables"][0]["semantic"] == TARGET_PUNCTUATION_ID
        punct_fn = ssa_mod["functions"][0]
        cond_branches = sum(
            1 for b in punct_fn["blocks"]
            if isinstance(b["terminator"], dict) and "ConditionalBranch" in b["terminator"]
        )
        assert cond_branches >= 22, f"expected lowered switch dispatch chain, got {cond_branches}"

        art_path = tmp / "lexer_punctuation_native.vm.json"
        art_path.write_bytes(art1_bytes)
        admit_payload = _run_admit_check(art_path)

        # Tampered artifact refused by mncs-vm admit
        tampered_path = tmp / "lexer_punctuation_tampered.vm.json"
        tampered = json.loads(art1_bytes.decode("utf-8"))
        tampered["callables"][0]["name"] = "tampered_punctuation"
        tampered_path.write_text(json.dumps(tampered, sort_keys=True) + "\n")
        admit_bad = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(tampered_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit_bad.returncode != 0, admit_bad.stdout
        tamper_payload = json.loads(admit_bad.stdout)
        assert tamper_payload["status"] == "refused", tamper_payload

        # ------------------------------------------------------------------
        # 3. Fresh canonical mncs-vm execution vs locked Stage-0 (192 pairs).
        # ------------------------------------------------------------------
        pairs = [(a, b) for a in OUTER_VALUES for b in INNER_VALUES]
        vm_sess = single_emit.FreshVmSession(art_path)
        vm_results = []
        vm_max_cells = 0
        vm_max_steps = 0
        vm_total_steps = 0
        try:
            for a, b in pairs:
                res = vm_sess.call("mncs.compiler.lexer.v1", "punctuation", [_u64_val(a), _u64_val(b)])
                assert res["outcome"]["kind"] == "completed", ((a, b), res)
                usage = res["record"]["usage"]
                vm_max_cells = max(vm_max_cells, usage.get("max_memory_cells", usage["memory_cells"]))
                vm_max_steps = max(vm_max_steps, usage["steps"])
                vm_total_steps += usage["steps"]
                vm_results.append(res["execution"]["returned"])
        finally:
            vm_sess.close()

        oracle = single_emit.Stage0ModuleOracle(
            "mncs.compiler.lexer.v1",
            [ROOT / "src", single_emit.STDLIB_ROOT / "library"],
        )
        matched = 0
        oracle_results = []
        try:
            for idx, (a, b) in enumerate(pairs):
                s0 = oracle.call("punctuation", [_u64_val(a), _u64_val(b)])
                assert s0["status"] == "returned", ((a, b), s0)
                assert s0["returned"] == vm_results[idx], (
                    f"punctuation({a}, {b}) mismatch: vm={vm_results[idx]} stage0={s0['returned']}"
                )
                oracle_results.append(s0["returned"])
                matched += 1
        finally:
            oracle.close()
        assert matched == len(pairs) == 192
        semantic_digest = hashlib.sha256(json.dumps(oracle_results, sort_keys=True).encode()).hexdigest()

        # ------------------------------------------------------------------
        # 4. Whole-module entry usability + transitive-callee callable selection
        #    + malformed ScalarSwitch / join-arg fail-closed checks (M=64 probe).
        # ------------------------------------------------------------------
        p_small = LexerCallableEmitterProbe(
            roots=[
                "compile_and_emit_vm_module",
                "compile_project_for_emit",
                "emit_verified_project_ssa",
                "compile_and_emit_project_vm_callable",
            ],
            seeds=[
                {
                    "module": "mncs.compiler.vm_emit.v1",
                    "function": "compile_and_emit_vm_module",
                    "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
                },
                {
                    "module": "mncs.compiler.vm_emit.v1",
                    "function": "compile_project_for_emit",
                    "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
                },
                {
                    "module": "mncs.compiler.vm_emit.v1",
                    "function": "compile_and_emit_project_vm_callable",
                    "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
                },
            ],
        )
        try:
            targs64 = [sem.nat_arg(64), sem.nat_arg(1024)]
            recs_small = p_small.record_types("mncs.compiler.vm_emit.v1")

            whole_bytes = WHOLE_MODULE_SWITCH_SOURCE.encode("utf-8")
            whole_emit = p_small.call("compile_and_emit_vm_module", single_emit._paged_args(whole_bytes), targs64)
            assert whole_emit["ok"] is True and whole_emit["function_count"] == 2, whole_emit

            sel_bytes = CALLABLE_SELECT_SOURCE.encode("utf-8")
            sel_sources = [("00_callable_select_switch.mncs", Path("00_callable_select_switch.mncs"), sel_bytes)]
            sel_emit = p_small.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs_small, sel_sources, 0)
                + [_make_semantic_id_wire(recs_small, "mncs:0.2:function:mncs.test.callable_select_switch.v1::selected_entry")],
                targs64,
            )
            assert sel_emit["ok"] is True and sel_emit["function_count"] == 2, sel_emit

            tamper_results = _run_tamper_checks(p_small, recs_small)
        finally:
            p_small.close()

        # Admit & execute whole-module switch + callable-selected transitive closure in fresh mncs-vm
        whole_art = native_vm_artifact.package_native_vm_artifact(
            whole_emit, whole_bytes,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        whole_path = tmp / "whole_switch.vm.json"
        whole_path.write_text(json.dumps(whole_art, sort_keys=True) + "\n")
        _run_admit_check(whole_path)
        vm_whole = single_emit.FreshVmSession(whole_path)
        try:
            for a, c, exp in [(0, 43, 110), (1, 45, 220), (9, 61, 330), (2, 0, 399)]:
                res = vm_whole.call("mncs.test.scalar_switch_whole.v1", "dispatch_pair", [_u64_val(a), _u64_val(c)])
                assert res["outcome"]["kind"] == "completed", res
                assert res["execution"]["returned"] == [_u64_val(exp)], ((a, c, exp), res)
        finally:
            vm_whole.close()

        sel_art = native_vm_artifact.package_native_vm_artifact(
            sel_emit, sel_bytes,
            allow_multi_module=True,
            producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            ssa_producer_entry="mncs.compiler.project.v1::compile_project_target",
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        sel_callables = [c["semantic"] for c in sel_art["callables"]]
        assert sel_callables == [
            "mncs:0.2:function:mncs.test.callable_select_switch.v1::selected_entry",
            "mncs:0.2:function:mncs.test.callable_select_switch.v1::helper_switch",
        ], sel_callables
        sel_path = tmp / "callable_select.vm.json"
        sel_path.write_text(json.dumps(sel_art, sort_keys=True) + "\n")
        _run_admit_check(sel_path)
        vm_sel = single_emit.FreshVmSession(sel_path)
        try:
            for x, y, exp in [(1, 5, 16), (2, 8, 30), (99, 7, 40)]:
                res = vm_sel.call("mncs.test.callable_select_switch.v1", "selected_entry", [_u64_val(x), _u64_val(y)])
                assert res["outcome"]["kind"] == "completed", res
                assert res["execution"]["returned"] == [_u64_val(exp)], ((x, y, exp), res)
        finally:
            vm_sel.close()

        summary = {
            "status": "passed",
            "provenance": {
                "callable_selection_and_lowering": "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
                "native_project_ssa_producer": "mncs.compiler.project.v1::compile_project_target",
                "transport_codec_and_sealer": "tools/native_vm_artifact.py",
                "independent_vm_admission_and_execution": str(single_emit.VM_BIN),
                "behavioral_oracle": f"locked Stage-0 mncs-compiler-stage0-probe ({stage0_rev})",
                "compiler_revision": compiler_rev,
                "vm_revision": vm_rev,
                "emitter_source_sha256": emitter_src_sha256,
            },
            "lexer_punctuation": {
                "target_module": "mncs.compiler.lexer.v1",
                "target_callable": TARGET_PUNCTUATION_ID,
                "unchanged_lexer_mncs_sha256": lexer_sha256,
                "closure_module_count": len(sources_lexer),
                "emitted_callables": [c["semantic"] for c in art1["callables"]],
                "function_count": emit1["function_count"],
                "block_count": emit1["block_count"],
                "instruction_count": emit1["instruction_count"],
                "conditional_branch_count": cond_branches,
                "symbolic_bounds_remaining": 0,
                "artifact_id": art1["artifact_id"],
                "ssa_module_identity": art1["source"]["selected_ssa_identity"],
                "artifact_sha256": art_sha256,
                "artifact_bytes": len(art1_bytes),
                "deterministic_across_fresh_processes": True,
                "emit_process1_wall_s": round(emit1_wall_s, 3),
                "emit_process1_peak_rss_bytes": p1.peak_rss_bytes,
                "emit_process2_wall_s": round(emit2_wall_s, 3),
                "emit_process2_peak_rss_bytes": p2.peak_rss_bytes,
                "vm_admission_status": admit_payload["status"],
                "vm_differential_cases": len(pairs),
                "vm_differential_matches": matched,
                "semantic_digest": semantic_digest,
                "vm_session_wall_s": round(vm_sess.elapsed_s, 4),
                "vm_resident_peak_rss_bytes": vm_sess.peak_rss_bytes,
                "vm_max_memory_cells": vm_max_cells,
                "vm_max_steps_per_call": vm_max_steps,
                "vm_total_steps": vm_total_steps,
            },
            "whole_module_switch_entry": {
                "module": "mncs.test.scalar_switch_whole.v1",
                "function_count": whole_emit["function_count"],
                "artifact_id": whole_art["artifact_id"],
                "vm_cases_matched": 4,
            },
            "callable_selection_with_transitive_callee": {
                "module": "mncs.test.callable_select_switch.v1",
                "emitted_callables": sel_callables,
                "skipped_unsupported_sibling": "mncs:0.2:function:mncs.test.callable_select_switch.v1::unselected_finite_switch",
                "artifact_id": sel_art["artifact_id"],
                "vm_cases_matched": 3,
            },
            "negative_rejections": {
                "missing_target_callable": {"ok": neg_missing["ok"], "error_code": neg_missing["error_code"]},
                "uninstantiated_generic_callable": {"ok": neg_generic["ok"], "error_code": neg_generic["error_code"]},
                "tampered_artifact_vm_admission": {"status": tamper_payload["status"], "returncode": admit_bad.returncode},
                "malformed_scalar_switch_and_join": tamper_results,
            },
        }
        if args.json_out:
            args.json_out.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
