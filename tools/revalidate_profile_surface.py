#!/usr/bin/env python3
"""Compare Profile 0.18 syntax admission in native decl parsing vs Stage-0."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
CAMPAIGN_ID = os.environ.get("MNCS_CAMPAIGN_ID", datetime.now(timezone.utc).strftime("%Y%m%d"))
OUT = ROOT / f".build/campaign-{CAMPAIGN_ID}-profile-surface-results.json"
CAMPAIGN_OUT = ROOT / f"evidence/campaign-{CAMPAIGN_ID}-profile-surface-results.json"
CASES = [
    ("CP-0015-not", "mncs 0.18; module p.bool_not; fn f(a: bool) -> (r: bool) { return !a; }", True, True),
    ("CP-0015-not-nonbool", "mncs 0.18; module p.bool_not_bad; fn f() -> (r: bool) { return !1; }", True, False),
    ("CP-0015-not-012", "mncs 0.12; module p.bool_not_old; fn f(a: bool) -> (r: bool) { return !a; }", False, False),
    ("CP-0015-negative", "mncs 0.18; module p.negative; fn f() -> (r: i64) { return -5; }", True, True),
    ("CP-0015-negative-012", "mncs 0.12; module p.negative_old; fn f() -> (r: i64) { return -5; }", False, False),
    ("CP-0015-repeat", "mncs 0.18; module p.repeat; fn f() -> (r: [u64; 4]) { return [0; 4]; }", True, True),
    ("CP-0015-repeat-mismatch", "mncs 0.18; module p.repeat_bad; fn f() -> (r: [u64; 4]) { return [0; 3]; }", True, False),
    ("CP-0015-repeat-symbolic", "mncs 0.18; module p.repeat_symbolic; fn f(n: u64) -> (r: [u64; 4]) { return [0; n]; }", True, False),
    ("CP-0015-repeat-012", "mncs 0.12; module p.repeat_old; fn f() -> (r: [u64; 4]) { return [0; 4]; }", False, False),
    ("CP-0015-next", "mncs 0.18; module p.next_field; record R { next: u64 } fn f(v: R) -> (r: u64) { return v.next; }", True, True),
    ("CP-0013-next-013", "mncs 0.13; module p.next_field_013; record R { next: u64 } fn f(v: R) -> (r: u64) { return v.next; }", True, True),
    ("CP-0013-next-010", "mncs 0.10; module p.next_field_010; record R { next: u64 } fn f(v: R) -> (r: u64) { return v.next; }", False, False),
    ("CP-0015-scalar-match", "mncs 0.18; module p.scalar_match; fn f(x: u64) -> (r: u64) { return match x { 0 => 1, _ => 2 }; }", True, True),
    ("CP-0015-scalar-match-expressions", "mncs 0.18; module p.scalar_match_expressions; fn inc(x: u64) -> (r: u64) { return x + 1; } fn f(x: u64) -> (r: u64) { return match x { 0 => inc(x), _ => x + 2 }; }", True, True),
    ("CP-0015-scalar-match-negative-pattern", "mncs 0.18; module p.scalar_match_negative; fn f(x: i64) -> (r: i64) { return match x { -5 => 1, _ => 2 }; }", True, True),
    ("CP-0015-scalar-match-missing-default", "mncs 0.18; module p.scalar_match_missing; fn f(x: u64) -> (r: u64) { return match x { 0 => 1 }; }", True, False),
    ("CP-0015-scalar-match-duplicate", "mncs 0.18; module p.scalar_match_duplicate; fn f(x: u64) -> (r: u64) { return match x { 0 => 1, 0 => 2, _ => 3 }; }", True, False),
    ("CP-0015-scalar-match-negative-unsigned", "mncs 0.18; module p.scalar_match_unsigned; fn f(x: u64) -> (r: u64) { return match x { -1 => 1, _ => 2 }; }", True, False),
    ("CP-0015-scalar-match-012", "mncs 0.12; module p.scalar_match_old; fn f(x: u64) -> (r: u64) { return match x { 0 => 1, _ => 2 }; }", False, False),
    ("CP-0010-scalar-match-013", "mncs 0.13; module p.scalar_match_013; fn f(x: u64) -> (r: u64) { return match x { 0 => 1, _ => 2 }; }", True, True),
    ("CP-0010-scalar-match-010", "mncs 0.10; module p.scalar_match_010; fn f(x: u64) -> (r: u64) { return match x { 0 => 1, _ => 2 }; }", False, False),
]
SOURCE_BOUND = max(len(source.encode()) for _, source, _, _ in CASES)


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
        for identity, source, expected_parse, expected_proof in CASES:
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
                "expected_native_parse_ok": expected_parse,
                "expected_native_proof_ok": expected_proof,
                "native_conformance": isinstance(unit, dict) and unit.get("ok") == expected_parse and isinstance(proof, dict) and proof.get("ok") == expected_proof,
                "native_failure": native.get("failure"),
            })
    finally:
        probe.close()
    report = {
        "schema_version": 1,
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "source_profile": "0.18",
        "stage0_reference_mode": os.environ.get("MNCS_PROBE_REFERENCE_MODE", "locked"),
        "scope": "pinned Stage-0 differential for Profile 0.18 unary not, signed atoms, repeat literals, contextual next fields, scalar integer matches, semantic rejections, and CP-0010/CP-0013 old-profile gates",
        "identical_native_repetitions": 1,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "conformance": all(item["native_conformance"] for item in results),
        "results": results,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    CAMPAIGN_OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    CAMPAIGN_OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"stage0_revision": report["stage0_revision"], "elapsed_seconds": report["elapsed_seconds"], "conformance": report["conformance"], "results": results}, indent=2))
    if not report["conformance"]:
        raise SystemExit("native Profile 0.18 surface differs from expected Stage-0 admission/proof facts")


if __name__ == "__main__":
    main()
