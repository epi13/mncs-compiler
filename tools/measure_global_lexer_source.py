#!/usr/bin/env python3
"""Measure selected global lexer calls over one pinned real compiler source.

The input snapshot is loaded from Git so later lexer edits cannot move token
positions or alter the sample. Stage-0 supplies expected tokens; the selected
global lexer calls are then checked under the reference interpreter and
Cranelift while per-call interpreter steps are grouped by token kind.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import test_vm_global_segment_frontier as frontier  # noqa: E402
from test_vm_global_segment_frontier import segment  # noqa: E402


INPUT_REVISION = "ee3611f8be5cd99856c0dde53e3071a48fa8e5d1"
INPUT_PATH = "src/compiler/lexer.mncs"
SAMPLE_COUNT = 192
STRIDE = 1024


def source_at(revision: str) -> bytes:
    return subprocess.check_output(
        ["git", "show", f"{revision}:{INPUT_PATH}"], cwd=ROOT
    )


def proc_memory(pid: int) -> dict[str, int | None]:
    values: dict[str, int | None] = {"rss_kib": None, "hwm_kib": None}
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
            key, sep, value = line.partition(":")
            if sep and key in ("VmRSS", "VmHWM"):
                values[{"VmRSS": "rss_kib", "VmHWM": "hwm_kib"}[key]] = int(value.split()[0])
    except (OSError, ValueError, IndexError):
        pass
    return values


def run_backend(raw: bytes, lexical: dict, backend: str | None) -> dict:
    tokens = lexical["tokens"]
    diagnostics = {
        (item["span"]["start"], item["span"]["end"]): item["code"]
        for item in lexical["diagnostics"]
    }
    kind_ids = {
        name: int(number)
        for number, name in json.loads(
            (ROOT / "src/compiler/token-kinds.json").read_text()
        ).items()
    }
    sample_count = min(SAMPLE_COUNT, len(tokens))
    indices = sorted(
        {
            round(index * (len(tokens) - 1) / max(1, sample_count - 1))
            for index in range(sample_count)
        }
    )
    pages = frontier.pages_wire(raw, STRIDE)
    probe = frontier.Probe(backend)
    rows: list[tuple[str, int, int]] = []
    digest = hashlib.sha256()
    before = resource.getrusage(resource.RUSAGE_CHILDREN)
    started = time.perf_counter()
    try:
        for index in indices:
            token = tokens[index]
            start = token["span"]["start"]
            end = token["span"]["end"]
            case = {
                "function": "next_token",
                "args": [frontier.byte_sequence(raw), frontier.integer(start)],
                "pages_wire": pages,
            }
            result = probe.send(frontier.make_request(case, STRIDE))
            if result.get("status") != "returned":
                raise AssertionError(f"token {index} failed: {result}")
            value = segment.decode(result["returned"][0])
            code = diagnostics.get((start, end))
            expected = {
                "kind": kind_ids[token["kind"]],
                "start": start,
                "end": end,
                "diagnostic": {"MNL001": 1, "MNL002": 2}.get(code, 0),
            }
            if value != expected:
                raise AssertionError((index, value, expected))
            digest.update(json.dumps([index, value], sort_keys=True).encode() + b"\n")
            rows.append((token["kind"], end - start, int(result.get("steps", 0))))
        memory = proc_memory(probe.proc.pid)
    finally:
        probe.close()
    after = resource.getrusage(resource.RUSAGE_CHILDREN)

    by_kind: dict[str, list[int]] = collections.defaultdict(list)
    by_length: dict[int, list[int]] = collections.defaultdict(list)
    for kind, length, steps in rows:
        by_kind[kind].append(steps)
        by_length[length].append(steps)
    measured_backend = backend or "reference_interpreter"
    profile: dict[str, object] = {
        "backend": measured_backend,
        "requests": len(rows),
        "semantic_digest_sha256": digest.hexdigest(),
        "wall_seconds": round(time.perf_counter() - started, 3),
        "child_cpu_seconds": round(
            after.ru_utime - before.ru_utime + after.ru_stime - before.ru_stime, 3
        ),
        "probe_process_memory_kib": memory,
        "per_kind": {
            kind: {
                "count": len(values),
                "mean_steps": round(statistics.mean(values), 1),
                "median_steps": statistics.median(values),
                "max_steps": max(values),
            }
            for kind, values in sorted(by_kind.items())
        },
        "per_length": {
            str(length): {
                "count": len(values),
                "mean_steps": round(statistics.mean(values), 1),
            }
            for length, values in sorted(by_length.items())
        },
    }
    if backend is None:
        profile["total_interpreter_steps"] = sum(row[2] for row in rows)
    else:
        profile["step_accounting_note"] = (
            "Cranelift reports one backend request step per call; per-call step "
            "counts are not comparable with reference interpreter instruction steps."
        )
    return profile


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-revision", default=INPUT_REVISION)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    raw = source_at(args.input_revision)
    if any(byte >= 128 for byte in raw):
        raise SystemExit("pinned lexer source contains non-ASCII bytes")
    oracle = frontier.Probe(None)
    try:
        lexical = oracle.send({"oracle": raw.decode("ascii")})["lexical"]
    finally:
        oracle.close()
    if not lexical["tokens"]:
        raise SystemExit("pinned source produced no Stage-0 tokens")

    compiler_revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
    ).strip()
    report = {
        "schema_version": "mncs.compiler.real-source-token-profile/1",
        "kind": "global-lexer-stratified-token-profile",
        "compiler_revision": compiler_revision,
        "compiler_branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "implementation_lexer_sha256": hashlib.sha256(
            (ROOT / "src/compiler/lexer.mncs").read_bytes()
        ).hexdigest(),
        "target": {
            "module": "mncs.compiler.segment.v1",
            "function": "next_token_global",
            "generic_arguments": [1024, 1024],
        },
        "input": {
            "repository_revision": args.input_revision,
            "path": INPUT_PATH,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "stage0_token_count": len(lexical["tokens"]),
            "sample_count": min(SAMPLE_COUNT, len(lexical["tokens"])),
            "sample_strategy": "192 evenly spaced token indices from the Stage-0 token stream",
            "page_stride": STRIDE,
        },
        "stage0_revision": json.loads(
            (ROOT / "mncs-language.lock.json").read_text()
        )["revision"],
        "profiles": {
            "reference_interpreter": run_backend(raw, lexical, None),
            "cranelift": run_backend(raw, lexical, "cranelift"),
        },
        "scope": (
            "Each sampled call reads the same pinned real compiler source snapshot, "
            "and the returned token is checked against Stage-0. The profile measures "
            "selected next_token_global calls only; it does not claim whole-compiler speedup."
        ),
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered)
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
