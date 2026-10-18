#!/usr/bin/env python3
"""Verify the single-dynamic-offset SequenceCopy lowering branches."""
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
configured_probe = os.environ.get("MNCS_NATIVE_APP_PROBE_BIN") or os.environ.get("MNCS_COMPILER_PROBE")
support.PROBE = Path(configured_probe) if configured_probe else (
    ROOT / "tools/stage0-probe/target/release/mncs-compiler-stage0-probe"
)
EVIDENCE_PATH = ROOT / os.environ.get(
    "MNCS_VM_OFFSET_BRANCH_EVIDENCE_PATH",
    "evidence/vm-runtime-checked-sequence-copy-offset-branches-20261018.json",
)
VARIANTS = {
    "dynamic_source_only": """mncs 0.18;
module mncs.test.runtime_dynamic_offset_copy;
fn copy_window(
    destination: [byte; 8],
    destination_at: u64,
    source: [byte; up_to 8],
    source_at: u64
) -> (result: [byte; 8]) {
    return copy_span(destination, 1, source, source_at, 3);
}
""",
    "dynamic_destination_only": """mncs 0.18;
module mncs.test.runtime_dynamic_offset_copy;
fn copy_window(
    destination: [byte; 8],
    destination_at: u64,
    source: [byte; 8],
    source_at: u64
) -> (result: [byte; 8]) {
    return copy_span(destination, destination_at, source, 2, 3);
}
""",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    if EVIDENCE_PATH.exists():
        raise SystemExit(f"refusing to overwrite existing evidence: {EVIDENCE_PATH}")
    if not support.PROBE.is_file() or not support.VM_BIN.is_file():
        raise SystemExit(f"missing compiler probe or canonical VM: {support.PROBE}, {support.VM_BIN}")

    common.FUNCTION = "copy_window"
    case_specs = {
        "dynamic_source_only": [
            (list(range(8)), 0, list(range(10, 18)), 1),
            (list(range(8)), 0, list(range(10, 18)), 6),
        ],
        "dynamic_destination_only": [
            (list(range(8)), 2, list(range(10, 18)), 0),
            (list(range(8)), 6, list(range(10, 18)), 0),
        ],
    }
    reports: dict[str, dict] = {}
    all_cases: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="mncs-copy-offset-branches-") as raw_directory:
        directory = Path(raw_directory)
        probe = support.Probe(
            directory / "probe-cache",
            application_cache_dir=directory / "application-cache",
        )
        try:
            for variant, source in VARIANTS.items():
                calls = [
                    common.call_request(destination, destination_at, values, source_at)
                    for destination, destination_at, values, source_at in case_specs[variant]
                ]
                built, build_metrics = probe.send({"compile_application": {
                    "schema_version": "mncs.compiler.application-build-request/1",
                    "target": "mncs-vm-0.1",
                    "root_source_id": f"native/mncs/test/{variant}.mncs",
                    "root_source": source,
                    "entry": {"module": MODULE, "function": "copy_window"},
                    "modules": {},
                    "generic_seeds": [],
                    "capabilities": [],
                    "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
                }}, timeout=600)
                if built.get("entry", {}).get("status") != "verified":
                    raise AssertionError(f"{variant} application did not verify: " +
                                         json.dumps(built, indent=2)[:5000])
                artifact = built.get("artifact", {}).get("value")
                if not isinstance(artifact, dict):
                    raise AssertionError(f"{variant} compiler result omitted its VM artifact")

                oracle, oracle_metrics = probe.send({"project_oracle": {
                    "root": source,
                    "modules": {},
                    "calls": calls,
                }}, timeout=120)
                oracle_rows = oracle.get("executions", [])
                if not oracle.get("valid") or len(oracle_rows) != len(calls):
                    raise AssertionError(f"{variant} Stage-0 oracle failed: " +
                                         json.dumps(oracle, indent=2)[:5000])

                graph = artifact.get("code", {}).get("mncs-selected-ssa", {}).get("module", {})
                module = common.thaw(graph)
                instructions = [
                    instruction
                    for function in module.get("functions", [])
                    for block in function.get("blocks", [])
                    for instruction in block.get("instructions", [])
                ]
                variants = [
                    (instruction, *common.instruction_variant(instruction))
                    for instruction in instructions
                ]
                lowerings = [
                    row for row in module.get("transformations", [])
                    if row.get("rule") ==
                    "vm-runtime-checked-sequence-copy-dynamic-offsets-to-project-replace/1"
                ]
                projections = [
                    row for row, name, body in variants
                    if name == "SequenceProject" and "runtime_checked" in body.get("evidence", {})
                ]
                replacements = [
                    row for row, name, body in variants
                    if name == "SequenceReplace" and "runtime_checked" in body.get("evidence", {})
                ]
                checked_adds = [
                    row for row, name, body in variants
                    if name == "Integer" and body.get("operator") == "add"
                    and str(body.get("intent", "")).lower() == "checked"
                ]
                if len(lowerings) != 1 or len(projections) != 3 or len(replacements) != 3:
                    sequence_copies = [
                        {"inputs": row.get("inputs"), "body": body}
                        for row, name, body in variants if name == "SequenceCopy"
                    ]
                    constants = [
                        {"outputs": row.get("outputs"), "value": body}
                        for row, name, body in variants if name == "Constant"
                    ]
                    raise AssertionError(
                        f"{variant} has incomplete single-offset lowering: "
                        f"lowerings={len(lowerings)} projections={len(projections)} "
                        f"replacements={len(replacements)} sequence_copies={sequence_copies} "
                        f"constants={constants}"
                    )
                if len(checked_adds) != 2:
                    raise AssertionError(f"{variant} must use two checked offset increments")
                if any(name == "SequenceCopy" for _, name, _ in variants):
                    raise AssertionError(f"{variant} retained SequenceCopy in the selected artifact")

                obligations = [
                    row for row in module.get("obligations", [])
                    if row.get("method") == "runtime-checked-copy-index-offset"
                ]
                if len(obligations) != 2 or any(row.get("status") != "UNKNOWN" for row in obligations):
                    raise AssertionError(f"{variant} lost explicit checked-add obligations")
                if any(row["identity"] not in lowerings[0].get("obligations_required", [])
                       for row in obligations):
                    raise AssertionError(f"{variant} lowering omitted its checked-add obligations")

                vm_rows = []
                vm_metrics = []
                common.FUNCTION = "copy_window"
                for index, call in enumerate(calls):
                    vm_row, metrics = common.run_vm(artifact, call, directory / variant / f"case-{index}")
                    vm_rows.append(vm_row)
                    vm_metrics.append(metrics)

                cases = []
                for index, (oracle_row, vm_row) in enumerate(zip(oracle_rows, vm_rows)):
                    outcome = vm_row.get("outcome", {})
                    if oracle_row.get("status") == "returned":
                        expected = support.decode_wire(oracle_row["returned"][0])
                        actual = support.decode_wire(support.vm_to_wire(vm_row["record"]["returned"][0]))
                        if outcome.get("kind") != "completed" or actual != expected:
                            raise AssertionError(json.dumps({
                                "variant": variant, "case": index,
                                "oracle": oracle_row, "vm": vm_row,
                                "expected": expected, "actual": actual,
                            }, indent=2)[:5000])
                        result = {"case": index, "status": "returned", "matches_stage0": True,
                                  "result": actual}
                    else:
                        if (oracle_row.get("status") != "runtime_failure"
                                or outcome.get("kind") != "program_failure"
                                or outcome.get("mode") != "runtime-failure"):
                            raise AssertionError(f"{variant} failure differs from Stage-0: {vm_row}")
                        result = {"case": index, "status": "runtime_failure",
                                  "matches_stage0": True, "outcome": outcome}
                    cases.append(result)
                    all_cases.append({"variant": variant, **result})

                reports[variant] = {
                    "source_sha256": sha(source.encode()),
                    "application_artifact_identity": artifact.get("artifact_id"),
                    "checked_add_count": len(checked_adds),
                    "unknown_checked_add_obligation_count": len(obligations),
                    "runtime_checked_projection_count": len(projections),
                    "runtime_checked_replacement_count": len(replacements),
                    "application_build_metrics": build_metrics,
                    "stage0_oracle_metrics": oracle_metrics,
                    "canonical_vm_metrics": vm_metrics,
                    "cases": cases,
                }
        finally:
            probe_stderr = probe.close()

    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    report = {
        "schema_version": "mncs.compiler.vm-runtime-checked-sequence-copy-offset-branches/1",
        "kind": "compiler-single-dynamic-offset-sequence-copy-canonical-vm-frontier",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
        "compiler": {
            "revision": head,
            "branch": subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
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
            "variants": reports,
            "cases": all_cases,
            "case_count": len(all_cases),
            "stage0_matches": all(row["matches_stage0"] for row in all_cases),
            "language_verified_facts_reused": 0,
        },
        "measurements": {"probe_stderr_sha256": sha(probe_stderr.encode())},
        "claim_boundary": (
            "The single-dynamic-source-offset and single-dynamic-destination-offset lowering paths each emit "
            "three runtime-checked projections and replacements, plus two checked u64 additions whose "
            "obligations remain UNKNOWN with runtime fallbacks. Four valid and invalid cases match Stage-0 "
            "on the canonical VM. This does not establish Language-owned verified-fact reuse or an "
            "authenticated Forge/Environment/Store execution."
        ),
    }
    report["evidence_artifact"] = EVIDENCE_PATH.relative_to(ROOT).as_posix()
    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
