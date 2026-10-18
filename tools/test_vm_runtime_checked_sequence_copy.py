#!/usr/bin/env python3
"""Verify bounded-view span-copy lowering on the canonical VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_vm_native_application as support


ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.test.runtime_view_copy"
FUNCTION = "copy_digest"
SOURCE = """mncs 0.18;
module mncs.test.runtime_view_copy;
fn copy_digest(view: [byte; up_to 64]) -> (result: [byte; 32]) {
    let seed: [byte; 32] = [0; 32];
    return copy_span(seed, 0, view, 0, 32);
}
"""
EVIDENCE_PATH = ROOT / os.environ.get(
    "MNCS_VM_RUNTIME_SEQUENCE_COPY_EVIDENCE_PATH",
    "evidence/vm-runtime-checked-sequence-copy-20261018.json",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def byte_sequence(values: list[int]) -> dict:
    return {"sequence": {"values": [{"byte": {"value": value}} for value in values]}}


def call_request(view: list[int]) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": FUNCTION},
        "arguments": [byte_sequence(view)],
        "type_arguments": [],
        "step_budget": 200_000,
    }


def run_vm(artifact: dict, call: dict, directory: Path) -> tuple[dict, dict]:
    directory.mkdir(parents=True, exist_ok=True)
    artifact_path = directory / "application.vm.json"
    call_path = directory / "call.json"
    type_arguments_path = directory / "type-arguments.json"
    artifact_path.write_text(
        json.dumps(artifact, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    call_path.write_text(json.dumps(call["arguments"], separators=(",", ":")), encoding="utf-8")
    type_arguments_path.write_text("[]", encoding="utf-8")
    started = time.monotonic()
    process = subprocess.run(
        [
            "/usr/bin/time", "-v", str(support.VM_BIN), "run",
            "--artifact", str(artifact_path),
            "--callable", f"{MODULE}::{FUNCTION}",
            "--args", str(call_path),
            "--type-args", str(type_arguments_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    wall_seconds = time.monotonic() - started
    if process.returncode != 0:
        raise AssertionError(f"canonical VM invocation failed: {process.stderr[-3000:]}")
    document = json.loads(process.stdout)
    rss_match = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", process.stderr)
    user_match = re.search(r"User time \(seconds\):\s*([0-9.]+)", process.stderr)
    system_match = re.search(r"System time \(seconds\):\s*([0-9.]+)", process.stderr)
    return document, {
        "wall_seconds": round(wall_seconds, 6),
        "user_seconds": float(user_match.group(1)) if user_match else None,
        "system_seconds": float(system_match.group(1)) if system_match else None,
        "peak_rss_bytes": int(rss_match.group(1)) * 1024 if rss_match else None,
    }


def main() -> None:
    if not support.PROBE.is_file() or not support.VM_BIN.is_file():
        raise SystemExit(f"missing compiler probe or canonical VM: {support.PROBE}, {support.VM_BIN}")
    request = {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/test/runtime_view_copy.mncs",
        "root_source": SOURCE,
        "entry": {"module": MODULE, "function": FUNCTION},
        "modules": {},
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }
    full_view = list(range(32))
    short_view = list(range(31))
    calls = [call_request(full_view), call_request(short_view)]

    with tempfile.TemporaryDirectory(prefix="mncs-runtime-view-copy-") as raw_directory:
        directory = Path(raw_directory)
        probe = support.Probe(
            directory / "probe-cache",
            application_cache_dir=directory / "application-cache",
        )
        try:
            built, build_metrics = probe.send({"compile_application": request}, timeout=600)
            if built.get("entry", {}).get("status") != "verified":
                raise AssertionError("runtime-view-copy application did not verify: " +
                                     json.dumps(built, indent=2)[:6000])
            artifact = built.get("artifact", {}).get("value")
            if not isinstance(artifact, dict):
                raise AssertionError("compiler did not return a canonical VM artifact")
            oracle, oracle_metrics = probe.send({"project_oracle": {
                "root": SOURCE,
                "modules": {},
                "calls": calls,
            }}, timeout=120)
            oracle_rows = oracle.get("executions", [])
            if not oracle.get("valid") or len(oracle_rows) != 2:
                raise AssertionError("Stage-0 copy oracle failed: " + json.dumps(oracle, indent=2)[:7000])
        finally:
            probe_stderr = probe.close()

        lowered_module = artifact.get("code", {}).get("mncs-selected-ssa", {}).get("module", {})
        module_text = json.dumps(lowered_module, sort_keys=True)
        lowering_ref = next((
            row for row in artifact.get("source", {}).get("lowering_refs", [])
            if row.startswith("vm-runtime-checked-sequence-copy-lowering/1:")
        ), None)
        if lowering_ref is None:
            raise AssertionError("artifact omitted runtime-checked copy lowering provenance")
        if "vm-runtime-checked-sequence-copy-to-project-replace/1" not in module_text:
            raise AssertionError("artifact omitted the runtime-checked copy transformation record")

        vm_rows = []
        vm_metrics = []
        for index, call in enumerate(calls):
            row, metrics = run_vm(artifact, call, directory / f"call-{index}")
            vm_rows.append(row)
            vm_metrics.append(metrics)

        valid_oracle, invalid_oracle = oracle_rows
        valid_vm, invalid_vm = vm_rows
        if valid_oracle.get("status") != "returned" or valid_vm.get("outcome", {}).get("kind") != "completed":
            raise AssertionError(json.dumps({"oracle": valid_oracle, "vm": valid_vm}, indent=2)[:7000])
        expected = support.decode_wire(valid_oracle["returned"][0])
        actual = support.decode_wire(support.vm_to_wire(valid_vm["record"]["returned"][0]))
        if actual != expected or actual != full_view:
            raise AssertionError("runtime-checked view copy differs from Stage-0 source semantics")
        if invalid_oracle.get("status") != "runtime_failure":
            raise AssertionError(f"short view did not fail in Stage-0: {invalid_oracle}")
        invalid_outcome = invalid_vm.get("outcome", {})
        if invalid_outcome.get("kind") != "program_failure" or invalid_outcome.get("mode") != "runtime-failure":
            raise AssertionError(f"short view did not produce a canonical VM runtime failure: {invalid_vm}")

        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        vm_revision = subprocess.check_output(
            ["git", "-C", str(support.VM_ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        report = {
            "schema_version": "mncs.compiler.vm-runtime-checked-sequence-copy-frontier/1",
            "kind": "compiler-runtime-checked-bounded-view-sequence-copy-canonical-vm-frontier",
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": "passed",
            "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
            "compiler": {
                "revision": revision,
                "branch": subprocess.check_output(
                    ["git", "branch", "--show-current"], cwd=ROOT, text=True
                ).strip(),
                "worktree": str(ROOT),
                "emitter": "tools/vm_emit.rs",
                "emitter_sha256": sha((ROOT / "tools/vm_emit.rs").read_bytes()),
                "test_harness": "tools/test_vm_runtime_checked_sequence_copy.py",
                "test_harness_sha256": sha(Path(__file__).read_bytes()),
            },
            "vm": {
                "revision": vm_revision,
                "binary": str(support.VM_BIN),
                "binary_sha256": sha(support.VM_BIN.read_bytes()),
            },
            "source": {
                "module": MODULE,
                "function": FUNCTION,
                "sha256": sha(SOURCE.encode()),
                "copy_window": {"destination_offset": 0, "source_offset": 0, "length": 32},
                "valid_view_length": len(full_view),
                "short_view_length": len(short_view),
                "expected_result_sha256": sha(bytes(full_view)),
                "actual_result_sha256": sha(bytes(actual)),
            },
            "verification": {
                "application_build_status": built.get("status"),
                "application_entry_status": built.get("entry", {}).get("status"),
                "application_artifact_identity": artifact.get("artifact_id"),
                "application_build_contract": built.get("schema_version"),
                "lowering_ref": lowering_ref,
                "runtime_checked_source_projections": 32,
                "valid_result_matches_stage0": actual == expected,
                "short_view_stage0_status": invalid_oracle.get("status"),
                "short_view_vm_outcome": invalid_outcome,
                "short_view_runtime_failure_matches": (
                    invalid_oracle.get("status") == "runtime_failure"
                    and invalid_outcome.get("kind") == "program_failure"
                    and invalid_outcome.get("mode") == "runtime-failure"
                ),
                "language_verified_facts_reused": 0,
            },
            "measurements": {
                "application_build": build_metrics,
                "stage0_oracle": oracle_metrics,
                "canonical_vm_cases": vm_metrics,
                "probe_stderr_sha256": sha(probe_stderr.encode()),
            },
            "claim_boundary": (
                "The compiler lowered the actual 0..32 bounded-view copy shape used by Test provider copy_digest. "
                "The source offsets and length are constant while the view length remains runtime-sized; all 32 "
                "source projections retain RuntimeChecked evidence and the Isolated failure mode. A 32-byte view "
                "matches Stage-0 on the canonical VM, and a 31-byte view produces a runtime failure. Copies with "
                "dynamic windows, nonzero empty-copy source offsets, or lengths above 256 remain explicit. This "
                "does not establish Language-owned verified-fact reuse or the authorized Forge/Environment/Store "
                "provider route."
            ),
        }
        report["evidence_artifact"] = EVIDENCE_PATH.relative_to(ROOT).as_posix()
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
