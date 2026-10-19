#!/usr/bin/env python3
"""Run the current Compiler lexer over its own source in the canonical VM."""
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
import test_vm_lexer_batch_frontier as batch_frontier
import test_vm_segment
import test_vm_emit

ROOT = Path(__file__).resolve().parents[1]
MODULE = batch_frontier.MODULE
PAGE_BOUND = batch_frontier.PAGE_BOUND
STRIDE = batch_frontier.STRIDE
STEP_BUDGET = 8_000_000
SOURCE_PATH = ROOT / "src/compiler/lexer.mncs"


def cgroup_snapshot() -> dict:
    result = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            result[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            result[name] = None
    return result


def request(cursor: int, pages_wire: dict, total: int) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": "lex_tokens_from"},
        "arguments": [pages_wire, cp.integer(STRIDE),
                      cp.integer(total), cp.integer(cursor)],
        "type_arguments": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(STRIDE)],
        "step_budget": STEP_BUDGET,
    }


def main() -> dict:
    raw = SOURCE_PATH.read_bytes()
    text = raw.decode("ascii")
    total = len(raw)
    pages = cp.chunk(raw, STRIDE)
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    memory_before = cgroup_snapshot()
    started = time.monotonic()
    previous_backend = os.environ.get("MNCS_PROBE_BACKEND")
    backend = cp.backend_policy.DEFAULTS["cp0001"]
    os.environ["MNCS_PROBE_BACKEND"] = backend
    probe = cp.Probe("source,lexer", MODULE, [batch_frontier.SEED])
    cases: list[dict] = []
    stage0_steps: list[int] = []
    native_tokens: list[tuple[int, int, int, int]] = []
    batch_summaries: list[dict] = []
    try:
        expected = cp.oracle_tokens_raw(probe, text)
        pages_wire = cp.pages_value(pages)
        cursor = 0
        batch_index = 0
        while True:
            args = request(cursor, pages_wire, total)["arguments"]
            response = probe.send({
                "schema_version": "0.1",
                "target": {"module": MODULE, "function": "lex_tokens_from"},
                "arguments": args,
                "type_arguments": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(STRIDE)],
                "step_budget": STEP_BUDGET,
            })
            if response.get("status") != "returned":
                raise AssertionError({"batch": batch_index, "response": response})
            actual = batch_frontier.batch_wire(response["returned"][0])
            stage0_steps.append(response["steps"])
            token_rows = actual["tokens"]
            if actual["count"] != len(token_rows):
                raise AssertionError({"batch": batch_index, "result": actual})
            if any(token["diagnostic"] == 5 for token in token_rows):
                raise AssertionError({"batch": batch_index,
                                      "overlong_token": token_rows[-1]})
            for token in token_rows:
                if token["kind"] != 0:
                    native_tokens.append((token["kind"], token["start"],
                                          token["end"], token["diagnostic"]))
            case_id = f"lexer-self-source:{batch_index}"
            cases.append({
                "id": case_id,
                "function": "lex_tokens_from",
                "args": args,
                "type_args": [cp.nat_arg(PAGE_BOUND), cp.nat_arg(STRIDE)],
                "step_budget": STEP_BUDGET,
                "expected": actual,
            })
            batch_summaries.append({
                "id": case_id,
                "start": cursor,
                "next": actual["next"],
                "count": actual["count"],
                "eof": actual["eof"],
                "stage0_reported_steps": response["steps"],
            })
            if actual["eof"]:
                if actual["next"] != total:
                    raise AssertionError({"final_batch": actual, "total": total})
                break
            if actual["count"] != 1024 or actual["next"] <= cursor:
                raise AssertionError({"nonprogressing_batch": actual})
            cursor = actual["next"]
            batch_index += 1

        kinds = json.loads((ROOT / "src/compiler/token-kinds.json").read_text())
        kinds_inv = {name: int(number) for number, name in kinds.items()}
        comparable = cp.join_trivia_prefixes(native_tokens)
        exact, not_gaps, first_gap = cp.compare_modulo_not(
            comparable, expected, raw, kinds_inv,
        )
        if not_gaps != 35 or first_gap != [10338, 10339] or exact:
            raise AssertionError({"exact": exact, "not_gaps": not_gaps,
                                  "first_gap": first_gap})

        emitted = probe.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = emitted["artifact"]
        if artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError("lexer self-ingestion artifact schema mismatch")
    finally:
        probe.close()
        if previous_backend is None:
            os.environ.pop("MNCS_PROBE_BACKEND", None)
        else:
            os.environ["MNCS_PROBE_BACKEND"] = previous_backend

    vm_started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-lexer-self-ingestion-") as tmp_name:
        tmp = Path(tmp_name)
        artifact_path = tmp / "lexer.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall, vm_peak_kb, calls_bytes, results_bytes = test_vm_segment.run_batch(
            artifact_path, cases, tmp, module=MODULE,
        )
    vm_wall = time.monotonic() - vm_started
    vm_steps_by_id = {}
    for case, row in zip(cases, document["results"]):
        if row.get("id") != case["id"] or row.get("outcome") != {"kind": "completed"}:
            raise AssertionError({"case": case["id"], "result": row})
        returned = row["record"]["returned"][0]["Record"]["fields"]
        fields = {key: value for key, value in returned}
        actual = {
            "tokens": batch_frontier.vm_token_list(fields["tokens"]),
            "next": batch_frontier.integer_value(fields["next"]),
            "eof": batch_frontier.boolean_value(fields["eof"]),
            "count": batch_frontier.integer_value(fields["count"]),
        }
        if actual != case["expected"]:
            raise AssertionError({"case": case["id"], "expected": case["expected"],
                                  "actual": actual})
        vm_steps_by_id[case["id"]] = row["record"]["usage"]["steps"]

    for summary in batch_summaries:
        summary["canonical_vm_steps"] = vm_steps_by_id[summary["id"]]

    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after_children.ru_utime - before_children.ru_utime
                 + after_children.ru_stime - before_children.ru_stime)
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()
    branch = subprocess.run(["git", "branch", "--show-current"], cwd=ROOT,
                            check=True, capture_output=True, text=True).stdout.strip()
    vm_revision = subprocess.run(
        ["git", "-C", str(test_vm_emit.VM_REPO), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    source_sha = hashlib.sha256(raw).hexdigest()
    result = {
        "schema_version": "mncs.compiler.lexer-current-source-canonical-vm/1",
        "status": "passed_with_known_cp0022_divergence",
        "compiler_head": head,
        "compiler_branch": branch,
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "stage0_execution_backend": backend,
        "vm_revision": vm_revision,
        "source_path": "src/compiler/lexer.mncs",
        "source_bytes": total,
        "source_sha256": source_sha,
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_is_ascii": True,
        "page_stride": STRIDE,
        "page_count": len(pages),
        "oracle_token_count": len(expected),
        "native_token_count": len(native_tokens),
        "not_token_count": not_gaps,
        "first_cp0022_span": first_gap,
        "other_lexical_divergences": 0,
        "canonical_vm_calls": len(cases),
        "canonical_vm_artifact_identity": artifact["artifact_id"],
        "canonical_vm_matches_stage0_batch_results": True,
        "stage0_lexical_equivalence_modulo_cp0022": True,
        "batches": batch_summaries,
        "canonical_vm_steps": {
            "total": sum(vm_steps_by_id.values()),
            "maximum": max(vm_steps_by_id.values()),
        },
        "stage0_reported_steps": {
            "total": sum(stage0_steps),
            "maximum": max(stage0_steps),
        },
        "canonical_vm_wall_seconds": round(vm_wall, 6),
        "canonical_vm_peak_rss_kib": vm_peak_kb,
        "canonical_vm_calls_bytes": calls_bytes,
        "canonical_vm_results_bytes": results_bytes,
        "child_cpu_seconds": round(child_cpu, 6),
        "cgroup_before": memory_before,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "scope": f"The selected lex_tokens_from<64,1024> specialization tokenized the current {total}-byte Compiler lexer source in {len(cases)} paginated batches on the configured {backend} Stage-0 backend. Every fresh canonical VM batch matched the Stage-0 batch result. The combined token stream matched the Stage-0 oracle except for the 35 already classified CP-0022 `not` token differences.",
        "limitations": [
            "This is lexer transport and tokenization self-ingestion only; parser, proof, Flow, SSA, and project compilation were not run.",
            "The CP-0022 not-token divergence is preserved and counted; it is not reported as exact whole-stream equality.",
            "Cgroup memory may be unavailable; process peak RSS is the canonical VM batch process measurement.",
        ],
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / f"evidence/self-host-lexer-current-source-canonical-vm-{timestamp}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["evidence_path"] = str(path.relative_to(ROOT))
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
