#!/usr/bin/env python3
"""Verify constant-length dynamic-offset copies on the canonical VM."""
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
MODULE = "mncs.test.runtime_dynamic_offset_copy"
FUNCTION = "copy_window"
SOURCE = """mncs 0.18;
module mncs.test.runtime_dynamic_offset_copy;
fn copy_window(
    destination: [byte; 8],
    destination_at: u64,
    source: [byte; up_to 8],
    source_at: u64
) -> (result: [byte; 8]) {
    return copy_span(destination, destination_at, source, source_at, 3);
}
"""
EVIDENCE_PATH = ROOT / os.environ.get(
    "MNCS_VM_DYNAMIC_OFFSET_COPY_EVIDENCE_PATH",
    "evidence/vm-runtime-checked-sequence-copy-dynamic-offsets-20261018.json",
)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def thaw(graph: dict) -> object:
    """Decode the compiler's bounded interned-node representation."""
    nodes = graph.get("nodes")
    root = graph.get("root")
    if not isinstance(nodes, list) or not isinstance(root, int):
        raise AssertionError("selected SSA metadata is not a frozen node graph")
    cache: dict[int, object] = {}

    def visit(index: int) -> object:
        if index in cache:
            return cache[index]
        node = nodes[index]
        if node == "N":
            value = None
        elif isinstance(node, dict) and len(node) == 1:
            tag, payload = next(iter(node.items()))
            if tag in ("S", "I", "B"):
                value = payload
            elif tag == "A":
                value = [visit(child) for child in payload]
            elif tag == "O":
                value = {visit(key): visit(child) for key, child in payload}
            else:
                raise AssertionError(f"unknown frozen SSA node tag: {tag}")
        else:
            raise AssertionError(f"invalid frozen SSA node: {node!r}")
        cache[index] = value
        return value

    return visit(root)


def instruction_variant(instruction: dict) -> tuple[str, dict]:
    kind = instruction.get("kind")
    if not isinstance(kind, dict) or len(kind) != 1:
        raise AssertionError(f"invalid SSA instruction kind: {kind!r}")
    name, payload = next(iter(kind.items()))
    return name, payload if isinstance(payload, dict) else {}


def u64(value: int) -> dict:
    return {"integer": {"value": value, "type": {"bits": 64, "signed": False}}}


def byte_sequence(values: list[int]) -> dict:
    return {"sequence": {"values": [{"byte": {"value": value}} for value in values]}}


def call_request(destination: list[int], destination_at: int,
                 source: list[int], source_at: int) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": FUNCTION},
        "arguments": [byte_sequence(destination), u64(destination_at),
                      byte_sequence(source), u64(source_at)],
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
        "root_source_id": "native/mncs/test/runtime_dynamic_offset_copy.mncs",
        "root_source": SOURCE,
        "entry": {"module": MODULE, "function": FUNCTION},
        "modules": {},
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }
    calls = [
        call_request(list(range(8)), 2, list(range(10, 18)), 1),
        call_request(list(range(8)), 5, list(range(10, 18)), 5),
        call_request(list(range(8)), 2, list(range(8)), 1),
        call_request(list(range(8)), 2, [10, 11, 12], 1),
        call_request(list(range(8)), 6, list(range(10, 18)), 1),
        call_request(list(range(8)), (1 << 64) - 1, list(range(10, 18)), 1),
    ]

    with tempfile.TemporaryDirectory(prefix="mncs-dynamic-offset-copy-") as raw_directory:
        directory = Path(raw_directory)
        probe = support.Probe(
            directory / "probe-cache",
            application_cache_dir=directory / "application-cache",
        )
        try:
            built, build_metrics = probe.send({"compile_application": request}, timeout=600)
            if built.get("entry", {}).get("status") != "verified":
                raise AssertionError("dynamic-offset copy application did not verify: " +
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
                raise AssertionError("Stage-0 dynamic-offset oracle failed: " +
                                     json.dumps(oracle, indent=2)[:8000])
        finally:
            probe_stderr = probe.close()

        frozen_module = artifact.get("code", {}).get("mncs-selected-ssa", {}).get("module", {})
        lowered_module = thaw(frozen_module)
        if not isinstance(lowered_module, dict):
            raise AssertionError("selected SSA graph did not decode to an object")
        instructions = [
            instruction
            for function in lowered_module.get("functions", [])
            for block in function.get("blocks", [])
            for instruction in block.get("instructions", [])
        ]
        variants = [(instruction, *instruction_variant(instruction)) for instruction in instructions]
        dynamic_ref = next((
            row for row in artifact.get("source", {}).get("lowering_refs", [])
            if row.startswith("vm-runtime-checked-sequence-copy-dynamic-offset-lowering/1:")
        ), None)
        if dynamic_ref is None:
            raise AssertionError("artifact omitted dynamic-offset lowering provenance")
        runtime_projections = [
            row for row, name, body in variants
            if name == "SequenceProject" and "runtime_checked" in body.get("evidence", {})
        ]
        runtime_replacements = [
            row for row, name, body in variants
            if name == "SequenceReplace" and "runtime_checked" in body.get("evidence", {})
        ]
        checked_adds = [
            row for row, name, body in variants
            if name == "Integer" and body.get("operator") == "add"
            and str(body.get("intent", "")).lower() == "checked"
        ]
        if len(runtime_projections) != 3:
            raise AssertionError("artifact omitted the three runtime source projections")
        if len(runtime_replacements) != 3:
            raise AssertionError("artifact omitted the three runtime destination replacements")
        if len(checked_adds) != 4:
            raise AssertionError("artifact omitted checked u64 additions for dynamic offsets")
        if not any(
            row.get("rule") == "vm-runtime-checked-sequence-copy-dynamic-offsets-to-project-replace/1"
            for row in lowered_module.get("transformations", [])
        ):
            raise AssertionError("artifact omitted the dynamic-offset transformation record")
        if any(name == "SequenceCopy" for _, name, _ in variants):
            raise AssertionError("dynamic-offset SequenceCopy remained in the executable artifact")
        generated_add_obligations = [
            row for row in lowered_module.get("obligations", [])
            if row.get("method") == "runtime-checked-copy-index-offset"
        ]
        if len(generated_add_obligations) != 4 or any(
            row.get("status") != "UNKNOWN" for row in generated_add_obligations
        ):
            raise AssertionError("checked index additions lost their explicit unknown obligations")
        lowering_record = next(
            row for row in lowered_module.get("transformations", [])
            if row.get("rule") == "vm-runtime-checked-sequence-copy-dynamic-offsets-to-project-replace/1"
        )
        if any(
            row["identity"] not in lowering_record.get("obligations_required", [])
            for row in generated_add_obligations
        ):
            raise AssertionError("dynamic-offset transformation omitted generated arithmetic obligations")

        vm_rows = []
        vm_metrics = []
        for index, call in enumerate(calls):
            row, metrics = run_vm(artifact, call, directory / f"call-{index}")
            vm_rows.append(row)
            vm_metrics.append(metrics)

        comparisons = []
        for index, (oracle_row, vm_row) in enumerate(zip(oracle_rows, vm_rows)):
            oracle_status = oracle_row.get("status")
            outcome = vm_row.get("outcome", {})
            if oracle_status == "returned":
                expected = support.decode_wire(oracle_row["returned"][0])
                actual = support.decode_wire(support.vm_to_wire(vm_row["record"]["returned"][0]))
                if outcome.get("kind") != "completed" or actual != expected:
                    raise AssertionError(json.dumps({
                        "case": index, "oracle": oracle_row, "vm": vm_row,
                        "expected": expected, "actual": actual,
                    }, indent=2)[:8000])
                comparisons.append({"case": index, "status": "returned", "matches": True,
                                    "result": actual})
            else:
                if oracle_status != "runtime_failure":
                    raise AssertionError(f"unexpected Stage-0 status for case {index}: {oracle_row}")
                if outcome.get("kind") != "program_failure" or outcome.get("mode") != "runtime-failure":
                    raise AssertionError(f"VM failure differs from Stage-0 for case {index}: {vm_row}")
                comparisons.append({"case": index, "status": "runtime_failure",
                                    "matches": True, "outcome": outcome})

        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        vm_revision = subprocess.check_output(
            ["git", "-C", str(support.VM_ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        report = {
            "schema_version": "mncs.compiler.vm-runtime-checked-sequence-copy-dynamic-offset-frontier/1",
            "kind": "compiler-runtime-checked-dynamic-offset-sequence-copy-canonical-vm-frontier",
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
                "test_harness": "tools/test_vm_runtime_checked_sequence_copy_dynamic_offsets.py",
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
                "constant_length": 3,
                "dynamic_source_offset": True,
                "dynamic_destination_offset": True,
            },
            "verification": {
                "application_build_status": built.get("status"),
                "application_entry_status": built.get("entry", {}).get("status"),
                "application_artifact_identity": artifact.get("artifact_id"),
                "application_build_contract": built.get("schema_version"),
                "lowering_ref": dynamic_ref,
                "runtime_checked_source_projections": len(runtime_projections),
                "runtime_checked_destination_replacements": len(runtime_replacements),
                "checked_index_additions": len(checked_adds),
                "generated_checked_add_obligations": len(generated_add_obligations),
                "generated_checked_add_obligation_status": "UNKNOWN with runtime checked fallback",
                "execution_equivalent_to_stage0": all(row["matches"] for row in comparisons),
                "cases": comparisons,
                "language_verified_facts_reused": 0,
            },
            "measurements": {
                "application_build": build_metrics,
                "stage0_oracle": oracle_metrics,
                "canonical_vm_cases": vm_metrics,
                "probe_stderr_sha256": sha(probe_stderr.encode()),
            },
            "claim_boundary": (
                "The Compiler lowers positive constant lengths up to the existing 256-element bound when "
                "source or destination offsets are dynamic. Dynamic indices use checked u64 additions; "
                "source projections and destination replacements keep RuntimeChecked failure evidence. "
                "All valid and invalid cases match the Stage-0 oracle on the canonical VM. Dynamic lengths, "
                "empty copies, Language-owned verified-fact reuse, and the authenticated Forge/Environment/Store "
                "route are not established by this evidence."
            ),
        }
        report["evidence_artifact"] = EVIDENCE_PATH.relative_to(ROOT).as_posix()
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
