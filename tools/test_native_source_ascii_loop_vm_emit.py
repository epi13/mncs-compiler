#!/usr/bin/env python3
"""End-to-end differential, non-aliasing, determinism, and fail-closed proof for
compiling and emitting unchanged `mncs.compiler.source.v1::ascii<32>` (and its
bounded loop in `ascii_view<32>`) from native Project SSA
(`mncs.compiler.project.v1` -> `mncs.compiler.vm_emit.v1`) and independently
admitting/executing it in a fresh canonical VM (`mncs-vm`).

Provenance boundary:
- Concrete Nat specialization, bounded-loop + ViewConstruct lowering, fixed-point
  sequence type propagation, closure selection, and call/CFG verification:
      100% MNCS (`mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_module`).
- DAG transport encoding & SHA-256 sealing:
      `tools/native_vm_artifact.py` (no lowering or closure selection).
- Independent admission & execution:
      fresh `mncs-vm` release binary (`da99dee`).
- Behavioral oracle:
      locked Stage-0 `mncs-compiler-stage0-probe` (`6a3f6907`).
"""

import argparse
import hashlib
import json
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


ROOT_ASCII32_SOURCE = """\
mncs 0.19;
module mncs.test.source_ascii_32.v1;

use mncs.compiler.source.v1 as src;

fn ascii32(buf: [byte; up_to 32]) -> (result: bool) {
    return src.ascii<32>(buf);
}
"""

ROOT_DUAL_CAP_SOURCE = """\
mncs 0.19;
module mncs.test.source_ascii_dual.v1;

use mncs.compiler.source.v1 as src;

fn ascii32(buf: [byte; up_to 32]) -> (result: bool) {
    return src.ascii<32>(buf);
}

fn ascii24(buf: [byte; up_to 24]) -> (result: bool) {
    return src.ascii<24>(buf);
}
"""

COUNTED_LOOP_SOURCE = """\
mncs 0.19;
module mncs.test.counted_loop_up_to.v1;

fn run_sum6(stop_at: u64) -> (result: u64) {
    iterate i up_to 6 carrying total: u64 = 0 {
        if i >= stop_at { return total; }
        next total = total + i + 1;
    }
    return total;
}
"""

NEG_UNINSTANTIATED_ROOT = """\
mncs 0.19;
module mncs.test.neg_ascii_uninst.v1;

use mncs.compiler.source.v1 as src;

fn wrapper<M: Nat>(buf: [byte; up_to M]) -> (result: bool) {
    return src.ascii<M>(buf);
}
"""

NEG_MISMATCHED_BOUND = """\
mncs 0.19;
module mncs.test.neg_ascii_mismatch.v1;

use mncs.compiler.source.v1 as src;

fn bad_ascii(buf: [byte; up_to 16]) -> (result: bool) {
    return src.ascii<32>(buf);
}
"""

NEG_ZERO_BOUND = """\
mncs 0.19;
module mncs.test.neg_ascii_zero.v1;

use mncs.compiler.source.v1 as src;

fn bad_zero(buf: [byte; up_to 16]) -> (result: bool) {
    return src.ascii<0>(buf);
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
            assert k not in ("param", "up_to_param", "generic_param"), (
                f"symbolic generic survived at {path}.{k}: {v}"
            )
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


def _instr_kinds(fn_obj):
    out = set()
    for blk in fn_obj["blocks"]:
        for inst in blk["instructions"]:
            kind = inst["kind"]
            out.add(next(iter(kind.keys())) if isinstance(kind, dict) else kind)
    return out


def _ascii32_corpus():
    """Corpus spanning empty, boundary lengths (1..32), group boundaries, and early non-ASCII exits."""
    def ascii_bytes(n):
        return [0x20 + ((i * 7) % 95) for i in range(n)]

    def with_bad(n, pos, val):
        buf = ascii_bytes(n)
        buf[pos] = val
        return buf

    return [
        ("empty_0", []),
        ("valid_1", [0x41]),
        ("valid_nul_del_15", [0x00] + ascii_bytes(13) + [0x7F]),
        ("valid_exact_group_16", ascii_bytes(16)),
        ("valid_group_plus_tail1_17", ascii_bytes(17)),
        ("valid_group_plus_tail15_31", ascii_bytes(31)),
        ("valid_two_groups_32", [0x00] + ascii_bytes(30) + [0x7F]),
        ("bad_len1_at0_0x80", [0x80]),
        ("bad_len1_at0_0xff", [0xFF]),
        ("bad_len15_tail_at14", with_bad(15, 14, 0x80)),
        ("bad_len16_at0", with_bad(16, 0, 0x80)),
        ("bad_len16_mid7", with_bad(16, 7, 0xC3)),
        ("bad_len16_at15", with_bad(16, 15, 0xFF)),
        ("bad_len17_tail_at16", with_bad(17, 16, 0x80)),
        ("bad_len17_group0_at5", with_bad(17, 5, 0x80)),
        ("bad_len31_tail_at30", with_bad(31, 30, 0xF4)),
        ("bad_len32_at0", with_bad(32, 0, 0x80)),
        ("bad_len32_mid7", with_bad(32, 7, 0x80)),
        ("bad_len32_group0_at15", with_bad(32, 15, 0x80)),
        ("bad_len32_group1_at16", with_bad(32, 16, 0x80)),
        ("bad_len32_last_at31", with_bad(32, 31, 0xFF)),
    ]


def _run_admit_check(artifact_path):
    res = subprocess.run(
        [str(single_emit.VM_BIN), "admit", "--artifact", str(artifact_path)],
        capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, f"mncs-vm admit failed: {res.stdout} {res.stderr}"
    payload = json.loads(res.stdout)
    assert payload["status"] == "admitted", payload
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    assert single_emit.PROBE_BIN.exists(), f"probe missing: {single_emit.PROBE_BIN}"
    assert single_emit.VM_BIN.exists(), f"mncs-vm missing: {single_emit.VM_BIN}"

    source_path = ROOT / "src" / "compiler" / "source.mncs"
    source_bytes = source_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    root32_bytes = ROOT_ASCII32_SOURCE.encode("utf-8")
    dual_bytes = ROOT_DUAL_CAP_SOURCE.encode("utf-8")
    counted_bytes = COUNTED_LOOP_SOURCE.encode("utf-8")

    sources_ascii32 = [
        ("00_compiler_source.mncs", source_path, source_bytes),
        ("01_root_ascii32.mncs", Path("01_root_ascii32.mncs"), root32_bytes),
    ]
    sources_dual = [
        ("00_compiler_source.mncs", source_path, source_bytes),
        ("01_root_ascii_dual.mncs", Path("01_root_ascii_dual.mncs"), dual_bytes),
    ]
    sources_counted = [
        ("00_counted_loop_up_to.mncs", Path("00_counted_loop_up_to.mncs"), counted_bytes),
    ]
    target_idx = 1

    closure_digests_32 = [(sid, hashlib.sha256(raw).hexdigest()) for sid, _, raw in sources_ascii32]
    closure_digests_dual = [(sid, hashlib.sha256(raw).hexdigest()) for sid, _, raw in sources_dual]
    closure_digests_counted = [(sid, hashlib.sha256(raw).hexdigest()) for sid, _, raw in sources_counted]
    emitter_src_sha256 = hashlib.sha256((ROOT / "src" / "compiler" / "vm_emit.mncs").read_bytes()).hexdigest()
    compiler_rev = single_emit._git_head(ROOT)
    vm_rev = single_emit._git_head(single_emit.VM_REPO)
    stage0_rev = single_emit._stage0_lock_rev()

    with tempfile.TemporaryDirectory(prefix="mncs-source-ascii-loop-vm-") as tmpdir:
        tmp = Path(tmpdir)

        # ------------------------------------------------------------------
        # 1. Process 1 (single-target emit): Emit ascii<32>, then separate bounded
        #    probe sessions for dual/counted & negative checks so RSS stays <8 GiB.
        # ------------------------------------------------------------------
        t1 = time.monotonic()
        p1 = single_emit.NativeEmitterProbe()
        try:
            proj_recs = p1.record_types("mncs.compiler.vm_emit.v1")
            emit32_1 = p1.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(proj_recs, sources_ascii32, target_idx),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert emit32_1["ok"] is True, emit32_1
            assert emit32_1["function_count"] == 3, emit32_1  # ascii32, ascii<32>, ascii_view<32>
        finally:
            p1.close()
        emit1_wall_s = time.monotonic() - t1

        p_aux = single_emit.NativeEmitterProbe()
        try:
            proj_recs_aux = p_aux.record_types("mncs.compiler.vm_emit.v1")
            emit_dual = p_aux.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(proj_recs_aux, sources_dual, target_idx),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert emit_dual["ok"] is True, emit_dual
            assert emit_dual["function_count"] == 6, emit_dual  # ascii32, ascii24, ascii<32>, ascii_view<32>, ascii<24>, ascii_view<24>

            emit_counted = p_aux.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(proj_recs_aux, sources_counted, 0),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert emit_counted["ok"] is True, emit_counted
            assert emit_counted["function_count"] == 1, emit_counted  # run_sum6
        finally:
            p_aux.close()

        p_neg = single_emit.NativeEmitterProbe()
        try:
            proj_recs_neg = p_neg.record_types("mncs.compiler.vm_emit.v1")
            neg_uninst = p_neg.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(
                    proj_recs_neg,
                    [
                        ("00_compiler_source.mncs", source_path, source_bytes),
                        ("01_neg_uninst.mncs", Path("01_neg_uninst.mncs"), NEG_UNINSTANTIATED_ROOT.encode("utf-8")),
                    ],
                    target_idx,
                ),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_uninst["ok"] is False and neg_uninst["error_code"] == 11, neg_uninst

            neg_bound = p_neg.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(
                    proj_recs_neg,
                    [
                        ("00_compiler_source.mncs", source_path, source_bytes),
                        ("01_neg_mismatch.mncs", Path("01_neg_mismatch.mncs"), NEG_MISMATCHED_BOUND.encode("utf-8")),
                    ],
                    target_idx,
                ),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_bound["ok"] is False and neg_bound["error_code"] in (1, 10), neg_bound

            neg_zero = p_neg.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(
                    proj_recs_neg,
                    [
                        ("00_compiler_source.mncs", source_path, source_bytes),
                        ("01_neg_zero.mncs", Path("01_neg_zero.mncs"), NEG_ZERO_BOUND.encode("utf-8")),
                    ],
                    target_idx,
                ),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_zero["ok"] is False and neg_zero["error_code"] in (1, 11), neg_zero

            # Direct vm_emit concrete bound mismatch check (`error_code == 10`) via tampered call target <32> -> <24>
            compile_args_32 = proj_emit._build_compile_only_args(proj_recs_neg, sources_ascii32)
            compiled_wire = p_neg.call_raw(
                "compile_project_for_emit",
                compile_args_32,
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            tampered_root_bytes = root32_bytes.replace(b"<32>", b"<24>", 1)
            assert len(tampered_root_bytes) == len(root32_bytes)
            tampered_src_args = proj_emit._build_compile_only_args(
                proj_recs_neg,
                [
                    ("00_compiler_source.mncs", source_path, source_bytes),
                    ("01_root_ascii32.mncs", Path("01_root_ascii32.mncs"), tampered_root_bytes),
                ],
            )
            neg_vm_emit_bound_mismatch = p_neg.call(
                "emit_verified_project_ssa_with_sources",
                tampered_src_args + [compiled_wire, sem.integer(target_idx)],
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_vm_emit_bound_mismatch["ok"] is False and neg_vm_emit_bound_mismatch["error_code"] == 10, (
                neg_vm_emit_bound_mismatch
            )

            # Negative check: unverified reachable source.mncs SSA (`value_ssa.valid = false`)
            mutated = False
            for mod_w in proj_emit._walk_wire_flist(proj_emit._wire_field(compiled_wire, "modules")):
                if proj_emit._wire_field(mod_w, "source_index")["integer"]["value"] == 0:
                    proj_emit._set_wire_field(
                        proj_emit._wire_field(mod_w, "value_ssa"),
                        "valid",
                        {"boolean": {"value": False}},
                    )
                    mutated = True
            assert mutated
            neg_unverified_ssa = p_neg.call(
                "emit_verified_project_ssa_with_sources",
                compile_args_32 + [compiled_wire, sem.integer(target_idx)],
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_unverified_ssa["ok"] is False and neg_unverified_ssa["error_code"] == 1, neg_unverified_ssa
        finally:
            p_neg.close()

        # ------------------------------------------------------------------
        # 2. Process 2: Fresh emission & bit-for-bit determinism
        # ------------------------------------------------------------------
        t2 = time.monotonic()
        p2 = single_emit.NativeEmitterProbe()
        try:
            proj_recs_2 = p2.record_types("mncs.compiler.vm_emit.v1")
            emit32_2 = p2.call(
                "compile_and_emit_project_vm_module",
                proj_emit._build_project_args(proj_recs_2, sources_ascii32, target_idx),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert emit32_2["ok"] is True, emit32_2
        finally:
            p2.close()
        emit2_wall_s = time.monotonic() - t2

        art32_1 = native_vm_artifact.package_native_vm_artifact(
            emit32_1,
            root32_bytes,
            allow_multi_module=True,
            closure_source_digests=closure_digests_32,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        art32_2 = native_vm_artifact.package_native_vm_artifact(
            emit32_2,
            root32_bytes,
            allow_multi_module=True,
            closure_source_digests=closure_digests_32,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        art32_1_bytes = json.dumps(art32_1, separators=(",", ":")).encode("utf-8")
        art32_2_bytes = json.dumps(art32_2, separators=(",", ":")).encode("utf-8")
        assert art32_1_bytes == art32_2_bytes, "ascii<32> artifact must be bit-for-bit deterministic"
        assert art32_1["artifact_id"] == art32_2["artifact_id"]
        art32_sha256 = hashlib.sha256(art32_1_bytes).hexdigest()

        ssa32 = _extract_dag_module(art32_1)
        _assert_no_symbolic_bounds(ssa32)
        assert "bounded-iteration" in art32_1["source"]["target"]["assumptions"]
        assert "bounded over" in art32_1["source"]["target"]["assumptions"]["bounded-iteration"]

        spec32_hash = hashlib.sha256(b"value:32").hexdigest()[:8]
        spec24_hash = hashlib.sha256(b"value:24").hexdigest()[:8]
        expected_ascii32_sem = f"mncs:0.2:function:mncs.compiler.source.v1::ascii__spec_{spec32_hash}"
        expected_view32_sem = f"mncs:0.2:function:mncs.compiler.source.v1::ascii_view__spec_{spec32_hash}"
        expected_ascii24_sem = f"mncs:0.2:function:mncs.compiler.source.v1::ascii__spec_{spec24_hash}"
        expected_view24_sem = f"mncs:0.2:function:mncs.compiler.source.v1::ascii_view__spec_{spec24_hash}"

        fn_by_sem_32 = {fn["semantic_identity"]: fn for fn in ssa32["functions"]}
        assert expected_ascii32_sem in fn_by_sem_32 and expected_view32_sem in fn_by_sem_32
        view32_fn = fn_by_sem_32[expected_view32_sem]
        assert view32_fn["inputs"][0]["ty"] == {"sequence": {"element": "byte", "bound": {"up_to": 32}}}
        assert view32_fn["cycle_policy"] == "bounded_iteration_only"
        assert len(view32_fn["bounded_iterations"]) == 1
        bi32 = view32_fn["bounded_iterations"][0]
        assert bi32["bound"] == 32
        assert bi32["domain"] == {"over_sequence": {"element_type": "byte"}}
        assert bi32["sequence_bound"] == {"up_to": 32}
        assert len(bi32["body_blocks"]) > 0
        view32_kinds = _instr_kinds(view32_fn)
        assert {"ViewConstruct", "SequenceLength", "SequenceProject", "IntegerCompare", "Integer", "Constant"}.issubset(
            view32_kinds
        ), view32_kinds

        # Verify dual-capacity non-aliasing (<32> vs <24>)
        art_dual = native_vm_artifact.package_native_vm_artifact(
            emit_dual,
            dual_bytes,
            allow_multi_module=True,
            closure_source_digests=closure_digests_dual,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        ssa_dual = _extract_dag_module(art_dual)
        _assert_no_symbolic_bounds(ssa_dual)
        fn_by_sem_dual = {fn["semantic_identity"]: fn for fn in ssa_dual["functions"]}
        assert expected_ascii32_sem in fn_by_sem_dual and expected_ascii24_sem in fn_by_sem_dual
        assert expected_view32_sem in fn_by_sem_dual and expected_view24_sem in fn_by_sem_dual
        assert fn_by_sem_dual[expected_view32_sem]["bounded_iterations"][0]["bound"] == 32
        assert fn_by_sem_dual[expected_view24_sem]["bounded_iterations"][0]["bound"] == 24
        assert fn_by_sem_dual[expected_view32_sem]["inputs"][0]["ty"]["sequence"]["bound"] == {"up_to": 32}
        assert fn_by_sem_dual[expected_view24_sem]["inputs"][0]["ty"]["sequence"]["bound"] == {"up_to": 24}

        # Verify counted `up_to` artifact
        art_counted = native_vm_artifact.package_native_vm_artifact(
            emit_counted,
            counted_bytes,
            allow_multi_module=True,
            closure_source_digests=closure_digests_counted,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        ssa_counted = _extract_dag_module(art_counted)
        _assert_no_symbolic_bounds(ssa_counted)
        sum6_fn = next(f for f in ssa_counted["functions"] if f["semantic_identity"].endswith("::run_sum6"))
        assert sum6_fn["bounded_iterations"][0]["bound"] == 6
        assert sum6_fn["bounded_iterations"][0]["domain"] == "attempts"

        # ------------------------------------------------------------------
        # 3. Independent `mncs-vm admit` + tamper refusal
        # ------------------------------------------------------------------
        art32_path = tmp / "source_ascii_32.native.vm.json"
        art32_path.write_bytes(art32_1_bytes)
        admit32_payload = _run_admit_check(art32_path)
        assert admit32_payload["artifact_id"] == art32_1["artifact_id"]

        art_dual_path = tmp / "source_ascii_dual.native.vm.json"
        art_dual_bytes = json.dumps(art_dual, separators=(",", ":")).encode("utf-8")
        art_dual_path.write_bytes(art_dual_bytes)
        _run_admit_check(art_dual_path)

        art_counted_path = tmp / "counted_loop_up_to.native.vm.json"
        art_counted_path.write_bytes(json.dumps(art_counted, separators=(",", ":")).encode("utf-8"))
        _run_admit_check(art_counted_path)

        tampered = json.loads(art32_1_bytes.decode("utf-8"))
        tampered["assumptions"].append("tampered")
        tampered_path = tmp / "source_ascii_32.tampered.vm.json"
        tampered_path.write_text(json.dumps(tampered, separators=(",", ":")))
        admit_bad = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(tampered_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit_bad.returncode != 0
        tamper_payload = json.loads(admit_bad.stdout)
        assert tamper_payload["status"] == "refused", tamper_payload

        # ------------------------------------------------------------------
        # 4. Differential VM execution vs locked Stage-0
        # ------------------------------------------------------------------
        corpus = _ascii32_corpus()
        vm_sess = single_emit.FreshVmSession(art32_path)
        vm_max_cells = 0
        vm_max_steps = 0
        vm_total_steps = 0
        vm_results = []
        try:
            for name, raw in corpus:
                res = vm_sess.call("mncs.test.source_ascii_32.v1", "ascii32", [_byte_seq_val(raw)])
                assert res["outcome"]["kind"] == "completed", (name, res)
                usage = res["record"]["usage"]
                vm_max_cells = max(vm_max_cells, usage["max_memory_cells"])
                vm_max_steps = max(vm_max_steps, usage["steps"])
                vm_total_steps += usage["steps"]
                vm_results.append((name, res["execution"]["returned"]))

            direct_res = vm_sess.call(
                "mncs.compiler.source.v1",
                "ascii",
                [_byte_seq_val([0x41] * 32)],
                type_arguments=[sem.nat_arg(32)],
            )
            assert direct_res["outcome"]["kind"] == "completed", direct_res
            assert direct_res["execution"]["returned"] == [{"boolean": {"value": True}}], direct_res

            refused_spec24 = vm_sess.call(
                "mncs.compiler.source.v1",
                "ascii",
                [_byte_seq_val([0x41])],
                type_arguments=[sem.nat_arg(24)],
                expect_ok=False,
            )
            assert refused_spec24.get("result", {}).get("outcome", {}).get("kind") == "invalid_request", refused_spec24
        finally:
            vm_sess.close()

        # Verify bounded iteration budget enforcement in mncs-vm (`iterations = 1` vs 32-byte 2-group input)
        env_path = tmp / "tight_iters_env.json"
        env_path.write_text(json.dumps({"limits": [{"dimension": "iterations", "limit": 1}]}))
        args_32_path = tmp / "args_32_ascii.json"
        args_32_path.write_text(json.dumps([_byte_seq_val([0x41] * 32)]))
        budget_run = subprocess.run(
            [
                str(single_emit.VM_BIN),
                "run",
                "--artifact", str(art32_path),
                "--callable", "mncs.test.source_ascii_32.v1::ascii32",
                "--args", str(args_32_path),
                "--envelope", str(env_path),
            ],
            capture_output=True, text=True, check=False,
        )
        budget_payload = json.loads(budget_run.stdout)
        assert budget_payload["outcome"] == {"kind": "budget_exhausted", "dimension": "iterations"}, budget_payload

        # Locked Stage-0 oracle on identical root + unchanged source.mncs
        stage0_lib = tmp / "stage0_lib"
        (stage0_lib / "compiler").mkdir(parents=True, exist_ok=True)
        (stage0_lib / "test").mkdir(parents=True, exist_ok=True)
        (stage0_lib / "compiler" / "source.mncs").write_bytes(source_bytes)
        (stage0_lib / "test" / "source_ascii_32.mncs").write_bytes(root32_bytes)
        (stage0_lib / "test" / "source_ascii_dual.mncs").write_bytes(dual_bytes)
        (stage0_lib / "test" / "counted_loop_up_to.mncs").write_bytes(counted_bytes)

        oracle32 = single_emit.Stage0ModuleOracle("mncs.test.source_ascii_32.v1", [stage0_lib])
        matched = 0
        try:
            for idx, (name, raw) in enumerate(corpus):
                s0 = oracle32.call("ascii32", [_byte_seq_val(raw)])
                assert s0["status"] == "returned", (name, s0)
                assert s0["returned"] == vm_results[idx][1], (
                    f"ascii<32> mismatch on {name}: vm={vm_results[idx][1]} stage0={s0['returned']}"
                )
                matched += 1
        finally:
            oracle32.close()
        assert matched == len(corpus)

        # Dual-cap (<32> + <24>) differential check
        dual_cases = [
            ("ascii32", [0x41] * 32),
            ("ascii32", ([0x41] * 31) + [0x80]),
            ("ascii24", [0x41] * 24),
            ("ascii24", ([0x41] * 23) + [0xFF]),
            ("ascii24", []),
        ]
        vm_dual = single_emit.FreshVmSession(art_dual_path)
        vm_dual_results = []
        try:
            for fn_name, raw in dual_cases:
                res = vm_dual.call("mncs.test.source_ascii_dual.v1", fn_name, [_byte_seq_val(raw)])
                assert res["outcome"]["kind"] == "completed", (fn_name, res)
                vm_dual_results.append(res["execution"]["returned"])
        finally:
            vm_dual.close()

        oracle_dual = single_emit.Stage0ModuleOracle("mncs.test.source_ascii_dual.v1", [stage0_lib])
        try:
            for idx, (fn_name, raw) in enumerate(dual_cases):
                s0 = oracle_dual.call(fn_name, [_byte_seq_val(raw)])
                assert s0["status"] == "returned" and s0["returned"] == vm_dual_results[idx]
        finally:
            oracle_dual.close()

        # Counted up_to loop differential check
        counted_cases = [0, 1, 4, 6, 10]
        vm_counted = single_emit.FreshVmSession(art_counted_path)
        vm_counted_results = []
        try:
            for stop_at in counted_cases:
                res = vm_counted.call("mncs.test.counted_loop_up_to.v1", "run_sum6", [_u64_val(stop_at)])
                assert res["outcome"]["kind"] == "completed", (stop_at, res)
                vm_counted_results.append(res["execution"]["returned"])
        finally:
            vm_counted.close()

        oracle_counted = single_emit.Stage0ModuleOracle("mncs.test.counted_loop_up_to.v1", [stage0_lib])
        try:
            for idx, stop_at in enumerate(counted_cases):
                s0 = oracle_counted.call("run_sum6", [_u64_val(stop_at)])
                assert s0["status"] == "returned" and s0["returned"] == vm_counted_results[idx]
        finally:
            oracle_counted.close()

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
            "source_ascii_32": {
                "root_module": "mncs.test.source_ascii_32.v1",
                "specialization": "mncs.compiler.source.v1::ascii<32>",
                "unchanged_source_mncs_sha256": source_sha256,
                "specialized_callable_identities": [expected_ascii32_sem, expected_view32_sem],
                "emitted_callables": [c["semantic"] for c in art32_1["callables"]],
                "generic_entrypoints": art32_1["generic_entrypoints"],
                "bounded_iterations": view32_fn["bounded_iterations"],
                "view_instructions_present": sorted(list(view32_kinds)),
                "symbolic_bounds_remaining": 0,
                "function_count": emit32_1["function_count"],
                "block_count": emit32_1["block_count"],
                "instruction_count": emit32_1["instruction_count"],
                "artifact_id": art32_1["artifact_id"],
                "artifact_sha256": art32_sha256,
                "artifact_bytes": len(art32_1_bytes),
                "deterministic_across_fresh_processes": True,
                "emit_process1_wall_s": round(emit1_wall_s, 3),
                "emit_process1_peak_rss_bytes": p1.peak_rss_bytes,
                "emit_process2_wall_s": round(emit2_wall_s, 3),
                "emit_process2_peak_rss_bytes": p2.peak_rss_bytes,
                "vm_admission_status": admit32_payload["status"],
                "vm_differential_cases": len(corpus),
                "vm_differential_matches": matched,
                "vm_direct_generic_dispatch_matched": True,
                "vm_uncompiled_spec24_refused": True,
                "vm_iteration_budget_exhausted_enforced": True,
                "vm_session_wall_s": round(vm_sess.elapsed_s, 4),
                "vm_resident_peak_rss_bytes": vm_sess.peak_rss_bytes,
                "vm_max_memory_cells": vm_max_cells,
                "vm_max_steps_per_call": vm_max_steps,
                "vm_total_steps": vm_total_steps,
            },
            "dual_capacity_non_aliasing": {
                "root_module": "mncs.test.source_ascii_dual.v1",
                "specializations": [expected_ascii32_sem, expected_ascii24_sem],
                "view_specializations": [expected_view32_sem, expected_view24_sem],
                "distinct_identities": expected_ascii32_sem != expected_ascii24_sem,
                "spec32_bound": fn_by_sem_dual[expected_view32_sem]["bounded_iterations"][0]["bound"],
                "spec24_bound": fn_by_sem_dual[expected_view24_sem]["bounded_iterations"][0]["bound"],
                "artifact_id": art_dual["artifact_id"],
                "vm_differential_matches": len(dual_cases),
            },
            "counted_up_to_loop": {
                "root_module": "mncs.test.counted_loop_up_to.v1",
                "bound": sum6_fn["bounded_iterations"][0]["bound"],
                "domain": sum6_fn["bounded_iterations"][0]["domain"],
                "artifact_id": art_counted["artifact_id"],
                "vm_differential_matches": len(counted_cases),
            },
            "negative_rejections": {
                "uninstantiated_generic_root": {"ok": neg_uninst["ok"], "error_code": neg_uninst["error_code"]},
                "mismatched_specialization_bound": {"ok": neg_bound["ok"], "error_code": neg_bound["error_code"]},
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
