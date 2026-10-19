#!/usr/bin/env python3
"""Parse the actual lex_tokens_from declaration through the canonical VM."""
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

import test_vm_decl as decl
from test_vm_segment import run_batch, vm_to_wire
import test_vm_emit

ROOT = Path(__file__).resolve().parents[1]
MODULE = decl.MODULE
TEST = decl.test_decl
PAGE_BOUND = TEST.PAGE_BOUND
STRIDE = TEST.STRIDE_BOUND
STEP_BUDGET = TEST.EXECUTION_STEP_BUDGET
RAW = (ROOT / "src/compiler/lexer.mncs").read_bytes()
FUNCTION_NAME = b"lex_tokens_from"
FUNCTION_START = RAW.index(b"fn " + FUNCTION_NAME)
FUNCTION_BODY_END = RAW.rfind(b"}", FUNCTION_START, RAW.index(b"\nfn batch_step", FUNCTION_START)) + 1


class Probe:
    def __init__(self, backend: str):
        env = dict(os.environ)
        if backend == "reference_interpreter":
            env.pop("MNCS_PROBE_BACKEND", None)
        else:
            env["MNCS_PROBE_BACKEND"] = backend
        env["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl"
        env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
        env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
            "module": MODULE,
            "function": "parse_fn",
            "type_arguments": [TEST.nat_arg(PAGE_BOUND), TEST.nat_arg(STRIDE)],
        }])
        env.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build" / "probe-cache"))
        self.backend = backend
        self.proc = subprocess.Popen(
            [str(decl.PROBE)], cwd=ROOT, env=env, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def send(self, request: dict) -> dict:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self.proc.stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise AssertionError(f"probe exited unexpectedly: {self.proc.poll()}")
        return json.loads(line)

    def close(self) -> str:
        assert self.proc.stdin is not None and self.proc.stderr is not None
        self.proc.stdin.close()
        code = self.proc.wait(timeout=120)
        stderr = self.proc.stderr.read()
        if code != 0:
            raise AssertionError(f"probe exited {code}: {stderr[-2000:]}")
        return stderr


def request() -> dict:
    pages = [RAW[offset:offset + STRIDE] for offset in range(0, len(RAW), STRIDE)]
    cursor = FUNCTION_START + len(b"fn")
    return {
        "schema_version": "0.1",
        "target": {"module": MODULE, "function": "parse_fn"},
        "arguments": [
            TEST.pages_value(pages), TEST.integer(STRIDE), TEST.integer(len(RAW)),
            TEST.integer(cursor), TEST.integer(FUNCTION_START),
            {"boolean": {"value": True}}, {"boolean": {"value": True}},
        ],
        "type_arguments": [TEST.nat_arg(PAGE_BOUND), TEST.nat_arg(STRIDE)],
        "step_budget": STEP_BUDGET,
    }


def cgroup_snapshot() -> dict:
    result = {}
    for name in ("memory.current", "memory.max", "memory.peak"):
        try:
            result[name] = (Path("/sys/fs/cgroup") / name).read_text().strip()
        except OSError:
            result[name] = None
    return result


def validate_ast(value: dict) -> dict:
    if not value["ok"]:
        raise AssertionError({"parse_fn": value})
    if value["fn_start"] != FUNCTION_START or value["fn_end"] != FUNCTION_BODY_END:
        raise AssertionError({"span": [value["fn_start"], value["fn_end"]],
                              "expected": [FUNCTION_START, FUNCTION_BODY_END]})
    if value["cursor"] != FUNCTION_BODY_END:
        raise AssertionError({"cursor": value["cursor"], "expected": FUNCTION_BODY_END})
    signature = value["sig"]
    name = RAW[signature["name_start"]:signature["name_end"]]
    if name != FUNCTION_NAME:
        raise AssertionError({"function_name": name.decode("ascii", "replace")})
    generics = TEST.flist(signature["generics"], 1)
    generic_names = [RAW[item["name_start"]:item["name_end"]].decode("ascii")
                     for item in generics]
    if generic_names != ["P", "N"]:
        raise AssertionError({"generic_names": generic_names})
    params = TEST.flist(signature["params"], 1)
    param_names = [RAW[item["name_start"]:item["name_end"]].decode("ascii")
                   for item in params]
    if param_names != ["pages", "stride", "total", "start"]:
        raise AssertionError({"param_names": param_names})
    top_level = TEST.flist(value["body"]["body"], 1)
    statement_kinds = [item["$v"] for item in top_level]
    # The implementation seeds its accumulator, runs one bounded iterate,
    # then retains BatchResult in FnBody.ret after parse_return_tail.
    if statement_kinds != [0, 4] or not value["body"]["has_ret"]:
        raise AssertionError({"top_level_statement_kinds": statement_kinds})
    outer_body = TEST.flist(top_level[1]["$p"]["body"], 1)
    nested_kinds = [item["$v"] for item in outer_body]
    if nested_kinds != [4, 1]:
        raise AssertionError({"outer_iterate_body_kinds": nested_kinds})
    return {
        "function_name": name.decode("ascii"),
        "function_span": [value["fn_start"], value["fn_end"]],
        "generic_names": generic_names,
        "parameter_names": param_names,
        "result_count": len(TEST.flist(signature["results"], 1)),
        "top_level_statement_kinds": statement_kinds,
        "outer_iterate_body_kinds": nested_kinds,
        "body_retained": value["body"]["has_ret"],
    }


def main() -> dict:
    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    memory_before = cgroup_snapshot()
    started = time.monotonic()
    req = request()

    reference_started = time.monotonic()
    reference = Probe("reference_interpreter")
    try:
        reference_status = reference.send({"execution_status": True})
        reference_response = reference.send(req)
        if reference_response.get("status") != "returned":
            raise AssertionError({"reference_parse_fn": reference_response})
        reference_value = TEST.decode(reference_response["returned"][0])
        ast_summary = validate_ast(reference_value)
        artifact_response = reference.send({"emit_vm_artifact": {"module": MODULE}})
        artifact = artifact_response.get("artifact")
        if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError({"artifact_emission": artifact_response})
    finally:
        reference_stderr = reference.close()
    reference_wall = time.monotonic() - reference_started

    cranelift_started = time.monotonic()
    cranelift = Probe("cranelift")
    try:
        cranelift_status = cranelift.send({"execution_status": True})
        cranelift_response = cranelift.send(req)
        if cranelift_response.get("status") != "returned":
            raise AssertionError({"cranelift_parse_fn": cranelift_response})
        cranelift_value = TEST.decode(cranelift_response["returned"][0])
        if cranelift_value != reference_value:
            raise AssertionError("Cranelift parse_fn result differs from reference interpreter")
    finally:
        cranelift_stderr = cranelift.close()
    cranelift_wall = time.monotonic() - cranelift_started

    vm_case = {
        "id": "compiler-source-lexer-parse-fn",
        "function": "parse_fn",
        "args": req["arguments"],
        "type_args": req["type_arguments"],
        "step_budget": STEP_BUDGET,
    }
    vm_started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-compiler-decl-self-ingestion-") as tmp_name:
        tmp = Path(tmp_name)
        artifact_path = tmp / "decl.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        document, vm_wall_reported, vm_peak_kib, calls_bytes, results_bytes = run_batch(
            artifact_path, [vm_case], tmp, module=MODULE,
        )
    vm_wall = time.monotonic() - vm_started
    rows = document.get("results", [])
    if len(rows) != 1 or rows[0].get("id") != vm_case["id"]:
        raise AssertionError({"canonical_vm_results": rows})
    row = rows[0]
    if row.get("outcome") != {"kind": "completed"}:
        raise AssertionError({"canonical_vm_outcome": row})
    vm_value = TEST.decode(vm_to_wire(row["record"]["returned"][0]))
    if vm_value != reference_value:
        raise AssertionError("canonical VM parse_fn result differs from reference interpreter")

    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (after_children.ru_utime - before_children.ru_utime
                 + after_children.ru_stime - before_children.ru_stime)
    result = {
        "schema_version": "mncs.compiler.current-source-declaration-canonical-vm/1",
        "status": "passed_selected_current_source_declaration_frontier",
        "compiler_head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "compiler_branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "vm_revision": subprocess.check_output(
            ["git", "-C", str(test_vm_emit.VM_REPO), "rev-parse", "HEAD"],
            text=True,
        ).strip(),
        "source_path": "src/compiler/lexer.mncs",
        "source_sha256": hashlib.sha256(RAW).hexdigest(),
        "source_bytes": len(RAW),
        "declaration_source_path": "src/compiler/decl.mncs",
        "declaration_source_sha256": hashlib.sha256(
            (ROOT / "src/compiler/decl.mncs").read_bytes()
        ).hexdigest(),
        "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "target_module": MODULE,
        "target_function": "parse_fn",
        "type_arguments": [PAGE_BOUND, STRIDE],
        "function_source_span": [FUNCTION_START, FUNCTION_BODY_END],
        "function_source_bytes": FUNCTION_BODY_END - FUNCTION_START,
        "parsed_ast_summary": ast_summary,
        "cranelift_matches_reference": cranelift_value == reference_value,
        "canonical_vm_matches_reference": vm_value == reference_value,
        "canonical_vm_artifact_identity": artifact["artifact_id"],
        "canonical_vm_steps": row["record"]["usage"]["steps"],
        "measurements": {
            "reference_and_emission_wall_seconds": round(reference_wall, 6),
            "reference_interpreter_steps": reference_response["steps"],
            "cranelift_wall_seconds": round(cranelift_wall, 6),
            "cranelift_reported_steps": cranelift_response["steps"],
            "canonical_vm_wall_seconds": round(vm_wall, 6),
            "canonical_vm_reported_wall_seconds": round(vm_wall_reported, 6),
            "canonical_vm_peak_rss_kib": vm_peak_kib,
            "canonical_vm_calls_bytes": calls_bytes,
            "canonical_vm_results_bytes": results_bytes,
            "child_cpu_seconds": round(child_cpu, 6),
            "cgroup_before": memory_before,
            "cgroup_after": cgroup_snapshot(),
            "total_wall_seconds": round(time.monotonic() - started, 6),
        },
        "executor_stderr_sha256": {
            "reference_interpreter": hashlib.sha256(reference_stderr.encode()).hexdigest(),
            "cranelift": hashlib.sha256(cranelift_stderr.encode()).hexdigest(),
        },
        "language_verified_facts_reused": 0,
        "stage1_claimed": False,
        "stage2_claimed": False,
        "scope": "The current Compiler lexer source's complete lex_tokens_from generic function declaration and body were parsed by decl.parse_fn<P=1024,N=1024> through the reference interpreter, Cranelift, and a fresh canonical VM artifact. The returned signature spans, generics, parameters, retained body shape, and complete parsed declaration matched across executors. This does not perform body type verification, full-unit declaration checking, Language fact reuse, or compiler succession.",
        "limitations": [
            "This is one declaration parser callable over a 971-byte source declaration, not full lexer-module parsing or type checking.",
            "No Language-owned verified facts were consumed.",
        ],
    }
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = ROOT / f"evidence/self-host-compiler-source-declaration-canonical-vm-{timestamp}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["evidence_path"] = str(path.relative_to(ROOT))
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
