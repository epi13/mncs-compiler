#!/usr/bin/env python3
"""End-to-end differential, non-aliasing, determinism, and fail-closed proof for
compiling and emitting unchanged `mncs.std.utf8_scalar.v1::step_scalar_generic<4>`
from native Project SSA (`mncs.compiler.project.v1` -> `mncs.compiler.vm_emit.v1`)
and independently admitting/executing it in a fresh canonical VM (`mncs-vm`).

Provenance boundary:
- Concrete Nat specialization, bounded byte-view lowering, nominal record schema
  extraction, closure selection, and call/CFG verification:
      100% MNCS (`mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_module`).
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

import native_vm_artifact
import test_native_project_vm_emit as proj_emit
import test_native_vm_emit as single_emit
import test_sem as sem


ROOT_STEP4_SOURCE = """\
mncs 0.18;
module mncs.test.utf8_scalar_step4.v1;

use mncs.std.utf8_scalar.v1 as utf8;

fn step4(view: [byte; up_to 4], view_length: u64, at: u64) -> (result: utf8.ScalarStep) {
    return utf8.step_scalar_generic<4>(view, view_length, at);
}
"""

ROOT_DUAL_CAP_SOURCE = """\
mncs 0.18;
module mncs.test.utf8_scalar_dual_cap.v1;

use mncs.std.utf8_scalar.v1 as utf8;

fn step4(view: [byte; up_to 4], view_length: u64, at: u64) -> (result: utf8.ScalarStep) {
    return utf8.step_scalar_generic<4>(view, view_length, at);
}

fn step8(view: [byte; up_to 8], view_length: u64, at: u64) -> (result: utf8.ScalarStep) {
    return utf8.step_scalar_generic<8>(view, view_length, at);
}
"""

# Negative case 1: root with ONLY an uninstantiated generic template
NEG_UNINSTANTIATED_ROOT = """\
mncs 0.18;
module mncs.test.neg_uninstantiated.v1;

use mncs.std.utf8_scalar.v1 as utf8;

fn wrapper<M: Nat>(view: [byte; up_to M], view_length: u64, at: u64) -> (result: utf8.ScalarStep) {
    return utf8.step_scalar_generic<M>(view, view_length, at);
}
"""

# Negative case 2: mismatched concrete specialization bound (caller passes [byte; up_to 8] to <4>)
NEG_MISMATCHED_BOUND = """\
mncs 0.18;
module mncs.test.neg_mismatched_bound.v1;

use mncs.std.utf8_scalar.v1 as utf8;

fn bad_step(view: [byte; up_to 8], view_length: u64, at: u64) -> (result: utf8.ScalarStep) {
    return utf8.step_scalar_generic<4>(view, view_length, at);
}
"""

# Negative case 3: mismatched generic argument arity (<4, 8> for single-param generic)
NEG_MISMATCHED_ARITY = """\
mncs 0.18;
module mncs.test.neg_mismatched_arity.v1;

use mncs.std.utf8_scalar.v1 as utf8;

fn bad_arity(view: [byte; up_to 4], view_length: u64, at: u64) -> (result: utf8.ScalarStep) {
    return utf8.step_scalar_generic<4, 8>(view, view_length, at);
}
"""

# Negative case 4: zero Nat bound specialization (<0>)
NEG_ZERO_BOUND = """\
mncs 0.18;
module mncs.test.neg_zero_bound.v1;

use mncs.std.utf8_scalar.v1 as utf8;

fn bad_zero(view: [byte; up_to 4], view_length: u64, at: u64) -> (result: utf8.ScalarStep) {
    return utf8.step_scalar_generic<0>(view, view_length, at);
}
"""


def _byte_seq_val(raw_bytes):
    return {"sequence": {"values": [{"byte": {"value": b}} for b in raw_bytes]}}


def _u64_val(v):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": v}}


def _assert_no_symbolic_bounds(node, path="root"):
    """Fail if any symbolic generic parameter or bound survived into emitted SSA."""
    if isinstance(node, dict):
        for k, v in node.items():
            assert k not in ("param", "up_to_param", "generic_param"), f"symbolic generic survived at {path}.{k}: {v}"
            _assert_no_symbolic_bounds(v, f"{path}.{k}")
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            _assert_no_symbolic_bounds(item, f"{path}[{idx}]")


def _extract_dag_module(artifact):
    """Decode DAG-encoded SsaModule solely to verify structural invariants in tests."""
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


def _step4_corpus():
    """Comprehensive UTF-8 scalar corpus for capacity <= 4."""
    return [
        # ASCII
        ("ascii_nul", [0x00], 1, 0),
        ("ascii_A", [0x41], 1, 0),
        ("ascii_space_mid", [0x41, 0x20, 0x7A], 3, 1),
        ("ascii_del_end", [0x41, 0x42, 0x43, 0x7F], 4, 3),
        # Valid 2-byte scalars (U+0080, U+00E9, U+07FF)
        ("utf8_2b_min_u0080", [0xC2, 0x80], 2, 0),
        ("utf8_2b_eacute_u00e9", [0xC3, 0xA9, 0x41], 3, 0),
        ("utf8_2b_max_u07ff_offset2", [0x41, 0x42, 0xDF, 0xBF], 4, 2),
        # Valid 3-byte scalars (U+0800, U+20AC, U+D7FF, U+E000, U+FFFF)
        ("utf8_3b_min_u0800", [0xE0, 0xA0, 0x80], 3, 0),
        ("utf8_3b_euro_u20ac", [0xE2, 0x82, 0xAC], 3, 0),
        ("utf8_3b_pre_surrogate_ud7ff", [0xED, 0x9F, 0xBF], 3, 0),
        ("utf8_3b_post_surrogate_ue000", [0xEE, 0x80, 0x80], 3, 0),
        ("utf8_3b_max_uffff_offset1", [0x21, 0xEF, 0xBF, 0xBF], 4, 1),
        # Valid 4-byte scalars (U+10000, U+1F600, U+10FFFF)
        ("utf8_4b_min_u10000", [0xF0, 0x90, 0x80, 0x80], 4, 0),
        ("utf8_4b_grin_u1f600", [0xF0, 0x9F, 0x98, 0x80], 4, 0),
        ("utf8_4b_max_u10ffff", [0xF4, 0x8F, 0xBF, 0xBF], 4, 0),
        # Malformed leads & continuations
        ("malformed_bare_cont_80", [0x80], 1, 0),
        ("malformed_bare_cont_bf", [0xBF, 0x41], 2, 0),
        ("malformed_overlong_2b_c0", [0xC0, 0x80], 2, 0),
        ("malformed_overlong_2b_c1", [0xC1, 0xBF], 2, 0),
        ("malformed_overlong_3b_e0_80", [0xE0, 0x80, 0x80], 3, 0),
        ("malformed_overlong_3b_e0_9f", [0xE0, 0x9F, 0xBF], 3, 0),
        ("malformed_surrogate_ed_a0", [0xED, 0xA0, 0x80], 3, 0),
        ("malformed_surrogate_ed_bf", [0xED, 0xBF, 0xBF], 3, 0),
        ("malformed_overlong_4b_f0_80", [0xF0, 0x80, 0x80, 0x80], 4, 0),
        ("malformed_above_max_f4_90", [0xF4, 0x90, 0x80, 0x80], 4, 0),
        ("malformed_invalid_lead_f5", [0xF5, 0x80, 0x80, 0x80], 4, 0),
        ("malformed_invalid_lead_ff", [0xFF], 1, 0),
        ("malformed_2b_bad_cont", [0xC3, 0x28], 2, 0),
        ("malformed_3b_bad_cont2", [0xE2, 0x82, 0x28], 3, 0),
        ("malformed_4b_bad_cont1", [0xF0, 0x28, 0x80, 0x80], 4, 0),
        ("malformed_4b_bad_cont2", [0xF0, 0x9F, 0x28, 0x80], 4, 0),
        ("malformed_4b_bad_cont3", [0xF0, 0x9F, 0x98, 0x28], 4, 0),
        # Truncated inputs (by actual length and by view_length window clamp)
        ("truncated_2b_len1", [0xC3], 1, 0),
        ("truncated_2b_window1", [0xC3, 0xA9], 1, 0),
        ("truncated_3b_len2", [0xE2, 0x82], 2, 0),
        ("truncated_3b_window2", [0xE2, 0x82, 0xAC], 2, 0),
        ("truncated_4b_len3", [0xF0, 0x9F, 0x98], 3, 0),
        ("truncated_4b_window3", [0xF0, 0x9F, 0x98, 0x80], 3, 0),
        ("truncated_4b_at_offset1", [0x41, 0xF0, 0x9F, 0x98], 4, 1),
        # Offsets at or beyond the window & empty view & view_length > view.len
        ("empty_view_at0", [], 0, 0),
        ("empty_view_len4_at0", [], 4, 0),
        ("zero_window_at0", [0x41, 0x42], 0, 0),
        ("at_equals_window", [0x41, 0x42, 0x43], 3, 3),
        ("at_equals_clamped_window", [0x41, 0x42, 0x43], 2, 2),
        ("at_beyond_window", [0x41, 0x42], 2, 5),
        ("view_length_exceeds_len_clamped", [0xC3, 0xA9], 99, 0),
        ("view_length_exceeds_len_at_end", [0xC3, 0xA9], 99, 2),
    ]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    assert single_emit.PROBE_BIN.exists(), f"probe missing: {single_emit.PROBE_BIN}"
    assert single_emit.VM_BIN.exists(), f"mncs-vm missing: {single_emit.VM_BIN}"

    prim_path = single_emit.STDLIB_ROOT / "library" / "std" / "utf8_primitives.mncs"
    scal_path = single_emit.STDLIB_ROOT / "library" / "std" / "utf8_scalar.mncs"
    prim_bytes = prim_path.read_bytes()
    scal_bytes = scal_path.read_bytes()
    root4_bytes = ROOT_STEP4_SOURCE.encode("utf-8")
    dual_bytes = ROOT_DUAL_CAP_SOURCE.encode("utf-8")

    sources_step4 = [
        ("00_std_utf8_primitives.mncs", prim_path, prim_bytes),
        ("01_std_utf8_scalar.mncs", scal_path, scal_bytes),
        ("02_utf8_scalar_step4.mncs", Path("02_utf8_scalar_step4.mncs"), root4_bytes),
    ]
    sources_dual = [
        ("00_std_utf8_primitives.mncs", prim_path, prim_bytes),
        ("01_std_utf8_scalar.mncs", scal_path, scal_bytes),
        ("02_utf8_scalar_dual_cap.mncs", Path("02_utf8_scalar_dual_cap.mncs"), dual_bytes),
    ]
    target_idx = 2

    closure_digests_step4 = [(sid, hashlib.sha256(raw).hexdigest()) for sid, _, raw in sources_step4]
    closure_digests_dual = [(sid, hashlib.sha256(raw).hexdigest()) for sid, _, raw in sources_dual]
    emitter_src_sha256 = hashlib.sha256((ROOT / "src" / "compiler" / "vm_emit.mncs").read_bytes()).hexdigest()
    compiler_rev = single_emit._git_head(ROOT)
    vm_rev = single_emit._git_head(single_emit.VM_REPO)
    stage0_rev = single_emit._stage0_lock_rev()

    with tempfile.TemporaryDirectory(prefix="mncs-utf8-scalar-generic-vm-") as tmpdir:
        tmp = Path(tmpdir)

        # ------------------------------------------------------------------
        # 1. Process 1 (cold/first build): Emit step_scalar_generic<4> + dual-cap + negative checks
        # ------------------------------------------------------------------
        t1 = time.monotonic()
        p1 = single_emit.NativeEmitterProbe()
        try:
            proj_recs = p1.record_types("mncs.compiler.vm_emit.v1")
            emit_step4_1 = p1.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(proj_recs, sources_step4, target_idx),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert emit_step4_1["ok"] is True, emit_step4_1
            assert emit_step4_1["function_count"] == 5, emit_step4_1
            assert emit_step4_1["record_type_count"] == 1, emit_step4_1

            # Dual-capacity (<4> and <8>) non-aliasing emission
            emit_dual = p1.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(proj_recs, sources_dual, target_idx),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert emit_dual["ok"] is True, emit_dual
            assert emit_dual["function_count"] == 7, emit_dual  # step4, step8, spec<4>, spec<8>, + 3 primitives

            # Negative 1: uninstantiated generic-only root
            neg_uninst = p1.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(
                    proj_recs,
                    [
                        ("00_std_utf8_primitives.mncs", prim_path, prim_bytes),
                        ("01_std_utf8_scalar.mncs", scal_path, scal_bytes),
                        ("02_neg_uninst.mncs", Path("02_neg_uninst.mncs"), NEG_UNINSTANTIATED_ROOT.encode("utf-8")),
                    ],
                    target_idx,
                ),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_uninst["ok"] is False and neg_uninst["error_code"] == 11, neg_uninst

            # Negative 2: mismatched concrete specialization bound ([byte; up_to 8] passed to <4>)
            # Native Project proof already rejects this (`error_code == 1`), and vm_emit also
            # enforces concrete bound equality at call-edge verification (`error_code == 10` below).
            neg_bound = p1.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(
                    proj_recs,
                    [
                        ("00_std_utf8_primitives.mncs", prim_path, prim_bytes),
                        ("01_std_utf8_scalar.mncs", scal_path, scal_bytes),
                        ("02_neg_bound.mncs", Path("02_neg_bound.mncs"), NEG_MISMATCHED_BOUND.encode("utf-8")),
                    ],
                    target_idx,
                ),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_bound["ok"] is False and neg_bound["error_code"] in (1, 10), neg_bound

            # Negative 3: mismatched generic argument arity (<4, 8>)
            neg_arity = p1.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(
                    proj_recs,
                    [
                        ("00_std_utf8_primitives.mncs", prim_path, prim_bytes),
                        ("01_std_utf8_scalar.mncs", scal_path, scal_bytes),
                        ("02_neg_arity.mncs", Path("02_neg_arity.mncs"), NEG_MISMATCHED_ARITY.encode("utf-8")),
                    ],
                    target_idx,
                ),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_arity["ok"] is False and neg_arity["error_code"] in (1, 11), neg_arity

            # Negative 4: zero Nat bound specialization (<0>)
            neg_zero = p1.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(
                    proj_recs,
                    [
                        ("00_std_utf8_primitives.mncs", prim_path, prim_bytes),
                        ("01_std_utf8_scalar.mncs", scal_path, scal_bytes),
                        ("02_neg_zero.mncs", Path("02_neg_zero.mncs"), NEG_ZERO_BOUND.encode("utf-8")),
                    ],
                    target_idx,
                ),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_zero["ok"] is False and neg_zero["error_code"] in (1, 11), neg_zero

            # Negative 5a: direct vm_emit concrete bound mismatch check (`error_code == 10`):
            # keep valid compiled ProjectResult wire from step4, but swap `<4>` -> `<8>` in
            # the root source bytes passed to `emit_verified_project_ssa_with_sources` so
            # caller passes `[byte; up_to 4]` into specialized `step_scalar_generic<8>`.
            compile_args_step4 = proj_emit._build_compile_only_args(proj_recs, sources_step4)
            compiled_wire = p1.call_raw(
                "compile_project_for_emit",
                compile_args_step4,
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            tampered_root_bytes = root4_bytes.replace(b"<4>", b"<8>", 1)
            assert len(tampered_root_bytes) == len(root4_bytes)
            tampered_sources = [
                ("00_std_utf8_primitives.mncs", prim_path, prim_bytes),
                ("01_std_utf8_scalar.mncs", scal_path, scal_bytes),
                ("02_utf8_scalar_step4.mncs", Path("02_utf8_scalar_step4.mncs"), tampered_root_bytes),
            ]
            tampered_src_args = proj_emit._build_compile_only_args(proj_recs, tampered_sources)
            neg_vm_emit_bound_mismatch = p1.call(
                "emit_verified_project_ssa_with_sources",
                tampered_src_args + [compiled_wire, sem.integer(target_idx)],
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert (
                neg_vm_emit_bound_mismatch["ok"] is False
                and neg_vm_emit_bound_mismatch["error_code"] == 10
            ), neg_vm_emit_bound_mismatch

            # Negative 5b: unverified reachable provider SSA (`utf8_scalar.mncs` mutated in-place `valid=false`)
            mutated = False
            for mod_w in proj_emit._walk_wire_flist(proj_emit._wire_field(compiled_wire, "modules")):
                if proj_emit._wire_field(mod_w, "source_index")["integer"]["value"] == 1:
                    proj_emit._set_wire_field(
                        proj_emit._wire_field(mod_w, "value_ssa"),
                        "valid",
                        {"boolean": {"value": False}},
                    )
                    mutated = True
            assert mutated
            neg_unverified_ssa = p1.call(
                "emit_verified_project_ssa_with_sources",
                compile_args_step4 + [compiled_wire, sem.integer(target_idx)],
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_unverified_ssa["ok"] is False and neg_unverified_ssa["error_code"] == 1, neg_unverified_ssa
        finally:
            p1.close()
        emit1_wall_s = time.monotonic() - t1

        # ------------------------------------------------------------------
        # 2. Process 2 (fresh warm build): Bit-for-bit determinism check
        # ------------------------------------------------------------------
        t2 = time.monotonic()
        p2 = single_emit.NativeEmitterProbe()
        try:
            proj_recs_2 = p2.record_types("mncs.compiler.vm_emit.v1")
            emit_step4_2 = p2.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(proj_recs_2, sources_step4, target_idx),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert emit_step4_2["ok"] is True, emit_step4_2
        finally:
            p2.close()
        emit2_wall_s = time.monotonic() - t2

        art4_1 = native_vm_artifact.package_native_vm_artifact(
            emit_step4_1,
            root4_bytes,
            allow_multi_module=True,
            closure_source_digests=closure_digests_step4,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        art4_2 = native_vm_artifact.package_native_vm_artifact(
            emit_step4_2,
            root4_bytes,
            allow_multi_module=True,
            closure_source_digests=closure_digests_step4,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        art4_1_bytes = json.dumps(art4_1, separators=(",", ":")).encode("utf-8")
        art4_2_bytes = json.dumps(art4_2, separators=(",", ":")).encode("utf-8")
        assert art4_1_bytes == art4_2_bytes, "step_scalar_generic<4> artifact must be bit-for-bit deterministic"
        assert art4_1["artifact_id"] == art4_2["artifact_id"]
        art4_sha256 = hashlib.sha256(art4_1_bytes).hexdigest()

        # Verify zero symbolic bounds survived & exact specialization/record metadata
        ssa4 = _extract_dag_module(art4_1)
        _assert_no_symbolic_bounds(ssa4)
        assert len(ssa4["record_types"]) == 1, ssa4["record_types"]
        scalar_step_rec = ssa4["record_types"][0]
        assert scalar_step_rec["name"] == "ScalarStep"
        assert scalar_step_rec["identity"].startswith("mncs:0.2:record-type:mncs.std.utf8_scalar.v1::ScalarStep::")
        assert scalar_step_rec["fields"] == [
            ["next", {"integer": {"bits": 64, "signed": False}}],
            ["scalar", {"integer": {"bits": 32, "signed": False}}],
            ["valid", "bool"],
        ], scalar_step_rec

        spec4_hash = hashlib.sha256(b"value:4").hexdigest()[:8]
        spec8_hash = hashlib.sha256(b"value:8").hexdigest()[:8]
        expected_spec4_sem = f"mncs:0.2:function:mncs.std.utf8_scalar.v1::step_scalar_generic__spec_{spec4_hash}"
        expected_spec8_sem = f"mncs:0.2:function:mncs.std.utf8_scalar.v1::step_scalar_generic__spec_{spec8_hash}"
        assert art4_1["generic_entrypoints"] == [
            {
                "generic_module": "mncs.std.utf8_scalar.v1",
                "generic_function": "step_scalar_generic",
                "args_spellings": ["4"],
                "canonical_args": "value:4",
                "function": f"mncs:0.4:ssa:function:mncs.std.utf8_scalar.v1::step_scalar_generic__spec_{spec4_hash}",
            }
        ], art4_1["generic_entrypoints"]

        # Verify dual-capacity artifact has TWO distinct non-aliasing specializations
        art_dual = native_vm_artifact.package_native_vm_artifact(
            emit_dual,
            dual_bytes,
            allow_multi_module=True,
            closure_source_digests=closure_digests_dual,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        art_dual_bytes = json.dumps(art_dual, separators=(",", ":")).encode("utf-8")
        ssa_dual = _extract_dag_module(art_dual)
        _assert_no_symbolic_bounds(ssa_dual)
        dual_sems = [c["semantic"] for c in art_dual["callables"]]
        assert expected_spec4_sem in dual_sems and expected_spec8_sem in dual_sems, dual_sems
        assert expected_spec4_sem != expected_spec8_sem
        # Verify concrete input bounds on the two emitted specialization bodies
        fn_by_sem = {fn["semantic_identity"]: fn for fn in ssa_dual["functions"]}
        assert fn_by_sem[expected_spec4_sem]["inputs"][0]["ty"] == {"sequence": {"element": "byte", "bound": {"up_to": 4}}}
        assert fn_by_sem[expected_spec8_sem]["inputs"][0]["ty"] == {"sequence": {"element": "byte", "bound": {"up_to": 8}}}

        # ------------------------------------------------------------------
        # 3. Independent `mncs-vm admit` + tamper refusal
        # ------------------------------------------------------------------
        art4_path = tmp / "utf8_scalar_step4.native.vm.json"
        art4_path.write_bytes(art4_1_bytes)
        admit4 = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(art4_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit4.returncode == 0, f"mncs-vm admit failed: {admit4.stdout} {admit4.stderr}"
        admit4_payload = json.loads(admit4.stdout)
        assert admit4_payload["status"] == "admitted" and admit4_payload["artifact_id"] == art4_1["artifact_id"]

        tampered = json.loads(art4_1_bytes.decode("utf-8"))
        tampered["assumptions"].append("tampered")
        tampered_path = tmp / "utf8_scalar_step4.tampered.vm.json"
        tampered_path.write_text(json.dumps(tampered, separators=(",", ":")))
        admit_bad = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(tampered_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit_bad.returncode != 0
        tamper_payload = json.loads(admit_bad.stdout)
        assert tamper_payload["status"] == "refused", tamper_payload

        # ------------------------------------------------------------------
        # 4. Fresh `mncs-vm serve` differential execution vs locked Stage-0
        # ------------------------------------------------------------------
        corpus = _step4_corpus()
        vm_sess = single_emit.FreshVmSession(art4_path)
        vm_max_cells = 0
        vm_max_steps = 0
        vm_total_steps = 0
        vm_results = []
        try:
            for name, raw, vlen, at in corpus:
                wire_args = [_byte_seq_val(raw), _u64_val(vlen), _u64_val(at)]
                res = vm_sess.call("mncs.test.utf8_scalar_step4.v1", "step4", wire_args)
                assert res["outcome"]["kind"] == "completed", (name, res)
                usage = res["record"]["usage"]
                vm_max_cells = max(vm_max_cells, usage["max_memory_cells"])
                vm_max_steps = max(vm_max_steps, usage["steps"])
                vm_total_steps += usage["steps"]
                vm_results.append((name, res["execution"]["returned"]))

            # Direct generic entrypoint invocation on `mncs.std.utf8_scalar.v1::step_scalar_generic<4>`
            direct_res = vm_sess.call(
                "mncs.std.utf8_scalar.v1",
                "step_scalar_generic",
                [_byte_seq_val([0xF0, 0x9F, 0x98, 0x80]), _u64_val(4), _u64_val(0)],
                type_arguments=[sem.nat_arg(4)],
            )
            assert direct_res["outcome"]["kind"] == "completed", direct_res
            assert direct_res["execution"]["returned"] == vm_results[
                [i for i, (n, _, _, _) in enumerate(corpus) if n == "utf8_4b_grin_u1f600"][0]
            ][1]

            # Uncompiled specialization <8> refused by VM on the <4>-only artifact
            refused_spec8 = vm_sess.call(
                "mncs.std.utf8_scalar.v1",
                "step_scalar_generic",
                [_byte_seq_val([0x41]), _u64_val(1), _u64_val(0)],
                type_arguments=[sem.nat_arg(8)],
                expect_ok=False,
            )
            assert refused_spec8.get("result", {}).get("outcome", {}).get("kind") == "invalid_request", refused_spec8
        finally:
            vm_sess.close()

        # Run locked Stage-0 oracle on the exact same root + unchanged Stdlib sources
        stage0_lib = tmp / "stage0_lib"
        (stage0_lib / "test").mkdir(parents=True, exist_ok=True)
        (stage0_lib / "test" / "utf8_scalar_step4.mncs").write_bytes(root4_bytes)
        (stage0_lib / "test" / "utf8_scalar_dual_cap.mncs").write_bytes(dual_bytes)
        oracle = single_emit.Stage0ModuleOracle(
            "mncs.test.utf8_scalar_step4.v1",
            [stage0_lib, single_emit.STDLIB_ROOT / "library"],
        )
        matched = 0
        try:
            for idx, (name, raw, vlen, at) in enumerate(corpus):
                wire_args = [_byte_seq_val(raw), _u64_val(vlen), _u64_val(at)]
                s0 = oracle.call("step4", wire_args)
                assert s0["status"] == "returned", (name, s0)
                assert s0["returned"] == vm_results[idx][1], (
                    f"ScalarStep mismatch on {name}: vm={vm_results[idx][1]} stage0={s0['returned']}"
                )
                matched += 1
        finally:
            oracle.close()
        assert matched == len(corpus)

        # 5. Dual-capacity (<4> and <8>) fresh VM execution & Stage-0 parity
        art_dual_path = tmp / "utf8_scalar_dual_cap.native.vm.json"
        art_dual_path.write_bytes(art_dual_bytes)
        admit_dual = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(art_dual_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit_dual.returncode == 0, f"mncs-vm admit dual failed: {admit_dual.stdout} {admit_dual.stderr}"

        dual_cases = [
            ("step4", [0xE2, 0x82, 0xAC, 0x41], 4, 0),
            ("step4", [0xE2, 0x82, 0xAC, 0x41], 4, 3),
            ("step8", [0x41, 0xC3, 0xA9, 0xE2, 0x82, 0xAC, 0x42, 0x43], 8, 0),
            ("step8", [0x41, 0xC3, 0xA9, 0xE2, 0x82, 0xAC, 0x42, 0x43], 8, 1),
            ("step8", [0x41, 0xC3, 0xA9, 0xE2, 0x82, 0xAC, 0x42, 0x43], 8, 3),
            ("step8", [0x41, 0xC3, 0xA9, 0xE2, 0x82, 0xAC, 0x42, 0x43], 8, 6),
            ("step8", [0x41, 0xC3, 0xA9, 0xF0, 0x9F, 0x98, 0x80, 0x7A], 8, 3),
            ("step8", [0x41, 0xC3, 0xA9, 0xF0, 0x9F, 0x98, 0x80, 0x7A], 8, 7),
            ("step8", [0x41, 0xC3, 0xA9, 0xF0, 0x9F, 0x98, 0x80, 0x7A], 8, 8),
        ]
        vm_dual = single_emit.FreshVmSession(art_dual_path)
        vm_dual_results = []
        vm_dual_max_cells = 0
        try:
            for fn_name, raw, vlen, at in dual_cases:
                res = vm_dual.call(
                    "mncs.test.utf8_scalar_dual_cap.v1",
                    fn_name,
                    [_byte_seq_val(raw), _u64_val(vlen), _u64_val(at)],
                )
                assert res["outcome"]["kind"] == "completed", (fn_name, res)
                vm_dual_max_cells = max(vm_dual_max_cells, res["record"]["usage"]["max_memory_cells"])
                vm_dual_results.append(res["execution"]["returned"])
            refused_spec16 = vm_dual.call(
                "mncs.std.utf8_scalar.v1",
                "step_scalar_generic",
                [_byte_seq_val([0x41]), _u64_val(1), _u64_val(0)],
                type_arguments=[sem.nat_arg(16)],
                expect_ok=False,
            )
            assert refused_spec16.get("result", {}).get("outcome", {}).get("kind") == "invalid_request", refused_spec16
        finally:
            vm_dual.close()

        oracle_dual = single_emit.Stage0ModuleOracle(
            "mncs.test.utf8_scalar_dual_cap.v1",
            [stage0_lib, single_emit.STDLIB_ROOT / "library"],
        )
        try:
            for idx, (fn_name, raw, vlen, at) in enumerate(dual_cases):
                s0 = oracle_dual.call(fn_name, [_byte_seq_val(raw), _u64_val(vlen), _u64_val(at)])
                assert s0["status"] == "returned", (fn_name, s0)
                assert s0["returned"] == vm_dual_results[idx], (idx, fn_name, vm_dual_results[idx], s0["returned"])
        finally:
            oracle_dual.close()

        summary = {
            "status": "passed",
            "provenance": {
                "specialization_closure_and_lowering": "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_module",
                "native_project_ssa_producer": "mncs.compiler.project.v1::compile_project",
                "transport_codec_and_sealer": "tools/native_vm_artifact.py",
                "independent_vm_admission_and_execution": str(single_emit.VM_BIN),
                "behavioral_oracle": f"locked Stage-0 mncs-compiler-stage0-probe ({stage0_rev})",
                "compiler_revision": compiler_rev,
                "vm_revision": vm_rev,
                "emitter_source_sha256": emitter_src_sha256,
            },
            "step_scalar_generic_4": {
                "root_module": "mncs.test.utf8_scalar_step4.v1",
                "specialization": "mncs.std.utf8_scalar.v1::step_scalar_generic<4>",
                "specialized_callable_identity": expected_spec4_sem,
                "input_sources": closure_digests_step4,
                "emitted_callables": [c["semantic"] for c in art4_1["callables"]],
                "generic_entrypoints": art4_1["generic_entrypoints"],
                "record_types": ssa4["record_types"],
                "symbolic_bounds_remaining": 0,
                "function_count": emit_step4_1["function_count"],
                "block_count": emit_step4_1["block_count"],
                "instruction_count": emit_step4_1["instruction_count"],
                "artifact_id": art4_1["artifact_id"],
                "artifact_sha256": art4_sha256,
                "artifact_bytes": len(art4_1_bytes),
                "deterministic_across_fresh_processes": True,
                "emit_process1_cold_wall_s": round(emit1_wall_s, 3),
                "emit_process1_cold_peak_rss_bytes": p1.peak_rss_bytes,
                "emit_process2_warm_wall_s": round(emit2_wall_s, 3),
                "emit_process2_warm_peak_rss_bytes": p2.peak_rss_bytes,
                "vm_admission_status": admit4_payload["status"],
                "vm_differential_cases": len(corpus),
                "vm_differential_matches": matched,
                "vm_direct_generic_dispatch_matched": True,
                "vm_uncompiled_spec8_refused": True,
                "vm_session_wall_s": round(vm_sess.elapsed_s, 4),
                "vm_resident_peak_rss_bytes": vm_sess.peak_rss_bytes,
                "vm_max_memory_cells": vm_max_cells,
                "vm_max_steps_per_call": vm_max_steps,
                "vm_total_steps": vm_total_steps,
            },
            "dual_capacity_non_aliasing": {
                "root_module": "mncs.test.utf8_scalar_dual_cap.v1",
                "specializations": [expected_spec4_sem, expected_spec8_sem],
                "distinct_identities": expected_spec4_sem != expected_spec8_sem,
                "spec4_input_bound": fn_by_sem[expected_spec4_sem]["inputs"][0]["ty"],
                "spec8_input_bound": fn_by_sem[expected_spec8_sem]["inputs"][0]["ty"],
                "artifact_id": art_dual["artifact_id"],
                "artifact_bytes": len(art_dual_bytes),
                "vm_differential_cases": len(dual_cases),
                "vm_differential_matches": len(dual_cases),
                "vm_uncompiled_spec16_refused": True,
                "vm_max_memory_cells": vm_dual_max_cells,
                "vm_resident_peak_rss_bytes": vm_dual.peak_rss_bytes,
            },
            "negative_rejections": {
                "uninstantiated_generic_root": {"ok": neg_uninst["ok"], "error_code": neg_uninst["error_code"]},
                "mismatched_specialization_bound": {"ok": neg_bound["ok"], "error_code": neg_bound["error_code"]},
                "mismatched_generic_arity": {"ok": neg_arity["ok"], "error_code": neg_arity["error_code"]},
                "zero_nat_bound": {"ok": neg_zero["ok"], "error_code": neg_zero["error_code"]},
                "unverified_reachable_provider_ssa": {"ok": neg_unverified_ssa["ok"], "error_code": neg_unverified_ssa["error_code"]},
                "vm_emit_concrete_bound_mismatch": {"ok": neg_vm_emit_bound_mismatch["ok"], "error_code": neg_vm_emit_bound_mismatch["error_code"]},
                "tampered_artifact_vm_admission": {"status": tamper_payload["status"], "returncode": admit_bad.returncode},
            },
        }
        if args.json_out:
            args.json_out.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
