#!/usr/bin/env python3
"""Stage-0 and canonical VM differential for imported nested Nat sequences.

This invokes the project importer's signature transport directly with the
two-dimensional bounded sequence used by lexer -> source.byte_at_global.
The pinned Stage-0 interpreter and canonical VM execute the same seeded
compiler function and compare its complete typed result.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(os.environ.get(
    "MNCS_PROBE_BIN",
    ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe",
))
PROJECT_MODULE = "mncs.compiler.project.v1"
DECL_MODULE = "mncs.compiler.decl.v1"
TYPE_ARGS = [
    {"kind": "nat", "value": 1},
    {"kind": "nat", "value": 1},
]
STEP_BUDGET = 8_000_000
ARTIFACT_INPUTS = [
    "src/compiler/source.mncs", "src/compiler/lexer.mncs",
    "src/compiler/parser.mncs", "src/compiler/kernel.mncs",
    "src/compiler/segment.mncs", "src/compiler/decl.mncs",
    "src/compiler/flow.mncs", "src/compiler/ssa.mncs",
    "src/compiler/project.mncs", "mncs-language.lock.json",
]

# Artifact construction is compiler-owned; this probe uses the independent
# Stage-0 frontend and runs the resulting sealed artifact in mncs-vm.
os.environ["MNCS_PROBE_BACKEND"] = "reference_interpreter"
os.environ["MNCS_PROBE_EXECUTION_MODULES"] = PROJECT_MODULE
os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
    "module": PROJECT_MODULE,
    "function": "transport_sequence_type",
    "type_arguments": TYPE_ARGS,
}])

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_project as project
from test_vm_segment import run_batch, vm_to_wire
import test_vm_emit


def integer(value):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def boolean(value):
    return {"boolean": {"value": value}}


def finite(type_name, variant, discriminant, payload=(), module=DECL_MODULE):
    type_id = f"mncs:0.2:finite-type:{module}::{type_name}"
    variant_id = f"mncs:0.2:finite-variant:{module}::{type_name}::{variant}"
    return {"finite": {
        "discriminant": discriminant,
        "payload": [[name, value] for name, value in payload],
        "type_identity": type_id,
        "variant_identity": variant_id,
    }}


def sem_byte():
    return finite("SemType", "TByte", 1)


def sem_sequence(element, length, length_valid):
    return finite("SemType", "TSeqExact", 5, [
        ("element", element),
        ("length", integer(length)),
        ("length_valid", boolean(length_valid)),
    ])


def empty_nominals():
    return finite("NominalTypeIdentityList", "NINil", 0)


def request(element, outer_length, outer_valid):
    return {
        "schema_version": "0.1",
        "target": {"module": PROJECT_MODULE, "function": "transport_sequence_type"},
        "arguments": [
            {"sequence": {"values": []}},
            {"sequence": {"values": []}},
            integer(0),
            integer(1),
            element,
            integer(outer_length),
            boolean(outer_valid),
            empty_nominals(),
            empty_nominals(),
        ],
        "type_arguments": TYPE_ARGS,
        "step_budget": STEP_BUDGET,
    }


def artifact_key():
    digest = hashlib.sha256()
    for name in ARTIFACT_INPUTS:
        data = (ROOT / name).read_bytes()
        digest.update(name.encode() + b"\0" + len(data).to_bytes(8, "big") + data)
    digest.update(json.dumps({
        "module": PROJECT_MODULE,
        "function": "transport_sequence_type",
        "type_arguments": TYPE_ARGS,
    }, sort_keys=True).encode())
    return digest.hexdigest()


def main():
    test_vm_emit.ensure_vm()
    if not PROBE.is_file():
        raise SystemExit(f"Stage-0 probe missing: {PROBE}")

    # `up_to N` and `up_to P` are represented by opaque symbolic sentinels
    # and length_valid=false. A concrete pair checks that both explicit
    # lengths and validity bits survive the same path.
    symbolic_inner = sem_sequence(sem_byte(), (1 << 64) - 3, False)
    exact_inner = sem_sequence(sem_byte(), 7, True)
    cases = [
        {
            "id": "nested-symbolic-page-bounds",
            "function": "transport_sequence_type",
            "args": request(symbolic_inner, (1 << 64) - 2, False)["arguments"],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        },
        {
            "id": "nested-concrete-page-bounds",
            "function": "transport_sequence_type",
            "args": request(exact_inner, 3, True)["arguments"],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        },
        {
            "id": "reject-third-sequence-layer",
            "function": "transport_sequence_type",
            "args": request(sem_sequence(exact_inner, 5, True), 2, True)["arguments"],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        },
    ]

    probe = project.Probe()
    try:
        cache_key = artifact_key()
        artifact_cache = ROOT / ".build" / "vm-project-transport-artifact.json"
        cached = None
        if artifact_cache.is_file():
            cached = json.loads(artifact_cache.read_text())
        artifact_cache_reused = bool(
            cached and cached.get("input_key") == cache_key
            and isinstance(cached.get("artifact"), dict)
            and str(cached["artifact"].get("artifact_id", "")).startswith("sha256:"))
        if artifact_cache_reused:
            artifact = cached["artifact"]
        else:
            emitted = probe.send({"emit_vm_artifact": {"module": PROJECT_MODULE}})
            artifact = emitted["artifact"]
            artifact_cache.parent.mkdir(exist_ok=True)
            artifact_cache.write_text(json.dumps({
                "input_key": cache_key,
                "artifact": artifact,
            }, sort_keys=True) + "\n")
        assert artifact["schema_version"] == "mncs.vm.artifact/1"
        assert artifact["requirements"]["vm_contract"] == "mncs.vm/0.1"

        reference_rows = []
        for case in cases:
            result = probe.send({
                "schema_version": "0.1",
                "target": {"module": PROJECT_MODULE, "function": case["function"]},
                "arguments": case["args"],
                "type_arguments": case["type_args"],
                "step_budget": case["step_budget"],
            })
            assert result["status"] == "returned", (case["id"], result)
            reference_rows.append(result)

        with tempfile.TemporaryDirectory(prefix="mncs-project-sequence-") as directory:
            tmp = Path(directory)
            artifact_path = tmp / "project.json"
            artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
            document, wall, peak_kb, calls_bytes, results_bytes = run_batch(
                artifact_path, cases, tmp, module=PROJECT_MODULE)
            assert len(document["results"]) == len(cases), document
            vm_rows = document["results"]
            for row in vm_rows:
                assert row["outcome"] == {"kind": "completed"}, row["outcome"]
            # VM values intentionally omit the Rust-side identity strings;
            # compare the canonical finite/record structure and values.
            vm_values = [
                [project.decode(vm_to_wire(value))
                 for value in row["record"]["returned"]]
                for row in vm_rows
            ]
            reference_values = [
                [project.decode(value) for value in row["returned"]]
                for row in reference_rows
            ]
            assert reference_values == vm_values, {
                "reference": reference_values,
                "canonical_vm": vm_values,
            }
            digest = hashlib.sha256(json.dumps(
                reference_values, sort_keys=True).encode()).hexdigest()
            report = {
                "scope": "Stage-0 reference vs canonical VM: project nested sequence transport",
                "stage0_revision": json.loads(
                    (ROOT / "mncs-language.lock.json").read_text())["revision"],
                "vm_artifact_id": artifact["artifact_id"],
                "vm_artifact_cache_reused": artifact_cache_reused,
                "cases": [case["id"] for case in cases],
                "reference_steps": [row["steps"] for row in reference_rows],
                "canonical_vm_steps": [row["record"]["usage"]["steps"] for row in vm_rows],
                "semantic_digest": digest,
                "vm_wall_seconds": round(wall, 3),
                "vm_peak_rss_kb": peak_kb,
                "calls_bytes": calls_bytes,
                "results_bytes": results_bytes,
                "cranelift": "project artifact retention remains blocked by CP-0024",
            }
            output = ROOT / ".build" / "vm-project-imported-sequence-results.json"
            output.write_text(json.dumps(report, indent=2) + "\n")
            print(json.dumps(report, indent=2))
    finally:
        probe.close()


if __name__ == "__main__":
    main()
