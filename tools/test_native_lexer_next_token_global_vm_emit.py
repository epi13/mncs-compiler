#!/usr/bin/env python3
"""Differential & determinism harness for unchanged
`mncs.compiler.source.v1::byte_at_global<4,32>` and
`mncs.compiler.lexer.v1::next_token_global<4,32>` via the native selected-callable
Project SSA -> VM path (`compile_and_emit_project_vm_callable`).

Proves:
  1. Checkpoint 1: unchanged `source::byte_at_global<4,32>`, `<8,32>`, `<4,16>`
     emit with distinct specialization keys (changing outer or inner bound alone
     cannot alias), pass fresh canonical `mncs-vm` admission, and match locked
     Stage-0 (`6a3f6907`).
  2. Unchanged `lexer::next_token_global<4,32>` emits bit-for-bit deterministically
     across two fresh independent processes, passes fresh canonical `mncs-vm`
     admission (with tampered artifact refused), matches locked Stage-0 across
     single-page, cross-page, split valid/invalid UTF-8, short final page,
     start == total, start > total, zero stride, and missing-page cases, lexes
     the same logical source identically under two distinct page layouts through
     EOF, supports direct generic `<4,32>` dispatch, and refuses uncompiled
     `<8,32>` and `<4,16>`.
  3. Focused fail-closed rejections (wrong inner element type, wrong outer bound,
     wrong inner bound, non-scalar inner element, malformed projection, direct
     uninstantiated generic), recording which layer rejects each case.
  4. Next-frontier probes for unchanged `lexer::significant_global<4,32>`,
     `parser::scan_header_global<4,32>`, `parser::header_global<4,32>`, and
     `decl::parse_unit_signatures<4,32>`, distinguishing emission, admission,
     and execution.
"""

import argparse
import copy
import hashlib
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import backend_policy
import native_vm_artifact
import test_native_lexer_is_trivia_vm_emit as is_trivia_mod
import test_native_lexer_punctuation_vm_emit as punct
import test_native_project_vm_emit as proj_emit
import test_native_vm_emit as single_emit
import test_sem as sem

TARGET_BYTE_AT_4_32 = "mncs:0.2:function:mncs.test.byte_at_global_caller.v1::call_4_32"
TARGET_BYTE_AT_8_32 = "mncs:0.2:function:mncs.test.byte_at_global_caller.v1::call_8_32"
TARGET_BYTE_AT_4_16 = "mncs:0.2:function:mncs.test.byte_at_global_caller.v1::call_4_16"
TARGET_NTG_CALLER = "mncs:0.2:function:mncs.test.next_token_global_caller.v1::call_next_token_global"
TARGET_LEXER_NTG_DIRECT = "mncs:0.2:function:mncs.compiler.lexer.v1::next_token_global"
TARGET_SIG_GLOBAL_CALLER = "mncs:0.2:function:mncs.test.sig_global_caller.v1::call_significant_global"
TARGET_SCAN_HDR_CALLER = "mncs:0.2:function:mncs.test.scan_header_global_caller.v1::call_scan_header_global"
TARGET_HDR_GLOBAL_CALLER = "mncs:0.2:function:mncs.test.header_global_caller.v1::call_header_global"
TARGET_LEX_BATCH_CALLER = "mncs:0.2:function:mncs.test.lex_batch_caller.v1::call_lex_batch_from"

BYTE_AT_CALLER_SRC = """\
mncs 0.19;
module mncs.test.byte_at_global_caller.v1;
use mncs.compiler.source.v1 as src;

fn call_4_32(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64, offset: u64) -> (result: u64) {
    return src.byte_at_global<4, 32>(pages, stride, total, offset);
}
fn call_8_32(pages: [[byte; up_to 32]; up_to 8], stride: u64, total: u64, offset: u64) -> (result: u64) {
    return src.byte_at_global<8, 32>(pages, stride, total, offset);
}
fn call_4_16(pages: [[byte; up_to 16]; up_to 4], stride: u64, total: u64, offset: u64) -> (result: u64) {
    return src.byte_at_global<4, 16>(pages, stride, total, offset);
}
"""

NTG_CALLER_SRC = """\
mncs 0.19;
module mncs.test.next_token_global_caller.v1;
use mncs.compiler.lexer.v1 as lexer;

fn call_next_token_global(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64, start: u64) -> (result: lexer.Token) {
    return lexer.next_token_global<4, 32>(pages, stride, total, start);
}
"""

SIG_GLOBAL_CALLER_SRC = """\
mncs 0.19;
module mncs.test.sig_global_caller.v1;
use mncs.compiler.lexer.v1 as lexer;

fn call_significant_global(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64, start: u64) -> (result: lexer.Token) {
    return lexer.significant_global<4, 32>(pages, stride, total, start);
}
"""

SCAN_HDR_CALLER_SRC = """\
mncs 0.19;
module mncs.test.scan_header_global_caller.v1;
use mncs.compiler.parser.v1 as parser;

fn call_scan_header_global(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64, initial: parser.Header) -> (result: parser.Header) {
    return parser.scan_header_global<4, 32>(pages, stride, total, initial);
}
"""

HDR_GLOBAL_CALLER_SRC = """\
mncs 0.19;
module mncs.test.header_global_caller.v1;
use mncs.compiler.parser.v1 as parser;

fn call_header_global(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64) -> (result: parser.Header) {
    return parser.header_global<4, 32>(pages, stride, total);
}
"""

LEX_BATCH_CALLER_SRC = """\
mncs 0.19;
module mncs.test.lex_batch_caller.v1;
use mncs.compiler.lexer.v1 as lexer;
use mncs.compiler.segment.v1 as seg;

fn call_lex_batch_from(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64, start: u64) -> (result: lexer.BatchResult) {
    return seg.lex_batch_from<4, 32>(pages, stride, total, start);
}
"""

SMALL_NESTED_PROV_SRC = """\
mncs 0.19;
module mncs.test.nested_prov.v1;

fn read_byte<P: Nat, N: Nat>(pages: [[byte; up_to N]; up_to P], pi: u64, bi: u64) -> (result: u64) {
    if pi >= pages.len { return 256; }
    let pg: [byte; up_to N] = pages[pi];
    if bi >= pg.len { return 256; }
    return pg[bi] as u64;
}
"""

SMALL_NESTED_CALLER_SRC = """\
mncs 0.19;
module mncs.test.nested_caller.v1;
use mncs.test.nested_prov.v1 as prov;

fn go(pages: [[byte; up_to 8]; up_to 2], pi: u64, bi: u64) -> (result: u64) {
    return prov.read_byte<2, 8>(pages, pi, bi);
}
"""

NEG_WRONG_INNER_ELEM_SRC = """\
mncs 0.19;
module mncs.test.neg_wrong_inner_elem.v1;
use mncs.test.nested_prov.v1 as prov;

fn bad_elem(pages: [[u64; up_to 8]; up_to 2], pi: u64, bi: u64) -> (result: u64) {
    return prov.read_byte<2, 8>(pages, pi, bi);
}
"""

NEG_NONSCALAR_INNER_SRC = """\
mncs 0.19;
module mncs.test.neg_nonscalar_inner.v1;
record Box { v: u64 }

fn bad_rec_seq(pages: [[Box; up_to 4]; up_to 2]) -> (result: u64) {
    return pages.len;
}
"""


def _u64_val(v):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": v}}


def _vm_byte_seq(raw: bytes):
    return {"sequence": {"values": [{"byte": {"value": b}} for b in raw]}}


def _vm_pages(page_list):
    return {"sequence": {"values": [_vm_byte_seq(p) for p in page_list]}}


def _chunk_pages(raw: bytes, stride: int):
    if not raw:
        return []
    return [raw[i:i + stride] for i in range(0, len(raw), stride)]


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


def _decode_vm_header(val):
    assert "record" in val, val
    rec = val["record"]
    fields = {k: v["integer"]["value"] for k, v in rec["fields"]}
    return {
        "code": fields["code"],
        "start": fields["start"],
        "end": fields["end"],
        "version_start": fields["version_start"],
        "version_end": fields["version_end"],
        "module_start": fields["module_start"],
        "module_end": fields["module_end"],
        "next_offset": fields["next_offset"],
        "state": fields["state"],
    }


def _vm_header_seed(header_type_id):
    return {
        "record": {
            "name": "Header",
            "type_identity": header_type_id,
            "fields": [
                ["code", _u64_val(0)],
                ["end", _u64_val(0)],
                ["module_end", _u64_val(0)],
                ["module_start", _u64_val(0)],
                ["next_offset", _u64_val(0)],
                ["start", _u64_val(0)],
                ["state", _u64_val(0)],
                ["version_end", _u64_val(0)],
                ["version_start", _u64_val(0)],
            ],
        }
    }


def _stage0_byte_at_global_oracle(cases, p_cap, n_cap):
    env = dict(os.environ)
    env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{single_emit.STDLIB_ROOT / 'library'}"
    env["MNCS_PROBE_MODULES"] = "source"
    env["MNCS_PROBE_LIBRARY_MODULES"] = "mncs.std.text_utf8.v1"
    env["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.source.v1"
    env["MNCS_PROBE_BACKEND"] = backend_policy.resolve("source")
    env["MNCS_PROBE_BACKEND_ROOTS"] = "byte_at_global"
    env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
        {
            "module": "mncs.compiler.source.v1",
            "function": "byte_at_global",
            "type_arguments": [sem.nat_arg(p_cap), sem.nat_arg(n_cap)],
        }
    ])
    env["MNCS_PROBE_CACHE_DIR"] = str(single_emit.CACHE_DIR)
    env["MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES"] = str(single_emit.MAX_AS_BYTES)
    proc = subprocess.Popen(
        [str(single_emit.PROBE_BIN)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=ROOT, env=env,
    )
    out = {}
    try:
        for label, pages, stride, total, offset in cases:
            req = {
                "schema_version": "0.1",
                "target": {"module": "mncs.compiler.source.v1", "function": "byte_at_global"},
                "arguments": [_vm_pages(pages), _u64_val(stride), _u64_val(total), _u64_val(offset)],
                "type_arguments": [sem.nat_arg(p_cap), sem.nat_arg(n_cap)],
                "step_budget": 1_000_000,
            }
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            assert resp.get("status") == "returned", (label, resp)
            out[label] = sem.decode(resp["returned"][0])
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        assert proc.wait(timeout=60) == 0
    return out


def _stage0_lexer_global_oracle(cases, stream_layouts):
    """Run locked Stage-0 probe directly on unchanged lexer::next_token_global<4,32>
    and lexer::significant_global<4,32>."""
    env = dict(os.environ)
    env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{single_emit.STDLIB_ROOT / 'library'}"
    env["MNCS_PROBE_MODULES"] = "source,lexer"
    env["MNCS_PROBE_LIBRARY_MODULES"] = "mncs.std.text_utf8.v1"
    env["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.lexer.v1"
    env["MNCS_PROBE_BACKEND"] = backend_policy.resolve("lexer")
    env["MNCS_PROBE_BACKEND_ROOTS"] = "next_token_global,significant_global"
    env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
        {"module": "mncs.compiler.lexer.v1", "function": fn, "type_arguments": [sem.nat_arg(4), sem.nat_arg(32)]}
        for fn in ("next_token_global", "significant_global")
    ])
    env["MNCS_PROBE_CACHE_DIR"] = str(single_emit.CACHE_DIR)
    env["MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES"] = str(single_emit.MAX_AS_BYTES)
    proc = subprocess.Popen(
        [str(single_emit.PROBE_BIN)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=ROOT, env=env,
    )
    single_out = {}
    streams_out = {}
    sig_sample = None
    try:
        proc.stdin.write(json.dumps({"record_types": "mncs.compiler.lexer.v1"}) + "\n")
        proc.stdin.flush()
        recs_lexer = {r["name"]: r for r in json.loads(proc.stdout.readline())}
        token_id = recs_lexer["Token"]["identity"]

        def call_lex(fn_name, pages, stride, total, start):
            req = {
                "schema_version": "0.1",
                "target": {"module": "mncs.compiler.lexer.v1", "function": fn_name},
                "arguments": [_vm_pages(pages), _u64_val(stride), _u64_val(total), _u64_val(start)],
                "type_arguments": [sem.nat_arg(4), sem.nat_arg(32)],
                "step_budget": 1_000_000,
            }
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            assert resp.get("status") == "returned", (fn_name, resp)
            d = sem.decode(resp["returned"][0])
            return {
                "type_identity": token_id,
                "name": "Token",
                "kind": d["kind"],
                "start": d["start"],
                "end": d["end"],
                "diagnostic": d["diagnostic"],
            }

        for label, pages, stride, total, start in cases:
            single_out[label] = call_lex("next_token_global", pages, stride, total, start)

        for layout_name, pages, stride, total in stream_layouts:
            toks = []
            cur = 0
            for _ in range(128):
                tok = call_lex("next_token_global", pages, stride, total, cur)
                toks.append(tok)
                if tok["kind"] == 0 or tok["end"] <= cur:
                    break
                cur = tok["end"]
            streams_out[layout_name] = toks

        sig_sample = call_lex("significant_global", [b"  /* hi */ ", b"module a;"], 11, 20, 0)
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        assert proc.wait(timeout=60) == 0
    return single_out, streams_out, sig_sample


def _build_next_token_global_corpus():
    """Required cases covering single-page, cross-page, split valid & invalid UTF-8,
    short final page, start == total, start > total, zero stride, and missing page."""
    cases = [
        # --- Wholly inside a page ---
        ("inside_page_keyword_fn", [b"fn foo(x: u64)"], 16, 14, 0),
        ("inside_page_ident", [b"fn foo(x: u64)"], 16, 14, 3),
        ("inside_page_version", [b"mncs 0.19;"], 16, 10, 5),
        ("inside_page_arrow", [b"-> u64"], 16, 6, 0),
        ("inside_page_ws", [b"   abc"], 16, 6, 0),
        ("inside_page_line_comment", [b"// hi\nfn"], 16, 8, 0),
        ("inside_page_block_comment", [b"/* ok */fn"], 16, 10, 0),
        ("inside_second_page_ident", [b"aaaaaaaa", b"beta_123"], 8, 16, 8),

        # --- Spanning pages ---
        ("span_pages_ident", [b"  hello_", b"world_99;"], 8, 17, 2),
        ("span_pages_keyword_module", [b"  mod", b"ule a"], 5, 10, 2),
        ("span_pages_keyword_return", [b"ret", b"urn 1"], 3, 8, 0),
        ("span_pages_two_byte_arrow", [b"   -", b"> u64"], 4, 9, 3),
        ("span_pages_two_byte_wrap_add", [b"  +", b"% 1"], 3, 6, 2),
        ("span_pages_version_number", [b"  0.", b"19;"], 4, 7, 2),
        ("span_pages_block_comment_3pages", [b"/* a ", b"/* b */ ", b"c */x"], 5, 18, 0),
        ("span_pages_line_comment", [b"// hel", b"lo world\nfn"], 6, 17, 0),
        ("span_pages_whitespace", [b"   ", b"  \t\nfn"], 3, 9, 0),
        ("span_pages_long_word_3pages", [b"alpha_", b"beta_g", b"amma_9;"], 6, 19, 0),

        # --- Valid UTF-8 split at page boundary ---
        # Greek alpha (U+03B1 = CE B1) split between page 0 and page 1 inside ident
        ("utf8_valid_2byte_split_ident", [b"a\xce", b"\xb1b "], 2, 5, 0),
        # Greek alpha starting at boundary
        ("utf8_valid_2byte_split_start", [b" \xce", b"\xb1\xce\xb2;"], 2, 6, 1),
        # EM SPACE U+2003 (E2 80 83) split 1+2 across page boundary (Unicode whitespace)
        ("utf8_valid_3byte_split_ws", [b" \xe2", b"\x80\x83fn"], 2, 6, 0),
        # CJK U+4E2D (E4 B8 AD) split 2+1 across page boundary (Unicode ident)
        ("utf8_valid_3byte_split_ident", [b"\xe4\xb8", b"\xad_1;"], 2, 6, 0),
        # 4-byte scalar U+10400 Deseret Capital Letter Long I (F0 90 90 80) split 2+2 (alphabetic)
        ("utf8_valid_4byte_split_ident", [b"\xf0\x90", b"\x90\x80x;"], 2, 6, 0),
        # Valid UTF-8 inside block comment split across boundary
        ("utf8_valid_split_in_block_comment", [b"/* \xcf", b"\x80 */"], 4, 9, 0),

        # --- Malformed UTF-8 split at page boundary ---
        # Lead 0xCE at end of page 0, non-continuation 'A' at start of page 1
        ("utf8_invalid_2byte_split_bad_cont", [b"\xce", b"A"], 1, 2, 0),
        # Overlong 3-byte E0 80 AF split 1+2
        ("utf8_invalid_3byte_overlong_split", [b"\xe0", b"\x80\xaf"], 1, 3, 0),
        # Truncated 3-byte E4 B8 at EOF across boundary
        ("utf8_invalid_truncated_at_eof", [b"a\xe4", b"\xb8"], 2, 3, 1),
        # Invalid UTF-8 inside line comment split across page boundary
        ("utf8_invalid_split_in_line_comment", [b"// \xc3", b"(\n"], 4, 6, 0),
        # Invalid UTF-8 inside block comment split across page boundary
        ("utf8_invalid_split_in_block_comment", [b"/* \xe2\x80", b"X*/"], 5, 8, 0),

        # --- Short final page ---
        ("short_final_page_inside", [b"fn a() {", b" x"], 8, 10, 9),
        ("short_final_page_spanning", [b"   modul", b"e"], 8, 9, 3),

        # --- Edge / malformed layout inputs ---
        ("start_eq_total_nonempty", [b"fn x"], 4, 4, 4),
        ("start_eq_total_empty", [], 4, 0, 0),
        ("start_gt_total", [b"fn x"], 4, 4, 9),
        ("zero_stride", [b"fn x"], 0, 4, 0),
        ("missing_page_direct", [b"abcdefgh"], 8, 16, 8),
        ("missing_page_empty_pages", [], 8, 4, 0),
        ("missing_page_short_page0_intra_past_len", [b"ab"], 8, 8, 5),
        ("unclosed_block_comment_spanning", [b"/* a ", b"b c"], 5, 8, 0),
    ]
    for label, pages, stride, total, start in cases:
        assert len(pages) <= 4, (label, len(pages))
        for pg in pages:
            assert len(pg) <= 32, (label, len(pg))
    return cases


def _find_all_insts_by_kind(proj_wire, kind):
    found = []
    for mod in proj_emit._walk_wire_flist(proj_emit._wire_field(proj_wire, "modules")):
        ssa_summary_wire = proj_emit._wire_field(mod, "value_ssa")
        for fn in proj_emit._walk_wire_flist(proj_emit._wire_field(ssa_summary_wire, "functions")):
            for blk in proj_emit._walk_wire_flist(proj_emit._wire_field(fn, "blocks")):
                for inst in proj_emit._walk_wire_flist(proj_emit._wire_field(blk, "instructions")):
                    if proj_emit._wire_field(inst, "kind")["integer"]["value"] == kind:
                        found.append((mod, fn, blk, inst))
    return found


def _run_focused_rejections():
    """Verify fail-closed rejections for wrong inner element type, wrong outer/inner
    capacities, non-scalar inner element, and malformed SequenceProject, recording
    the exact layer that rejects each case."""
    targs64 = [sem.nat_arg(64), sem.nat_arg(1024)]
    prov_b = SMALL_NESTED_PROV_SRC.encode("utf-8")
    caller_b = SMALL_NESTED_CALLER_SRC.encode("utf-8")
    sources_ok = [
        ("00_nested_prov.mncs", Path("00_nested_prov.mncs"), prov_b),
        ("01_nested_caller.mncs", Path("01_nested_caller.mncs"), caller_b),
    ]
    p = punct.LexerCallableEmitterProbe(
        roots=[
            "compile_and_emit_project_vm_callable",
            "compile_project_for_emit",
            "emit_verified_project_ssa_callable_with_sources",
        ],
        seeds=[
            {"module": "mncs.compiler.vm_emit.v1", "function": fn, "type_arguments": targs64}
            for fn in (
                "compile_and_emit_project_vm_callable",
                "compile_project_for_emit",
                "emit_verified_project_ssa_callable_with_sources",
            )
        ],
    )
    out = {}
    try:
        recs = p.record_types("mncs.compiler.vm_emit.v1")
        go_id = punct._make_semantic_id_wire(recs, "mncs:0.2:function:mncs.test.nested_caller.v1::go")

        # Baseline valid 2-module nested sequence fixture compiles cleanly
        e_ok = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, sources_ok, 1) + [go_id],
            targs64,
        )
        assert e_ok["ok"] is True and e_ok["function_count"] == 2, e_ok
        proj_ok_wire = p.call_raw("compile_project_for_emit", proj_emit._build_compile_only_args(recs, sources_ok), targs64)

        # 1a. Wrong inner element type end-to-end (`[[u64; up_to 8]; up_to 2]` passed to `read_byte<2, 8>`)
        #     Keep same byte length for caller signature swap in vm_emit check too!
        srcs_wrong_elem_e2e = [
            ("00_nested_prov.mncs", Path("00_nested_prov.mncs"), prov_b),
            ("01_bad_elem.mncs", Path("01_bad_elem.mncs"), NEG_WRONG_INNER_ELEM_SRC.encode("utf-8")),
        ]
        bad_elem_id = punct._make_semantic_id_wire(recs, "mncs:0.2:function:mncs.test.neg_wrong_inner_elem.v1::bad_elem")
        r_elem_e2e = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, srcs_wrong_elem_e2e, 1) + [bad_elem_id],
            targs64,
        )
        assert r_elem_e2e["ok"] is False and r_elem_e2e["error_code"] in (1, 10), r_elem_e2e
        out["wrong_inner_element_e2e"] = {
            "ok": False,
            "error_code": r_elem_e2e["error_code"],
            "rejecting_layer": "project_decl_proof" if r_elem_e2e["error_code"] == 1 else "vm_emit_closure_signature_check",
        }

        # 1b. Wrong inner element type at vm_emit layer (caller source has `[[u64 ; up_to 8]; up_to 2]` -> error_code 10)
        caller_u64_b = SMALL_NESTED_CALLER_SRC.replace("[byte; up_to 8]", "[u64 ; up_to 8]").encode("utf-8")
        assert len(caller_u64_b) == len(caller_b)
        srcs_u64_vm = [
            ("00_nested_prov.mncs", Path("00_nested_prov.mncs"), prov_b),
            ("01_nested_caller.mncs", Path("01_nested_caller.mncs"), caller_u64_b),
        ]
        r_elem_vm = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, srcs_u64_vm) + [proj_ok_wire, sem.integer(1), go_id],
            targs64,
        )
        assert r_elem_vm["ok"] is False and r_elem_vm["error_code"] == 10, r_elem_vm
        out["wrong_inner_element_vm_emit"] = {
            "ok": False, "error_code": r_elem_vm["error_code"], "rejecting_layer": "vm_emit_closure_signature_check",
        }

        # 2. Wrong outer capacity (`<3, 8>` vs caller `up_to 2` -> vm_emit error_code 10)
        caller_bad_outer_b = SMALL_NESTED_CALLER_SRC.replace("prov.read_byte<2, 8>", "prov.read_byte<3, 8>").encode("utf-8")
        srcs_bad_outer = [
            ("00_nested_prov.mncs", Path("00_nested_prov.mncs"), prov_b),
            ("01_nested_caller.mncs", Path("01_nested_caller.mncs"), caller_bad_outer_b),
        ]
        r_outer_vm = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, srcs_bad_outer) + [proj_ok_wire, sem.integer(1), go_id],
            targs64,
        )
        assert r_outer_vm["ok"] is False and r_outer_vm["error_code"] == 10, r_outer_vm
        out["wrong_outer_capacity_vm_emit"] = {
            "ok": False, "error_code": r_outer_vm["error_code"], "rejecting_layer": "vm_emit_closure_signature_check",
        }

        # 3. Wrong inner capacity (`<2, 4>` vs caller `up_to 8` -> vm_emit error_code 10)
        caller_bad_inner_b = SMALL_NESTED_CALLER_SRC.replace("prov.read_byte<2, 8>", "prov.read_byte<2, 4>").encode("utf-8")
        srcs_bad_inner = [
            ("00_nested_prov.mncs", Path("00_nested_prov.mncs"), prov_b),
            ("01_nested_caller.mncs", Path("01_nested_caller.mncs"), caller_bad_inner_b),
        ]
        r_inner_vm = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, srcs_bad_inner) + [proj_ok_wire, sem.integer(1), go_id],
            targs64,
        )
        assert r_inner_vm["ok"] is False and r_inner_vm["error_code"] == 10, r_inner_vm
        out["wrong_inner_capacity_vm_emit"] = {
            "ok": False, "error_code": r_inner_vm["error_code"], "rejecting_layer": "vm_emit_closure_signature_check",
        }

        # 4. Non-scalar inner element (`[[Box; up_to 4]; up_to 2]` -> vm_emit type parse error_code 4)
        srcs_nonscalar = [("00_nonscalar.mncs", Path("00_nonscalar.mncs"), NEG_NONSCALAR_INNER_SRC.encode("utf-8"))]
        bad_rec_id = punct._make_semantic_id_wire(recs, "mncs:0.2:function:mncs.test.neg_nonscalar_inner.v1::bad_rec_seq")
        r_nonscalar = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, srcs_nonscalar, 0) + [bad_rec_id],
            targs64,
        )
        assert r_nonscalar["ok"] is False and r_nonscalar["error_code"] == 4, r_nonscalar
        out["nonscalar_inner_element_vm_emit"] = {
            "ok": False, "error_code": r_nonscalar["error_code"], "rejecting_layer": "vm_emit_type_parse",
        }

        # 5. Malformed projection (corrupt outer `pages[pi]` SequenceProject kind 11 to claim TByte result instead of TSeq -> error_code 7)
        mut_proj = copy.deepcopy(proj_ok_wire)
        proj_insts = _find_all_insts_by_kind(mut_proj, 11)
        assert len(proj_insts) >= 2, len(proj_insts)
        tbyte_wire = copy.deepcopy(proj_emit._wire_field(proj_insts[1][3], "ty"))
        proj_emit._set_wire_field(proj_insts[0][3], "ty", tbyte_wire)
        r_bad_proj = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources_ok) + [mut_proj, sem.integer(1), go_id],
            targs64,
        )
        assert r_bad_proj["ok"] is False and r_bad_proj["error_code"] == 7, r_bad_proj
        out["malformed_outer_projection_result_type"] = {
            "ok": False, "error_code": r_bad_proj["error_code"], "rejecting_layer": "vm_emit_instruction_lowering",
        }

        # 6a. Malformed outer projection arity (argc=1 on outer SequenceProject prevents page type propagation -> error_code 4)
        mut_arity_outer = copy.deepcopy(proj_ok_wire)
        seq_proj_outer = _find_all_insts_by_kind(mut_arity_outer, 11)[0][3]
        proj_emit._set_wire_field(seq_proj_outer, "argc", sem.integer(1))
        r_bad_arity_outer = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources_ok) + [mut_arity_outer, sem.integer(1), go_id],
            targs64,
        )
        assert r_bad_arity_outer["ok"] is False and r_bad_arity_outer["error_code"] == 4, r_bad_arity_outer
        out["malformed_outer_projection_arity"] = {
            "ok": False, "error_code": r_bad_arity_outer["error_code"], "rejecting_layer": "vm_emit_seq_type_propagation",
        }

        # 6b. Malformed inner projection arity (argc=1 on inner byte SequenceProject -> error_code 7)
        mut_arity_inner = copy.deepcopy(proj_ok_wire)
        seq_proj_inner = _find_all_insts_by_kind(mut_arity_inner, 11)[1][3]
        proj_emit._set_wire_field(seq_proj_inner, "argc", sem.integer(1))
        r_bad_arity_inner = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources_ok) + [mut_arity_inner, sem.integer(1), go_id],
            targs64,
        )
        assert r_bad_arity_inner["ok"] is False and r_bad_arity_inner["error_code"] == 7, r_bad_arity_inner
        out["malformed_inner_projection_arity"] = {
            "ok": False, "error_code": r_bad_arity_inner["error_code"], "rejecting_layer": "vm_emit_instruction_lowering",
        }
    finally:
        p.close()
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    assert single_emit.PROBE_BIN.exists(), f"probe missing: {single_emit.PROBE_BIN}"
    assert single_emit.VM_BIN.exists(), f"mncs-vm missing: {single_emit.VM_BIN}"

    stdlib_dir = single_emit.STDLIB_ROOT / "library" / "std"
    sources_lexer = [
        ("00_utf8_primitives.mncs", stdlib_dir / "utf8_primitives.mncs", (stdlib_dir / "utf8_primitives.mncs").read_bytes()),
        ("01_utf8_scalar.mncs", stdlib_dir / "utf8_scalar.mncs", (stdlib_dir / "utf8_scalar.mncs").read_bytes()),
        ("02_text_utf8.mncs", stdlib_dir / "text_utf8.mncs", (stdlib_dir / "text_utf8.mncs").read_bytes()),
        ("03_source.mncs", ROOT / "src/compiler/source.mncs", (ROOT / "src/compiler/source.mncs").read_bytes()),
        ("04_lexer.mncs", ROOT / "src/compiler/lexer.mncs", (ROOT / "src/compiler/lexer.mncs").read_bytes()),
    ]
    sources_parser = sources_lexer + [
        ("05_parser.mncs", ROOT / "src/compiler/parser.mncs", (ROOT / "src/compiler/parser.mncs").read_bytes()),
    ]
    sources_segment = sources_lexer + [
        ("05_segment.mncs", ROOT / "src/compiler/segment.mncs", (ROOT / "src/compiler/segment.mncs").read_bytes()),
    ]

    lexer_sha256 = hashlib.sha256((ROOT / "src/compiler/lexer.mncs").read_bytes()).hexdigest()
    source_sha256 = hashlib.sha256((ROOT / "src/compiler/source.mncs").read_bytes()).hexdigest()
    emitter_src_sha256 = hashlib.sha256((ROOT / "src/compiler/vm_emit.mncs").read_bytes()).hexdigest()
    compiler_rev = single_emit._git_head(ROOT)
    vm_rev = single_emit._git_head(single_emit.VM_REPO)
    stage0_rev = single_emit._stage0_lock_rev()
    targs256 = [sem.nat_arg(256), sem.nat_arg(1024)]

    # 1. Focused negative rejections
    rejections = _run_focused_rejections()

    bag_bytes = BYTE_AT_CALLER_SRC.encode("utf-8")
    ntg_bytes = NTG_CALLER_SRC.encode("utf-8")
    sigg_bytes = SIG_GLOBAL_CALLER_SRC.encode("utf-8")
    shdr_bytes = SCAN_HDR_CALLER_SRC.encode("utf-8")
    hdr_bytes = HDR_GLOBAL_CALLER_SRC.encode("utf-8")
    batch_bytes = LEX_BATCH_CALLER_SRC.encode("utf-8")

    srcs_bag = sources_lexer + [("05_bag.mncs", Path("05_bag.mncs"), bag_bytes)]
    srcs_ntg = sources_lexer + [("05_ntg.mncs", Path("05_ntg.mncs"), ntg_bytes)]
    srcs_sigg = sources_lexer + [("05_sigg.mncs", Path("05_sigg.mncs"), sigg_bytes)]
    srcs_shdr = sources_parser + [("06_shdr.mncs", Path("06_shdr.mncs"), shdr_bytes)]
    srcs_hdr = sources_parser + [("06_hdr.mncs", Path("06_hdr.mncs"), hdr_bytes)]
    srcs_batch = sources_segment + [("06_lex_batch.mncs", Path("06_lex_batch.mncs"), batch_bytes)]
    ntg_closure_digests = [(sid, hashlib.sha256(raw).hexdigest()) for sid, _, raw in srcs_ntg]

    with tempfile.TemporaryDirectory(prefix="mncs-ntg-vm-") as tmpdir:
        tmp = Path(tmpdir)

        # 2. Process 1: Emit checkpoint byte_at_global (<4,32>, <8,32>, <4,16>),
        #    lexer::next_token_global<4,32>, direct generic refusal, and next-frontier probes
        t1 = time.monotonic()
        p1 = punct.LexerCallableEmitterProbe()
        try:
            recs1 = p1.record_types("mncs.compiler.vm_emit.v1")
            e_bag_4_32 = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, srcs_bag, 5) + [punct._make_semantic_id_wire(recs1, TARGET_BYTE_AT_4_32)],
                targs256,
            )
            assert e_bag_4_32["ok"] is True and e_bag_4_32["function_count"] == 2, e_bag_4_32

            e_bag_8_32 = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, srcs_bag, 5) + [punct._make_semantic_id_wire(recs1, TARGET_BYTE_AT_8_32)],
                targs256,
            )
            assert e_bag_8_32["ok"] is True and e_bag_8_32["function_count"] == 2, e_bag_8_32

            e_bag_4_16 = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, srcs_bag, 5) + [punct._make_semantic_id_wire(recs1, TARGET_BYTE_AT_4_16)],
                targs256,
            )
            assert e_bag_4_16["ok"] is True and e_bag_4_16["function_count"] == 2, e_bag_4_16

            e_ntg1 = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, srcs_ntg, 5) + [punct._make_semantic_id_wire(recs1, TARGET_NTG_CALLER)],
                targs256,
            )
            assert e_ntg1["ok"] is True, e_ntg1

            direct_gen = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, sources_lexer, 4) + [punct._make_semantic_id_wire(recs1, TARGET_LEXER_NTG_DIRECT)],
                targs256,
            )
            assert direct_gen["ok"] is False and direct_gen["error_code"] == 11, direct_gen

            e_sigg = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, srcs_sigg, 5) + [punct._make_semantic_id_wire(recs1, TARGET_SIG_GLOBAL_CALLER)],
                targs256,
            )
            e_shdr = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, srcs_shdr, 6) + [punct._make_semantic_id_wire(recs1, TARGET_SCAN_HDR_CALLER)],
                targs256,
            )
            e_hdr = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, srcs_hdr, 6) + [punct._make_semantic_id_wire(recs1, TARGET_HDR_GLOBAL_CALLER)],
                targs256,
            )
            e_batch = p1.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs1, srcs_batch, 6) + [punct._make_semantic_id_wire(recs1, TARGET_LEX_BATCH_CALLER)],
                targs256,
            )
        finally:
            p1.close()
        emit1_wall_s = time.monotonic() - t1

        # 3. Process 2: Fresh independent process for bit-for-bit determinism of next_token_global<4,32>
        t2 = time.monotonic()
        p2 = punct.LexerCallableEmitterProbe()
        try:
            recs2 = p2.record_types("mncs.compiler.vm_emit.v1")
            e_ntg2 = p2.call(
                "compile_and_emit_project_vm_callable",
                proj_emit._build_project_args(recs2, srcs_ntg, 5) + [punct._make_semantic_id_wire(recs2, TARGET_NTG_CALLER)],
                targs256,
            )
            assert e_ntg2["ok"] is True, e_ntg2
        finally:
            p2.close()
        emit2_wall_s = time.monotonic() - t2

        # 4. Verify Checkpoint 1: source::byte_at_global (<4,32>, <8,32>, <4,16>)
        bag_specs = {}
        bag_cases_4_32 = [
            ("p0_start", [b"ABCD", b"EFGH"], 4, 8, 0),
            ("p0_last", [b"ABCD", b"EFGH"], 4, 8, 3),
            ("p1_first", [b"ABCD", b"EFGH"], 4, 8, 4),
            ("short_final", [b"ABCD", b"XY"], 4, 6, 5),
            ("short_final_oob", [b"ABCD", b"XY"], 4, 8, 6),
            ("offset_eq_total", [b"ABCD"], 4, 4, 4),
            ("offset_gt_total", [b"ABCD"], 4, 4, 9),
            ("zero_stride", [b"ABCD"], 0, 4, 0),
            ("missing_page", [b"ABCD"], 4, 12, 8),
        ]
        bag_cases_8_32 = [
            ("p4_fifth_page", [b"0000", b"1111", b"2222", b"3333", b"4444Z"], 5, 25, 24),
            ("p5_missing", [b"0000", b"1111", b"2222", b"3333", b"4444Z"], 5, 30, 26),
        ]
        bag_cases_4_16 = [
            ("p1_stride16", [b"0123456789abcdef", b"GHIJ"], 16, 20, 18),
            ("eof_stride16", [b"0123456789abcdef", b"GHIJ"], 16, 20, 20),
        ]
        for key, emit_res, fn_name, p_cap, n_cap, cases in [
            ("4_32", e_bag_4_32, "call_4_32", 4, 32, bag_cases_4_32),
            ("8_32", e_bag_8_32, "call_8_32", 8, 32, bag_cases_8_32),
            ("4_16", e_bag_4_16, "call_4_16", 4, 16, bag_cases_4_16),
        ]:
            art = native_vm_artifact.package_native_vm_artifact(
                emit_res, bag_bytes, allow_multi_module=True,
                producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
                ssa_producer_entry="mncs.compiler.project.v1::compile_project_target_with_context",
            )
            punct._assert_no_symbolic_bounds(punct._extract_dag_module(art))
            ap = tmp / f"byte_at_{key}.vm.json"
            ap.write_text(json.dumps(art, sort_keys=True, separators=(",", ":")) + "\n")
            admit = subprocess.run([str(single_emit.VM_BIN), "admit", "--artifact", str(ap)], capture_output=True, text=True)
            assert admit.returncode == 0 and json.loads(admit.stdout)["status"] == "admitted", admit.stdout
            oracle = _stage0_byte_at_global_oracle(cases, p_cap, n_cap)
            vm = single_emit.FreshVmSession(ap)
            try:
                for label, pages, stride, total, offset in cases:
                    res = vm.call(
                        "mncs.test.byte_at_global_caller.v1", fn_name,
                        [_vm_pages(pages), _u64_val(stride), _u64_val(total), _u64_val(offset)],
                    )
                    assert res["outcome"]["kind"] == "completed", (key, label, res)
                    got = res["execution"]["returned"][0]["integer"]["value"]
                    assert got == oracle[label], (key, label, oracle[label], got)
                # Direct generic dispatch
                dres = vm.call(
                    "mncs.compiler.source.v1", "byte_at_global",
                    [_vm_pages(cases[0][1]), _u64_val(cases[0][2]), _u64_val(cases[0][3]), _u64_val(cases[0][4])],
                    type_arguments=[sem.nat_arg(p_cap), sem.nat_arg(n_cap)],
                )
                assert dres["outcome"]["kind"] == "completed"
                assert dres["execution"]["returned"][0]["integer"]["value"] == oracle[cases[0][0]]
            finally:
                vm.close()
            bag_specs[key] = {
                "artifact_id": art["artifact_id"],
                "specialized_callable": art["callables"][1]["semantic"],
                "vm_admission_status": "admitted",
                "vm_differential_matches": len(cases),
            }
        # Distinct specialization keys across <4,32>, <8,32>, <4,16> (no collision!)
        spec_ids = [bag_specs[k]["specialized_callable"] for k in ("4_32", "8_32", "4_16")]
        assert len(set(spec_ids)) == 3, spec_ids

        # 5. Package, verify bit-for-bit determinism, admit, and execute lexer::next_token_global<4,32>
        art1 = native_vm_artifact.package_native_vm_artifact(
            e_ntg1, ntg_bytes, allow_multi_module=True,
            producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            ssa_producer_entry="mncs.compiler.project.v1::compile_project_target_with_context",
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
            closure_source_digests=ntg_closure_digests,
        )
        art2 = native_vm_artifact.package_native_vm_artifact(
            e_ntg2, ntg_bytes, allow_multi_module=True,
            producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            ssa_producer_entry="mncs.compiler.project.v1::compile_project_target_with_context",
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
            closure_source_digests=ntg_closure_digests,
        )
        art1_bytes = (json.dumps(art1, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        art2_bytes = (json.dumps(art2, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        assert art1_bytes == art2_bytes, "non-deterministic lexer::next_token_global<4,32> emission!"
        assert art1["artifact_id"] == art2["artifact_id"]
        art_sha256 = hashlib.sha256(art1_bytes).hexdigest()

        ssa_mod = punct._extract_dag_module(art1)
        punct._assert_no_symbolic_bounds(ssa_mod)
        rec_names = sorted(
            native_vm_artifact._semantic_id_str(r["name"])
            for r in native_vm_artifact.flist(e_ntg1["record_types"])
        )

        art_path = tmp / "lexer_next_token_global_4_32.vm.json"
        art_path.write_bytes(art1_bytes)
        admit = subprocess.run([str(single_emit.VM_BIN), "admit", "--artifact", str(art_path)], capture_output=True, text=True)
        assert admit.returncode == 0 and json.loads(admit.stdout)["status"] == "admitted", admit.stdout

        tampered = json.loads(art1_bytes.decode("utf-8"))
        tampered["callables"][0]["name"] = "tampered_ntg"
        tampered_path = tmp / "lexer_ntg_tampered.vm.json"
        tampered_path.write_text(json.dumps(tampered, sort_keys=True) + "\n")
        admit_bad = subprocess.run([str(single_emit.VM_BIN), "admit", "--artifact", str(tampered_path)], capture_output=True, text=True)
        assert admit_bad.returncode != 0 and json.loads(admit_bad.stdout)["status"] == "refused", admit_bad.stdout

        # Build single-token corpus + two valid page layouts of the same logical program
        cases = _build_next_token_global_corpus()
        logical_src = b"mncs 0.19;\nmodule a.b; /* ok */ fn f(x: u64) -> (r: u64) { return x + 1; }"
        assert len(logical_src) == 74
        layout_a_pages = _chunk_pages(logical_src, 25)  # 3 pages: 25, 25, 24
        layout_b_pages = _chunk_pages(logical_src, 20)  # 4 pages: 20, 20, 20, 14
        stream_layouts = [
            ("layout_stride25_3pages", layout_a_pages, 25, len(logical_src)),
            ("layout_stride20_4pages", layout_b_pages, 20, len(logical_src)),
        ]
        oracle_single, oracle_streams, oracle_sig_sample = _stage0_lexer_global_oracle(cases, stream_layouts)
        assert oracle_streams["layout_stride25_3pages"] == oracle_streams["layout_stride20_4pages"]

        vm_sess = single_emit.FreshVmSession(art_path)
        vm_max_cells = 0
        vm_max_steps = 0
        vm_total_steps = 0
        matched = 0
        vm_streams = {}
        try:
            for label, pages, stride, total, start in cases:
                res = vm_sess.call(
                    "mncs.test.next_token_global_caller.v1",
                    "call_next_token_global",
                    [_vm_pages(pages), _u64_val(stride), _u64_val(total), _u64_val(start)],
                )
                assert res["outcome"]["kind"] == "completed", (label, res)
                got = _decode_vm_token(res["execution"]["returned"][0])
                expected = oracle_single[label]
                assert got == expected, (label, expected, got)
                usage = res["record"]["usage"]
                vm_max_cells = max(vm_max_cells, usage["max_memory_cells"])
                vm_max_steps = max(vm_max_steps, usage["steps"])
                vm_total_steps += usage["steps"]
                matched += 1

            # Lex full logical program under both valid page layouts through EOF
            for layout_name, pages, stride, total in stream_layouts:
                toks = []
                cur = 0
                for _ in range(128):
                    res = vm_sess.call(
                        "mncs.test.next_token_global_caller.v1",
                        "call_next_token_global",
                        [_vm_pages(pages), _u64_val(stride), _u64_val(total), _u64_val(cur)],
                    )
                    assert res["outcome"]["kind"] == "completed", (layout_name, cur, res)
                    tok = _decode_vm_token(res["execution"]["returned"][0])
                    toks.append(tok)
                    usage = res["record"]["usage"]
                    vm_max_cells = max(vm_max_cells, usage["max_memory_cells"])
                    vm_max_steps = max(vm_max_steps, usage["steps"])
                    vm_total_steps += usage["steps"]
                    if tok["kind"] == 0 or tok["end"] <= cur:
                        break
                    cur = tok["end"]
                assert toks == oracle_streams[layout_name], (layout_name, toks, oracle_streams[layout_name])
                vm_streams[layout_name] = toks
            assert vm_streams["layout_stride25_3pages"] == vm_streams["layout_stride20_4pages"]

            # Direct generic dispatch <4, 32>
            direct_res = vm_sess.call(
                "mncs.compiler.lexer.v1", "next_token_global",
                [_vm_pages([b"  mod", b"ule a"]), _u64_val(5), _u64_val(10), _u64_val(2)],
                type_arguments=[sem.nat_arg(4), sem.nat_arg(32)],
            )
            assert direct_res["outcome"]["kind"] == "completed", direct_res
            assert _decode_vm_token(direct_res["execution"]["returned"][0]) == oracle_single["span_pages_keyword_module"]

            # Refusal of uncompiled specializations <8, 32> and <4, 16>
            bad_outer_disp = vm_sess.call(
                "mncs.compiler.lexer.v1", "next_token_global",
                [_vm_pages([b"fn x"]), _u64_val(4), _u64_val(4), _u64_val(0)],
                type_arguments=[sem.nat_arg(8), sem.nat_arg(32)],
                expect_ok=False,
            )
            assert (not bad_outer_disp.get("ok")) or bad_outer_disp["result"]["outcome"]["kind"] != "completed", bad_outer_disp

            bad_inner_disp = vm_sess.call(
                "mncs.compiler.lexer.v1", "next_token_global",
                [_vm_pages([b"fn x"]), _u64_val(4), _u64_val(4), _u64_val(0)],
                type_arguments=[sem.nat_arg(4), sem.nat_arg(16)],
                expect_ok=False,
            )
            assert (not bad_inner_disp.get("ok")) or bad_inner_disp["result"]["outcome"]["kind"] != "completed", bad_inner_disp
        finally:
            vm_sess.close()

        # 6. Probe next milestones: significant_global<4,32>, scan_header_global<4,32>,
        #    header_global<4,32>, and decl::parse_unit_signatures<4,32>
        def probe_summary(emit_res, root_b, label):
            info = {
                "emitted": emit_res["ok"],
                "error_code": emit_res["error_code"],
                "error_function": emit_res["error_function"],
                "error_block": emit_res["error_block"],
                "error_instruction": emit_res["error_instruction"],
                "function_count": emit_res["function_count"],
                "block_count": emit_res["block_count"],
                "instruction_count": emit_res["instruction_count"],
                "record_type_count": emit_res["record_type_count"],
                "admitted": False,
                "executed_sample": False,
            }
            if emit_res["ok"]:
                art_p = native_vm_artifact.package_native_vm_artifact(emit_res, root_b, allow_multi_module=True)
                ap = tmp / f"probe_{label}.vm.json"
                ap.write_text(json.dumps(art_p, sort_keys=True, separators=(",", ":")) + "\n")
                adm = subprocess.run([str(single_emit.VM_BIN), "admit", "--artifact", str(ap)], capture_output=True, text=True)
                info["artifact_id"] = art_p["artifact_id"]
                info["admitted"] = (adm.returncode == 0 and json.loads(adm.stdout)["status"] == "admitted")
                if info["admitted"] and label == "significant_global_4_32":
                    vm_p = single_emit.FreshVmSession(ap)
                    try:
                        r = vm_p.call(
                            "mncs.test.sig_global_caller.v1", "call_significant_global",
                            [_vm_pages([b"  /* hi */ ", b"module a;"]), _u64_val(11), _u64_val(20), _u64_val(0)],
                        )
                        assert r["outcome"]["kind"] == "completed", r
                        got_s = _decode_vm_token(r["execution"]["returned"][0])
                        assert got_s == oracle_sig_sample, (got_s, oracle_sig_sample)
                        info["executed_sample"] = True
                        info["sample_result"] = got_s
                    finally:
                        vm_p.close()
                elif info["admitted"] and label == "scan_header_global_4_32":
                    hdr_id = next(
                        native_vm_artifact._semantic_id_str(r["identity"])
                        for r in native_vm_artifact.flist(emit_res["record_types"])
                        if native_vm_artifact._semantic_id_str(r["name"]) == "Header"
                    )
                    vm_p = single_emit.FreshVmSession(ap)
                    try:
                        r = vm_p.call(
                            "mncs.test.scan_header_global_caller.v1", "call_scan_header_global",
                            [_vm_pages([b"mncs 0.19; ", b"module a.b;"]), _u64_val(11), _u64_val(22), _vm_header_seed(hdr_id)],
                        )
                        assert r["outcome"]["kind"] == "completed", r
                        got_h = _decode_vm_header(r["execution"]["returned"][0])
                        assert got_h["code"] == 0 and got_h["state"] == 8 and got_h["module_start"] == 18 and got_h["module_end"] == 21, got_h
                        info["executed_sample"] = True
                        info["sample_result"] = got_h
                    finally:
                        vm_p.close()
                elif info["admitted"] and label == "header_global_4_32":
                    vm_p = single_emit.FreshVmSession(ap)
                    try:
                        r = vm_p.call(
                            "mncs.test.header_global_caller.v1", "call_header_global",
                            [_vm_pages([b"mncs 0.19; ", b"module a.b;"]), _u64_val(11), _u64_val(22)],
                        )
                        assert r["outcome"]["kind"] == "completed", r
                        got_h = _decode_vm_header(r["execution"]["returned"][0])
                        assert got_h["code"] == 0 and got_h["state"] == 8 and got_h["module_start"] == 18 and got_h["module_end"] == 21, got_h
                        info["executed_sample"] = True
                        info["sample_result"] = got_h
                    finally:
                        vm_p.close()
            return info

        next_probes = {
            "lexer_significant_global_4_32": probe_summary(e_sigg, sigg_bytes, "significant_global_4_32"),
            "parser_scan_header_global_4_32": probe_summary(e_shdr, shdr_bytes, "scan_header_global_4_32"),
            "parser_header_global_4_32": probe_summary(e_hdr, hdr_bytes, "header_global_4_32"),
            "segment_lex_batch_from_4_32": probe_summary(e_batch, batch_bytes, "segment_lex_batch_from_4_32"),
        }

        host_peak_rss_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        semantic_digest = hashlib.sha256(
            json.dumps({"single": oracle_single, "streams": oracle_streams}, sort_keys=True).encode()
        ).hexdigest()

        summary = {
            "status": "passed",
            "provenance": {
                "callable_selection_and_lowering": "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
                "native_project_ssa_producer": "mncs.compiler.project.v1::compile_project_target_with_context",
                "provider_body_materializer": "mncs.compiler.project.v1::materialize_provider_callable",
                "transport_codec_and_sealer": "tools/native_vm_artifact.py",
                "independent_vm_admission_and_execution": str(single_emit.VM_BIN),
                "behavioral_oracle": f"locked Stage-0 mncs-compiler-stage0-probe ({stage0_rev})",
                "compiler_revision": compiler_rev,
                "vm_revision": vm_rev,
                "emitter_source_sha256": emitter_src_sha256,
            },
            "checkpoint1_source_byte_at_global": {
                "specializations": bag_specs,
                "distinct_specialization_keys": True,
            },
            "lexer_next_token_global_4_32": {
                "target_callable": TARGET_NTG_CALLER,
                "target_specialization": "mncs.compiler.lexer.v1::next_token_global<4, 32>",
                "unchanged_lexer_mncs_sha256": lexer_sha256,
                "unchanged_source_mncs_sha256": source_sha256,
                "emitted_callables": [c["semantic"] for c in art1["callables"]],
                "emitted_record_schemas": rec_names,
                "function_count": e_ntg1["function_count"],
                "block_count": e_ntg1["block_count"],
                "instruction_count": e_ntg1["instruction_count"],
                "record_type_count": e_ntg1["record_type_count"],
                "symbolic_bounds_remaining": 0,
                "artifact_id": art1["artifact_id"],
                "ssa_module_identity": art1["source"]["selected_ssa_identity"],
                "artifact_sha256": art_sha256,
                "artifact_bytes": len(art1_bytes),
                "deterministic_across_fresh_processes": True,
                "emit_process1_wall_s": round(emit1_wall_s, 3),
                "emit_process1_child_peak_rss_bytes": p1.peak_rss_bytes,
                "emit_process2_wall_s": round(emit2_wall_s, 3),
                "emit_process2_child_peak_rss_bytes": p2.peak_rss_bytes,
                "host_python_peak_rss_kib": host_peak_rss_kib,
                "vm_admission_status": "admitted",
                "tampered_artifact_vm_admission": {"status": "refused", "returncode": admit_bad.returncode},
                "vm_differential_cases": len(cases),
                "vm_differential_matches": matched,
                "layout_invariance_stream_tokens": len(vm_streams["layout_stride25_3pages"]),
                "layout_invariance_layouts_identical_through_eof": True,
                "semantic_digest": semantic_digest,
                "vm_direct_generic_dispatch_matched": True,
                "vm_uncompiled_8_32_refused": True,
                "vm_uncompiled_4_16_refused": True,
                "vm_session_wall_s": round(vm_sess.elapsed_s, 4),
                "vm_resident_peak_rss_bytes": vm_sess.peak_rss_bytes,
                "vm_max_memory_cells": vm_max_cells,
                "vm_max_steps_per_call": vm_max_steps,
                "vm_total_steps": vm_total_steps,
                "direct_uninstantiated_refusal": {
                    "ok": direct_gen["ok"],
                    "error_code": direct_gen["error_code"],
                    "error_function": direct_gen["error_function"],
                },
            },
            "focused_rejections": rejections,
            "next_frontier_probes": next_probes,
        }
        if args.json_out:
            args.json_out.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
