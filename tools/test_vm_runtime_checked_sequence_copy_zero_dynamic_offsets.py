#!/usr/bin/env python3
"""Verify checked zero-length copies with dynamic offsets on the canonical VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from datetime import datetime, timezone

import test_vm_runtime_checked_sequence_copy_dynamic_offsets as common
import test_vm_native_application as support


ROOT = Path(__file__).resolve().parents[1]
MODULE = common.MODULE
FUNCTION = common.FUNCTION
SOURCE = """mncs 0.18;
module mncs.test.runtime_dynamic_offset_copy;
fn copy_window(
    destination: [byte; 8],
    destination_at: u64,
    source: [byte; up_to 8],
    source_at: u64
) -> (result: [byte; 8]) {
    return copy_span(destination, destination_at, source, source_at, 0);
}
"""
configured_probe = os.environ.get("MNCS_NATIVE_APP_PROBE_BIN") or os.environ.get("MNCS_COMPILER_PROBE")
support.PROBE = Path(configured_probe) if configured_probe else (
    ROOT / "tools/stage0-probe/target/release/mncs-compiler-stage0-probe"
)
EVIDENCE_PATH = ROOT / os.environ.get(
    "MNCS_VM_ZERO_OFFSET_COPY_EVIDENCE_PATH",
    "evidence/vm-runtime-checked-sequence-copy-zero-dynamic-offsets-20261018.json",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    if EVIDENCE_PATH.exists():
        raise SystemExit(f"refusing to overwrite existing evidence: {EVIDENCE_PATH}")
    if not support.PROBE.is_file() or not support.VM_BIN.is_file():
        raise SystemExit(f"missing compiler probe or canonical VM: {support.PROBE}, {support.VM_BIN}")

    case_inputs = [
        ("interior-offsets", list(range(8)), 3, list(range(10, 18)), 4),
        ("both-boundaries", list(range(8)), 8, list(range(10, 18)), 8),
        ("empty-source-at-zero", list(range(8)), 0, [], 0),
        ("destination-out-of-bounds", list(range(8)), 9, list(range(10, 18)), 4),
        ("source-out-of-bounds", list(range(8)), 3, list(range(10, 18)), 9),
        ("extreme-source-offset", list(range(8)), 3, list(range(10, 18)), (1 << 64) - 1),
    ]
    calls = [
        common.call_request(destination, destination_at, source, source_at)
        for _, destination, destination_at, source, source_at in case_inputs
    ]
    request = {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/test/runtime_dynamic_offset_copy_zero.mncs",
        "root_source": SOURCE,
        "entry": {"module": MODULE, "function": FUNCTION},
        "modules": {},
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }

    with tempfile.TemporaryDirectory(prefix="mncs-empty-offset-copy-") as raw_directory:
        directory = Path(raw_directory)
        probe = support.Probe(
            directory / "probe-cache",
            application_cache_dir=directory / "application-cache",
        )
        try:
            built, build_metrics = probe.send({"compile_application": request}, timeout=600)
            if built.get("entry", {}).get("status") != "verified":
                raise AssertionError("zero-length application did not verify: " +
                                     json.dumps(built, indent=2)[:7000])
            artifact = built.get("artifact", {}).get("value")
            if not isinstance(artifact, dict):
                raise AssertionError("compiler did not return a canonical VM artifact")
            oracle, oracle_metrics = probe.send({"project_oracle": {
                "root": SOURCE,
                "modules": {},
                "calls": calls,
            }}, timeout=120)
            oracle_rows = oracle.get("executions", [])
            if not oracle.get("valid") or len(oracle_rows) != len(calls):
                raise AssertionError("Stage-0 zero-length oracle failed: " +
                                     json.dumps(oracle, indent=2)[:8000])
        finally:
            probe_stderr = probe.close()

        frozen_module = artifact.get("code", {}).get("mncs-selected-ssa", {}).get("module", {})
        module = common.thaw(frozen_module)
        if not isinstance(module, dict):
            raise AssertionError("selected SSA did not decode to an object")
        instructions = [
            instruction
            for function in module.get("functions", [])
            for block in function.get("blocks", [])
            for instruction in block.get("instructions", [])
        ]
        variants = [(row, *common.instruction_variant(row)) for row in instructions]
        if any(name == "SequenceCopy" for _, name, _ in variants):
            raise AssertionError("selected application retained SequenceCopy")
        views = [
            row for row, name, body in variants
            if name == "ViewConstruct" and body.get("view_bound") == {"up_to": 0}
        ]
        lengths = [
            row for row, name, body in variants
            if name == "SequenceLength" and body.get("bound") == {"up_to": 0}
        ]
        compares = [
            row for row, name, body in variants
            if name == "IntegerCompare" and body.get("predicate") == "eq"
        ]
        conjunctions = [
            row for row, name, body in variants
            if name == "BooleanOp" and body.get("operator") == "and"
        ]
        if not (len(views) == len(lengths) == len(compares) == 2
                and len(conjunctions) == 1):
            raise AssertionError(
                "empty-copy artifact omitted checked view dependencies: "
                f"views={len(views)} lengths={len(lengths)} "
                f"compares={len(compares)} conjunctions={len(conjunctions)}"
            )
        lowering = [
            row for row in module.get("transformations", [])
            if row.get("rule") ==
            "vm-runtime-checked-sequence-copy-zero-length-to-checked-empty-views/1"
        ]
        if len(lowering) != 1 or lowering[0].get("backend_promises_permitted") != [
            "functional-runtime-checked-empty-span-copy"
        ]:
            raise AssertionError("empty-copy transformation provenance is missing or imprecise")
        lowering_refs = artifact.get("source", {}).get("lowering_refs", [])
        if not any(ref.startswith("vm-runtime-checked-sequence-copy-dynamic-offset-lowering/1:")
                   for ref in lowering_refs if isinstance(ref, str)):
            raise AssertionError("artifact omitted dynamic-offset lowering fingerprint")

        vm_rows = []
        vm_metrics = []
        case_rows = []
        for index, (oracle_row, call, (name, _, _, _, _)) in enumerate(
            zip(oracle_rows, calls, case_inputs)
        ):
            vm_row, metrics = common.run_vm(artifact, call, directory / f"case-{index}")
            vm_rows.append(vm_row)
            vm_metrics.append(metrics)
            outcome = vm_row.get("outcome", {})
            if oracle_row.get("status") == "returned":
                expected = support.decode_wire(oracle_row["returned"][0])
                actual = support.decode_wire(support.vm_to_wire(vm_row["record"]["returned"][0]))
                if outcome.get("kind") != "completed" or actual != expected:
                    raise AssertionError(json.dumps({
                        "case": name, "oracle": oracle_row, "vm": vm_row,
                        "expected": expected, "actual": actual,
                    }, indent=2)[:5000])
                case_rows.append({
                    "case": name, "status": "returned", "matches_stage0": True,
                    "result": actual,
                })
            else:
                if (oracle_row.get("status") != "runtime_failure"
                        or outcome.get("kind") != "program_failure"
                        or outcome.get("mode") != "runtime-failure"):
                    raise AssertionError(f"{name} failure differs from Stage-0: {vm_row}")
                case_rows.append({
                    "case": name, "status": "runtime_failure", "matches_stage0": True,
                    "outcome": outcome,
                })

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    report = {
        "schema_version": "mncs.compiler.vm-runtime-checked-sequence-copy-zero-dynamic-offsets/1",
        "kind": "compiler-zero-length-dynamic-offset-copy-canonical-vm-frontier",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
        "compiler": {
            "revision": head,
            "branch": subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=ROOT, text=True
            ).strip(),
            "worktree": str(ROOT),
            "emitter_sha256": sha((ROOT / "tools/vm_emit.rs").read_bytes()),
            "test_harness": str(Path(__file__).relative_to(ROOT)),
            "test_harness_sha256": sha(Path(__file__).read_bytes()),
            "probe_binary": str(support.PROBE),
            "probe_binary_sha256": sha(support.PROBE.read_bytes()),
            "canonical_vm_binary": str(support.VM_BIN),
            "canonical_vm_binary_sha256": sha(support.VM_BIN.read_bytes()),
        },
        "verification": {
            "application_artifact_identity": artifact.get("artifact_id"),
            "lowering_rule": lowering[0]["rule"],
            "checked_empty_view_count": len(views),
            "empty_view_length_check_count": len(lengths),
            "stage0_cases": case_rows,
            "stage0_match_count": sum(row["matches_stage0"] for row in case_rows),
            "case_count": len(case_rows),
            "language_verified_facts_reused": 0,
        },
        "measurements": {
            "application_build": build_metrics,
            "stage0_oracle": oracle_metrics,
            "canonical_vm": vm_metrics,
            "probe_stderr_sha256": sha(probe_stderr.encode()),
        },
        "claim_boundary": (
            "A constant zero-length SequenceCopy with dynamic source and destination offsets "
            "lowers to two checked empty views and returns the original destination. Both sequence "
            "boundaries, empty-source offset zero, out-of-range destination/source offsets, and an "
            "extreme u64 source offset match Stage-0 on the canonical VM. This does not establish "
            "Language-owned verified-fact reuse or authenticated Forge execution."
        ),
    }
    report["evidence_artifact"] = EVIDENCE_PATH.relative_to(ROOT).as_posix()
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
