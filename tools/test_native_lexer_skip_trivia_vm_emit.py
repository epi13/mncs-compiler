#!/usr/bin/env python3
"""End-to-end differential, determinism, provider-handoff, negative-rejection,
and next-blocker proof for compiling and emitting a cross-module call to
unchanged `mncs.compiler.lexer.v1::skip_trivia<32>` through the selected-callable
VM path (`mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable`) and
independently admitting/executing it in a fresh canonical VM (`mncs-vm`).

Provenance boundary:
- Selected-target context + on-demand provider schema & verified-body handoff:
      100% MNCS (`mncs.compiler.project.v1::compile_project_target_with_context`,
      `mncs.compiler.project.v1::materialize_provider_module`).
- Targeted callable selection, RecordUpdate (kind 16 op 1) lowering,
  exact scalar SequenceConstruct (kind 18) lowering, concrete Nat
  specialization, and closure verification:
      100% MNCS (`mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable`).
- Whole-project differential control:
      100% MNCS (`mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable_whole_project`).
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
import test_native_lexer_is_trivia_vm_emit as is_trivia_mod
import test_native_lexer_punctuation_vm_emit as punct
import test_native_project_vm_emit as proj_emit
import test_native_vm_emit as single_emit
import test_sem as sem

TARGET_CALL_SKIP_TRIVIA_ID = "mncs:0.2:function:mncs.test.skip_trivia_caller.v1::call_skip_trivia"
TARGET_LEXER_SKIP_TRIVIA_ID = "mncs:0.2:function:mncs.compiler.lexer.v1::skip_trivia"

# Small two-module fixture with a nominal record + concrete Nat-specialized call,
# plus an unrelated valid source whose body is outside vm_emit's supported subset.
TWO_MOD_PROVIDER_SRC = """\
mncs 0.19;
module mncs.test.provider_pair.v1;

record Pair { left: u64, right: u64 }

fn bump<N: Nat>(buf: [byte; up_to N], p: Pair) -> (result: Pair) {
    let first: u64 = select(buf.len > 0, buf[0] as u64, 0);
    return Pair { left: p.left +% first, right: p.right +% buf.len };
}
"""

UNRELATED_OUT_OF_SUBSET_SRC = """\
mncs 0.19;
module mncs.test.unrelated_enum.v1;

enum Color { Red, Green, Blue }

fn color_code(c: Color) -> (result: u64) {
    return match c {
        Color.Red => 1,
        Color.Green => 2,
        Color.Blue => 3
    };
}
"""

TWO_MOD_ROOT_SRC = """\
mncs 0.19;
module mncs.test.pair_caller.v1;
use mncs.test.provider_pair.v1 as prov;

fn call_bump(buf: [byte; up_to 8], p: prov.Pair) -> (result: prov.Pair) {
    return prov.bump<8>(buf, p);
}
"""

# Focused fixture for RecordUpdate (kind 16 op 1) + SequenceConstruct (kind 18).
RECORD_UPDATE_AND_SEQ_SRC = """\
mncs 0.19;
module mncs.test.record_update_seq.v1;

record BoxA { a: u64, b: u64, flag: bool }
record BoxB { x: u64, y: u64, flag: bool }

fn update_and_lookup(x: BoxA, other: BoxB, idx: u64) -> (result: BoxA) {
    let table: [u64; 4] = [10, 20, 30, 40];
    let safe_idx: u64 = select(idx < table.len, idx, 0);
    let delta: u64 = table[safe_idx];
    let bumped: BoxA = BoxA { ..x, a: x.a +% delta, flag: other.flag };
    return bumped;
}
"""

# Next-frontier probes: (1) significant<32> (calls skip_trivia<32> in a loop),
# (2) next_token_global<4, 32> (paged [[byte; up_to 32]; up_to 4] nested sequence).
SIGNIFICANT_CALLER_SRC = """\
mncs 0.19;
module mncs.test.significant_caller.v1;
use mncs.compiler.lexer.v1 as lexer;

fn call_significant(text: [byte; up_to 32], start: u64) -> (result: lexer.Token) {
    return lexer.significant<32>(text, start);
}
"""

NEXT_TOKEN_GLOBAL_CALLER_SRC = """\
mncs 0.19;
module mncs.test.next_token_global_caller.v1;
use mncs.compiler.lexer.v1 as lexer;

fn call_next_token_global(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64, start: u64) -> (result: lexer.Token) {
    return lexer.next_token_global<4, 32>(pages, stride, total, start);
}
"""


def _u64_val(v):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": v}}


def _vm_byte_seq(raw: bytes):
    return {"sequence": {"values": [{"byte": {"value": b}} for b in raw]}}


def _vm_token_val(token_type_id, kind, start, end, diag):
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


def _decode_vm_token(val):
    assert "record" in val, val
    rec = val["record"]
    fields = {k: v["integer"]["value"] for k, v in rec["fields"]}
    return {
        "type_identity": rec["type_identity"],
        "name": rec["name"],
        "kind": fields["kind"],
        "start": fields["start"],
        "end": fields["end"],
        "diagnostic": fields["diagnostic"],
    }


def _build_skip_trivia_corpus():
    """Cover non-trivia passthrough, trivia->next token (ws, line/block comment,
    keywords, ident, Unicode ident/ws, numbers, operators), EOF, and diagnostics."""
    cases = [
        # --- Non-trivia passthrough (returned unchanged, regardless of text) ---
        ("passthrough_ident", b"fn x", 4, 3, 4, 0),
        ("passthrough_keyword_fn", b"fn x", 12, 0, 2, 0),
        ("passthrough_punct_arrow", b"-> y", 80, 0, 2, 0),
        ("passthrough_number", b"0.19", 6, 0, 4, 0),
        ("passthrough_eof", b"abc", 0, 3, 3, 0),
        ("passthrough_unknown", b"@", 7, 0, 1, 2),
        # Diagnostic != 0 on trivia kind must NOT be treated as trivia (passthrough!)
        ("passthrough_diag_on_block_comment", b"/* unclosed", 3, 0, 11, 1),
        ("passthrough_diag_on_ws_kind", b"  fn", 1, 0, 2, 3),

        # --- Whitespace trivia -> next significant token ---
        ("ws_to_keyword_fn", b"  fn x", 1, 0, 2, 0),
        ("ws_tab_nl_to_keyword_let", b"\t\n let a", 1, 0, 3, 0),
        ("ws_to_ident", b"   hello_1", 1, 0, 3, 0),
        ("ws_to_number_int", b" 42;", 1, 0, 1, 0),
        ("ws_to_number_version", b" 0.19;", 1, 0, 1, 0),
        ("ws_to_two_byte_arrow", b"  -> u64", 1, 0, 2, 0),
        ("ws_to_two_byte_fat_arrow", b" => 1", 1, 0, 1, 0),
        ("ws_to_two_byte_wrap_add", b" +% 2", 1, 0, 1, 0),
        ("ws_to_single_punct_semi", b" ;", 1, 0, 1, 0),
        ("ws_to_unicode_ident", "  αβ1 ".encode("utf-8"), 1, 0, 2, 0),
        ("unicode_ws_to_keyword_return", "\u00a0return".encode("utf-8"), 1, 0, 2, 0),
        ("ws_to_consecutive_block_comment", b"  /* c */ x", 1, 0, 2, 0),

        # --- Line comment trivia -> next token ---
        ("line_comment_to_keyword_match", b"// hi\nmatch", 2, 0, 5, 0),
        ("line_comment_after_nl_to_ident", b"// hi\nfoo", 1, 5, 6, 0),
        ("line_comment_utf8_to_punct", "// π\n==".encode("utf-8"), 1, 6, 7, 0),

        # --- Block comment trivia -> next token ---
        ("block_comment_nested_to_ident", b"/* a /* b */ c */foo", 3, 0, 17, 0),
        ("block_comment_to_operator", b"/* ok */==", 3, 0, 8, 0),

        # --- EOF cases ---
        ("ws_to_eof", b"   ", 1, 0, 3, 0),
        ("empty_text_ws_token_eof", b"", 1, 0, 0, 0),
        ("line_comment_to_eof", b"// eof", 2, 0, 6, 0),
        ("block_comment_to_eof", b"/* eof */", 3, 0, 9, 0),

        # --- Diagnostic cases produced by next_token after trivia ---
        ("diag_invalid_cursor_past_len", b"abc", 1, 0, 10, 0),
        ("diag_unclosed_block_comment", b" /* unclosed", 1, 0, 1, 0),
        ("diag_unknown_punct_at", b" @x", 1, 0, 1, 0),
        ("diag_invalid_utf8_lead", b" \xffabc", 1, 0, 1, 0),
        ("diag_invalid_utf8_in_block", b" /*\xff*/", 1, 0, 1, 0),
        ("diag_invalid_utf8_in_line", b" //\xff\nx", 1, 0, 1, 0),
        ("diag_non_ident_unicode_symbol", " §".encode("utf-8"), 1, 0, 1, 0),
    ]
    for label, raw, *_ in cases:
        assert len(raw) <= 32, (label, len(raw))
    return cases


def _stage0_skip_trivia_oracle(cases):
    """Run locked Stage-0 probe directly on unchanged lexer::skip_trivia<32>."""
    env = dict(os.environ)
    env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{single_emit.STDLIB_ROOT / 'library'}"
    env["MNCS_PROBE_MODULES"] = "source,lexer"
    env["MNCS_PROBE_LIBRARY_MODULES"] = "mncs.std.text_utf8.v1"
    env["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.lexer.v1"
    env["MNCS_PROBE_BACKEND"] = backend_policy.resolve("lexer")
    env["MNCS_PROBE_BACKEND_ROOTS"] = "skip_trivia"
    env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
        {
            "module": "mncs.compiler.lexer.v1",
            "function": "skip_trivia",
            "type_arguments": [sem.nat_arg(32)],
        }
    ])
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
        token_id = recs_lexer["Token"]["identity"]
        for label, raw, kind, start, end, diag in cases:
            req = {
                "schema_version": "0.1",
                "target": {"module": "mncs.compiler.lexer.v1", "function": "skip_trivia"},
                "arguments": [
                    _vm_byte_seq(raw),
                    is_trivia_mod._stage0_token_wire(recs_lexer, kind, start, end, diag),
                ],
                "type_arguments": [sem.nat_arg(32)],
                "step_budget": 1_000_000,
            }
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            assert resp.get("status") == "returned", (label, resp)
            decoded = sem.decode(resp["returned"][0])
            out[label] = {
                "type_identity": token_id,
                "name": "Token",
                "kind": decoded["kind"],
                "start": decoded["start"],
                "end": decoded["end"],
                "diagnostic": decoded["diagnostic"],
            }
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        assert proc.wait(timeout=60) == 0
    return out


def _find_inst_by_kind(proj_wire, kind, operator=None):
    for mod in proj_emit._walk_wire_flist(proj_emit._wire_field(proj_wire, "modules")):
        ssa_summary_wire = proj_emit._wire_field(mod, "value_ssa")
        for fn in proj_emit._walk_wire_flist(proj_emit._wire_field(ssa_summary_wire, "functions")):
            for blk in proj_emit._walk_wire_flist(proj_emit._wire_field(fn, "blocks")):
                for inst in proj_emit._walk_wire_flist(proj_emit._wire_field(blk, "instructions")):
                    k = proj_emit._wire_field(inst, "kind")["integer"]["value"]
                    op = proj_emit._wire_field(inst, "operator")["integer"]["value"]
                    if k == kind and (operator is None or op == operator):
                        return mod, fn, blk, inst
    raise AssertionError(f"no instruction kind={kind} op={operator} found")


def _run_small_fixture_and_negative_checks():
    """Prove:
    (a) Two-module record + concrete Nat-specialized provider handoff matches
        whole-project differential control bit-for-bit, while keeping an
        unrelated out-of-subset enum source outside the selected closure.
    (b) Focused RecordUpdate (kind 16 op 1) + SequenceConstruct (kind 18)
        emission and execution in fresh mncs-vm.
    (c) Fail-closed refusals for missing provider body, wrong nominal owner,
        mismatched specialization, and malformed RecordUpdate/SequenceConstruct.
    """
    targs64 = [sem.nat_arg(64), sem.nat_arg(1024)]
    two_prov_bytes = TWO_MOD_PROVIDER_SRC.encode("utf-8")
    two_root_bytes = TWO_MOD_ROOT_SRC.encode("utf-8")
    sources_two = [
        ("00_provider_pair.mncs", Path("00_provider_pair.mncs"), two_prov_bytes),
        ("01_pair_caller.mncs", Path("01_pair_caller.mncs"), two_root_bytes),
    ]
    sources_with_unrelated = [
        ("00_provider_pair.mncs", Path("00_provider_pair.mncs"), two_prov_bytes),
        ("01_unrelated_enum.mncs", Path("01_unrelated_enum.mncs"), UNRELATED_OUT_OF_SUBSET_SRC.encode("utf-8")),
        ("02_pair_caller.mncs", Path("02_pair_caller.mncs"), two_root_bytes),
    ]
    rec_upd_bytes = RECORD_UPDATE_AND_SEQ_SRC.encode("utf-8")
    sources_rec_upd = [("00_record_update_seq.mncs", Path("00_record_update_seq.mncs"), rec_upd_bytes)]

    p = punct.LexerCallableEmitterProbe(
        roots=[
            "compile_and_emit_project_vm_callable",
            "compile_and_emit_project_vm_callable_whole_project",
            "compile_project_for_emit",
            "compile_project_target_for_emit",
            "emit_verified_project_ssa_callable_with_sources",
        ],
        seeds=[
            {"module": "mncs.compiler.vm_emit.v1", "function": fn, "type_arguments": targs64}
            for fn in (
                "compile_and_emit_project_vm_callable",
                "compile_and_emit_project_vm_callable_whole_project",
                "compile_project_for_emit",
                "compile_project_target_for_emit",
                "emit_verified_project_ssa_callable_with_sources",
            )
        ],
    )
    try:
        recs = p.record_types("mncs.compiler.vm_emit.v1")
        call_bump_id = punct._make_semantic_id_wire(recs, "mncs:0.2:function:mncs.test.pair_caller.v1::call_bump")
        prov_bump_id = punct._make_semantic_id_wire(recs, "mncs:0.2:function:mncs.test.provider_pair.v1::bump")

        # (a) Two-module selected vs whole-project vs with unrelated out-of-subset source
        e_sel = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, sources_two, 1) + [call_bump_id],
            targs64,
        )
        assert e_sel["ok"] is True and e_sel["function_count"] == 2 and e_sel["record_type_count"] == 1, e_sel

        e_whole = p.call(
            "compile_and_emit_project_vm_callable_whole_project",
            proj_emit._build_project_args(recs, sources_two, 1) + [call_bump_id],
            targs64,
        )
        assert e_whole == e_sel, "selected-callable diverged from whole-project control"

        e_unrel = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, sources_with_unrelated, 2) + [call_bump_id],
            targs64,
        )
        assert e_unrel == e_sel, "unrelated out-of-subset source affected selected closure"

        # Execute two-module emitted artifact in fresh canonical mncs-vm
        art_two = native_vm_artifact.package_native_vm_artifact(
            e_sel, TWO_MOD_ROOT_SRC.encode("utf-8"), allow_multi_module=True,
            producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            ssa_producer_entry="mncs.compiler.project.v1::compile_project_target_with_context",
        )
        pair_rec_id = native_vm_artifact._semantic_id_str(native_vm_artifact.flist(e_sel["record_types"])[0]["identity"])
        with tempfile.TemporaryDirectory(prefix="mncs-two-mod-") as td:
            ap = Path(td) / "two_mod.vm.json"
            ap.write_text(json.dumps(art_two, sort_keys=True, separators=(",", ":")) + "\n")
            admit = subprocess.run([str(single_emit.VM_BIN), "admit", "--artifact", str(ap)], capture_output=True, text=True)
            assert admit.returncode == 0 and json.loads(admit.stdout)["status"] == "admitted", admit.stdout
            vm = single_emit.FreshVmSession(ap)
            try:
                res = vm.call(
                    "mncs.test.pair_caller.v1", "call_bump",
                    [
                        _vm_byte_seq(b"ABC"),
                        {
                            "record": {
                                "name": "Pair",
                                "type_identity": pair_rec_id,
                                "fields": [["left", _u64_val(10)], ["right", _u64_val(20)]],
                            }
                        },
                    ],
                )
                assert res["outcome"]["kind"] == "completed", res
                ret_fields = dict(res["execution"]["returned"][0]["record"]["fields"])
                assert ret_fields["left"]["integer"]["value"] == 10 + 65, ret_fields
                assert ret_fields["right"]["integer"]["value"] == 20 + 3, ret_fields
            finally:
                vm.close()

        # (b) Focused RecordUpdate + SequenceConstruct emission and VM execution
        upd_id = punct._make_semantic_id_wire(recs, "mncs:0.2:function:mncs.test.record_update_seq.v1::update_and_lookup")
        e_upd = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, sources_rec_upd, 0) + [upd_id],
            targs64,
        )
        assert e_upd["ok"] is True and e_upd["function_count"] == 1, e_upd
        art_upd = native_vm_artifact.package_native_vm_artifact(
            e_upd, rec_upd_bytes, allow_multi_module=True,
            producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            ssa_producer_entry="mncs.compiler.project.v1::compile_project_target_with_context",
        )
        rec_by_name = {
            native_vm_artifact._semantic_id_str(r["name"]): native_vm_artifact._semantic_id_str(r["identity"])
            for r in native_vm_artifact.flist(e_upd["record_types"])
        }
        with tempfile.TemporaryDirectory(prefix="mncs-rec-upd-") as td:
            ap = Path(td) / "rec_upd.vm.json"
            ap.write_text(json.dumps(art_upd, sort_keys=True, separators=(",", ":")) + "\n")
            admit = subprocess.run([str(single_emit.VM_BIN), "admit", "--artifact", str(ap)], capture_output=True, text=True)
            assert admit.returncode == 0 and json.loads(admit.stdout)["status"] == "admitted", admit.stdout
            vm = single_emit.FreshVmSession(ap)
            try:
                res = vm.call(
                    "mncs.test.record_update_seq.v1", "update_and_lookup",
                    [
                        {
                            "record": {
                                "name": "BoxA", "type_identity": rec_by_name["BoxA"],
                                "fields": [["a", _u64_val(5)], ["b", _u64_val(77)], ["flag", {"boolean": {"value": False}}]],
                            }
                        },
                        {
                            "record": {
                                "name": "BoxB", "type_identity": rec_by_name["BoxB"],
                                "fields": [["flag", {"boolean": {"value": True}}], ["x", _u64_val(1)], ["y", _u64_val(2)]],
                            }
                        },
                        _u64_val(2),
                    ],
                )
                assert res["outcome"]["kind"] == "completed", res
                rf = dict(res["execution"]["returned"][0]["record"]["fields"])
                assert rf["a"]["integer"]["value"] == 35, rf
                assert rf["b"]["integer"]["value"] == 77, rf
                assert rf["flag"]["boolean"]["value"] is True, rf
            finally:
                vm.close()

        # (c) Negative refusals:
        negatives = {}

        # c1. Direct uninstantiated generic prov::bump -> error_code 11
        r_gen = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, sources_two, 0) + [prov_bump_id],
            targs64,
        )
        assert r_gen["ok"] is False and r_gen["error_code"] == 11, r_gen
        negatives["direct_uninstantiated_generic_two_mod"] = {"ok": False, "error_code": r_gen["error_code"]}

        # c2. Missing provider body (tampered callee identity on two-module compiled ProjectResult)
        proj_two_wire = p.call_raw("compile_project_for_emit", proj_emit._build_compile_only_args(recs, sources_two), targs64)
        mut_missing = copy.deepcopy(proj_two_wire)
        _, _, _, call_inst = _find_inst_by_kind(mut_missing, 3)
        proj_emit._set_wire_field(
            call_inst, "identity",
            punct._make_semantic_id_wire(recs, "mncs:0.2:function:mncs.test.provider_pair.v1::nonexistent_body"),
        )
        r_miss = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources_two) + [mut_missing, sem.integer(1), call_bump_id],
            targs64,
        )
        assert r_miss["ok"] is False and r_miss["error_code"] == 9, r_miss
        negatives["missing_provider_body"] = {"ok": False, "error_code": r_miss["error_code"]}

        # c3. Missing provider schema + body via legacy target-only ProjectResult
        proj_target_wire = p.call_raw(
            "compile_project_target_for_emit",
            proj_emit._build_project_args(recs, sources_two, 1),
            targs64,
        )
        r_target_only = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources_two) + [proj_target_wire, sem.integer(1), call_bump_id],
            targs64,
        )
        assert r_target_only["ok"] is False and r_target_only["error_code"] == 4, r_target_only
        negatives["target_only_missing_provider_schema"] = {"ok": False, "error_code": r_target_only["error_code"]}

        # c4a. Missing concrete specialization on generic call (`prov.bump` without `<8>` -> error_code 11)
        two_root_no_spec_bytes = TWO_MOD_ROOT_SRC.replace("prov.bump<8>", "prov.bump   ").encode("utf-8")
        sources_two_no_spec = [
            ("00_provider_pair.mncs", Path("00_provider_pair.mncs"), two_prov_bytes),
            ("01_pair_caller.mncs", Path("01_pair_caller.mncs"), two_root_no_spec_bytes),
        ]
        r_spec_missing = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources_two_no_spec) + [proj_two_wire, sem.integer(1), call_bump_id],
            targs64,
        )
        assert r_spec_missing["ok"] is False and r_spec_missing["error_code"] == 11, r_spec_missing
        negatives["mismatched_specialization_missing_nat_arg"] = {
            "ok": False, "error_code": r_spec_missing["error_code"],
        }

        # c4b. Mismatched concrete specialization capacity (`<8>` replaced with `<4>` -> arg [byte; up_to 8] != param [byte; up_to 4] -> error_code 10)
        two_root_mismatch_bytes = TWO_MOD_ROOT_SRC.replace("prov.bump<8>", "prov.bump<4>").encode("utf-8")
        sources_two_mismatch = [
            ("00_provider_pair.mncs", Path("00_provider_pair.mncs"), two_prov_bytes),
            ("01_pair_caller.mncs", Path("01_pair_caller.mncs"), two_root_mismatch_bytes),
        ]
        r_spec_cap = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources_two_mismatch) + [proj_two_wire, sem.integer(1), call_bump_id],
            targs64,
        )
        assert r_spec_cap["ok"] is False and r_spec_cap["error_code"] == 10, r_spec_cap
        negatives["mismatched_specialization_capacity"] = {
            "ok": False, "error_code": r_spec_cap["error_code"],
        }

        # c5. Wrong nominal owner on RecordUpdate (BoxB nominal_identity on BoxA update)
        proj_upd_wire = p.call_raw("compile_project_for_emit", proj_emit._build_compile_only_args(recs, sources_rec_upd), targs64)
        mut_owner = copy.deepcopy(proj_upd_wire)
        _, _, _, upd_inst = _find_inst_by_kind(mut_owner, 16, operator=1)
        proj_emit._set_wire_field(
            upd_inst, "nominal_identity",
            punct._make_semantic_id_wire(recs, rec_by_name["BoxB"]),
        )
        r_owner = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources_rec_upd) + [mut_owner, sem.integer(0), upd_id],
            targs64,
        )
        assert r_owner["ok"] is False and r_owner["error_code"] in (4, 7), r_owner
        negatives["wrong_nominal_owner_record_update"] = {"ok": False, "error_code": r_owner["error_code"]}

        # c6. Malformed SequenceConstruct count mismatch (operator 5 != length 4)
        mut_seq = copy.deepcopy(proj_upd_wire)
        _, _, _, seq_inst = _find_inst_by_kind(mut_seq, 18)
        proj_emit._set_wire_field(seq_inst, "operator", sem.integer(5))
        r_seq = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources_rec_upd) + [mut_seq, sem.integer(0), upd_id],
            targs64,
        )
        assert r_seq["ok"] is False and r_seq["error_code"] == 7, r_seq
        negatives["malformed_sequence_construct_length"] = {"ok": False, "error_code": r_seq["error_code"]}

        return {
            "two_module_handoff": {
                "ok": True,
                "function_count": e_sel["function_count"],
                "block_count": e_sel["block_count"],
                "instruction_count": e_sel["instruction_count"],
                "record_type_count": e_sel["record_type_count"],
                "matches_whole_project_control": True,
                "unrelated_out_of_subset_source_excluded": True,
                "vm_execution_verified": True,
            },
            "record_update_and_seq_construct_regression": {
                "ok": True,
                "function_count": e_upd["function_count"],
                "block_count": e_upd["block_count"],
                "instruction_count": e_upd["instruction_count"],
                "vm_execution_verified": True,
            },
            "negative_rejections": negatives,
        }
    finally:
        p.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    assert single_emit.PROBE_BIN.exists(), f"probe missing: {single_emit.PROBE_BIN}"
    assert single_emit.VM_BIN.exists(), f"mncs-vm missing: {single_emit.VM_BIN}"

    # 1. Small two-module handoff + differential control + focused op regressions + negatives
    small_report = _run_small_fixture_and_negative_checks()

    stdlib_dir = single_emit.STDLIB_ROOT / "library" / "std"
    prim_path = stdlib_dir / "utf8_primitives.mncs"
    scal_path = stdlib_dir / "utf8_scalar.mncs"
    text_path = stdlib_dir / "text_utf8.mncs"
    source_path = ROOT / "src" / "compiler" / "source.mncs"
    lexer_path = ROOT / "src" / "compiler" / "lexer.mncs"

    lexer_bytes = lexer_path.read_bytes()
    lexer_sha256 = hashlib.sha256(lexer_bytes).hexdigest()
    source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
    sources_lexer = [
        ("00_utf8_primitives.mncs", prim_path, prim_path.read_bytes()),
        ("01_utf8_scalar.mncs", scal_path, scal_path.read_bytes()),
        ("02_text_utf8.mncs", text_path, text_path.read_bytes()),
        ("03_source.mncs", source_path, source_path.read_bytes()),
        ("04_lexer.mncs", lexer_path, lexer_bytes),
    ]
    root_bytes = is_trivia_mod.SKIP_TRIVIA_ROOT_SOURCE.encode("utf-8")
    sources_skip_caller = sources_lexer + [
        ("05_skip_trivia_caller.mncs", Path("05_skip_trivia_caller.mncs"), root_bytes),
    ]
    sources_skip_with_unrelated = sources_lexer + [
        ("05_unrelated_enum.mncs", Path("05_unrelated_enum.mncs"), UNRELATED_OUT_OF_SUBSET_SRC.encode("utf-8")),
        ("06_skip_trivia_caller.mncs", Path("06_skip_trivia_caller.mncs"), root_bytes),
    ]
    sources_sig_caller = sources_lexer + [
        ("05_significant_caller.mncs", Path("05_significant_caller.mncs"), SIGNIFICANT_CALLER_SRC.encode("utf-8")),
    ]
    sources_global_caller = sources_lexer + [
        ("05_next_token_global_caller.mncs", Path("05_next_token_global_caller.mncs"), NEXT_TOKEN_GLOBAL_CALLER_SRC.encode("utf-8")),
    ]
    closure_digests = [(sid, hashlib.sha256(raw).hexdigest()) for sid, _, raw in sources_skip_caller]

    emitter_src_sha256 = hashlib.sha256((ROOT / "src" / "compiler" / "vm_emit.mncs").read_bytes()).hexdigest()
    project_src_sha256 = hashlib.sha256((ROOT / "src" / "compiler" / "project.mncs").read_bytes()).hexdigest()
    compiler_rev = single_emit._git_head(ROOT)
    vm_rev = single_emit._git_head(single_emit.VM_REPO)
    stage0_rev = single_emit._stage0_lock_rev()
    targs256 = [sem.nat_arg(256), sem.nat_arg(1024)]

    with tempfile.TemporaryDirectory(prefix="mncs-lexer-skip-trivia-vm-") as tmpdir:
        tmp = Path(tmpdir)

        # 2. Process 1: Emit unchanged lexer::skip_trivia<32> via selected-callable path
        #    + verify unrelated out-of-subset source exclusion
        #    + verify direct uninstantiated lexer::skip_trivia refusal (error_code 11)
        #    + probe next callers (significant<32>, next_token_global<4, 32>)
        t1 = time.monotonic()
        p1 = punct.LexerCallableEmitterProbe()
        try:
            recs1 = p1.record_types("mncs.compiler.vm_emit.v1")
            target_id1 = punct._make_semantic_id_wire(recs1, TARGET_CALL_SKIP_TRIVIA_ID)

            emit1 = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, sources_skip_caller, 5) + [target_id1],
                targs256,
            )
            assert emit1["ok"] is True, emit1

            emit_unrel = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, sources_skip_with_unrelated, 6) + [target_id1],
                targs256,
            )
            assert emit_unrel == emit1, "unrelated out-of-subset source changed skip_trivia emission!"

            direct_gen = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, sources_lexer, 4)
                + [punct._make_semantic_id_wire(recs1, TARGET_LEXER_SKIP_TRIVIA_ID)],
                targs256,
            )
            assert direct_gen["ok"] is False and direct_gen["error_code"] == 11, direct_gen

            probe_sig = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, sources_sig_caller, 5)
                + [punct._make_semantic_id_wire(recs1, "mncs:0.2:function:mncs.test.significant_caller.v1::call_significant")],
                targs256,
            )
            probe_global = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, sources_global_caller, 5)
                + [punct._make_semantic_id_wire(recs1, "mncs:0.2:function:mncs.test.next_token_global_caller.v1::call_next_token_global")],
                targs256,
            )
        finally:
            p1.close()
        emit1_wall_s = time.monotonic() - t1

        # 3. Process 2: Fresh independent process for bit-for-bit determinism
        t2 = time.monotonic()
        p2 = punct.LexerCallableEmitterProbe()
        try:
            recs2 = p2.record_types("mncs.compiler.vm_emit.v1")
            emit2 = p2.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs2, sources_skip_caller, 5)
                + [punct._make_semantic_id_wire(recs2, TARGET_CALL_SKIP_TRIVIA_ID)],
                targs256,
            )
            assert emit2["ok"] is True, emit2
        finally:
            p2.close()
        emit2_wall_s = time.monotonic() - t2

        art1 = native_vm_artifact.package_native_vm_artifact(
            emit1, root_bytes, allow_multi_module=True,
            producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            ssa_producer_entry="mncs.compiler.project.v1::compile_project_target_with_context",
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
            closure_source_digests=closure_digests,
        )
        art2 = native_vm_artifact.package_native_vm_artifact(
            emit2, root_bytes, allow_multi_module=True,
            producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            ssa_producer_entry="mncs.compiler.project.v1::compile_project_target_with_context",
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
            closure_source_digests=closure_digests,
        )
        art1_bytes = (json.dumps(art1, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        art2_bytes = (json.dumps(art2, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        assert art1_bytes == art2_bytes, "non-deterministic lexer::skip_trivia<32> artifact emission"
        assert art1["artifact_id"] == art2["artifact_id"]
        art_sha256 = hashlib.sha256(art1_bytes).hexdigest()

        ssa_mod = punct._extract_dag_module(art1)
        punct._assert_no_symbolic_bounds(ssa_mod)
        rec_by_name = {
            native_vm_artifact._semantic_id_str(r["name"]): native_vm_artifact._semantic_id_str(r["identity"])
            for r in native_vm_artifact.flist(emit1["record_types"])
        }
        assert "Token" in rec_by_name, rec_by_name
        token_type_id = rec_by_name["Token"]

        # 4. Independent admission + tampered refusal in fresh canonical mncs-vm
        art_path = tmp / "lexer_skip_trivia_32.vm.json"
        art_path.write_bytes(art1_bytes)
        admit = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(art_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit.returncode == 0, (admit.returncode, admit.stdout, admit.stderr)
        admit_payload = json.loads(admit.stdout)
        assert admit_payload["status"] == "admitted", admit_payload

        tampered_path = tmp / "lexer_skip_trivia_32_tampered.vm.json"
        tampered = json.loads(art1_bytes.decode("utf-8"))
        tampered["callables"][0]["name"] = "tampered_call_skip_trivia"
        tampered_path.write_text(json.dumps(tampered, sort_keys=True) + "\n")
        admit_bad = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(tampered_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit_bad.returncode != 0, admit_bad.stdout
        tamper_payload = json.loads(admit_bad.stdout)
        assert tamper_payload["status"] == "refused", tamper_payload

        # 5. Differential execution in fresh canonical mncs-vm vs locked Stage-0
        cases = _build_skip_trivia_corpus()
        oracle_results = _stage0_skip_trivia_oracle(cases)

        vm_sess = single_emit.FreshVmSession(art_path)
        vm_max_cells = 0
        vm_max_steps = 0
        vm_total_steps = 0
        matched = 0
        direct_generic_matched = False
        uncompiled_spec16_refused = False
        try:
            for label, raw, kind, start, end, diag in cases:
                res = vm_sess.call(
                    "mncs.test.skip_trivia_caller.v1",
                    "call_skip_trivia",
                    [_vm_byte_seq(raw), _vm_token_val(token_type_id, kind, start, end, diag)],
                )
                assert res["outcome"]["kind"] == "completed", (label, res)
                got = _decode_vm_token(res["execution"]["returned"][0])
                expected = oracle_results[label]
                assert got == expected, (label, expected, got)
                usage = res["record"]["usage"]
                vm_max_cells = max(vm_max_cells, usage["max_memory_cells"])
                vm_max_steps = max(vm_max_steps, usage["steps"])
                vm_total_steps += usage["steps"]
                matched += 1

            # Direct generic dispatch lexer::skip_trivia<32> must match call_skip_trivia
            direct_res = vm_sess.call(
                "mncs.compiler.lexer.v1",
                "skip_trivia",
                [_vm_byte_seq(b"  fn x"), _vm_token_val(token_type_id, 1, 0, 2, 0)],
                type_arguments=[sem.nat_arg(32)],
            )
            assert direct_res["outcome"]["kind"] == "completed", direct_res
            assert _decode_vm_token(direct_res["execution"]["returned"][0]) == oracle_results["ws_to_keyword_fn"]
            direct_generic_matched = True

            # Uncompiled specialization lexer::skip_trivia<16> must be refused by mncs-vm
            bad_spec = vm_sess.call(
                "mncs.compiler.lexer.v1",
                "skip_trivia",
                [_vm_byte_seq(b"  fn x"), _vm_token_val(token_type_id, 1, 0, 2, 0)],
                type_arguments=[sem.nat_arg(16)],
                expect_ok=False,
            )
            assert (not bad_spec.get("ok")) or bad_spec["result"]["outcome"]["kind"] != "completed", bad_spec
            uncompiled_spec16_refused = True
        finally:
            vm_sess.close()
        assert matched == len(cases)
        semantic_digest = hashlib.sha256(json.dumps(oracle_results, sort_keys=True).encode()).hexdigest()

        summary = {
            "status": "passed",
            "provenance": {
                "callable_selection_and_lowering": "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
                "native_project_ssa_producer": "mncs.compiler.project.v1::compile_project_target_with_context",
                "provider_body_materializer": "mncs.compiler.project.v1::materialize_provider_module",
                "whole_project_differential_control": "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable_whole_project",
                "transport_codec_and_sealer": "tools/native_vm_artifact.py",
                "independent_vm_admission_and_execution": str(single_emit.VM_BIN),
                "behavioral_oracle": f"locked Stage-0 mncs-compiler-stage0-probe ({stage0_rev})",
                "compiler_revision": compiler_rev,
                "vm_revision": vm_rev,
                "emitter_source_sha256": emitter_src_sha256,
                "project_source_sha256": project_src_sha256,
            },
            "small_fixture_and_negatives": small_report,
            "lexer_skip_trivia_32": {
                "target_callable": TARGET_CALL_SKIP_TRIVIA_ID,
                "target_specialization": "mncs.compiler.lexer.v1::skip_trivia<32>",
                "unchanged_lexer_mncs_sha256": lexer_sha256,
                "unchanged_source_mncs_sha256": source_sha256,
                "emitted_callables": [c["semantic"] for c in art1["callables"]],
                "emitted_record_schemas": sorted(rec_by_name.keys()),
                "token_record_identity": token_type_id,
                "function_count": emit1["function_count"],
                "block_count": emit1["block_count"],
                "instruction_count": emit1["instruction_count"],
                "record_type_count": emit1["record_type_count"],
                "symbolic_bounds_remaining": 0,
                "unrelated_out_of_subset_source_excluded": True,
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
                "tampered_artifact_vm_admission": {"status": tamper_payload["status"], "returncode": admit_bad.returncode},
                "vm_differential_cases": len(cases),
                "vm_differential_matches": matched,
                "semantic_digest": semantic_digest,
                "vm_direct_generic_dispatch_matched": direct_generic_matched,
                "vm_uncompiled_spec16_refused": uncompiled_spec16_refused,
                "vm_session_wall_s": round(vm_sess.elapsed_s, 4),
                "vm_resident_peak_rss_bytes": vm_sess.peak_rss_bytes,
                "vm_max_memory_cells": vm_max_cells,
                "vm_max_steps_per_call": vm_max_steps,
                "vm_total_steps": vm_total_steps,
                "direct_uninstantiated_lexer_skip_trivia_refusal": {
                    "ok": direct_gen["ok"],
                    "error_code": direct_gen["error_code"],
                    "error_function": direct_gen["error_function"],
                },
            },
            "next_frontier_probes": {
                "lexer_significant_32": {
                    k: probe_sig[k]
                    for k in ("ok", "error_code", "error_function", "error_block", "error_instruction", "function_count", "block_count", "instruction_count")
                },
                "lexer_next_token_global_4_32": {
                    k: probe_global[k]
                    for k in ("ok", "error_code", "error_function", "error_block", "error_instruction", "function_count", "block_count", "instruction_count")
                },
            },
        }
        if args.json_out:
            args.json_out.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
