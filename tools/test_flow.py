#!/usr/bin/env python3
"""CFG/typed-expression differential against the pinned Rust Stage-0 oracle.

The native flow module consumes the existing declaration tree and proof.
Python only moves request values and compares emitted facts with the oracle.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
OUT = ROOT / ".build"
OUT.mkdir(exist_ok=True)

os.environ["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow"


def integer(n):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": n}}


def blob(data):
    return {"sequence": {"values": [{"byte": {"value": n}} for n in data]}}


def decode(value):
    if "record" in value:
        return {key: decode(item) for key, item in value["record"]["fields"]}
    if "finite" in value:
        finite = value["finite"]
        return {
            "$v": finite["discriminant"],
            "$p": {key: decode(item) for key, item in finite.get("payload", [])},
        }
    if "sequence" in value:
        return [decode(item) for item in value["sequence"]["values"]]
    if "boolean" in value:
        return value["boolean"]["value"]
    return next(iter(value.values()))["value"]


def flist(value, cons=1):
    result = []
    while value["$v"] == cons:
        result.append(value["$p"]["head"])
        value = value["$p"]["tail"]
    assert value["$v"] == 0, value
    return result


class Probe:
    def __init__(self):
        self.proc = subprocess.Popen(
            [".bootstrap/target/debug/mncs-compiler-stage0-probe"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),
        )
        self.digest = hashlib.sha256()
        self.requests = 0
        self.steps = []

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"Stage-0 probe terminated: {self.proc.poll()}"
        result = json.loads(line)
        self.digest.update(json.dumps([request, result], sort_keys=True).encode())
        self.requests += 1
        return result

    def run(self, source):
        raw = source.encode()
        assert len(raw) <= 256, (len(raw), source)
        chunks = [raw[i : i + 64] for i in range(0, len(raw), 64)]
        chunks += [b""] * (4 - len(chunks))
        request = {
            "schema_version": "0.1",
            "target": {"module": "mncs.compiler.flow.v1", "function": "lower_unit"},
            "arguments": [blob(chunk) for chunk in chunks] + [integer(len(raw))],
            "step_budget": 8_000_000,
        }
        result = self.send(request)
        assert result["status"] == "returned", result
        self.steps.append(result["steps"])
        return decode(result["returned"][0])

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=60) == 0


CASES = [
    (
        "fallthrough_join",
        "mncs 0.18; module pressure.flow; fn f(x: bool) -> (r: u64) { let y: u64 = 1; if x { return y; } return 2; }",
        0,
        (1, 2),
    ),
    (
        "multiple_functions",
        "mncs 0.18; module pressure.flow; fn f(x: bool) -> (r: u64) { if x { return 1; } return 2; } fn g() -> (r: bool) { return true; }",
        0,
        (1, 3),
    ),
    (
        "unreachable_join",
        "mncs 0.18; module pressure.flow; fn f(x: bool) -> (r: u64) { if x { return 1; } else { return 2; } return 3; }",
        1,
        None,
    ),
    (
        "nested_unreachable_joins",
        "mncs 0.18; module pressure.flow; fn f(x: bool, y: bool) -> (r: u64) { if x { if y { return 1; } else { return 2; } } else { return 3; } return 4; }",
        2,
        None,
    ),
]


def oracle_diagnostics(items):
    return sorted(
        (item["code"], item["span"]["start"], item["span"]["end"])
        for item in items
        if item["code"].startswith(("MNE", "MNB"))
    )


def native_diagnostics(items):
    return sorted((f"MNB{item['code']:03}", item["start"], item["end"]) for item in flist(items))


def graph_shape(flow):
    assert flow["proof"]["ok"] is True
    functions = flist(flow["functions"])
    assigned_ops = []
    all_blocks = []
    for fn in functions:
        blocks = flist(fn["blocks"])
        assert fn["verified"] is True
        assert fn["block_count"] == len(blocks)
        ids = [block["id"] for block in blocks]
        assert sorted(ids) == list(range(len(blocks))), ids
        known = set(ids)
        all_blocks.extend(blocks)
        for block in blocks:
            expressions = flist(block["expressions"])
            typed_operations = 0
            for expression in expressions:
                ops = flist(expression["ops"])
                assert expression["op_count"] == len(ops) and ops, expression
                assert expression["start"] <= expression["end"]
                assert expression["role"] in (1, 2, 3)
                assigned_ops.append(expression["ops"])
                for op in ops:
                    assert expression["start"] <= op["$p"]["start"] <= op["$p"]["end"] <= expression["end"], (expression, op)
                typed_operations += len(ops)
            assert block["instructions"] == typed_operations, block
            if block["kind"] == 1:
                assert block["succ_count"] == 2
                assert block["succ0"] in known and block["succ1"] in known
                assert expressions and expressions[-1]["role"] == 2
                assert (expressions[-1]["start"], expressions[-1]["end"]) == (block["term_start"], block["term_end"])
            elif block["kind"] == 2:
                assert block["succ_count"] == 1 and block["succ0"] in known
            else:
                assert block["kind"] in (3, 4) and block["succ_count"] == 0
                if block["kind"] == 3:
                    assert expressions and expressions[-1]["role"] == 3
                    assert (expressions[-1]["start"], expressions[-1]["end"]) == (block["term_start"], block["term_end"])
    proof_ops = flist(flow["proof"]["tops"])
    assert assigned_ops == proof_ops, (len(assigned_ops), len(proof_ops))
    return {
        "functions": len(functions),
        "blocks": len(all_blocks),
        "branches": sum(block["kind"] == 1 for block in all_blocks),
        "jumps": sum(block["kind"] == 2 for block in all_blocks),
        "returns": sum(block["kind"] == 3 for block in all_blocks),
        "typed_expressions": sum(len(flist(block["expressions"])) for block in all_blocks),
        "typed_operations": sum(block["instructions"] for block in all_blocks),
    }


def suite():
    probe = Probe()
    results = []
    try:
        for name, source, expected_mnb_count, expected_shape in CASES:
            native1 = probe.run(source)
            native2 = probe.run(source)
            assert native1 == native2, name
            oracle1 = probe.send({"elaborate": source})
            oracle2 = probe.send({"elaborate": source})
            assert oracle1 == oracle2, name
            got = native_diagnostics(native1["diagnostics"])
            want = oracle_diagnostics(oracle1)
            assert got == want, (name, got, want)
            assert len(want) == expected_mnb_count, (name, want, expected_mnb_count)
            assert all(code == "MNB038" and start == 0 and end == len(source) for code, start, end in want), (name, want)
            assert native1["graph_ok"] is True
            assert native1["valid"] is (expected_mnb_count == 0)
            shape = graph_shape(native1)
            if expected_shape:
                assert (shape["branches"], shape["returns"]) == expected_shape, (name, shape)
                ssa1 = probe.send({"ssa": source})
                ssa2 = probe.send({"ssa": source})
                assert ssa1 == ssa2, name
                ssa = ssa1["ssa"]
                assert ssa is not None, ssa1
                ref_fns = ssa["functions"]
                ref_shape = {
                    "branches": sum("ConditionalBranch" in block["terminator"] for fn in ref_fns for block in fn["blocks"]),
                    "returns": sum("Return" in block["terminator"] for fn in ref_fns for block in fn["blocks"]),
                }
                assert (shape["branches"], shape["returns"]) == (
                    ref_shape["branches"], ref_shape["returns"]
                ), (name, shape, ref_shape)
            results.append(
                {
                    "case": name,
                    "diagnostics": got,
                    "native_graph": shape,
                    "stage0_ssa_shape": expected_shape,
                    "deterministic_repetitions": 2,
                }
            )
        return {
            "requests": probe.requests,
            "result_sha256": probe.digest.hexdigest(),
            "execution_steps_total": sum(probe.steps),
            "execution_steps_max": max(probe.steps),
            "cases": results,
        }
    finally:
        probe.close()


if __name__ == "__main__":
    started = time.monotonic()
    result = suite()
    report = {
        "schema_version": 1,
        "stage0_revision": json.loads(Path("mncs-language.lock.json").read_text())["revision"],
        "source_profile": "0.18",
        "identical_native_repetitions": 2,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "scope": "MNCS typed expression operations assigned to CFG blocks and join reachability vs current Rust Stage-0 diagnostics and SSA control shape",
        **result,
    }
    (OUT / "flow-results.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
