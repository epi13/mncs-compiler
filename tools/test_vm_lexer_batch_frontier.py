#!/usr/bin/env python3
"""Verify direct lexer batch completion and pagination in the canonical VM."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_cp0001 as cp
import test_vm_segment
import test_vm_emit

ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.lexer.v1"
PAGE_BOUND = 64
STRIDE = 1024
STEP_BUDGET = 8_000_000
SEED = {
    "module": MODULE,
    "function": "lex_tokens_from",
    "type_arguments": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(STRIDE)],
}
INPUTS = [
    ("empty", b""),
    ("short-source", b"fn tiny(){return 1;}"),
    ("paged-1025-punctuation", b";" * 1025),
]


def token_list_wire(value: dict) -> list[dict]:
    """Decode a potentially long recursive TokenList without Python recursion."""
    rows = []
    while True:
        finite = value["finite"]
        if finite["discriminant"] == 0:
            return rows
        if finite["discriminant"] != 1:
            raise AssertionError(finite)
        payload = {key: item for key, item in finite.get("payload", [])}
        rows.append(cp.decode(payload["head"]))
        value = payload["tail"]


def batch_wire(value: dict) -> dict:
    fields = {key: item for key, item in value["record"]["fields"]}
    return {
        "tokens": token_list_wire(fields["tokens"]),
        "next": cp.decode(fields["next"]),
        "eof": cp.decode(fields["eof"]),
        "count": cp.decode(fields["count"]),
    }


def vm_token_list(value: dict) -> list[dict]:
    rows = []
    while True:
        finite = value["Finite"]
        if finite["discriminant"] == 0:
            return rows
        if finite["discriminant"] != 1:
            raise AssertionError(finite)
        payload = {key: item for key, item in finite.get("payload", [])}
        rows.append(cp.decode(test_vm_segment.vm_to_wire(payload["head"])))
        value = payload["tail"]


def integer_value(value: dict) -> int:
    return value["Integer"]["value"]


def boolean_value(value: dict) -> bool:
    return value["Boolean"]


def main() -> dict:
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.monotonic()
    kind_names = json.loads((ROOT / "src/compiler/token-kinds.json").read_text())
    kind_ids = {name: int(number) for number, name in kind_names.items()}
    old_backend = os.environ.get("MNCS_PROBE_BACKEND")
    os.environ["MNCS_PROBE_BACKEND"] = "reference_interpreter"
    probe = cp.Probe("source,lexer", MODULE, [SEED])
    vm_cases: list[dict] = []
    reference_steps: list[int] = []
    batch_observations: list[dict] = []
    input_summaries = []
    try:
        execution = probe.send({"execution_status": True})
        for label, raw in INPUTS:
            oracle = probe.send({"oracle": raw.decode("ascii")})["lexical"]
            expected = [
                {
                    "kind": kind_ids[token["kind"]],
                    "start": token["span"]["start"],
                    "end": token["span"]["end"],
                    "diagnostic": 0,
                }
                for token in oracle["tokens"]
            ]
            expected.append({
                "kind": 0, "start": len(raw), "end": len(raw), "diagnostic": 0,
            })
            pages = cp.chunk(raw, STRIDE)
            cursor = 0
            expected_index = 0
            batch_index = 0
            while True:
                args = [cp.pages_value(pages), cp.integer(STRIDE),
                        cp.integer(len(raw)), cp.integer(cursor)]
                request = {
                    "schema_version": "0.1",
                    "target": {"module": MODULE, "function": "lex_tokens_from"},
                    "arguments": args,
                    "type_arguments": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(STRIDE)],
                    "step_budget": STEP_BUDGET,
                }
                response = probe.send(request)
                if response.get("status") != "returned":
                    raise AssertionError({"input": label, "response": response})
                actual = batch_wire(response["returned"][0])
                reference_steps.append(response["steps"])
                wanted = expected[expected_index:expected_index + 1024]
                if actual["tokens"] != wanted:
                    raise AssertionError({"input": label, "batch": batch_index,
                                          "expected": wanted[:8],
                                          "actual": actual["tokens"][:8]})
                if actual["count"] != len(wanted):
                    raise AssertionError({"input": label, "batch": batch_index,
                                          "count": actual["count"],
                                          "expected_count": len(wanted)})
                expected_index += actual["count"]
                vm_cases.append({
                    "id": f"{label}:{batch_index}",
                    "function": "lex_tokens_from",
                    "args": args,
                    "type_args": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(STRIDE)],
                    "step_budget": STEP_BUDGET,
                    "expected": actual,
                })
                batch_observations.append({
                    "id": f"{label}:{batch_index}",
                    "input": label,
                    "batch_index": batch_index,
                    "start": cursor,
                    "next": actual["next"],
                    "count": actual["count"],
                    "eof": actual["eof"],
                    "reference_interpreter_steps": response["steps"],
                })
                if actual["eof"]:
                    if actual["next"] != len(raw) or expected_index != len(expected):
                        raise AssertionError({"input": label, "final_batch": actual,
                                              "expected_index": expected_index,
                                              "expected_total": len(expected)})
                    break
                if actual["next"] <= cursor or actual["count"] != 1024:
                    raise AssertionError({"input": label, "nonprogressing_batch": actual})
                cursor = actual["next"]
                batch_index += 1
            input_summaries.append({
                "id": label,
                "source_bytes": len(raw),
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "oracle_tokens_including_eof": len(expected),
                "batch_count": batch_index + 1,
            })

        emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted["artifact"]
        if artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError("lexer did not emit mncs.vm.artifact/1")
    finally:
        probe.close()
        if old_backend is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = old_backend

    vm_started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-lexer-batch-frontier-") as tmp_name:
        tmp = Path(tmp_name)
        artifact_path = tmp / "lexer.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_kb, calls_bytes, results_bytes = test_vm_segment.run_batch(
            artifact_path, vm_cases, tmp, module=MODULE,
        )
    vm_wall = time.monotonic() - vm_started
    vm_steps = []
    vm_steps_by_id = {}
    for case, row in zip(vm_cases, document["results"]):
        if row.get("id") != case["id"] or row.get("outcome") != {"kind": "completed"}:
            raise AssertionError({"case": case["id"], "result": row})
        returned = row["record"]["returned"][0]["Record"]["fields"]
        fields = {key: value for key, value in returned}
        actual = {
            "tokens": vm_token_list(fields["tokens"]),
            "next": integer_value(fields["next"]),
            "eof": boolean_value(fields["eof"]),
            "count": integer_value(fields["count"]),
        }
        if actual != case["expected"]:
            raise AssertionError({"case": case["id"],
                                  "expected": case["expected"], "actual": actual})
        vm_steps.append(row["record"]["usage"]["steps"])
        vm_steps_by_id[case["id"]] = row["record"]["usage"]["steps"]

    for observation in batch_observations:
        observation["canonical_vm_steps"] = vm_steps_by_id[observation["id"]]

    after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime)
    try:
        compiler_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        compiler_branch = subprocess.run(
            ["git", "branch", "--show-current"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        ).stdout.strip()
        vm_revision = subprocess.run(
            ["git", "-C", str(test_vm_emit.VM_REPO), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except subprocess.CalledProcessError as error:
        raise AssertionError(error.stderr) from error
    result = {
        "schema_version": "mncs.compiler.lexer-batch-canonical-vm-frontier/1",
        "status": "passed",
        "compiler_head": compiler_head,
        "compiler_branch": compiler_branch,
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "vm_revision": vm_revision,
        "target": f"{MODULE}::lex_tokens_from<64,1024>",
        "source_sha256": hashlib.sha256((ROOT / "src/compiler/lexer.mncs").read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "artifact_identity": artifact["artifact_id"],
        "input_cases": input_summaries,
        "canonical_vm_calls": len(vm_cases),
        "canonical_vm_batches": [case["id"] for case in vm_cases],
        "batch_step_observations": batch_observations,
        "batch_pagination_verified": True,
        "batch_results_match_stage0": True,
        "reference_interpreter_steps": {
            "total": sum(reference_steps), "maximum": max(reference_steps),
        },
        "canonical_vm_steps": {"total": sum(vm_steps), "maximum": max(vm_steps)},
        "canonical_vm_wall_seconds": round(vm_wall, 6),
        "canonical_vm_peak_rss_kib": vm_peak_kb,
        "canonical_vm_calls_bytes": calls_bytes,
        "canonical_vm_results_bytes": results_bytes,
        "child_cpu_seconds": round(child_cpu, 6),
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "scope": "Direct lexer batch execution completed the empty and short inputs early, and paginated 1,025 punctuation tokens over multiple calls with exact Stage-0 and canonical-VM agreement. The 1,024-token bound and final EOF status were preserved.",
        "limitations": [
            "This proves the selected lex_tokens_from generic specialization and these exact inputs; it is not full lexer-module or compiler succession proof.",
            "The measurements are workload-specific and are not a before/after speedup claim.",
        ],
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_path = ROOT / f"evidence/self-host-lexer-batch-early-exit-{timestamp}.json"
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["evidence_path"] = str(evidence_path.relative_to(ROOT))
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
