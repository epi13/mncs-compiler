#!/usr/bin/env python3
"""Verify resumable MNCS kernel evidence batches through the canonical VM.

The Stage-0 lexer supplies expected token evidence. The kernel advances that
evidence in bounded 128-token slices, and the reference interpreter,
Cranelift, and one emitted VM artifact must agree for every slice and page
layout. This measures the reduction in host calls without claiming whole-
module canonical-VM proof.
"""
from __future__ import annotations

import hashlib
import json
import os
import resource
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import test_cp0001 as cp0001
import test_vm_emit
import test_vm_native_application as native_app
import test_vm_segment

ROOT = Path(__file__).resolve().parents[1]
MODULE = "mncs.compiler.kernel.v1"
FUNCTION = "lex_evidence_batch_global"
TYPE_ARGUMENTS = [cp0001.nat_arg(cp0001.PAGE_BOUND),
                  cp0001.nat_arg(cp0001.STRIDE_BOUND)]
SEEDS = [{"module": MODULE, "function": FUNCTION,
          "type_arguments": TYPE_ARGUMENTS}]
STRIDES = (7, 64, 1024)
BATCH_SIZE = 128
STEP_BUDGET = 8_000_000
EVIDENCE = ROOT / "evidence" / "self-host-kernel-global-evidence-batch-canonical-vm-20261017.json"

test_vm_emit.VM_REPO = native_app.VM_ROOT
test_vm_emit.VM_BIN = native_app.VM_BIN


def open_probe(backend: str) -> cp0001.Probe:
    previous = os.environ.get("MNCS_PROBE_BACKEND")
    os.environ["MNCS_PROBE_BACKEND"] = backend
    try:
        return cp0001.Probe("source,lexer,parser,kernel", MODULE, SEEDS)
    finally:
        if previous is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = previous


def evidence_value(tokens: list[dict], kind_ids: dict[str, int]) -> tuple[list[dict], int]:
    state = {"code": 0, "cursor": 0, "tokens": 0, "trivia": 0,
             "errors": 0, "first_start": 0, "first_end": 0}
    states = []
    for index, token in enumerate(tokens):
        kind = kind_ids[token["kind"]]
        start, end = token["span"]["start"], token["span"]["end"]
        diagnostic = token["diagnostic"]
        error = diagnostic not in (0, 6)
        first = error and state["errors"] == 0
        state = {
            "code": 1 if error else state["code"],
            "cursor": end,
            "tokens": state["tokens"] + 1,
            "trivia": state["trivia"] + int(1 <= kind <= 3),
            "errors": state["errors"] + int(error),
            "first_start": start if first else state["first_start"],
            "first_end": end if first else state["first_end"],
        }
        if (index + 1) % BATCH_SIZE == 0 or index + 1 == len(tokens):
            states.append(dict(state))
    return states, len(tokens)


def oracle_tokens(probe: cp0001.Probe, raw: bytes) -> list[dict]:
    lexical = probe.send({"oracle": raw.decode("ascii")})["lexical"]
    diagnostics = {
        (item["span"]["start"], item["span"]["end"]): item["code"]
        for item in lexical["diagnostics"]
    }
    result = []
    for token in lexical["tokens"]:
        span = token["span"]
        code = diagnostics.get((span["start"], span["end"]))
        result.append({
            "kind": token["kind"], "span": span,
            "diagnostic": {"MNL001": 1, "MNL002": 2}.get(code, 0),
        })
    return result


def record_wire(type_identity: str, state: dict) -> dict:
    fields = ("code", "cursor", "tokens", "trivia", "errors",
              "first_start", "first_end")
    return {"record": {
        "type_identity": type_identity,
        "name": "LexicalEvidence",
        "fields": [[name, cp0001.integer(state[name])] for name in fields],
    }}


def run_slice(probe: cp0001.Probe, pages: list[bytes], stride: int,
              total: int, type_identity: str, state: dict) -> tuple[dict, int]:
    response = probe.run_global_wire(
        MODULE, FUNCTION, pages, stride, total,
        (record_wire(type_identity, state),), budget=STEP_BUDGET,
    )
    return cp0001.decode(response["returned"][0]), response["steps"]


def semantic_digest(rows: list[dict]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, sort_keys=True).encode() + b"\n")
    return digest.hexdigest()


def cgroup_snapshot() -> dict:
    values = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            values[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            values[name] = None
    return values


def main() -> None:
    if EVIDENCE.exists():
        raise SystemExit(f"refusing to overwrite immutable evidence: {EVIDENCE}")
    if not test_vm_emit.PROBE.is_file():
        raise SystemExit(f"Stage-0 probe is missing: {test_vm_emit.PROBE}")
    test_vm_emit.ensure_vm()

    raw = b"a " * 300
    kind_table = json.loads((ROOT / "src/compiler/token-kinds.json").read_text())
    kind_ids = {name: int(identity) for identity, name in kind_table.items()}
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    memory_before = cgroup_snapshot()
    started = time.monotonic()
    phases = {}
    all_rows: dict[str, list[dict]] = {
        "reference_interpreter": [], "cranelift": [], "canonical_vm": [],
    }
    vm_cases = []
    batch_count = 0
    total_steps: dict[str, list[int]] = {
        "reference_interpreter": [], "cranelift": [],
    }

    reference = open_probe("reference_interpreter")
    try:
        type_info = reference.send({"record_types": MODULE})
        types = {item["name"]: item for item in type_info}
        type_identity = types["LexicalEvidence"]["identity"]
        tokens = oracle_tokens(reference, raw)
        expected_states, token_count = evidence_value(tokens, kind_ids)
        expected_by_stride = {}
        for stride in STRIDES:
            pages = cp0001.chunk(raw, stride)
            state = {"code": 0, "cursor": 0, "tokens": 0, "trivia": 0,
                     "errors": 0, "first_start": 0, "first_end": 0}
            expected_states_for_stride = expected_states or [state]
            reference_states = []
            for batch_index, expected in enumerate(expected_states_for_stride):
                input_state = dict(state)
                actual, steps = run_slice(reference, pages, stride, len(raw),
                                          type_identity, input_state)
                if actual != expected:
                    raise AssertionError(
                        f"reference stride {stride}, batch {batch_index}: {actual} != {expected}"
                    )
                reference_states.append(actual)
                total_steps["reference_interpreter"].append(steps)
                state = actual
                vm_cases.append({
                    "id": f"stride-{stride}-batch-{batch_index}",
                    "function": FUNCTION,
                    "args": [cp0001.pages_value(pages), cp0001.integer(stride),
                             cp0001.integer(len(raw)),
                             record_wire(type_identity, input_state)],
                    "type_args": TYPE_ARGUMENTS,
                    "step_budget": STEP_BUDGET,
                    "expected": expected,
                })
                row = {"stride": stride, "batch": batch_index,
                       "evidence": actual}
                all_rows["reference_interpreter"].append(row)
            expected_by_stride[stride] = expected_states_for_stride
            batch_count += len(expected_states_for_stride)
        emitted = reference.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError(f"reference did not emit a canonical VM artifact: {emitted}")
        entrypoints = [row for row in artifact.get("generic_entrypoints", [])
                       if row.get("generic_function") == FUNCTION]
        if not any(row.get("args_spellings") == ["1024", "1024"]
                   for row in entrypoints):
            raise AssertionError(f"artifact omitted {FUNCTION}<1024,1024>")
        phases["reference_and_oracle"] = round(time.monotonic() - started, 6)
    finally:
        reference.close()

    cranelift_started = time.monotonic()
    cranelift = open_probe("cranelift")
    try:
        for stride in STRIDES:
            pages = cp0001.chunk(raw, stride)
            states = expected_by_stride[stride]
            state = {"code": 0, "cursor": 0, "tokens": 0, "trivia": 0,
                     "errors": 0, "first_start": 0, "first_end": 0}
            for batch_index, expected in enumerate(states):
                actual, steps = run_slice(cranelift, pages, stride, len(raw),
                                          type_identity, state)
                if actual != expected:
                    raise AssertionError(
                        f"Cranelift stride {stride}, batch {batch_index}: {actual} != {expected}"
                    )
                total_steps["cranelift"].append(steps)
                all_rows["cranelift"].append({
                    "stride": stride, "batch": batch_index, "evidence": actual,
                })
                state = actual
    finally:
        cranelift.close()
    phases["cranelift"] = round(time.monotonic() - cranelift_started, 6)

    vm_started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-vm-kernel-evidence-batch-") as raw_dir:
        directory = Path(raw_dir)
        artifact_path = directory / "kernel-evidence-batch.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_rss, calls_bytes, results_bytes = test_vm_segment.run_batch(
            artifact_path, vm_cases, directory, module=MODULE,
        )
        if len(document["results"]) != len(vm_cases):
            raise AssertionError("canonical VM omitted evidence batch results")
        for case, result in zip(vm_cases, document["results"]):
            if result.get("id") != case["id"] or result.get("outcome") != {"kind": "completed"}:
                raise AssertionError(f"canonical VM failed {case['id']}: {result}")
            returned = result["record"].get("returned", [])
            if len(returned) != 1:
                raise AssertionError(f"canonical VM returned the wrong value count: {case['id']}")
            actual = cp0001.decode(test_vm_segment.vm_to_wire(returned[0]))
            if actual != case["expected"]:
                raise AssertionError(f"canonical VM differs for {case['id']}: {actual}")
            _, stride_text, _, batch_text = case["id"].split("-")
            all_rows["canonical_vm"].append({
                "stride": int(stride_text), "batch": int(batch_text),
                "evidence": actual,
            })
    phases["canonical_vm"] = round(time.monotonic() - vm_started, 6)

    digests = {name: semantic_digest(rows) for name, rows in all_rows.items()}
    if len(set(digests.values())) != 1:
        raise AssertionError(f"executor evidence digests differ: {digests}")
    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after_children.ru_utime - before_children.ru_utime
                 + after_children.ru_stime - before_children.ru_stime)
    report = {
        "schema_version": "mncs.compiler.self-consumption-frontier/1",
        "kind": "kernel-global-evidence-batch-canonical-vm-current-source",
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "passed",
        "claim_boundary": (
            "kernel.lex_evidence_batch_global advances at most 128 global lexical facts per call. "
            "Stage-0 token evidence, reference interpretation, Cranelift, and a fresh canonical VM "
            "artifact agree across every batch for a 600-token source and page strides 7, 64, and "
            "1024. The host makes 15 continuation calls instead of 1800 one-token calls. This is "
            "a selected kernel frontier, not whole kernel-module proof or compiler succession."
        ),
        "compiler": {
            "tested_source_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
            ).strip(),
            "branch": subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=ROOT, text=True,
            ).strip(),
            "selected_worktree": str(ROOT),
            "target_module": MODULE,
            "target_function": FUNCTION,
            "kernel_source_sha256": hashlib.sha256(
                (ROOT / "src/compiler/kernel.mncs").read_bytes()).hexdigest(),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        },
        "stage0": {
            "revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "probe_sha256": hashlib.sha256(test_vm_emit.PROBE.read_bytes()).hexdigest(),
        },
        "canonical_vm": {
            "revision": subprocess.check_output(
                ["git", "-C", str(native_app.VM_ROOT), "rev-parse", "HEAD"], text=True,
            ).strip(),
            "binary_sha256": hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest(),
            "case_count": len(vm_cases),
            "artifact_sha256": hashlib.sha256(
                json.dumps(artifact, sort_keys=True).encode()).hexdigest(),
            "peak_rss_kib": vm_peak_rss,
            "execution_seconds": round(vm_wall, 6),
            "calls_bytes": calls_bytes,
            "results_bytes": results_bytes,
            "fresh_process_batch": True,
        },
        "verification": {
            "token_count": token_count,
            "batch_size": BATCH_SIZE,
            "batch_calls_per_stride": {
                str(stride): len(expected_by_stride[stride]) for stride in STRIDES
            },
            "single_step_calls_per_stride": {
                str(stride): token_count for stride in STRIDES
            },
            "total_batch_calls_across_backends": batch_count * 3,
            "semantic_digests": digests,
            "reference_interpreter_steps": total_steps["reference_interpreter"],
            "cranelift_reported_steps": total_steps["cranelift"],
        },
        "measurements": {
            "phase_seconds": phases,
            "whole_frontier_seconds": round(time.monotonic() - started, 6),
            "child_cpu_seconds": round(child_cpu, 6),
            "cgroup_memory_before": memory_before,
            "cgroup_memory_after": cgroup_snapshot(),
        },
    }
    EVIDENCE.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "evidence": str(EVIDENCE.relative_to(ROOT)),
        "token_count": token_count,
        "batch_calls_per_stride": report["verification"]["batch_calls_per_stride"],
        "semantic_digests": digests,
        "phase_seconds": phases,
        "canonical_vm_peak_rss_kib": vm_peak_rss,
    }, indent=2))


if __name__ == "__main__":
    main()
