#!/usr/bin/env python3
"""Compare the Project module-name cache key helper across Stage 0 and VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PROJECT_MODULE = "mncs.compiler.project.v1"
TARGET_FUNCTION = "module_resolution_cache_key_equal"
TYPE_ARGS = [
    {"kind": "nat", "value": 1024},
    {"kind": "nat", "value": 1024},
]
STEP_BUDGET = 8_000_000

os.environ["MNCS_PROBE_BACKEND"] = "reference_interpreter"
os.environ["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa,project"
os.environ["MNCS_PROBE_EXECUTION_MODULES"] = PROJECT_MODULE
os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
    "module": PROJECT_MODULE,
    "function": TARGET_FUNCTION,
    "type_arguments": TYPE_ARGS,
}])
os.environ["MNCS_PROBE_REQUEST_TIMEOUT_S"] = "300"

import test_project as project  # noqa: E402
from test_vm_segment import run_batch, vm_to_wire  # noqa: E402
import test_vm_emit  # noqa: E402


def _request(identities, raw: bytes, left: tuple[int, int],
             right: tuple[int, int]) -> dict:
    chunks = project.chunk(raw, 1024)
    source = project.source_value(
        identities, "project-name-cache-source.mncs",
        "project-name-cache-source.mncs", 0, len(chunks), 1024, len(raw),
    )
    return {
        "schema_version": "0.1",
        "target": {"module": PROJECT_MODULE, "function": TARGET_FUNCTION},
        "arguments": [
            {"sequence": {"values": [source]}},
            project.pages_value(chunks),
            project.integer(0),
            project.integer(left[0]),
            project.integer(left[1]),
            project.integer(right[0]),
            project.integer(right[1]),
        ],
        "type_arguments": TYPE_ARGS,
        "step_budget": STEP_BUDGET,
    }


def main() -> dict:
    test_vm_emit.ensure_vm()
    probe = project.Probe()
    try:
        execution = probe.send({"execution_status": True})
        identities = project.identity_map(probe)
        cases = [
            {
                "id": "equal-short-module-name",
                "raw": b"demo.dep;demo.dep",
                "left": (0, 8), "right": (9, 17), "expected": True,
            },
            {
                "id": "mismatch-at-sixth-byte",
                "raw": b"demo.dep;demo.foo",
                "left": (0, 8), "right": (9, 17), "expected": False,
            },
            {
                "id": "equal-empty-name",
                "raw": b";",
                "left": (0, 0), "right": (1, 1), "expected": True,
            },
            {
                "id": "different-length-names",
                "raw": b"demo.dep;demo.de",
                "left": (0, 8), "right": (9, 16), "expected": False,
            },
            {
                "id": "out-of-range-name-span",
                "raw": b"demo.dep",
                "left": (0, 8), "right": (1, 9), "expected": False,
            },
        ]

        requests = [
            _request(identities, case["raw"], case["left"], case["right"])
            for case in cases
        ]
        reference_values = []
        for case, request in zip(cases, requests):
            response = probe.send(request)
            assert response.get("status") == "returned", (case["id"], response)
            value = project.decode(response["returned"][0])
            assert value is case["expected"], (case["id"], value)
            reference_values.append(value)

        emitted = probe.send({"emit_vm_artifact": {"module": PROJECT_MODULE}})
        artifact = emitted.get("artifact")
        assert isinstance(artifact, dict), emitted
        assert artifact.get("schema_version") == "mncs.vm.artifact/1", artifact
        assert "program" not in artifact
        entrypoints = [
            row for row in artifact.get("generic_entrypoints", [])
            if row.get("generic_function") == TARGET_FUNCTION
        ]
        assert len(entrypoints) == 1, entrypoints

        vm_cases = [{
            "id": case["id"],
            "function": TARGET_FUNCTION,
            "args": request["arguments"],
            "type_args": TYPE_ARGS,
            "step_budget": STEP_BUDGET,
        } for case, request in zip(cases, requests)]
        with tempfile.TemporaryDirectory(prefix="project-name-cache-vm-") as directory:
            tmp = Path(directory)
            artifact_path = tmp / "project.vm.json"
            artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
            document, wall, peak_kib, calls_bytes, results_bytes = run_batch(
                artifact_path, vm_cases, tmp, module=PROJECT_MODULE,
            )

        vm_values = []
        for case, row in zip(cases, document["results"]):
            assert row.get("outcome") == {"kind": "completed"}, (case["id"], row)
            returned = row["record"]["returned"]
            assert len(returned) == 1, (case["id"], returned)
            value = project.decode(vm_to_wire(returned[0]))
            assert value is case["expected"], (case["id"], value)
            vm_values.append(value)
        assert reference_values == vm_values, (reference_values, vm_values)

        digest = hashlib.sha256(json.dumps([
            [case["id"], value]
            for case, value in zip(cases, vm_values)
        ], sort_keys=True).encode()).hexdigest()
        report = {
            "schema_version": "mncs.compiler.project-import-name-cache-vm-frontier/1",
            "status": "passed",
            "target_module": PROJECT_MODULE,
            "target_function": TARGET_FUNCTION,
            "type_arguments": ["1024", "1024"],
            "stage0_revision": json.loads(
                (ROOT / "mncs-language.lock.json").read_text()
            )["revision"],
            "stage0_backend": execution.get("backend"),
            "artifact_identity": artifact.get("artifact_id"),
            "generic_entrypoint_count": len(entrypoints),
            "cases": [case["id"] for case in cases],
            "reference_values": reference_values,
            "canonical_vm_values": vm_values,
            "stage0_vm_equivalent": True,
            "semantic_digest_sha256": digest,
            "vm_wall_seconds": round(wall, 6),
            "vm_peak_rss_kib": peak_kib,
            "vm_calls_bytes": calls_bytes,
            "vm_results_bytes": results_bytes,
            "step_budget_per_case": STEP_BUDGET,
        }
        output = ROOT / ".build" / "vm-project-import-name-cache-frontier.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        probe.close()


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
