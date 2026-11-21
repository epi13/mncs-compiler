#!/usr/bin/env python3
"""End-to-end differential, determinism, negative-rejection, and next-caller
proof for compiling and emitting unchanged `mncs.compiler.lexer.v1::is_trivia`
from native Project SSA (`mncs.compiler.project.v1` -> `mncs.compiler.vm_emit.v1`)
and independently admitting/executing it in a fresh canonical VM (`mncs-vm`).

Provenance boundary:
- Project proof/CFG/Value SSA (`compile_project_target`), targeted callable
  selection, RecordProject (kind 8) schema/owner/index/name/type binding and
  lowering, CFG/join verification, and closure checks:
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
import test_native_lexer_punctuation_vm_emit as punct
import test_native_project_vm_emit as proj_emit
import test_native_vm_emit as single_emit
import test_sem as sem

TARGET_IS_TRIVIA_ID = "mncs:0.2:function:mncs.compiler.lexer.v1::is_trivia"
TARGET_IS_TRIVIA_CONT_ID = "mncs:0.2:function:mncs.compiler.lexer.v1::is_trivia_cont"
TARGET_SKIP_TRIVIA_ID = "mncs:0.2:function:mncs.compiler.lexer.v1::skip_trivia"
TARGET_TOKEN_NONWORD_ID = "mncs:0.2:function:mncs.compiler.lexer.v1::token_from_nonword_scan"

SKIP_TRIVIA_ROOT_SOURCE = """\
mncs 0.19;
module mncs.test.skip_trivia_caller.v1;
use mncs.compiler.lexer.v1 as lexer;

fn call_skip_trivia(text: [byte; up_to 32], tok: lexer.Token) -> (result: lexer.Token) {
    return lexer.skip_trivia<32>(text, tok);
}
"""

TAMPER_PROJECT_SOURCE = """\
mncs 0.19;
module mncs.test.tamper_record_project.v1;

record RecA { alpha: u64, flag: bool }
record RecB { beta: u64, flag: bool }

fn read_alpha(a: RecA, b: RecB, n: u64) -> (result: u64) {
    let x: u64 = a.alpha;
    let y: u64 = b.beta;
    let ok: bool = a.flag;
    return select(ok, x +% n, y);
}
"""


def _u64_val(v):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": v}}


def _bool_val(v):
    return {"boolean": {"value": bool(v)}}


def _vm_token_val(token_type_id, kind, start, end, diag):
    # Canonical sorted field order for mncs-vm JSON Value::Record.
    return {
        "record": {
            "name": "Token",
            "type_identity": token_type_id,
            "fields": [
                ["diagnostic", _u64_val(diag)],
                ["end", _u64_val(end)],
                ["kind", _u64_val(kind)],
                ["start", _u64_val(start)],
            ],
        }
    }


def _stage0_token_wire(recs_lexer, kind, start, end, diag):
    rec = recs_lexer["Token"]
    return {
        "record": {
            "type_identity": rec["identity"],
            "name": rec["name"],
            "fields": [
                ["diagnostic", sem.integer(diag)],
                ["end", sem.integer(end)],
                ["kind", sem.integer(kind)],
                ["start", sem.integer(start)],
            ],
        }
    }


def _stage0_is_trivia_oracle(cases):
    """Run locked Stage-0 probe directly on unchanged lexer::is_trivia."""
    env = dict(os.environ)
    env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{single_emit.STDLIB_ROOT / 'library'}"
    env["MNCS_PROBE_MODULES"] = "source,lexer"
    env["MNCS_PROBE_LIBRARY_MODULES"] = "mncs.std.text_utf8.v1"
    env["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.lexer.v1"
    env["MNCS_PROBE_BACKEND"] = backend_policy.resolve("lexer")
    env["MNCS_PROBE_BACKEND_ROOTS"] = "is_trivia"
    env["MNCS_PROBE_CACHE_DIR"] = str(single_emit.CACHE_DIR)
    env["MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES"] = str(single_emit.MAX_AS_BYTES)
    proc = subprocess.Popen(
        [str(single_emit.PROBE_BIN)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=ROOT,
        env=env,
    )
    out = {}
    try:
        proc.stdin.write(json.dumps({"record_types": "mncs.compiler.lexer.v1"}) + "\n")
        proc.stdin.flush()
        recs_lexer = {r["name"]: r for r in json.loads(proc.stdout.readline())}
        for label, kind, start, end, diag in cases:
            req = {
                "schema_version": "0.1",
                "target": {"module": "mncs.compiler.lexer.v1", "function": "is_trivia"},
                "arguments": [_stage0_token_wire(recs_lexer, kind, start, end, diag)],
                "type_arguments": [],
                "step_budget": 100_000,
            }
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            assert resp.get("status") == "returned", (label, resp)
            val = sem.decode(resp["returned"][0])
            assert isinstance(val, bool), (label, val)
            out[label] = val
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        assert proc.wait(timeout=60) == 0
    return out


def _find_first_record_project_inst(proj_result_wire):
    mod = next(proj_emit._walk_wire_flist(proj_emit._wire_field(proj_result_wire, "modules")))
    ssa_summary_wire = proj_emit._wire_field(mod, "value_ssa")
    fn = next(proj_emit._walk_wire_flist(proj_emit._wire_field(ssa_summary_wire, "functions")))
    for blk in proj_emit._walk_wire_flist(proj_emit._wire_field(fn, "blocks")):
        for inst in proj_emit._walk_wire_flist(proj_emit._wire_field(blk, "instructions")):
            if proj_emit._wire_field(inst, "kind")["integer"]["value"] == 8:
                return fn, blk, inst
    raise AssertionError("no RecordProject (kind 8) instruction found")


def _run_record_project_tamper_checks(small_probe, recs):
    """Exercise fail-closed rejections on mismatched owners, fields, indices, and types."""
    raw = TAMPER_PROJECT_SOURCE.encode("utf-8")
    targs64 = [sem.nat_arg(64), sem.nat_arg(1024)]
    sources = [("00_tamper_record_project.mncs", Path("00_tamper_record_project.mncs"), raw)]

    proj_wire = small_probe.call_raw(
        "compile_project_for_emit",
        proj_emit._build_compile_only_args(recs, sources),
        targs64,
    )
    emit_args_prefix = proj_emit._build_compile_only_args(recs, sources)
    baseline = small_probe.call(
        "emit_verified_project_ssa_with_sources",
        emit_args_prefix + [proj_wire, sem.integer(0)],
        targs64,
    )
    assert baseline["ok"] is True and baseline["function_count"] == 1, baseline

    # Locate the two record identities and the 'flag' field span in TAMPER_PROJECT_SOURCE.
    rec_types = native_vm_artifact.flist(baseline["record_types"])
    rec_id_by_name = {
        native_vm_artifact._semantic_id_str(r["name"]): native_vm_artifact._semantic_id_str(r["identity"])
        for r in rec_types
    }
    assert "RecA" in rec_id_by_name and "RecB" in rec_id_by_name, rec_id_by_name
    rec_b_id = rec_id_by_name["RecB"]
    flag_span_start = raw.index(b"a.flag") + 2
    flag_span_end = flag_span_start + 4
    assert raw[flag_span_start:flag_span_end] == b"flag"

    # Find the scalar parameter `n: u64` value id and a genuine TBool SemType wire value (from `a.flag`).
    fn0, blk0, _ = _find_first_record_project_inst(proj_wire)
    fn_inputs = list(proj_emit._walk_wire_flist(proj_emit._wire_field(fn0, "inputs")))
    scalar_input_id = None
    for v in fn_inputs:
        ty_payload = proj_emit._wire_field(v, "ty")["finite"].get("payload") or []
        if any(k == "width" for k, _ in ty_payload):
            scalar_input_id = proj_emit._wire_field(v, "id")["integer"]["value"]
            break
    assert scalar_input_id is not None, fn_inputs

    tbool_wire = None
    for inst in proj_emit._walk_wire_flist(proj_emit._wire_field(blk0, "instructions")):
        if proj_emit._wire_field(inst, "kind")["integer"]["value"] == 8:
            ty = proj_emit._wire_field(inst, "ty")
            if ty["finite"]["discriminant"] == 0 and not (ty["finite"].get("payload") or []):
                tbool_wire = copy.deepcopy(ty)
                break
    assert tbool_wire is not None, "could not find TBool SemType wire template"

    results = {}

    def check_tamper(label, mutator, expected_codes=(4, 7)):
        mut = copy.deepcopy(proj_wire)
        mutator(mut)
        out = small_probe.call(
            "emit_verified_project_ssa_with_sources",
            emit_args_prefix + [mut, sem.integer(0)],
            targs64,
        )
        assert out["ok"] is False and out["error_code"] in expected_codes, (label, out)
        results[label] = {
            "ok": out["ok"],
            "error_code": out["error_code"],
            "error_block": out["error_block"],
            "error_instruction": out["error_instruction"],
        }

    # 1. Mismatched nominal owner on instruction (RecB instead of RecA)
    def tamper_owner_rec_b(w):
        _, _, inst = _find_first_record_project_inst(w)
        proj_emit._set_wire_field(inst, "nominal_identity", punct._make_semantic_id_wire(recs, rec_b_id))
    check_tamper("mismatched_nominal_owner", tamper_owner_rec_b, (7,))

    # 2. Unknown nominal schema identity
    def tamper_unknown_owner(w):
        _, _, inst = _find_first_record_project_inst(w)
        proj_emit._set_wire_field(
            inst,
            "nominal_identity",
            punct._make_semantic_id_wire(recs, "mncs:0.2:record-type:mncs.test.tamper_record_project.v1::Ghost::"),
        )
    check_tamper("unknown_nominal_schema", tamper_unknown_owner, (4, 7))

    # 3. Mismatched field index (operator 1 = flag while span is 'alpha')
    def tamper_field_index(w):
        _, _, inst = _find_first_record_project_inst(w)
        proj_emit._set_wire_field(inst, "operator", sem.integer(1))
    check_tamper("mismatched_field_index", tamper_field_index, (7,))

    # 4. Out-of-bounds field index (operator 99)
    def tamper_oob_field_index(w):
        _, _, inst = _find_first_record_project_inst(w)
        proj_emit._set_wire_field(inst, "operator", sem.integer(99))
    check_tamper("out_of_bounds_field_index", tamper_oob_field_index, (7,))

    # 5. Mismatched field name span ('flag' while operator is 0 = alpha)
    def tamper_field_name_span(w):
        _, _, inst = _find_first_record_project_inst(w)
        proj_emit._set_wire_field(inst, "binding_start", sem.integer(flag_span_start))
        proj_emit._set_wire_field(inst, "binding_end", sem.integer(flag_span_end))
    check_tamper("mismatched_field_name_span", tamper_field_name_span, (7,))

    # 6. Mismatched result type (TBool instead of u64)
    def tamper_result_type_bool(w):
        _, _, inst = _find_first_record_project_inst(w)
        proj_emit._set_wire_field(inst, "ty", copy.deepcopy(tbool_wire))
    check_tamper("mismatched_result_type_bool", tamper_result_type_bool, (7,))

    # 7. Mismatched result integer width (u32 instead of u64)
    def tamper_result_type_width(w):
        _, _, inst = _find_first_record_project_inst(w)
        ty = proj_emit._wire_field(inst, "ty")
        for idx, (k, _) in enumerate(ty["finite"]["payload"]):
            if k == "width":
                ty["finite"]["payload"][idx] = ["width", sem.integer(32)]
    check_tamper("mismatched_result_type_width", tamper_result_type_width, (7,))

    # 8. Non-record operand (scalar u64 value id)
    def tamper_non_record_operand(w):
        _, _, inst = _find_first_record_project_inst(w)
        inputs_wire = proj_emit._wire_field(inst, "inputs")
        for k, v in inputs_wire["finite"]["payload"]:
            if k == "head":
                proj_emit._set_wire_field(v, "value", sem.integer(scalar_input_id))
    check_tamper("non_record_operand", tamper_non_record_operand, (4,))

    # 9. Wrong operand arity (argc = 0)
    def tamper_wrong_arity(w):
        _, _, inst = _find_first_record_project_inst(w)
        proj_emit._set_wire_field(inst, "argc", sem.integer(0))
    check_tamper("wrong_arity_zero", tamper_wrong_arity, (7,))

    return results


def _build_is_trivia_corpus():
    """Cover trivia, non-trivia, diagnostic, and edge u64 token shapes."""
    cases = []
    # Trivia (kind 1=ws, 2=line comment, 3=block comment; diagnostic == 0)
    for kind, start, end in [
        (1, 0, 1),
        (1, 10, 42),
        (2, 0, 15),
        (2, 100, 200),
        (3, 5, 25),
        (3, 0, (1 << 64) - 1),
    ]:
        cases.append((f"trivia_k{kind}_s{start}_e{end}", kind, start, end, 0))
    # Non-trivia (kind 0=EOF, 4=ident, 5=int, 6=version, 7=unknown, 10=mncs, 50=+, 80=->, 96=*|, max)
    for kind, start, end in [
        (0, 0, 0),
        (0, 10, 10),
        (4, 0, 4),
        (5, 3, 7),
        (6, 0, 4),
        (7, 0, 1),
        (10, 0, 4),
        (50, 1, 2),
        (80, 1, 3),
        (96, 2, 4),
        (999, 0, 0),
        ((1 << 64) - 1, 0, 0),
    ]:
        cases.append((f"nontrivia_k{kind}_s{start}", kind, start, end, 0))
    # Diagnostic cases on trivia kinds (1..3): any non-zero diagnostic MUST NOT be trivia!
    for kind in (1, 2, 3):
        for diag in (1, 2, 3, 4, 5, 6, 99, (1 << 64) - 1):
            cases.append((f"diag_trivia_k{kind}_d{diag}", kind, 0, 10, diag))
    # Diagnostic cases on non-trivia kinds (0, 4, 7)
    for kind, diag in [(0, 4), (4, 1), (7, 2), (7, 3), (7, 5), (0, 6)]:
        cases.append((f"diag_nontrivia_k{kind}_d{diag}", kind, 0, 1, diag))
    return cases


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

    skip_root_bytes = SKIP_TRIVIA_ROOT_SOURCE.encode("utf-8")
    sources_skip_caller = sources_lexer + [
        ("05_skip_trivia_caller.mncs", Path("05_skip_trivia_caller.mncs"), skip_root_bytes),
    ]

    emitter_src_sha256 = hashlib.sha256((ROOT / "src" / "compiler" / "vm_emit.mncs").read_bytes()).hexdigest()
    compiler_rev = single_emit._git_head(ROOT)
    vm_rev = single_emit._git_head(single_emit.VM_REPO)
    stage0_rev = single_emit._stage0_lock_rev()
    targs256 = [sem.nat_arg(256), sem.nat_arg(1024)]

    with tempfile.TemporaryDirectory(prefix="mncs-lexer-is-trivia-vm-") as tmpdir:
        tmp = Path(tmpdir)

        # ------------------------------------------------------------------
        # 1. Process 1: Emit unchanged mncs.compiler.lexer.v1::is_trivia
        #    + probe smallest lexer caller consuming a projected Token.
        # ------------------------------------------------------------------
        t1 = time.monotonic()
        p1 = punct.LexerCallableEmitterProbe()
        try:
            recs1 = p1.record_types("mncs.compiler.vm_emit.v1")
            base_args = proj_emit._build_project_args(recs1, sources_lexer, lexer_target_idx)
            emit1 = p1.call(
                "compile_and_emit_project_vm_callable",
                base_args + [punct._make_semantic_id_wire(recs1, TARGET_IS_TRIVIA_ID)],
                targs256,
            )
            assert emit1["ok"] is True, emit1
            assert emit1["function_count"] == 1, emit1
            assert emit1["record_type_count"] == 1, emit1

            # Bonus check: is_trivia_cont also projects Token fields.
            emit_cont = p1.call(
                "compile_and_emit_project_vm_callable",
                base_args + [punct._make_semantic_id_wire(recs1, TARGET_IS_TRIVIA_CONT_ID)],
                targs256,
            )
            assert emit_cont["ok"] is True and emit_cont["function_count"] == 1, emit_cont

            # Probe smallest lexer caller consuming a projected Token:
            # (a) direct uninstantiated lexer::skip_trivia -> generic refusal (code 11)
            skip_direct = p1.call(
                "compile_and_emit_project_vm_callable",
                base_args + [punct._make_semantic_id_wire(recs1, TARGET_SKIP_TRIVIA_ID)],
                targs256,
            )
            # (b) concrete specialized caller root calling lexer.skip_trivia<32>(text, tok)
            skip_caller_args = proj_emit._build_project_args(recs1, sources_skip_caller, 5)
            skip_specialized = p1.call(
                "compile_and_emit_project_vm_callable",
                skip_caller_args
                + [punct._make_semantic_id_wire(recs1, "mncs:0.2:function:mncs.test.skip_trivia_caller.v1::call_skip_trivia")],
                targs256,
            )
        finally:
            p1.close()
        emit1_wall_s = time.monotonic() - t1

        # ------------------------------------------------------------------
        # 2. Process 2: Fresh independent process for bit-for-bit determinism.
        # ------------------------------------------------------------------
        t2 = time.monotonic()
        p2 = punct.LexerCallableEmitterProbe()
        try:
            recs2 = p2.record_types("mncs.compiler.vm_emit.v1")
            base_args2 = proj_emit._build_project_args(recs2, sources_lexer, lexer_target_idx)
            emit2 = p2.call(
                "compile_and_emit_project_vm_callable",
                base_args2 + [punct._make_semantic_id_wire(recs2, TARGET_IS_TRIVIA_ID)],
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
        assert art1_bytes == art2_bytes, "non-deterministic lexer::is_trivia artifact emission"
        assert art1["artifact_id"] == art2["artifact_id"]
        art_sha256 = hashlib.sha256(art1_bytes).hexdigest()

        ssa_mod = punct._extract_dag_module(art1)
        punct._assert_no_symbolic_bounds(ssa_mod)
        assert len(art1["callables"]) == 1
        assert art1["callables"][0]["semantic"] == TARGET_IS_TRIVIA_ID
        rec_types_emitted = ssa_mod.get("record_types") or []
        assert len(rec_types_emitted) == 1 and rec_types_emitted[0]["name"] == "Token", rec_types_emitted
        token_type_id = rec_types_emitted[0]["identity"]

        is_trivia_fn = ssa_mod["functions"][0]
        record_project_insts = [
            inst["kind"]["RecordProject"]
            for blk in is_trivia_fn["blocks"]
            for inst in blk["instructions"]
            if isinstance(inst["kind"], dict) and "RecordProject" in inst["kind"]
        ]
        assert len(record_project_insts) == 3, record_project_insts
        assert all(rp["type_identity"] == token_type_id for rp in record_project_insts), record_project_insts
        assert [rp["field"] for rp in record_project_insts] == ["kind", "kind", "diagnostic"], record_project_insts

        art_path = tmp / "lexer_is_trivia_native.vm.json"
        art_path.write_bytes(art1_bytes)
        admit_payload = punct._run_admit_check(art_path)

        # Tampered artifact refused by mncs-vm admit
        tampered_path = tmp / "lexer_is_trivia_tampered.vm.json"
        tampered = json.loads(art1_bytes.decode("utf-8"))
        tampered["callables"][0]["name"] = "tampered_is_trivia"
        tampered_path.write_text(json.dumps(tampered, sort_keys=True) + "\n")
        admit_bad = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(tampered_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit_bad.returncode != 0, admit_bad.stdout
        tamper_payload = json.loads(admit_bad.stdout)
        assert tamper_payload["status"] == "refused", tamper_payload

        # ------------------------------------------------------------------
        # 3. Differential execution in fresh canonical mncs-vm vs Stage-0.
        # ------------------------------------------------------------------
        cases = _build_is_trivia_corpus()
        oracle_results = _stage0_is_trivia_oracle(cases)
        vm_sess = single_emit.FreshVmSession(art_path)
        vm_max_cells = 0
        vm_max_steps = 0
        vm_total_steps = 0
        matched = 0
        try:
            for label, kind, start, end, diag in cases:
                res = vm_sess.call(
                    "mncs.compiler.lexer.v1",
                    "is_trivia",
                    [_vm_token_val(token_type_id, kind, start, end, diag)],
                )
                assert res["outcome"]["kind"] == "completed", (label, res)
                returned = res["execution"]["returned"]
                expected = oracle_results[label]
                assert returned == [_bool_val(expected)], (label, expected, returned)
                usage = res["record"]["usage"]
                vm_max_cells = max(vm_max_cells, usage["max_memory_cells"])
                vm_max_steps = max(vm_max_steps, usage["steps"])
                vm_total_steps += usage["steps"]
                matched += 1
        finally:
            vm_sess.close()
        assert matched == len(cases)
        semantic_digest = hashlib.sha256(json.dumps(oracle_results, sort_keys=True).encode()).hexdigest()

        # ------------------------------------------------------------------
        # 4. Negative fail-closed rejections on mismatched owners/fields/types.
        # ------------------------------------------------------------------
        p_small = punct.LexerCallableEmitterProbe(
            roots=[
                "compile_project_for_emit",
                "emit_verified_project_ssa_with_sources",
            ],
            seeds=[
                {
                    "module": "mncs.compiler.vm_emit.v1",
                    "function": "compile_project_for_emit",
                    "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
                },
                {
                    "module": "mncs.compiler.vm_emit.v1",
                    "function": "emit_verified_project_ssa_with_sources",
                    "type_arguments": [sem.nat_arg(64), sem.nat_arg(1024)],
                },
            ],
        )
        try:
            recs_small = p_small.record_types("mncs.compiler.vm_emit.v1")
            tamper_results = _run_record_project_tamper_checks(p_small, recs_small)
        finally:
            p_small.close()

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
            "lexer_is_trivia": {
                "target_module": "mncs.compiler.lexer.v1",
                "target_callable": TARGET_IS_TRIVIA_ID,
                "unchanged_lexer_mncs_sha256": lexer_sha256,
                "closure_module_count": len(sources_lexer),
                "emitted_callables": [c["semantic"] for c in art1["callables"]],
                "token_record_identity": token_type_id,
                "record_project_instructions": record_project_insts,
                "function_count": emit1["function_count"],
                "block_count": emit1["block_count"],
                "instruction_count": emit1["instruction_count"],
                "record_type_count": emit1["record_type_count"],
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
                "vm_differential_cases": len(cases),
                "vm_differential_matches": matched,
                "semantic_digest": semantic_digest,
                "vm_session_wall_s": round(vm_sess.elapsed_s, 4),
                "vm_resident_peak_rss_bytes": vm_sess.peak_rss_bytes,
                "vm_max_memory_cells": vm_max_cells,
                "vm_max_steps_per_call": vm_max_steps,
                "vm_total_steps": vm_total_steps,
            },
            "bonus_lexer_is_trivia_cont": {
                "ok": emit_cont["ok"],
                "function_count": emit_cont["function_count"],
                "block_count": emit_cont["block_count"],
                "instruction_count": emit_cont["instruction_count"],
            },
            "negative_rejections": {
                "tampered_artifact_vm_admission": {"status": tamper_payload["status"], "returncode": admit_bad.returncode},
                "malformed_record_project": tamper_results,
            },
            "next_caller_probe_skip_trivia": {
                "direct_uninstantiated_skip_trivia": {
                    "target": TARGET_SKIP_TRIVIA_ID,
                    "ok": skip_direct["ok"],
                    "error_code": skip_direct["error_code"],
                    "error_function": skip_direct["error_function"],
                    "error_block": skip_direct["error_block"],
                    "error_instruction": skip_direct["error_instruction"],
                },
                "specialized_skip_trivia_32_caller": {
                    "target": "mncs:0.2:function:mncs.test.skip_trivia_caller.v1::call_skip_trivia -> lexer.skip_trivia<32>",
                    "ok": skip_specialized["ok"],
                    "error_code": skip_specialized["error_code"],
                    "error_function": skip_specialized["error_function"],
                    "error_block": skip_specialized["error_block"],
                    "error_instruction": skip_specialized["error_instruction"],
                },
            },
        }
        if args.json_out:
            args.json_out.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
