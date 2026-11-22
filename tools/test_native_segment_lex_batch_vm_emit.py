#!/usr/bin/env python3
"""Differential & determinism harness for unchanged
`mncs.compiler.lexer.v1::reverse_token_step` and
`mncs.compiler.segment.v1::lex_batch_from<4,32>` via the native selected-callable
Project SSA -> VM path (`compile_and_emit_project_vm_callable`).

Proves:
  1. Checkpoint 1: unchanged `lexer::reverse_token_step` emits cleanly with
     finite enum `TokenList` (`TNil` / `TCons`), passes fresh canonical
     `mncs-vm` admission, and matches locked Stage-0 (`6a3f6907`) across
     empty, single-token, and multi-token `TokenReverse` inputs.
  2. Checkpoint 2: unchanged `segment::lex_batch_from<4,32>` emits bit-for-bit
     deterministically across two fresh independent processes, passes fresh
     canonical `mncs-vm` admission (with tampered artifact refused), matches
     locked Stage-0 across empty, single-page, multi-page cross-boundary
     tokens/strings/comments/UTF-8, malformed UTF-8, invalid cursor, zero
     stride, short final page, and missing page cases, preserves dual-layout
     paging invariance, supports direct generic `<4,32>` dispatch, and refuses
     uncompiled `<8,32>` and `<4,16>`.
  3. Focused fail-closed finite/enum and generic rejections (direct
     uninstantiated generic, wrong finite owner, unknown variant, wrong payload
     field, malformed finite payload owner), recording the exact rejecting
     layer and structured `VmEmitFailureDetail`.
  4. Optional next-frontier probe beyond `segment::lex_batch_from<4,32>`
     (`decl::parse_use<4,32>`), distinguishing emission, admission, and
     execution and recording structured `VmEmitFailureDetail` when emission
     refuses.
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
import test_native_lexer_next_token_global_vm_emit as ntg_mod
import test_native_lexer_punctuation_vm_emit as punct
import test_native_project_vm_emit as proj_emit
import test_native_vm_emit as single_emit
import test_sem as sem

TARGET_REVERSE_STEP_DIRECT = "mncs:0.2:function:mncs.compiler.lexer.v1::reverse_token_step"
TARGET_LEX_BATCH_CALLER = "mncs:0.2:function:mncs.test.lex_batch_caller.v1::call_lex_batch_from"
TARGET_SEG_LEX_BATCH_DIRECT = "mncs:0.2:function:mncs.compiler.segment.v1::lex_batch_from"
TARGET_PARSE_USE_CALLER = "mncs:0.2:function:mncs.test.parse_use_caller.v1::call_parse_use"

LEX_BATCH_CALLER_SRC = """\
mncs 0.19;
module mncs.test.lex_batch_caller.v1;
use mncs.compiler.lexer.v1 as lexer;
use mncs.compiler.segment.v1 as seg;

fn call_lex_batch_from(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64, start: u64) -> (result: lexer.BatchResult) {
    return seg.lex_batch_from<4, 32>(pages, stride, total, start);
}
"""

PARSE_USE_CALLER_SRC = """\
mncs 0.19;
module mncs.test.parse_use_caller.v1;
use mncs.compiler.decl.v1 as decl;

fn call_parse_use(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64, cursor: u64) -> (result: decl.UseDecl) {
    return decl.parse_use<4, 32>(pages, stride, total, cursor);
}
"""

SMALL_FINITE_SRC = """\
mncs 0.19;
module mncs.test.small_finite.v1;

enum Color {
    Empty,
    Rgb { r: u64, g: u64, b: u64 }
}

fn score(c: Color) -> (result: u64) {
    let made: Color = Color.Rgb { r: 1, g: 2, b: 3 };
    return match c {
        Empty => match made { Empty => 0, Rgb { r: rv, g: gv, b: bv } => rv + gv + bv },
        Rgb { r: rv, g: gv, b: bv } => rv + gv * 10 + bv * 100
    };
}
"""

TOKEN_LIST_ID = "mncs:0.2:finite-type:mncs.compiler.lexer.v1::TokenList"
TOKEN_LIST_NIL_ID = "mncs:0.2:finite-variant:mncs.compiler.lexer.v1::TokenList::TNil"
TOKEN_LIST_CONS_ID = "mncs:0.2:finite-variant:mncs.compiler.lexer.v1::TokenList::TCons"


def _u64(v):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": v}}


def _vm_token(kind, start, end, diag, token_id):
    return {
        "record": {
            "name": "Token",
            "type_identity": token_id,
            "fields": [
                ["diagnostic", _u64(diag)],
                ["end", _u64(end)],
                ["kind", _u64(kind)],
                ["start", _u64(start)],
            ],
        }
    }


def _vm_token_list(tokens, token_id):
    cur = {
        "finite": {
            "type_identity": TOKEN_LIST_ID,
            "variant_identity": TOKEN_LIST_NIL_ID,
            "discriminant": 0,
            "payload": [],
        }
    }
    for tok in reversed(tokens):
        cur = {
            "finite": {
                "type_identity": TOKEN_LIST_ID,
                "variant_identity": TOKEN_LIST_CONS_ID,
                "discriminant": 1,
                "payload": [
                    ["head", _vm_token(*tok, token_id)],
                    ["tail", cur],
                ],
            }
        }
    return cur


def _vm_token_reverse(rest_tokens, acc_tokens, token_id, token_reverse_id):
    return {
        "record": {
            "name": "TokenReverse",
            "type_identity": token_reverse_id,
            "fields": [
                ["acc", _vm_token_list(acc_tokens, token_id)],
                ["rest", _vm_token_list(rest_tokens, token_id)],
            ],
        }
    }


def _decode_vm_token_tuple(val):
    assert "record" in val, val
    fields = {k: v["integer"]["value"] for k, v in val["record"]["fields"]}
    return (fields["kind"], fields["start"], fields["end"], fields["diagnostic"])


def _decode_vm_token_list(val):
    out = []
    cur = val
    for _ in range(2048):
        assert "finite" in cur, cur
        fin = cur["finite"]
        if fin["discriminant"] == 0:
            assert fin["variant_identity"] == TOKEN_LIST_NIL_ID, fin
            return out
        assert fin["discriminant"] == 1 and fin["variant_identity"] == TOKEN_LIST_CONS_ID, fin
        payload = {k: v for k, v in fin["payload"]}
        out.append(_decode_vm_token_tuple(payload["head"]))
        cur = payload["tail"]
    raise AssertionError("TokenList exceeded 2048 nodes")


def _decode_vm_token_reverse(val):
    assert "record" in val, val
    fields = {k: v for k, v in val["record"]["fields"]}
    return {
        "rest": _decode_vm_token_list(fields["rest"]),
        "acc": _decode_vm_token_list(fields["acc"]),
    }


def _decode_vm_batch_result(val):
    assert "record" in val, val
    rec = val["record"]
    fields = {k: v for k, v in rec["fields"]}
    tokens = _decode_vm_token_list(fields["tokens"])
    return {
        "type_identity": rec["type_identity"],
        "name": rec["name"],
        "tokens": tokens,
        "next": fields["next"]["integer"]["value"],
        "eof": fields["eof"]["boolean"]["value"],
        "count": fields["count"]["integer"]["value"],
    }


def _decode_stage0_token_list(val):
    out = []
    cur = val
    for _ in range(2048):
        assert isinstance(cur, dict) and "$v" in cur, cur
        if cur["$v"] in (0, "TNil"):
            return out
        p = cur["$p"]
        h = p["head"]
        out.append((h["kind"], h["start"], h["end"], h["diagnostic"]))
        cur = p["tail"]
    raise AssertionError("Stage-0 TokenList exceeded 2048 nodes")


def _decode_stage0_token_reverse(val):
    return {
        "rest": _decode_stage0_token_list(val["rest"]),
        "acc": _decode_stage0_token_list(val["acc"]),
    }


def _decode_stage0_batch_result(val):
    return {
        "tokens": _decode_stage0_token_list(val["tokens"]),
        "next": val["next"],
        "eof": val["eof"],
        "count": val["count"],
    }


def _decode_sem_id(ident):
    if not isinstance(ident, dict) or not ident.get("valid"):
        return None
    raw = ident.get("bytes") or []
    length = int(ident.get("len") or 0)
    return bytes(raw[:length]).decode("utf-8", errors="replace")


def _emit_detail_summary(emit_res):
    det = emit_res.get("error_detail") or {}
    return {
        "ok": emit_res.get("ok"),
        "error_code": emit_res.get("error_code"),
        "error_function": emit_res.get("error_function"),
        "error_block": emit_res.get("error_block"),
        "error_instruction": emit_res.get("error_instruction"),
        "callable": _decode_sem_id(det.get("callable")),
        "phase": det.get("phase"),
        "ssa_kind": det.get("ssa_kind"),
        "expected_type_kind": det.get("expected_type_kind"),
        "observed_type_kind": det.get("observed_type_kind"),
        "observed_nominal": _decode_sem_id(det.get("observed_nominal")),
        "source_start": det.get("source_start"),
        "source_end": det.get("source_end"),
    }


def _admit_artifact(art_path: Path):
    proc = subprocess.run(
        [str(single_emit.VM_BIN), "admit", "--artifact", str(art_path)],
        capture_output=True,
        text=True,
    )
    parsed = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = {"raw_stdout": proc.stdout.strip()}
    ok = proc.returncode == 0 and isinstance(parsed, dict) and parsed.get("status") == "admitted"
    return {"ok": ok, "returncode": proc.returncode, "output": parsed, "stderr": proc.stderr.strip()}


def _stage0_reverse_step_oracle(cases):
    env = dict(os.environ)
    env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{single_emit.STDLIB_ROOT / 'library'}"
    env["MNCS_PROBE_MODULES"] = "source,lexer"
    env["MNCS_PROBE_LIBRARY_MODULES"] = "mncs.std.text_utf8.v1"
    env["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.lexer.v1"
    env["MNCS_PROBE_BACKEND"] = backend_policy.resolve("lexer")
    env["MNCS_PROBE_BACKEND_ROOTS"] = "reverse_token_step"
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
    results = {}
    try:
        proc.stdin.write(json.dumps({"record_types": "mncs.compiler.lexer.v1"}) + "\n")
        proc.stdin.flush()
        recs_lexer = {r["name"]: r for r in json.loads(proc.stdout.readline())}
        tok_id = recs_lexer["Token"]["identity"]
        trev_id = recs_lexer["TokenReverse"]["identity"]
        for label, rest_toks, acc_toks in cases:
            req = {
                "schema_version": "0.1",
                "target": {"module": "mncs.compiler.lexer.v1", "function": "reverse_token_step"},
                "arguments": [_vm_token_reverse(rest_toks, acc_toks, tok_id, trev_id)],
                "type_arguments": [],
                "step_budget": 1_000_000,
            }
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            assert resp.get("status") == "returned", (label, resp)
            results[label] = _decode_stage0_token_reverse(sem.decode(resp["returned"][0]))
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        assert proc.wait(timeout=60) == 0
    return results, tok_id, trev_id


def _stage0_lex_batch_oracle(cases, p_cap=4, n_cap=32):
    env = dict(os.environ)
    env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{single_emit.STDLIB_ROOT / 'library'}"
    env["MNCS_PROBE_MODULES"] = "source,lexer,segment"
    env["MNCS_PROBE_LIBRARY_MODULES"] = "mncs.std.text_utf8.v1"
    env["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.segment.v1"
    env["MNCS_PROBE_BACKEND"] = backend_policy.resolve("segment")
    env["MNCS_PROBE_BACKEND_ROOTS"] = "lex_batch_from"
    env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
        {
            "module": "mncs.compiler.segment.v1",
            "function": "lex_batch_from",
            "type_arguments": [sem.nat_arg(p_cap), sem.nat_arg(n_cap)],
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
    results = {}
    try:
        for label, pages, stride, total, start in cases:
            req = {
                "schema_version": "0.1",
                "target": {"module": "mncs.compiler.segment.v1", "function": "lex_batch_from"},
                "type_arguments": [sem.nat_arg(p_cap), sem.nat_arg(n_cap)],
                "arguments": [
                    ntg_mod._vm_pages(pages),
                    _u64(stride),
                    _u64(total),
                    _u64(start),
                ],
                "step_budget": 1_000_000,
            }
            proc.stdin.write(json.dumps(req) + "\n")
            proc.stdin.flush()
            resp = json.loads(proc.stdout.readline())
            assert resp.get("status") == "returned", (label, resp)
            results[label] = _decode_stage0_batch_result(sem.decode(resp["returned"][0]))
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        assert proc.wait(timeout=60) == 0
    return results


def _lex_batch_cases():
    multi_src = b"mncs 0.19; module a.b; fn f(x: u64) -> (r: u64) { /* ok */ return x + 1; }"
    utf8_src = "/* café — λ */ let s: u64 = 42;".encode("utf-8")
    cases = [
        ("empty_input", [], 32, 0, 0),
        ("single_page_simple", [b"fn f(x: u64) -> (r: u64) { return x; }"[0:32]], 32, 32, 0),
        ("single_page_tokens_eof", [b"let x: u64 = 123 + 45;"], 32, 22, 0),
        ("single_page_mid_start", [b"let x: u64 = 123 + 45;"], 32, 22, 13),
        ("multi_page_cross_ident", [b"let alpha_", b"beta_gamma", b" = 7 + 8;"], 10, 29, 0),
        ("multi_page_cross_comment", [b"/* hi ", b"there */", b" fn go()"], 8, 22, 0),
        ("multi_page_cross_string", [b'let s = "abc', b'def_ghi";'], 12, 21, 0),
        ("multi_page_full_mncs_4p", ntg_mod._chunk_pages(multi_src, 20), 20, len(multi_src), 0),
        ("multi_page_utf8_cross", ntg_mod._chunk_pages(utf8_src, 11), 11, len(utf8_src), 0),
        ("malformed_utf8_byte", [b"fn ", b"\xff\xfe", b" a;"], 3, 8, 0),
        ("start_at_total", [b"fn x;"], 8, 5, 5),
        ("start_gt_total", [b"fn x;"], 8, 5, 9),
        ("zero_stride", [b"fn x;"], 0, 5, 0),
        ("short_final_page", [b"let a = 1;", b"let b"], 10, 15, 0),
        ("missing_page", [b"let a = "], 8, 16, 0),
    ]
    for label, pages, stride, total, start in cases:
        assert len(pages) <= 4, (label, len(pages))
        for pg in pages:
            assert len(pg) <= 32, (label, len(pg))
    return cases


def _package(emit_res, sources, target_idx):
    closure_digests = [
        (src_id, hashlib.sha256(src_bytes).hexdigest())
        for src_id, _, src_bytes in sources
    ]
    return native_vm_artifact.package_native_vm_artifact(
        emit_res,
        sources[target_idx][2],
        allow_multi_module=True,
        closure_source_digests=closure_digests,
        producer_entry="mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
        ssa_producer_entry="mncs.compiler.project.v1::compile_project_target_with_context",
        emitter_source_sha256=hashlib.sha256((ROOT / "src/compiler/vm_emit.mncs").read_bytes()).hexdigest(),
        compiler_revision=single_emit._git_head(ROOT),
        stage0_revision=single_emit._stage0_lock_rev(),
    )


def _run_focused_finite_rejections():
    """Verify fail-closed finite/enum rejections via `vm_emit`."""
    targs64 = [sem.nat_arg(64), sem.nat_arg(1024)]
    src_b = SMALL_FINITE_SRC.encode("utf-8")
    sources = [("00_small_finite.mncs", Path("00_small_finite.mncs"), src_b)]
    score_id_str = "mncs:0.2:function:mncs.test.small_finite.v1::score"
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
        score_id = punct._make_semantic_id_wire(recs, score_id_str)

        e_ok = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, sources, 0) + [score_id],
            targs64,
        )
        assert e_ok["ok"] is True and e_ok["finite_type_count"] == 1, e_ok
        proj_ok_wire = p.call_raw("compile_project_for_emit", proj_emit._build_compile_only_args(recs, sources), targs64)

        # 1. Wrong finite owner on FiniteConstruct (kind 5) -> error_code 4
        mut_owner = copy.deepcopy(proj_ok_wire)
        k5 = ntg_mod._find_all_insts_by_kind(mut_owner, 5)
        assert len(k5) >= 1
        bad_nom = punct._make_semantic_id_wire(recs, "mncs:0.2:finite-type:mncs.test.small_finite.v1::WrongColor")
        proj_emit._set_wire_field(k5[0][3], "nominal_identity", bad_nom)
        r_owner = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources) + [mut_owner, sem.integer(0), score_id],
            targs64,
        )
        assert r_owner["ok"] is False and r_owner["error_code"] == 4, r_owner
        out["wrong_finite_owner_construct"] = _emit_detail_summary(r_owner)

        # 2. Unknown variant on FiniteConstruct (kind 5) -> error_code 7
        mut_var = copy.deepcopy(proj_ok_wire)
        k5_v = ntg_mod._find_all_insts_by_kind(mut_var, 5)
        score_off = src_b.index(b"score")
        proj_emit._set_wire_field(k5_v[0][3], "variant_start", sem.integer(score_off))
        proj_emit._set_wire_field(k5_v[0][3], "variant_end", sem.integer(score_off + 5))
        r_var = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources) + [mut_var, sem.integer(0), score_id],
            targs64,
        )
        assert r_var["ok"] is False and r_var["error_code"] == 7, r_var
        out["unknown_variant_construct"] = _emit_detail_summary(r_var)

        # 3. Wrong payload field on FinitePayloadProject (kind 6) -> error_code 7
        mut_proj = copy.deepcopy(proj_ok_wire)
        k6 = ntg_mod._find_all_insts_by_kind(mut_proj, 6)
        assert len(k6) >= 1
        params_wire = proj_emit._wire_field(k6[0][3], "params")
        proj_emit._set_wire_field(params_wire["finite"]["payload"][0][1], "start", sem.integer(score_off))
        proj_emit._set_wire_field(params_wire["finite"]["payload"][0][1], "end", sem.integer(score_off + 5))
        r_proj = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources) + [mut_proj, sem.integer(0), score_id],
            targs64,
        )
        assert r_proj["ok"] is False and r_proj["error_code"] == 7, r_proj
        out["wrong_payload_field_project"] = _emit_detail_summary(r_proj)

        # 4. Wrong finite owner on FinitePayloadProject (kind 6) -> error_code 7
        mut_pown = copy.deepcopy(proj_ok_wire)
        k6_o = ntg_mod._find_all_insts_by_kind(mut_pown, 6)
        proj_emit._set_wire_field(k6_o[0][3], "nominal_identity", bad_nom)
        r_pown = p.call(
            "emit_verified_project_ssa_callable_with_sources",
            proj_emit._build_compile_only_args(recs, sources) + [mut_pown, sem.integer(0), score_id],
            targs64,
        )
        assert r_pown["ok"] is False and r_pown["error_code"] == 7, r_pown
        out["wrong_finite_owner_project"] = _emit_detail_summary(r_pown)
    finally:
        p.close()
    return out


def _probe_next_frontier(base_sources):
    """Probe unchanged `decl::parse_use<4,32>` using M=1024, N=1024 (so decl.mncs fits)."""
    parser_b = (ROOT / "src/compiler/parser.mncs").read_bytes()
    decl_b = (ROOT / "src/compiler/decl.mncs").read_bytes()
    srcs = list(base_sources) + [
        ("04_parser.mncs", ROOT / "src/compiler/parser.mncs", parser_b),
        ("05_decl.mncs", ROOT / "src/compiler/decl.mncs", decl_b),
        ("06_parse_use_caller.mncs", Path("06_parse_use_caller.mncs"), PARSE_USE_CALLER_SRC.encode("utf-8")),
    ]
    targs = [sem.nat_arg(1024), sem.nat_arg(1024)]
    p = punct.LexerCallableEmitterProbe(
        roots=["compile_and_emit_project_vm_callable"],
        seeds=[{
            "module": "mncs.compiler.vm_emit.v1",
            "function": "compile_and_emit_project_vm_callable",
            "type_arguments": targs,
        }],
    )
    try:
        recs = p.record_types("mncs.compiler.vm_emit.v1")
        args = proj_emit._build_project_args(recs, srcs, len(srcs) - 1, stride=1024) + [
            punct._make_semantic_id_wire(recs, TARGET_PARSE_USE_CALLER)
        ]
        t0 = time.monotonic()
        emit_res = p.call("compile_and_emit_project_vm_callable", args, targs)
        elapsed_s = round(time.monotonic() - t0, 3)
    finally:
        p.close()

    entry = {
        "target_callable": TARGET_PARSE_USE_CALLER,
        "target_specialization": "mncs.compiler.decl.v1::parse_use<4, 32>",
        "emit_elapsed_s": elapsed_s,
        "emit_ok": bool(emit_res.get("ok")),
    }
    if not emit_res.get("ok"):
        entry["refusal"] = _emit_detail_summary(emit_res)
        return {"decl_parse_use_4_32": entry}

    art = _package(emit_res, srcs, len(srcs) - 1)
    with tempfile.TemporaryDirectory(prefix="mncs-frontier-parse-use-") as td:
        art_path = Path(td) / "artifact.json"
        art_path.write_text(json.dumps(art, separators=(",", ":")), encoding="utf-8")
        adm = _admit_artifact(art_path)
        entry["admitted"] = adm["ok"]
        entry["function_count"] = emit_res.get("function_count")
        entry["record_type_count"] = emit_res.get("record_type_count")
        entry["finite_type_count"] = emit_res.get("finite_type_count")
        entry["artifact_id"] = art["artifact_id"]
        if not adm["ok"]:
            entry["admit_error"] = adm
    return {"decl_parse_use_4_32": entry}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument("--probe-frontier", action="store_true")
    ap.add_argument("--emit-only-batch", type=Path, default=None)
    args = ap.parse_args()

    targs256 = [sem.nat_arg(256), sem.nat_arg(1024)]
    stdlib_dir = single_emit.STDLIB_ROOT / "library" / "std"
    sources_lexer = [
        ("00_utf8_primitives.mncs", stdlib_dir / "utf8_primitives.mncs", (stdlib_dir / "utf8_primitives.mncs").read_bytes()),
        ("01_utf8_scalar.mncs", stdlib_dir / "utf8_scalar.mncs", (stdlib_dir / "utf8_scalar.mncs").read_bytes()),
        ("02_text_utf8.mncs", stdlib_dir / "text_utf8.mncs", (stdlib_dir / "text_utf8.mncs").read_bytes()),
        ("03_source.mncs", ROOT / "src/compiler/source.mncs", (ROOT / "src/compiler/source.mncs").read_bytes()),
        ("04_lexer.mncs", ROOT / "src/compiler/lexer.mncs", (ROOT / "src/compiler/lexer.mncs").read_bytes()),
    ]
    base_sources = sources_lexer + [
        ("05_segment.mncs", ROOT / "src/compiler/segment.mncs", (ROOT / "src/compiler/segment.mncs").read_bytes()),
    ]
    batch_sources = base_sources + [
        ("06_lex_batch_caller.mncs", Path("06_lex_batch_caller.mncs"), LEX_BATCH_CALLER_SRC.encode("utf-8")),
    ]

    if args.emit_only_batch is not None:
        p2 = punct.LexerCallableEmitterProbe(
            roots=["compile_and_emit_project_vm_callable"],
            seeds=[{
                "module": "mncs.compiler.vm_emit.v1",
                "function": "compile_and_emit_project_vm_callable",
                "type_arguments": targs256,
            }],
        )
        try:
            recs2 = p2.record_types("mncs.compiler.vm_emit.v1")
            a2 = proj_emit._build_project_args(recs2, batch_sources, 6) + [
                punct._make_semantic_id_wire(recs2, TARGET_LEX_BATCH_CALLER)
            ]
            emit2 = p2.call("compile_and_emit_project_vm_callable", a2, targs256)
        finally:
            p2.close()
        assert emit2["ok"] is True, _emit_detail_summary(emit2)
        art2 = _package(emit2, batch_sources, 6)
        args.emit_only_batch.write_text(json.dumps(art2, separators=(",", ":")), encoding="utf-8")
        print(json.dumps({"artifact_id": art2["artifact_id"], "function_count": emit2["function_count"]}))
        return

    report = {
        "compiler_revision": single_emit._git_head(ROOT),
        "stage0_revision": single_emit._stage0_lock_rev(),
        "vm_revision": single_emit._git_head(single_emit.VM_REPO),
    }

    # Single emitter probe for Checkpoints 1, 2 (process 1), and direct uninstantiated refusal
    print("[1/4] Emitting reverse_token_step, segment::lex_batch_from<4,32>, and direct-generic refusal...")
    p = punct.LexerCallableEmitterProbe(
        roots=["compile_and_emit_project_vm_callable"],
        seeds=[{
            "module": "mncs.compiler.vm_emit.v1",
            "function": "compile_and_emit_project_vm_callable",
            "type_arguments": targs256,
        }],
    )
    try:
        recs = p.record_types("mncs.compiler.vm_emit.v1")

        # Checkpoint 1: unchanged lexer::reverse_token_step
        rev_sources = sources_lexer
        t0 = time.monotonic()
        rev_emit = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, rev_sources, 4)
            + [punct._make_semantic_id_wire(recs, TARGET_REVERSE_STEP_DIRECT)],
            targs256,
        )
        rev_s = round(time.monotonic() - t0, 3)

        # Checkpoint 2: unchanged segment::lex_batch_from<4,32>
        t0 = time.monotonic()
        batch_emit = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, batch_sources, 6)
            + [punct._make_semantic_id_wire(recs, TARGET_LEX_BATCH_CALLER)],
            targs256,
        )
        batch_s = round(time.monotonic() - t0, 3)

        # Direct uninstantiated generic refusal
        r_uninst = p.call(
            "compile_and_emit_project_vm_callable",
            proj_emit._build_project_args(recs, base_sources, 5)
            + [punct._make_semantic_id_wire(recs, TARGET_SEG_LEX_BATCH_DIRECT)],
            targs256,
        )
    finally:
        p.close()

    assert rev_emit["ok"] is True, _emit_detail_summary(rev_emit)
    assert rev_emit["finite_type_count"] == 1, rev_emit
    assert batch_emit["ok"] is True, _emit_detail_summary(batch_emit)
    assert batch_emit["finite_type_count"] == 1, batch_emit
    assert r_uninst["ok"] is False and r_uninst["error_code"] == 11, r_uninst

    # ------------------------------------------------------------------
    # Verify Checkpoint 1 in fresh canonical VM vs locked Stage-0
    # ------------------------------------------------------------------
    print("[2/4] Verifying Checkpoint 1 (lexer::reverse_token_step) in fresh mncs-vm vs Stage-0...")
    rev_art = _package(rev_emit, rev_sources, 4)
    rev_rec_ids = {
        native_vm_artifact._semantic_id_str(r["name"]): native_vm_artifact._semantic_id_str(r["identity"])
        for r in native_vm_artifact.flist(rev_emit["record_types"])
    }
    rev_cases = [
        ("empty_both", [], []),
        ("empty_rest_nonempty_acc", [], [(4, 0, 2, 0)]),
        ("one_rest_empty_acc", [(4, 0, 2, 0)], []),
        ("three_rest_two_acc", [(4, 0, 2, 0), (10, 3, 5, 0), (65, 5, 6, 0)], [(0, 6, 6, 0), (69, 2, 3, 0)]),
    ]
    stage0_rev, s0_tok_id, s0_trev_id = _stage0_reverse_step_oracle(rev_cases)
    vm_tok_id = rev_rec_ids["Token"]
    vm_trev_id = rev_rec_ids["TokenReverse"]
    with tempfile.TemporaryDirectory(prefix="mncs-rev-step-") as td:
        art_path = Path(td) / "rev_step.artifact.json"
        art_path.write_text(json.dumps(rev_art, separators=(",", ":")), encoding="utf-8")
        admit_rev = _admit_artifact(art_path)
        assert admit_rev["ok"], admit_rev
        vm_rev = single_emit.FreshVmSession(art_path)
        try:
            rev_matches = 0
            for label, rest_toks, acc_toks in rev_cases:
                res = vm_rev.call(
                    "mncs.compiler.lexer.v1", "reverse_token_step",
                    [_vm_token_reverse(rest_toks, acc_toks, vm_tok_id, vm_trev_id)],
                )
                assert res["outcome"]["kind"] == "completed", (label, res)
                got = _decode_vm_token_reverse(res["execution"]["returned"][0])
                assert got == stage0_rev[label], (label, got, stage0_rev[label])
                rev_matches += 1
        finally:
            vm_rev.close()

    report["checkpoint_1_reverse_token_step"] = {
        "emit_ok": True,
        "emit_elapsed_s": rev_s,
        "function_count": rev_emit["function_count"],
        "record_type_count": rev_emit["record_type_count"],
        "finite_type_count": rev_emit["finite_type_count"],
        "artifact_id": rev_art["artifact_id"],
        "admitted": True,
        "differential_cases": rev_matches,
        "differential_matches": rev_matches,
    }
    print(f"    PASS: artifact_id={rev_art['artifact_id'][:24]}... ({rev_matches}/{rev_matches} Stage-0 matches, {rev_s}s)")

    # ------------------------------------------------------------------
    # Verify Checkpoint 2: determinism across 2 fresh processes + VM vs Stage-0
    # ------------------------------------------------------------------
    print("[3/4] Verifying Checkpoint 2 (segment::lex_batch_from<4,32>) determinism + fresh mncs-vm vs Stage-0...")
    batch_art = _package(batch_emit, batch_sources, 6)
    batch_bytes_1 = json.dumps(batch_art, separators=(",", ":")).encode("utf-8")

    with tempfile.TemporaryDirectory(prefix="mncs-batch-det-") as td:
        art2_path = Path(td) / "batch2.artifact.json"
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--emit-only-batch", str(art2_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        batch_bytes_2 = art2_path.read_bytes()
        assert batch_bytes_1 == batch_bytes_2, "Non-deterministic artifact bytes across two fresh processes!"
        art1_path = Path(td) / "batch1.artifact.json"
        art1_path.write_bytes(batch_bytes_1)

        admit_batch = _admit_artifact(art1_path)
        assert admit_batch["ok"], admit_batch

        tampered = copy.deepcopy(batch_art)
        tampered["artifact_id"] = "sha256:" + ("0" * 64)
        tamp_path = Path(td) / "tampered.artifact.json"
        tamp_path.write_text(json.dumps(tampered, separators=(",", ":")), encoding="utf-8")
        admit_tamp = _admit_artifact(tamp_path)
        assert not admit_tamp["ok"], admit_tamp

        cases = _lex_batch_cases()
        stage0_batch = _stage0_lex_batch_oracle(cases, 4, 32)
        case_results = {}
        vm_batch = single_emit.FreshVmSession(art1_path)
        try:
            for label, pages, stride, total, start in cases:
                res = vm_batch.call(
                    "mncs.test.lex_batch_caller.v1", "call_lex_batch_from",
                    [ntg_mod._vm_pages(pages), _u64(stride), _u64(total), _u64(start)],
                )
                assert res["outcome"]["kind"] == "completed", (label, res)
                got = _decode_vm_batch_result(res["execution"]["returned"][0])
                exp = stage0_batch[label]
                assert got["tokens"] == exp["tokens"], (label, got["tokens"], exp["tokens"])
                assert got["next"] == exp["next"], (label, got["next"], exp["next"])
                assert got["eof"] == exp["eof"], (label, got["eof"], exp["eof"])
                assert got["count"] == exp["count"], (label, got["count"], exp["count"])
                case_results[label] = {
                    "count": got["count"],
                    "next": got["next"],
                    "eof": got["eof"],
                    "steps": res["execution"].get("steps"),
                }

            # Direct generic <4,32> dispatch
            probe_src = b"fn hi(a: u64) -> (r: u64) { return a; }"
            probe_pages = ntg_mod._chunk_pages(probe_src, 12)
            direct_res = vm_batch.call(
                "mncs.compiler.segment.v1", "lex_batch_from",
                [ntg_mod._vm_pages(probe_pages), _u64(12), _u64(len(probe_src)), _u64(0)],
                type_arguments=[sem.nat_arg(4), sem.nat_arg(32)],
            )
            assert direct_res["outcome"]["kind"] == "completed", direct_res
            direct_dec = _decode_vm_batch_result(direct_res["execution"]["returned"][0])

            # Dual-layout paging invariance across 3 distinct page layouts
            for alt_stride in (20, 32):
                alt_pages = ntg_mod._chunk_pages(probe_src, alt_stride)
                alt_res = vm_batch.call(
                    "mncs.compiler.segment.v1", "lex_batch_from",
                    [ntg_mod._vm_pages(alt_pages), _u64(alt_stride), _u64(len(probe_src)), _u64(0)],
                    type_arguments=[sem.nat_arg(4), sem.nat_arg(32)],
                )
                assert alt_res["outcome"]["kind"] == "completed", (alt_stride, alt_res)
                alt_dec = _decode_vm_batch_result(alt_res["execution"]["returned"][0])
                assert alt_dec == direct_dec, (alt_stride, alt_dec, direct_dec)

            # Refusal of uncompiled <8,32> and <4,16>
            refused_specs = {}
            for p_bad, n_bad in ((8, 32), (4, 16)):
                bad_res = vm_batch.call(
                    "mncs.compiler.segment.v1", "lex_batch_from",
                    [ntg_mod._vm_pages(probe_pages), _u64(12), _u64(len(probe_src)), _u64(0)],
                    type_arguments=[sem.nat_arg(p_bad), sem.nat_arg(n_bad)],
                    expect_ok=False,
                )
                assert (not bad_res.get("ok")) or bad_res["result"]["outcome"]["kind"] != "completed", bad_res
                refused_specs[f"{p_bad}_{n_bad}"] = True
        finally:
            vm_batch.close()

    report["checkpoint_2_segment_lex_batch_from_4_32"] = {
        "emit_ok": True,
        "emit_elapsed_s": batch_s,
        "function_count": batch_emit["function_count"],
        "block_count": batch_emit["block_count"],
        "instruction_count": batch_emit["instruction_count"],
        "record_type_count": batch_emit["record_type_count"],
        "finite_type_count": batch_emit["finite_type_count"],
        "emitted_callables": [c["semantic"] for c in batch_art["callables"]],
        "artifact_id": batch_art["artifact_id"],
        "artifact_bytes": len(batch_bytes_1),
        "deterministic_two_fresh_processes": True,
        "admitted": True,
        "tamper_refused": True,
        "differential_cases": len(cases),
        "differential_matches": len(case_results),
        "direct_generic_4_32_ok": True,
        "dual_layout_invariance_ok": True,
        "uncompiled_specs_refused": refused_specs,
        "cases": case_results,
    }
    print(f"    PASS: artifact_id={batch_art['artifact_id'][:24]}... ({len(case_results)}/{len(cases)} Stage-0 matches, deterministic, {batch_s}s)")

    # ------------------------------------------------------------------
    # Focused finite/enum & uninstantiated generic rejections
    # ------------------------------------------------------------------
    print("[4/4] Focused finite/enum & uninstantiated generic rejections...")
    rejections = _run_focused_finite_rejections()
    rejections["direct_uninstantiated_generic"] = _emit_detail_summary(r_uninst)
    report["focused_rejections"] = rejections
    print(f"    PASS: {len(rejections)} rejection cases verified")

    # Optional next-frontier probe
    if args.probe_frontier:
        print("[frontier] Probing unchanged decl::parse_use<4,32>...")
        report["next_frontier"] = _probe_next_frontier(base_sources)
        for k, v in report["next_frontier"].items():
            print(f"    {k}: emit_ok={v['emit_ok']} {v.get('refusal') or v.get('artifact_id')}")

    report["max_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    main()
