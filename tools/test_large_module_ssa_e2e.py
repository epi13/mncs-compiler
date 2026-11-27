#!/usr/bin/env python3
"""End-to-end 1,025-function source -> decl -> flow -> Value SSA probe check."""

from __future__ import annotations

import json
import os
from pathlib import Path
import resource
import subprocess
import sys

import test_native_vm_emit as single_emit
import test_project as project
import test_sem as sem

ROOT = single_emit.ROOT
SSA_MODULE = "mncs.compiler.ssa.v1"
PAGE_BOUND = 48
STRIDE = 1024


def build_1025_fn_module(valid_last: bool) -> bytes:
    header = b"mncs 0.19; module large.mod; "
    valid_fns = [
        f"fn f{i}() -> (r: u64) {{ return {i}; }} ".encode()
        for i in range(1024)
    ]
    last_fn = (
        b"fn f1024() -> (r: u64) { return 1024; } "
        if valid_last
        else b"fn f1024() -> (r: bool) { return 1; } "
    )
    raw = header + b"".join(valid_fns) + last_fn
    assert len(raw) <= PAGE_BOUND * STRIDE, (len(raw), PAGE_BOUND * STRIDE)
    return raw


def open_ssa_probe():
    env = dict(os.environ)
    env["MNCS_LIBRARY_PATH"] = f"{ROOT / 'src'}:{single_emit.STDLIB_ROOT / 'library'}"
    env["MNCS_PROBE_MODULES"] = ""
    env["MNCS_PROBE_LIBRARY_MODULES"] = SSA_MODULE
    env["MNCS_PROBE_EXECUTION_MODULES"] = SSA_MODULE
    env["MNCS_PROBE_BACKEND"] = "cranelift"
    env["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
        {
            "module": SSA_MODULE,
            "function": "lower_flow_unit_value_ssa",
            "type_arguments": [sem.nat_arg(PAGE_BOUND), sem.nat_arg(STRIDE)],
        }
    ])
    env["MNCS_PROBE_CACHE_DIR"] = str(single_emit.CACHE_DIR)
    env["MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES"] = str(single_emit.MAX_AS_BYTES)
    env["MNCS_PROBE_TOOLCHAIN_IDENTITY_ROOT"] = str(project.probe_toolchain_identity_root(ROOT))
    return subprocess.Popen(
        [str(single_emit.PROBE_BIN)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=ROOT,
        env=env,
    )


def call_probe(proc, function, arguments, *, type_arguments=()):
    req = {
        "schema_version": "0.1",
        "target": {"module": SSA_MODULE, "function": function},
        "arguments": arguments,
        "type_arguments": list(type_arguments),
        "step_budget": 1000000,
    }
    proc.stdin.write(json.dumps(req) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, f"probe closed unexpectedly: {proc.poll()}"
    return json.loads(line)


def main() -> None:
    sys.setrecursionlimit(20_000)
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = 8 * 1024 ** 3
    if soft == resource.RLIM_INFINITY or soft > limit:
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    proc = open_ssa_probe()
    try:
        targs = [sem.nat_arg(PAGE_BOUND), sem.nat_arg(STRIDE)]
        results = {}
        for label, valid_last in (("valid_1025", True), ("invalid_1025_late", False)):
            raw = build_1025_fn_module(valid_last)
            pages = project.chunk(raw, STRIDE)
            args = [project.pages_value(pages), project.integer(STRIDE), project.integer(len(raw))]
            resp = call_probe(proc, "lower_flow_unit_value_ssa", args, type_arguments=targs)
            assert resp["status"] == "returned", (label, resp)
            summary_rec = resp["returned"][0]
            summary = sem.decode(summary_rec)
            st_last_resp = call_probe(proc, "value_ssa_function_status", [summary_rec, project.integer(1024)])
            assert st_last_resp["status"] == "returned", (label, st_last_resp)
            st_last = sem.decode(st_last_resp["returned"][0])
            if valid_last:
                assert summary["valid"] is True, summary
                assert summary["function_count"] == 1025, summary
                assert summary["supported_function_count"] == 1025, summary
                assert summary["verified_function_count"] == 1025, summary
                assert st_last["found"] is True and st_last["verified"] is True, st_last
            else:
                assert summary["valid"] is False, summary
                # decl.prove_unit rejects the late ill-typed function before Value SSA lowers all 1025;
                # ensure the invalid 1,025th function is caught and never silently verified.
                assert summary["verified_function_count"] < 1025, summary
            results[label] = {
                "valid": summary["valid"],
                "function_count": summary["function_count"],
                "supported_function_count": summary["supported_function_count"],
                "verified_function_count": summary["verified_function_count"],
                "last_status": st_last,
                "source_bytes": len(raw),
            }
        print(json.dumps(results, indent=2))
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        assert proc.wait(timeout=60) == 0


if __name__ == "__main__":
    main()
