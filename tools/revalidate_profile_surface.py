#!/usr/bin/env python3
"""Compare Profile 0.18 syntax admission in native decl parsing vs Stage-0."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / ".build/profile-surface-results.json"
CASES = [
    ("CP-0015-not", "mncs 0.18; module p.bool_not; fn f(a: bool) -> (r: bool) { return !a; }"),
    ("CP-0015-negative", "mncs 0.18; module p.negative; fn f() -> (r: i64) { return -5; }"),
    ("CP-0015-repeat", "mncs 0.18; module p.repeat; fn f() -> (r: [u64; 4]) { return [0; 4]; }"),
    ("CP-0015-next", "mncs 0.18; module p.next_field; record R { next: u64 } fn f(v: R) -> (r: u64) { return v.next; }"),
    ("CP-0015-scalar-match", "mncs 0.18; module p.scalar_match; fn f(x: u64) -> (r: u64) { return match x { 0 => 1, _ => 2 }; }"),
]


def integer(value):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def blob(data):
    return {"sequence": {"values": [{"byte": {"value": item}} for item in data]}}


def split4(data):
    cuts = [min(len(data), 64), min(len(data), 128), min(len(data), 192)]
    parts = []
    previous = 0
    for cut in cuts:
        parts.append(data[previous:cut])
        previous = cut
    parts.append(data[previous:])
    return parts


def decode(value):
    if "record" in value:
        return {key: decode(item) for key, item in value["record"]["fields"]}
    if "finite" in value:
        finite = value["finite"]
        return {"variant": finite["discriminant"], "payload": {key: decode(item) for key, item in finite.get("payload", [])}}
    if "sequence" in value:
        return [decode(item) for item in value["sequence"]["values"]]
    if "boolean" in value:
        return value["boolean"]["value"]
    return next(iter(value.values()))["value"]


class Probe:
    def __init__(self):
        environment = dict(os.environ)
        environment["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl"
        self.process = subprocess.Popen(
            [".bootstrap/target/debug/mncs-compiler-stage0-probe"],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )

    def send(self, request):
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"Stage-0 probe exited {self.process.poll()}")
        return json.loads(line)

    def close(self):
        self.process.stdin.close()
        if self.process.wait(timeout=60) != 0:
            raise RuntimeError("Stage-0 probe did not exit cleanly")


def main():
    os.chdir(ROOT)
    probe = Probe()
    results = []
    started = time.monotonic()
    try:
        for identity, source in CASES:
            raw = source.encode()
            chunks = split4(raw)
            reference = probe.send({"elaborate": source})
            request = {
                "schema_version": "0.1",
                "target": {"module": "mncs.compiler.decl.v1", "function": "parse_unit"},
                "arguments": [blob(chunk) for chunk in chunks] + [integer(len(raw))],
                "step_budget": 8_000_000,
            }
            native = probe.send(request)
            unit = decode(native["returned"][0]) if native.get("status") == "returned" else None
            results.append({
                "pressure_id": identity,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "reference_diagnostics": [(item["code"], item["span"]["start"], item["span"]["end"]) for item in reference],
                "native_status": native.get("status"),
                "native_steps": native.get("steps"),
                "native_parse_ok": unit.get("ok") if isinstance(unit, dict) else None,
                "native_error_span": [unit.get("err_start"), unit.get("err_end")] if isinstance(unit, dict) and not unit.get("ok") else None,
                "native_failure": native.get("failure"),
            })
    finally:
        probe.close()
    report = {
        "schema_version": 1,
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "source_profile": "0.18",
        "scope": "reference elaboration acceptance vs native decl.parse_unit for the historical version-aware syntax rows",
        "identical_native_repetitions": 1,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "results": results,
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"stage0_revision": report["stage0_revision"], "elapsed_seconds": report["elapsed_seconds"], "results": results}, indent=2))


if __name__ == "__main__":
    main()
