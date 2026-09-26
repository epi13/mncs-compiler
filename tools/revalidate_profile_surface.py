#!/usr/bin/env python3
"""Compare Profile 0.18 syntax admission in native decl parsing vs Stage-0."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
OUT = ROOT / ".build/profile-surface-results.json"
CAMPAIGN_OUT = ROOT / "evidence/campaign-20260925-profile-surface-results.json"
CASES = [
    ("CP-0015-not", "mncs 0.18; module p.bool_not; fn f(a: bool) -> (r: bool) { return !a; }"),
    ("CP-0015-negative", "mncs 0.18; module p.negative; fn f() -> (r: i64) { return -5; }"),
    ("CP-0015-repeat", "mncs 0.18; module p.repeat; fn f() -> (r: [u64; 4]) { return [0; 4]; }"),
    ("CP-0015-next", "mncs 0.18; module p.next_field; record R { next: u64 } fn f(v: R) -> (r: u64) { return v.next; }"),
    ("CP-0015-scalar-match", "mncs 0.18; module p.scalar_match; fn f(x: u64) -> (r: u64) { return match x { 0 => 1, _ => 2 }; }"),
]
SOURCE_BOUND = max(len(source.encode()) for _, source in CASES)


def nat_arg(value):
    return {'kind': 'nat', 'value': value}


def integer(value):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def blob(data):
    return {"sequence": {"values": [{"byte": {"value": item}} for item in data]}}


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
        if environment.get("MNCS_PROBE_BACKEND") == "reference_interpreter":
            environment.pop("MNCS_PROBE_BACKEND", None)
        environment["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl"
        environment["MNCS_PROBE_EXECUTION_MODULES"] = "mncs.compiler.decl.v1"
        environment.setdefault("MNCS_PROBE_BACKEND", "cranelift")
        environment["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([{
            "module": "mncs.compiler.decl.v1",
            "function": function,
            "type_arguments": [nat_arg(SOURCE_BOUND)],
        } for function in ("parse_unit", "prove_unit")])
        self.process = subprocess.Popen(
            [environment.get("MNCS_PROBE_BIN", str(BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"))],
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
            original = source.encode()
            raw = original + b" " * (SOURCE_BOUND - len(original))
            source_text = raw.decode("ascii")
            reference = probe.send({"elaborate": source_text})
            request = {
                "schema_version": "0.1",
                "target": {"module": "mncs.compiler.decl.v1", "function": "parse_unit"},
                "arguments": [blob(raw)],
                "type_arguments": [nat_arg(SOURCE_BOUND)],
                "step_budget": 8_000_000,
            }
            native = probe.send(request)
            unit = decode(native["returned"][0]) if native.get("status") == "returned" else None
            proof_request = {
                **request,
                "target": {"module": "mncs.compiler.decl.v1", "function": "prove_unit"},
            }
            proof_native = probe.send(proof_request)
            proof = decode(proof_native["returned"][0]) if proof_native.get("status") == "returned" else None
            results.append({
                "pressure_id": identity,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "reference_diagnostics": [(item["code"], item["span"]["start"], item["span"]["end"]) for item in reference],
                "native_status": native.get("status"),
                "native_steps": native.get("steps"),
                "native_parse_ok": unit.get("ok") if isinstance(unit, dict) else None,
                "native_error_span": [unit.get("err_start"), unit.get("err_end")] if isinstance(unit, dict) and not unit.get("ok") else None,
                "native_proof_ok": proof.get("ok") if isinstance(proof, dict) else None,
                "native_proof_error_span": [proof.get("err_start"), proof.get("err_end")] if isinstance(proof, dict) and not proof.get("ok") else None,
                "native_failure": native.get("failure"),
            })
    finally:
        probe.close()
    report = {
        "schema_version": 1,
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "source_profile": "0.18",
        "stage0_reference_mode": os.environ.get("MNCS_PROBE_REFERENCE_MODE", "locked"),
        "scope": "reference elaboration acceptance vs native decl.parse_unit for the historical version-aware syntax rows",
        "identical_native_repetitions": 1,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "results": results,
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    CAMPAIGN_OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"stage0_revision": report["stage0_revision"], "elapsed_seconds": report["elapsed_seconds"], "results": results}, indent=2))


if __name__ == "__main__":
    main()
