#!/usr/bin/env python3
"""Three-executor differential for lexer.punctuation's nested scalar matches."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from test_vm_segment import run_batch, vm_to_wire

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(os.environ.get(
    "MNCS_PROBE_BIN",
    ROOT / ".bootstrap" / "target" / "release" / "mncs-compiler-stage0-probe",
))
MODULES = "source,lexer,parser,segment,decl,flow,ssa"
MODULE = "mncs.compiler.lexer.v1"
STEP_BUDGET = 8_000_000


def integer(value):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


class Probe:
    def __init__(self, backend=None):
        env = dict(os.environ)
        if backend is None or backend == "reference_interpreter":
            env.pop("MNCS_PROBE_BACKEND", None)
        else:
            env["MNCS_PROBE_BACKEND"] = backend
        env["MNCS_PROBE_MODULES"] = MODULES
        env["MNCS_PROBE_EXECUTION_MODULES"] = MODULE
        env["MNCS_PROBE_GENERIC_SEEDS"] = "[]"
        env.setdefault("MNCS_PROBE_CACHE_DIR", str(ROOT / ".build" / "probe-cache"))
        self.stderr_file = tempfile.TemporaryFile(mode="w+t")
        self.proc = subprocess.Popen(
            [str(PROBE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.stderr_file, text=True, env=env, cwd=ROOT,
        )

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"probe exited early: {self.proc.poll()}"
        return json.loads(line)

    def close(self):
        self.proc.stdin.close()
        status = self.proc.wait(timeout=120)
        self.stderr_file.flush()
        self.stderr_file.seek(0)
        stderr = self.stderr_file.read()
        self.stderr_file.close()
        assert status == 0, stderr[-8000:]
        return stderr


def digest(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def main():
    outer = [45, 61, 38, 124, 33, 60, 62, 46, 43, 42, 47, 37, 94, 58, 59, 44, 40, 41, 123, 125, 91, 93, 0, 255]
    inner = [0, 37, 38, 46, 61, 62, 124, 255]
    pairs = [(a, b) for a in outer for b in inner]
    requests = [
        {
            "schema_version": "0.1",
            "target": {"module": MODULE, "function": "punctuation"},
            "arguments": [integer(a), integer(b)],
            "type_arguments": [],
            "step_budget": STEP_BUDGET,
        }
        for a, b in pairs
    ]

    reference = Probe("reference_interpreter")
    reference_values = []
    reference_steps = []
    for request in requests:
        result = reference.send(request)
        assert result["status"] == "returned", result
        reference_values.append(result["returned"])
        reference_steps.append(result["steps"])
    emitted = reference.send({"emit_vm_artifact": {"module": MODULE}})
    artifact = emitted["artifact"]
    reference.close()

    cranelift = Probe("cranelift")
    cranelift_values = []
    cranelift_steps = []
    for request in requests:
        result = cranelift.send(request)
        assert result["status"] == "returned", result
        cranelift_values.append(result["returned"])
        cranelift_steps.append(result["steps"])
    cranelift.close()

    cases = [
        {
            "id": f"punctuation-{a}-{b}", "function": "punctuation",
            "args": request["arguments"], "type_args": [],
            "step_budget": STEP_BUDGET,
        }
        for (a, b), request in zip(pairs, requests)
    ]
    with tempfile.TemporaryDirectory(prefix="mncs-lexer-nested-match-") as directory:
        temp = Path(directory)
        artifact_path = temp / "lexer.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        vm_document, vm_wall, vm_rss, _, _ = run_batch(
            artifact_path, cases, temp, module=MODULE
        )
    vm_rows = vm_document["results"]
    assert all(row["outcome"] == {"kind": "completed"} for row in vm_rows), vm_rows
    vm_values = [
        [vm_to_wire(value) for value in row["record"]["returned"]]
        for row in vm_rows
    ]
    assert reference_values == cranelift_values == vm_values

    report = {
        "scope": "lexer.punctuation nested scalar matches: reference, fresh canonical VM artifact, and Cranelift",
        "pairs": len(pairs),
        "outer_values": outer,
        "inner_values": inner,
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "artifact_id": artifact["artifact_id"],
        "steps": {
            "reference_min": min(reference_steps), "reference_max": max(reference_steps),
            "cranelift_min": min(cranelift_steps), "cranelift_max": max(cranelift_steps),
            "canonical_vm_max": max(row["record"]["usage"]["steps"] for row in vm_rows),
        },
        "vm_peak_rss_kb": vm_rss,
        "vm_wall_seconds": vm_wall,
        "semantic_digest": digest(reference_values),
        "reference_digest": digest(reference_values),
        "cranelift_digest": digest(cranelift_values),
        "canonical_vm_digest": digest(vm_values),
    }
    assert report["reference_digest"] == report["cranelift_digest"] == report["canonical_vm_digest"]
    output = ROOT / ".build" / "lexer-nested-match-results.json"
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
