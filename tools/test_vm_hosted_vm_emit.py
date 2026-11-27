#!/usr/bin/env python3
"""Emit mncs.compiler.vm_emit.v1::compile_and_emit_vm_module<4,32> through
compile_and_emit_project_vm_callable<4096,1024>, execute the compiler twice
inside fresh canonical mncs-vm #1 on a <=128-byte scalar/call/branch module,
and seal + admit + execute VM #1's output inside fresh canonical mncs-vm #2
with full output and type_identity parity against locked Stage-0.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

import compiler_diagnosis
import native_vm_artifact
import test_native_decl_vm_emit as decl_vm
import test_native_vm_emit as single_emit

ROOT = decl_vm.ROOT
SEM = decl_vm.sem
BATCH = decl_vm.batch

CALLER_NAME = "10_vm_emit_module_caller.mncs"
CALLER_MODULE = "mncs.test.vm_emit_module_caller.v1"
CALLER_FN = "call_compile_and_emit_vm_module"
CALLER_TARGET = f"mncs:0.2:function:{CALLER_MODULE}::{CALLER_FN}"
CALLER_SOURCE = (
    f"mncs 0.19;\n"
    f"module {CALLER_MODULE};\n"
    f"use {decl_vm.EMITTER} as vm_emit;\n\n"
    f"fn {CALLER_FN}(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64) "
    f"-> (result: vm_emit.VmEmitResult) {{\n"
    f"    return vm_emit.compile_and_emit_vm_module<4, 32>(pages, stride, total);\n"
    f"}}\n"
).encode()

SAMPLE_MODULE_NAME = "mncs.a.b.v1"
SAMPLE_MODULE_BYTES = (
    b"mncs 0.19; module mncs.a.b.v1; "
    b"fn c(x:u64)->(r:u64){return x+%1;} "
    b"fn m(n:u64)->(r:u64){if n>0{return c(n);}return 0;}"
)
assert len(SAMPLE_MODULE_BYTES) <= 128, len(SAMPLE_MODULE_BYTES)

SAMPLE_CASES = [
    ("c", 0, 1),
    ("c", 1, 2),
    ("c", 41, 42),
    ("m", 0, 0),
    ("m", 1, 2),
    ("m", 41, 42),
]


class VmSessionWithBudget(single_emit.FreshVmSession):
    """Canonical mncs-vm serve session allowing an explicit per-call step budget."""

    def call_with_budget(
        self,
        module_name: str,
        function: str,
        args: list[dict],
        *,
        step_budget: int = 100_000_000,
        type_arguments: list[dict] | None = None,
    ) -> dict:
        self.req_id += 1
        req = {
            "schema_version": "mncs.vm.session/1",
            "id": self.req_id,
            "request": {
                "schema_version": "0.1",
                "target": {"module": module_name, "function": function},
                "arguments": args,
                "type_arguments": type_arguments or [],
                "step_budget": step_budget,
            },
        }
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            stderr = self.proc.stderr.read() if self.proc.stderr else ""
            raise AssertionError(f"mncs-vm serve terminated: {stderr}")
        reply = json.loads(line)
        assert reply.get("ok") is True, reply
        return reply["result"]


def build_sources() -> list[tuple[str, Path, bytes]]:
    _, ssa_sources = decl_vm.select_sources("ssa_lower_flow_unit")
    base_sources = ssa_sources[:-1]
    project_path = ROOT / "src/compiler/project.mncs"
    vm_emit_path = ROOT / "src/compiler/vm_emit.mncs"
    return list(base_sources) + [
        ("08_project.mncs", project_path, project_path.read_bytes()),
        ("09_vm_emit.mncs", vm_emit_path, vm_emit_path.read_bytes()),
        (CALLER_NAME, Path(CALLER_NAME), CALLER_SOURCE),
    ]


def scalar_type_identity(wire_value: dict):
    if "integer" in wire_value:
        return wire_value["integer"].get("type_identity") or wire_value["integer"]["type"]
    if "record" in wire_value:
        return wire_value["record"]["type_identity"]
    raise AssertionError(f"unexpected wire value shape: {wire_value}")


def run_stage0_compile_oracle(args: list[dict]) -> tuple[str, dict]:
    oracle = single_emit.Stage0ModuleOracle(
        decl_vm.EMITTER,
        [ROOT / "src", BATCH.single_emit.STDLIB_ROOT / "library"],
        generic_seeds=[("compile_and_emit_vm_module", [SEM.nat_arg(4), SEM.nat_arg(32)])],
        probe_bin=decl_vm.LOCKED_ORACLE_PROBE,
        toolchain_identity_root=decl_vm.project.probe_toolchain_identity_root(ROOT),
    )
    try:
        reply = oracle.call(
            "compile_and_emit_vm_module",
            args,
            type_arguments=[SEM.nat_arg(4), SEM.nat_arg(32)],
        )
        assert reply.get("status") == "returned", reply
        wire = reply["returned"][0]
        return wire["record"]["type_identity"], SEM.decode(wire)
    finally:
        oracle.close()


def run_stage0_sample_oracle() -> dict[tuple[str, int], tuple[str, int]]:
    with tempfile.TemporaryDirectory(prefix="mncs_vm_hosted_sample_") as tmp:
        mod_path = Path(tmp) / "a" / "b.mncs"
        mod_path.parent.mkdir(parents=True, exist_ok=True)
        mod_path.write_bytes(SAMPLE_MODULE_BYTES)
        oracle = single_emit.Stage0ModuleOracle(
            SAMPLE_MODULE_NAME,
            [Path(tmp)],
            probe_bin=decl_vm.LOCKED_ORACLE_PROBE,
            toolchain_identity_root=Path(tmp),
        )
        out: dict[tuple[str, int], tuple[str, int]] = {}
        try:
            for fn_name, arg_val, expected_val in SAMPLE_CASES:
                reply = oracle.call(fn_name, [single_emit._u64_val(arg_val)])
                assert reply.get("status") == "returned", (fn_name, arg_val, reply)
                wire = reply["returned"][0]
                decoded = SEM.decode(wire)
                assert decoded == expected_val, (fn_name, arg_val, decoded, expected_val)
                out[(fn_name, arg_val)] = (scalar_type_identity(wire), decoded)
        finally:
            oracle.close()
        return out


def run_harness(
    *,
    page_bound: int = 4096,
    emitter_step_budget: int = decl_vm.DEFAULT_STEP_BUDGET,
    vm1_step_budget: int = 100_000_000,
    artifact1_path: Path | None = None,
    artifact2_path: Path | None = None,
    output_path: Path | None = None,
) -> dict:
    decl_vm.guard_host_memory()
    sources = build_sources()
    min_bound, total_pages = decl_vm.frontier._project_page_capacity(sources)
    manifest = decl_vm.source_manifest(sources)

    report: dict = {
        "target": CALLER_TARGET,
        "hosted_callable": f"{decl_vm.EMITTER}::compile_and_emit_vm_module<4,32>",
        "sample_module": SAMPLE_MODULE_NAME,
        "sample_source_bytes": len(SAMPLE_MODULE_BYTES),
        "sample_source_sha256": hashlib.sha256(SAMPLE_MODULE_BYTES).hexdigest(),
        "compiler_revision": BATCH.single_emit._git_head(ROOT),
        "emitter_source_sha256": compiler_diagnosis.file_sha256(ROOT / compiler_diagnosis.EMITTER_REL_PATH),
        "project_source_sha256": compiler_diagnosis.file_sha256(ROOT / "src/compiler/project.mncs"),
        "decl_source_sha256": compiler_diagnosis.file_sha256(ROOT / "src/compiler/decl.mncs"),
        "stage0_revision": BATCH.single_emit._stage0_lock_rev(),
        "emitter_probe_bin": str(BATCH.single_emit.PROBE_BIN),
        "emitter_probe_sha256": compiler_diagnosis.file_sha256(BATCH.single_emit.PROBE_BIN),
        "locked_oracle_bin": str(decl_vm.LOCKED_ORACLE_PROBE),
        "locked_oracle_sha256": compiler_diagnosis.file_sha256(decl_vm.LOCKED_ORACLE_PROBE),
        "vm_revision": BATCH.single_emit._git_head(BATCH.single_emit.VM_REPO),
        "vm_bin": str(BATCH.single_emit.VM_BIN),
        "vm_bin_sha256": compiler_diagnosis.file_sha256(BATCH.single_emit.VM_BIN),
        "total_pages": total_pages,
        "minimum_supported_emitter_specialization": [min_bound, decl_vm.PROJECT_STRIDE],
        "selected_emitter_specialization": [page_bound, decl_vm.PROJECT_STRIDE],
        "memory_guard_bytes": decl_vm.MEMORY_LIMIT,
        "emitter_step_budget": emitter_step_budget,
        "vm1_step_budget": vm1_step_budget,
    }

    with tempfile.TemporaryDirectory(prefix="mncs_vm_hosted_artifacts_") as tmp:
        tmp_dir = Path(tmp)
        art1_path = artifact1_path or (tmp_dir / "compiler_vm_emit.artifact.json")
        art2_path = artifact2_path or (tmp_dir / "vm1_emitted_sample.artifact.json")

        response, metrics = decl_vm.run_request(CALLER_TARGET, sources, page_bound, emitter_step_budget)
        report["emitter_probe"] = metrics
        if response is None or response.get("status") != "returned":
            report["status"] = "emitter_refused"
            report["emitter_response"] = response
            if output_path:
                output_path.write_text(json.dumps(report, indent=2) + "\n")
            return report

        emitted1 = response["returned"][0]
        report["compiler_emission"] = {
            "ok": emitted1["ok"],
            "detail": BATCH._emit_detail_summary(emitted1),
            "function_count": emitted1["function_count"],
            "record_type_count": emitted1["record_type_count"],
            "finite_type_count": emitted1["finite_type_count"],
            "block_count": emitted1["block_count"],
            "instruction_count": emitted1["instruction_count"],
        }
        assert emitted1["ok"], report["compiler_emission"]
        expected_identity = f"mncs:0.2:function:{decl_vm.EMITTER}::compile_and_emit_vm_module"
        specializations = [
            [arg["value"] for arg in native_vm_artifact.flist(function["nat_args"])]
            for function in native_vm_artifact.flist(emitted1["functions"])
            if compiler_diagnosis.decode_semantic_id(function["identity"]) == expected_identity
        ]
        assert specializations == [[4, 32]], (expected_identity, specializations)

        artifact1 = BATCH._package(emitted1, sources, len(sources) - 1)
        artifact1_bytes = json.dumps(artifact1, separators=(",", ":")).encode()
        art1_path.parent.mkdir(parents=True, exist_ok=True)
        art1_path.write_bytes(artifact1_bytes)
        report["compiler_artifact"] = {
            "artifact_id": artifact1["artifact_id"],
            "artifact_sha256": hashlib.sha256(artifact1_bytes).hexdigest(),
            "artifact_bytes": len(artifact1_bytes),
        }

        admit1 = BATCH._admit_artifact(art1_path)
        report["vm1_admission"] = admit1
        assert admit1["ok"], admit1

        paged_args = single_emit._paged_args(SAMPLE_MODULE_BYTES, 32)
        oracle_tid, stage0_emit = run_stage0_compile_oracle(paged_args)
        assert stage0_emit["ok"], stage0_emit

        vm1 = VmSessionWithBudget(art1_path)
        try:
            r1 = vm1.call_with_budget(
                CALLER_MODULE, CALLER_FN, paged_args, step_budget=vm1_step_budget
            )
            r2 = vm1.call_with_budget(
                CALLER_MODULE, CALLER_FN, paged_args, step_budget=vm1_step_budget
            )
        finally:
            vm1.close()

        assert r1["outcome"]["kind"] == "completed", r1
        assert r2["outcome"]["kind"] == "completed", r2
        wire1 = r1["execution"]["returned"][0]
        wire2 = r2["execution"]["returned"][0]
        assert wire1 == wire2, "VM #1 non-deterministic raw wire VmEmitResult across two calls"
        vm1_emit1 = SEM.decode(wire1)
        vm1_emit2 = SEM.decode(wire2)
        assert vm1_emit1 == vm1_emit2, "VM #1 non-deterministic decoded VmEmitResult across two calls"
        assert vm1_emit1["ok"] is True, vm1_emit1
        assert wire1["record"]["type_identity"] == oracle_tid, (
            wire1["record"]["type_identity"],
            oracle_tid,
        )
        assert vm1_emit1 == stage0_emit, "VM #1 VmEmitResult differs from locked Stage-0"

        report["vm1_execution"] = {
            "deterministic_two_runs": True,
            "matches_stage0": True,
            "result_type_identity": wire1["record"]["type_identity"],
            "vm1_peak_rss_bytes": vm1.peak_rss_bytes,
            "vm1_elapsed_seconds": round(vm1.elapsed_s, 4),
            "run1": {
                "steps": r1["record"]["usage"]["steps"],
                "max_memory_cells": r1["record"]["usage"]["max_memory_cells"],
                "ok": vm1_emit1["ok"],
                "function_count": vm1_emit1["function_count"],
                "block_count": vm1_emit1["block_count"],
                "instruction_count": vm1_emit1["instruction_count"],
            },
            "run2": {
                "steps": r2["record"]["usage"]["steps"],
                "max_memory_cells": r2["record"]["usage"]["max_memory_cells"],
                "ok": vm1_emit2["ok"],
                "function_count": vm1_emit2["function_count"],
                "block_count": vm1_emit2["block_count"],
                "instruction_count": vm1_emit2["instruction_count"],
            },
        }

        artifact2 = native_vm_artifact.package_native_vm_artifact(
            vm1_emit1,
            SAMPLE_MODULE_BYTES,
            allow_multi_module=False,
            producer_entry=f"{decl_vm.EMITTER}::compile_and_emit_vm_module",
            ssa_producer_entry="mncs.compiler.ssa.v1::lower_flow_unit_value_ssa",
            emitter_source_sha256=report["emitter_source_sha256"],
            compiler_revision=report["compiler_revision"],
            stage0_revision=report["stage0_revision"],
        )
        artifact2_bytes = json.dumps(artifact2, separators=(",", ":")).encode()
        art2_path.parent.mkdir(parents=True, exist_ok=True)
        art2_path.write_bytes(artifact2_bytes)
        report["generated_artifact"] = {
            "artifact_id": artifact2["artifact_id"],
            "artifact_sha256": hashlib.sha256(artifact2_bytes).hexdigest(),
            "artifact_bytes": len(artifact2_bytes),
        }

        admit2 = BATCH._admit_artifact(art2_path)
        report["vm2_admission"] = admit2
        assert admit2["ok"], admit2

        stage0_sample = run_stage0_sample_oracle()
        vm2 = single_emit.FreshVmSession(art2_path)
        vm2_cases = []
        try:
            for fn_name, arg_val, expected_val in SAMPLE_CASES:
                res = vm2.call(SAMPLE_MODULE_NAME, fn_name, [single_emit._u64_val(arg_val)])
                assert res["outcome"]["kind"] == "completed", (fn_name, arg_val, res)
                wire_ret = res["execution"]["returned"][0]
                decoded_ret = SEM.decode(wire_ret)
                tid = scalar_type_identity(wire_ret)
                oracle_sample_tid, oracle_sample_val = stage0_sample[(fn_name, arg_val)]
                assert decoded_ret == oracle_sample_val == expected_val, (
                    fn_name,
                    arg_val,
                    decoded_ret,
                    oracle_sample_val,
                )
                assert tid == oracle_sample_tid, (fn_name, arg_val, tid, oracle_sample_tid)
                vm2_cases.append(
                    {
                        "function": fn_name,
                        "arg": arg_val,
                        "returned": decoded_ret,
                        "type_identity": tid,
                        "matches_stage0": True,
                        "steps": res["record"]["usage"]["steps"],
                        "max_memory_cells": res["record"]["usage"]["max_memory_cells"],
                    }
                )
        finally:
            vm2.close()

        report["vm2_execution"] = {
            "all_cases_passed": True,
            "matches_stage0_outputs_and_type_identity": True,
            "vm2_peak_rss_bytes": vm2.peak_rss_bytes,
            "vm2_elapsed_seconds": round(vm2.elapsed_s, 4),
            "max_steps": max(c["steps"] for c in vm2_cases),
            "max_memory_cells": max(c["max_memory_cells"] for c in vm2_cases),
            "cases": vm2_cases,
        }
        report["status"] = "passed"

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--page-bound", type=int, default=4096)
    parser.add_argument("--emitter-step-budget", type=int, default=decl_vm.DEFAULT_STEP_BUDGET)
    parser.add_argument("--vm1-step-budget", type=int, default=100_000_000)
    parser.add_argument("--artifact1", type=Path, default=None)
    parser.add_argument("--artifact2", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    started = time.monotonic()
    report = run_harness(
        page_bound=args.page_bound,
        emitter_step_budget=args.emitter_step_budget,
        vm1_step_budget=args.vm1_step_budget,
        artifact1_path=args.artifact1,
        artifact2_path=args.artifact2,
        output_path=args.output,
    )
    report["total_wall_seconds"] = round(time.monotonic() - started, 3)
    if args.output is not None:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
