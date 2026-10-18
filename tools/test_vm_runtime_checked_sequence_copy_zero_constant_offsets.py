#!/usr/bin/env python3
"""Verify empty copies with nonzero constant offsets on the canonical VM."""
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
    source: [byte; up_to 8]
) -> (result: [byte; 8]) {
    return copy_span(destination, 3, source, 4, 0);
}
"""
configured_probe = os.environ.get("MNCS_NATIVE_APP_PROBE_BIN") or os.environ.get("MNCS_COMPILER_PROBE")
support.PROBE = Path(configured_probe) if configured_probe else (
    ROOT / "tools/stage0-probe/target/release/mncs-compiler-stage0-probe"
)
EVIDENCE_PATH = ROOT / os.environ.get(
    "MNCS_VM_ZERO_CONSTANT_OFFSET_COPY_EVIDENCE_PATH",
    "evidence/vm-runtime-checked-sequence-copy-zero-constant-offsets-20261018.json",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def call_request(source: list[int]) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": FUNCTION},
        "arguments": [common.byte_sequence(list(range(8))), common.byte_sequence(source)],
        "type_arguments": [],
        "step_budget": 200_000,
    }


def main() -> None:
    if EVIDENCE_PATH.exists():
        raise SystemExit(f"refusing to overwrite existing evidence: {EVIDENCE_PATH}")
    if not support.PROBE.is_file() or not support.VM_BIN.is_file():
        raise SystemExit(f"missing compiler probe or canonical VM: {support.PROBE}, {support.VM_BIN}")

    case_sources = [
        ("interior-source-offset", list(range(10, 18))),
        ("source-boundary-offset", list(range(10, 14))),
        ("source-offset-out-of-bounds", [10, 11, 12]),
        ("empty-source", []),
    ]
    calls = [call_request(source) for _, source in case_sources]
    request = {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/test/runtime_dynamic_offset_copy_zero_constants.mncs",
        "root_source": SOURCE,
        "entry": {"module": MODULE, "function": FUNCTION},
        "modules": {},
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }

    with tempfile.TemporaryDirectory(prefix="mncs-empty-constant-offset-copy-") as raw_directory:
        directory = Path(raw_directory)
        probe = support.Probe(
            directory / "probe-cache",
            application_cache_dir=directory / "application-cache",
        )
        try:
            built, build_metrics = probe.send({"compile_application": request}, timeout=600)
            if built.get("entry", {}).get("status") != "verified":
                raise AssertionError("constant-offset zero-length application did not verify: " +
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
        constant_values = {
            output["identity"]: body["value"]
            for row, name, body in variants if name == "Constant"
            for output in row.get("outputs", [])
            if body.get("ty") == {"integer": {"bits": 64, "signed": False}}
        }
        empty_views = [
            row for row, name, body in variants
            if name == "ViewConstruct" and body.get("view_bound") == {"up_to": 0}
        ]
        if len(empty_views) != 2:
            raise AssertionError(f"expected two checked empty views, got {len(empty_views)}")
        offsets = [constant_values.get(row.get("inputs", [None, None])[1]) for row in empty_views]
        if offsets != [3, 4]:
            raise AssertionError(f"empty views did not retain constant offsets 3 and 4: {offsets}")
        lowering = [
            row for row in module.get("transformations", [])
            if row.get("rule") ==
            "vm-runtime-checked-sequence-copy-zero-length-to-checked-empty-views/1"
        ]
        if len(lowering) != 1 or lowering[0].get("backend_promises_permitted") != [
            "functional-runtime-checked-empty-span-copy"
        ]:
            raise AssertionError("empty-copy transformation provenance is missing or imprecise")

        vm_metrics = []
        case_rows = []
        for index, (oracle_row, call, (name, _source)) in enumerate(
            zip(oracle_rows, calls, case_sources)
        ):
            vm_row, metrics = common.run_vm(artifact, call, directory / f"case-{index}")
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
        "schema_version": "mncs.compiler.vm-runtime-checked-sequence-copy-zero-constant-offsets/1",
        "kind": "compiler-zero-length-constant-offset-copy-canonical-vm-frontier",
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
            "constant_offsets": offsets,
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
            "A constant zero-length SequenceCopy with constant destination/source offsets 3 and 4 "
            "lowers to checked empty views. The source offset at the sequence boundary returns the "
            "unchanged destination; source lengths below four produce runtime failures matching "
            "Stage-0. This does not establish Language-owned verified-fact reuse."
        ),
    }
    report["evidence_artifact"] = EVIDENCE_PATH.relative_to(ROOT).as_posix()
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
