#!/usr/bin/env python3
"""End-to-end verification of multi-module native Project SSA -> MNCS VM
emitter (`mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_module`) ->
canonical `mncs.vm.artifact/1` -> fresh `mncs-vm` differential execution.

Proves:
1. A scalar root module calling real Stdlib providers through BOTH repaired
   import paths — unaliased bare calls into `std/utf8_primitives.mncs` and
   a qualified transitive re-export into `std/utf8_scalar.mncs::is_whitespace_scalar`
   (while skipping uncalled non-scalar `step_scalar_generic` and an unreachable
   distractor module) — is compiled, closure-selected, verified, and lowered
   entirely in MNCS.
2. Original declaring-module identities are preserved across all 3 declaring
   modules (`mncs.test.utf8_app.v1`, `mncs.std.utf8_primitives.v1`,
   `mncs.std.utf8_scalar.v1`).
3. Two independent fresh probe processes produce bit-for-bit identical
   `mncs.vm.artifact/1` bytes and identical `artifact_id`.
4. A fresh canonical `mncs-vm` process admits and executes the artifact across
   160 inputs with 100% agreement against locked Stage-0 (`6a3f6907`), recording
   VM `memory_cells` and `steps` separately from resident bytes.
5. Fail-closed rejections: unverified project SSA (`error_code=1`), missing
   callee in reachable closure (`error_code=9`), mismatched callee signature
   (`error_code=10`), and tampered artifact bytes refused by `mncs-vm admit`.
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
import test_native_vm_emit as single_emit
import test_project as project
import test_sem as sem


REEXPORT_MID_SOURCE = """\
mncs 0.18;
module mncs.test.utf8_reexport.v1;
use mncs.std.utf8_primitives.v1;
use mncs.std.utf8_scalar.v1;

fn mid_is_ascii_space(value: byte) -> (result: bool) {
    return value == 32;
}
"""

UNUSED_DISTRACTOR_SOURCE = """\
mncs 0.18;
module mncs.test.unused_distractor.v1;
record UnusedBox { value: u64 }
fn wrap(x: u64) -> (result: UnusedBox) {
    return UnusedBox { value: x };
}
"""

UTF8_APP_ROOT_SOURCE = """\
mncs 0.18;
module mncs.test.utf8_app.v1;
use mncs.std.utf8_primitives.v1;
use mncs.test.utf8_reexport.v1 as u;

fn classify_lead_and_cont(lead: byte, cont: byte) -> (result: u64) {
    let need: u64 = utf8_lead_need(lead) as u64;
    let cont_ok: bool = utf8_first_continuation_valid(lead, cont);
    let ws: bool = u.is_whitespace_scalar(lead as u32);
    let score: u64 = need *% 10 +% select(cont_ok, 1, 0) +% select(ws, 100, 0);
    return score;
}
"""


def _build_project_args(identities, sources, target_index, stride=1024):
    req = project.request_value(identities, sources, stride=stride)
    return req["arguments"] + [sem.integer(target_index)]


def _build_compile_only_args(identities, sources, stride=1024):
    req = project.request_value(identities, sources, stride=stride)
    return req["arguments"]


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


def _mutate_first_call_in_target(project_result_wire, target_source_index, mutator):
    for mod in _walk_wire_flist(_wire_field(project_result_wire, "modules")):
        if _wire_field(mod, "source_index")["integer"]["value"] != target_source_index:
            continue
        ssa_sum = _wire_field(mod, "value_ssa")
        for fn in _walk_wire_flist(_wire_field(ssa_sum, "functions")):
            for blk in _walk_wire_flist(_wire_field(fn, "blocks")):
                for inst in _walk_wire_flist(_wire_field(blk, "instructions")):
                    if _wire_field(inst, "kind")["integer"]["value"] == 3:
                        mutator(inst)
                        return True
    return False


def _semantic_id_wire(ident_wire, text: str):
    raw = text.encode("utf-8")
    assert len(raw) <= 512
    padded = [{"byte": {"value": b}} for b in raw] + [{"byte": {"value": 0}}] * (512 - len(raw))
    _set_wire_field(ident_wire, "valid", {"boolean": {"value": True}})
    _set_wire_field(ident_wire, "len", sem.integer(len(raw)))
    _set_wire_field(ident_wire, "bytes", {"sequence": {"values": padded}})


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
    reexport_bytes = REEXPORT_MID_SOURCE.encode("utf-8")
    distractor_bytes = UNUSED_DISTRACTOR_SOURCE.encode("utf-8")
    app_bytes = UTF8_APP_ROOT_SOURCE.encode("utf-8")

    # Sorted lexicographically by source_id for ProjectRequest ordering.
    sources = [
        ("00_std_utf8_primitives.mncs", prim_path, prim_bytes),
        ("01_std_utf8_scalar.mncs", scal_path, scal_bytes),
        ("02_utf8_reexport.mncs", Path("02_utf8_reexport.mncs"), reexport_bytes),
        ("03_unused_distractor.mncs", Path("03_unused_distractor.mncs"), distractor_bytes),
        ("04_utf8_app.mncs", Path("04_utf8_app.mncs"), app_bytes),
    ]
    target_source_index = 4
    closure_digests = [
        (src_id, hashlib.sha256(raw).hexdigest())
        for src_id, _, raw in sources
    ]

    emitter_src_sha256 = hashlib.sha256((ROOT / "src" / "compiler" / "vm_emit.mncs").read_bytes()).hexdigest()
    compiler_rev = single_emit._git_head(ROOT)
    vm_rev = single_emit._git_head(single_emit.VM_REPO)
    stage0_rev = single_emit._stage0_lock_rev()

    minimal_sources = [
        ("00_std_utf8_primitives.mncs", prim_path, prim_bytes),
        ("01_utf8_app_small.mncs", Path("01_utf8_app_small.mncs"), (
            "mncs 0.18;\n"
            "module mncs.test.utf8_small.v1;\n"
            "use mncs.std.utf8_primitives.v1;\n"
            "fn need_u64(b: byte) -> (r: u64) { return utf8_lead_need(b) as u64; }\n"
        ).encode("utf-8")),
    ]

    with tempfile.TemporaryDirectory(prefix="mncs-native-project-vm-emit-") as tmpdir:
        tmp = Path(tmpdir)

        # 1. Process 1: Emit multi-module closure + run negative rejections
        t1 = time.monotonic()
        p1 = single_emit.NativeEmitterProbe()
        try:
            proj_recs = p1.record_types("mncs.compiler.vm_emit.v1")
            emit_args = _build_project_args(proj_recs, sources, target_source_index)
            emit_1 = p1.call(
                "compile_and_emit_project_vm_module",
                emit_args,
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert emit_1["ok"] is True, emit_1
            assert emit_1["function_count"] == 5, emit_1

            # Compile small 2-module project via Cranelift to get exact wire ProjectResult
            base_project_wire = p1.call_raw(
                "compile_project_for_emit",
                _build_compile_only_args(proj_recs, minimal_sources),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )

            # Negative A: unverified project via unresolved bare conflict
            bad_conflict_sources = [
                ("00_a.mncs", Path("00_a.mncs"), b"mncs 0.18; module demo.a; fn f(x: u64) -> (r: u64) { return x; }"),
                ("01_b.mncs", Path("01_b.mncs"), b"mncs 0.18; module demo.b; fn f(x: u64) -> (r: u64) { return x +% 1; }"),
                ("02_root.mncs", Path("02_root.mncs"), b"mncs 0.18; module demo.root; use demo.a; use demo.b; fn main(x: u64) -> (r: u64) { return f(x); }"),
            ]
            neg_conflict = p1.call(
                "compile_and_emit_project_vm_module",
                _build_project_args(proj_recs, bad_conflict_sources, 2),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert neg_conflict["ok"] is False and neg_conflict["error_code"] == 1, neg_conflict

            # Negative B: emit_verified_project_ssa rejects unverified ProjectResult (valid=False)
            unverified_wire = copy.deepcopy(base_project_wire)
            _set_wire_field(unverified_wire, "valid", {"boolean": {"value": False}})
            neg_unverified = p1.call(
                "emit_verified_project_ssa",
                [unverified_wire, sem.integer(1)],
                type_arguments=[],
            )
            assert neg_unverified["ok"] is False and neg_unverified["error_code"] == 1, neg_unverified

            # Negative C: emit_verified_project_ssa rejects missing callee (error_code=9)
            missing_callee_wire = copy.deepcopy(base_project_wire)
            assert _mutate_first_call_in_target(
                missing_callee_wire,
                1,
                lambda inst: _semantic_id_wire(
                    _wire_field(inst, "identity"),
                    "mncs:0.2:function:mncs.std.utf8_primitives.v1::nonexistent_helper",
                ),
            )
            neg_missing = p1.call(
                "emit_verified_project_ssa",
                [missing_callee_wire, sem.integer(1)],
                type_arguments=[],
            )
            assert neg_missing["ok"] is False and neg_missing["error_code"] == 9, neg_missing

            # Negative D: emit_verified_project_ssa rejects callee signature mismatch (error_code=10)
            mismatch_wire = copy.deepcopy(base_project_wire)
            def _flip_call_result_to_bool(inst):
                _set_wire_field(
                    inst,
                    "ty",
                    {
                        "finite": {
                            "type_identity": "mncs:0.2:finite-type:mncs.compiler.decl.v1::SemType",
                            "variant_identity": "mncs:0.2:finite-variant:mncs.compiler.decl.v1::SemType::TBool",
                            "discriminant": 0,
                            "payload": [],
                        }
                    },
                )
            assert _mutate_first_call_in_target(mismatch_wire, 1, _flip_call_result_to_bool)
            neg_mismatch = p1.call(
                "emit_verified_project_ssa",
                [mismatch_wire, sem.integer(1)],
                type_arguments=[],
            )
            assert neg_mismatch["ok"] is False and neg_mismatch["error_code"] == 10, neg_mismatch
        finally:
            p1.close()
        emit1_wall_s = time.monotonic() - t1

        # 2. Process 2: Independent fresh build for bit-for-bit determinism
        t2 = time.monotonic()
        p2 = single_emit.NativeEmitterProbe()
        try:
            proj_recs_2 = p2.record_types("mncs.compiler.vm_emit.v1")
            emit_2 = p2.call(
                "compile_and_emit_project_vm_module",
                _build_project_args(proj_recs_2, sources, target_source_index),
                type_arguments=[sem.nat_arg(64), sem.nat_arg(1024)],
            )
            assert emit_2["ok"] is True, emit_2
        finally:
            p2.close()
        emit2_wall_s = time.monotonic() - t2

        art1 = native_vm_artifact.package_native_vm_artifact(
            emit_1,
            app_bytes,
            allow_multi_module=True,
            closure_source_digests=closure_digests,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        art2 = native_vm_artifact.package_native_vm_artifact(
            emit_2,
            app_bytes,
            allow_multi_module=True,
            closure_source_digests=closure_digests,
            emitter_source_sha256=emitter_src_sha256,
            compiler_revision=compiler_rev,
            stage0_revision=stage0_rev,
        )
        art1_bytes = json.dumps(art1, separators=(",", ":")).encode("utf-8")
        art2_bytes = json.dumps(art2, separators=(",", ":")).encode("utf-8")
        assert art1_bytes == art2_bytes, "multi-module artifact emission must be bit-for-bit deterministic across fresh builds"
        assert art1["artifact_id"] == art2["artifact_id"]
        art_sha256 = hashlib.sha256(art1_bytes).hexdigest()

        # Verify exact declaring identities across the 3 declaring modules
        emitted_callables = [c["semantic"] for c in art1["callables"]]
        expected_callables = [
            "mncs:0.2:function:mncs.test.utf8_app.v1::classify_lead_and_cont",
            "mncs:0.2:function:mncs.std.utf8_primitives.v1::utf8_lead_need",
            "mncs:0.2:function:mncs.std.utf8_primitives.v1::utf8_first_continuation_valid",
            "mncs:0.2:function:mncs.std.utf8_scalar.v1::is_whitespace_scalar",
            "mncs:0.2:function:mncs.std.utf8_primitives.v1::is_utf8_continuation",
        ]
        assert emitted_callables == expected_callables, f"unexpected closure callables: {emitted_callables}"

        art_path = tmp / "utf8_app_multimodule.native.vm.json"
        art_path.write_bytes(art1_bytes)

        # 3. Independent `mncs-vm admit` + tamper fail-closed check
        admit = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(art_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit.returncode == 0, f"mncs-vm admit failed: {admit.stdout} {admit.stderr}"
        admit_payload = json.loads(admit.stdout)
        assert admit_payload["status"] == "admitted", admit_payload
        assert admit_payload["artifact_id"] == art1["artifact_id"]

        tampered = json.loads(art1_bytes.decode("utf-8"))
        tampered["assumptions"].append("tampered")
        tampered_path = tmp / "utf8_app_multimodule.tampered.vm.json"
        tampered_path.write_text(json.dumps(tampered, separators=(",", ":")))
        admit_bad = subprocess.run(
            [str(single_emit.VM_BIN), "admit", "--artifact", str(tampered_path)],
            capture_output=True, text=True, check=False,
        )
        assert admit_bad.returncode != 0, admit_bad.stdout
        tamper_payload = json.loads(admit_bad.stdout)
        assert tamper_payload["status"] == "refused", tamper_payload

        # 4. Fresh `mncs-vm serve` differential execution across 160 inputs vs locked Stage-0
        leads = [0, 9, 10, 13, 32, 127, 128, 160, 193, 194, 223, 224, 237, 240, 244, 255]
        conts = [0, 127, 128, 143, 144, 159, 160, 191, 192, 255]
        cases = [
            ("classify_lead_and_cont", [single_emit._byte_val(lead), single_emit._byte_val(cont)])
            for lead in leads
            for cont in conts
        ]

        vm_sess = single_emit.FreshVmSession(art_path)
        vm_max_cells = 0
        vm_max_steps = 0
        vm_total_steps = 0
        vm_results = []
        try:
            for fn_name, call_args in cases:
                res = vm_sess.call("mncs.test.utf8_app.v1", fn_name, call_args)
                assert res["outcome"]["kind"] == "completed", (fn_name, call_args, res)
                usage = res["record"]["usage"]
                vm_max_cells = max(vm_max_cells, usage["max_memory_cells"])
                vm_max_steps = max(vm_max_steps, usage["steps"])
                vm_total_steps += usage["steps"]
                vm_results.append((fn_name, call_args, res["execution"]["returned"]))
        finally:
            vm_sess.close()

        # Behavioral oracle on locked Stage-0 (role kept explicit in provenance)
        oracle_lib = tmp / "oracle_lib" / "test"
        oracle_lib.mkdir(parents=True)
        (oracle_lib / "utf8_reexport.mncs").write_bytes(reexport_bytes)
        (oracle_lib / "utf8_app.mncs").write_bytes(app_bytes)
        oracle = single_emit.Stage0ModuleOracle(
            "mncs.test.utf8_app.v1",
            [tmp / "oracle_lib", single_emit.STDLIB_ROOT / "library"],
        )
        try:
            for fn_name, call_args, vm_ret in vm_results:
                s0 = oracle.call(fn_name, call_args)
                assert s0["status"] == "returned", (fn_name, call_args, s0)
                assert s0["returned"] == vm_ret, f"MISMATCH {fn_name}({call_args}): vm={vm_ret} stage0={s0['returned']}"
        finally:
            oracle.close()

        summary = {
            "status": "passed",
            "provenance": {
                "closure_selection_and_executable_lowering": "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_module",
                "native_project_ssa_producer": "mncs.compiler.project.v1::compile_project",
                "transport_codec_and_sealer": "tools/native_vm_artifact.py",
                "independent_vm_admission_and_execution": str(single_emit.VM_BIN),
                "behavioral_oracle": f"locked Stage-0 mncs-compiler-stage0-probe ({stage0_rev})",
                "compiler_revision": compiler_rev,
                "vm_revision": vm_rev,
                "emitter_source_sha256": emitter_src_sha256,
            },
            "multi_module_utf8_closure": {
                "root_module": "mncs.test.utf8_app.v1",
                "input_sources": closure_digests,
                "unreachable_distractor_excluded": "03_unused_distractor.mncs",
                "uncalled_non_scalar_provider_fn_skipped": "mncs:0.2:function:mncs.std.utf8_scalar.v1::step_scalar_generic",
                "emitted_callables": emitted_callables,
                "declaring_modules": [
                    "mncs.test.utf8_app.v1",
                    "mncs.std.utf8_primitives.v1",
                    "mncs.std.utf8_scalar.v1",
                ],
                "function_count": emit_1["function_count"],
                "block_count": emit_1["block_count"],
                "instruction_count": emit_1["instruction_count"],
                "artifact_id": art1["artifact_id"],
                "artifact_sha256": art_sha256,
                "artifact_bytes": len(art1_bytes),
                "deterministic_across_fresh_processes": True,
                "emit_process1_wall_s": round(emit1_wall_s, 3),
                "emit_process1_peak_rss_bytes": p1.peak_rss_bytes,
                "emit_process2_wall_s": round(emit2_wall_s, 3),
                "emit_process2_peak_rss_bytes": p2.peak_rss_bytes,
                "vm_admission_status": admit_payload["status"],
                "vm_differential_cases": len(cases),
                "vm_differential_matches": len(cases),
                "vm_session_wall_s": round(vm_sess.elapsed_s, 4),
                "vm_resident_peak_rss_bytes": vm_sess.peak_rss_bytes,
                "vm_max_memory_cells": vm_max_cells,
                "vm_max_steps_per_call": vm_max_steps,
                "vm_total_steps": vm_total_steps,
            },
            "negative_rejections": {
                "unverified_project_bare_conflict": {"ok": neg_conflict["ok"], "error_code": neg_conflict["error_code"]},
                "unverified_project_result_wire": {"ok": neg_unverified["ok"], "error_code": neg_unverified["error_code"]},
                "missing_callee_in_closure": {"ok": neg_missing["ok"], "error_code": neg_missing["error_code"]},
                "mismatched_callee_signature": {"ok": neg_mismatch["ok"], "error_code": neg_mismatch["error_code"]},
                "tampered_artifact_vm_admission": {"status": tamper_payload["status"], "returncode": admit_bad.returncode},
            },
        }
        if args.json_out:
            args.json_out.write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
