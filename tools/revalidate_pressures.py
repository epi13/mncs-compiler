#!/usr/bin/env python3
"""Re-run historical pressure sources under the currently pinned profile.

The checked-in fixtures stay byte-for-byte historical. This script stages
copies with only their profile header changed to the current Stage-0 profile,
then records the reference compiler's actual acceptance and diagnostics.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import shutil

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
LOCK = json.loads((ROOT / "mncs-language.lock.json").read_text())
PROFILE = LOCK["source_profile"]
MNCS = ROOT / ".bootstrap/target/debug/mncs"
OUT = ROOT / ".build/current-pressure-repros"
OUT.mkdir(parents=True, exist_ok=True)

FIXTURES = [
    ("CP-0001", "source-65.mncs"),
    ("CP-0003", "unbounded-scan.mncs"),
    ("CP-0004", "bool-equality.mncs"),
    ("CP-0006", "leading-comment-envelope.mncs"),
    ("CP-0008", "finite-payload-home.mncs"),
    ("CP-0008", "finite-payload-consumer.mncs"),
    ("CP-0009", "counted-bound-256.mncs"),
    ("CP-0009", "iteration-identity-dup.mncs"),
    ("CP-0010", "match-u64-pattern.mncs"),
    ("CP-0011", "recursive-call.mncs"),
    ("CP-0012", "enum-sequence-payload.mncs"),
    ("CP-0013", "keyword-field-next.mncs"),
    ("CP-0014", "bool-payload.mncs"),
]

CURRENT_SYNTAX = [
    ("CP-0015-not", "module pressure.profile018_not; fn f(a: bool) -> (r: bool) { return !a; }"),
    ("CP-0015-negative", "module pressure.profile018_negative; fn f() -> (r: i64) { return -5; }"),
    ("CP-0015-repeat", "module pressure.profile018_repeat; fn f() -> (r: [u64; 4]) { return [0; 4]; }"),
    ("CP-0015-next", "module pressure.profile018_next; record R { next: u64 } fn f(v: R) -> (r: u64) { return v.next; }"),
    ("CP-0015-scalar-match", "module pressure.profile018_match; fn f(x: u64) -> (r: u64) { return match x { 0 => 1, _ => 2 }; }"),
    ("CP-0011-numeric-recursion", "module pressure.numeric_recursion; fn descend(n: u64) -> (r: u64) { if n == 0 { return 0; } return descend(n - 1); }"),
    ("CP-0011-mutual-recursion", "module pressure.mutual_recursion; fn even(n: u64) -> (r: bool) { if n == 0 { return true; } return odd(n - 1); } fn odd(n: u64) -> (r: bool) { if n == 0 { return false; } return even(n - 1); }"),
    ("CP-0002-unicode", "module pressure.caf\u00e9; fn f() -> (r: bool) { return true; }"),
]


def diagnostics(document):
    if isinstance(document, dict):
        if isinstance(document.get("diagnostics"), list):
            return document["diagnostics"]
        for key in ("result", "output", "compilation"):
            nested = document.get(key)
            if isinstance(nested, dict):
                found = diagnostics(nested)
                if found:
                    return found
    return []


def run_case(identity, original_bytes, staged_path):
    source_sha = hashlib.sha256(original_bytes).hexdigest()
    staged_bytes = staged_path.read_bytes()
    environment = dict(os.environ)
    environment["MNCS_LIBRARY_PATH"] = os.pathsep.join(
        [str(OUT), str(ROOT / "pressure/repro"), str(ROOT / "src")]
    )
    started = time.monotonic()
    process = subprocess.run(
        [str(MNCS), "abi", str(staged_path)],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    elapsed = time.monotonic() - started
    try:
        output = json.loads(process.stdout) if process.stdout.strip() else None
    except json.JSONDecodeError:
        output = None
    return {
        "pressure_id": identity,
        "fixture": str(staged_path.relative_to(ROOT)),
        "historical_source_sha256": source_sha,
        "current_profile_source_sha256": hashlib.sha256(staged_bytes).hexdigest(),
        "source_profile": PROFILE,
        "exit_code": process.returncode,
        "outcome": "accepted" if process.returncode == 0 else "rejected",
        "diagnostic_codes": [item.get("code") for item in diagnostics(output) if isinstance(item, dict)],
        "elapsed_seconds": round(elapsed, 4),
        "stderr": process.stderr.strip()[:2000],
        "output_schema_version": output.get("schema_version") if isinstance(output, dict) else None,
    }


def main():
    started = time.monotonic()
    results = []
    shutil.rmtree(OUT, ignore_errors=True)
    for pressure_id, name in FIXTURES:
        original = ROOT / "pressure/repro" / name
        raw = original.read_bytes()
        staged = re.sub(rb"mncs 0\.\d+;", f"mncs {PROFILE};".encode(), raw, count=1)
        if staged == raw:
            raise RuntimeError(f"profile header not found in {original}")
        module = re.search(rb"\bmodule\s+([A-Za-z0-9_.]+)\s*;", staged)
        if module is None:
            raise RuntimeError(f"module declaration not found in {original}")
        path = OUT.joinpath(*module.group(1).decode().split(".")).with_suffix(".mncs")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(staged)
        results.append(run_case(pressure_id, raw, path))
    for pressure_id, body in CURRENT_SYNTAX:
        source = f"mncs {PROFILE};\n{body}\n".encode()
        path = OUT / f"{pressure_id}.mncs"
        path.write_bytes(source)
        results.append(run_case(pressure_id, source, path))
    report = {
        "schema_version": 1,
        "stage0_revision": LOCK["revision"],
        "source_profile": PROFILE,
        "method": "historical reproducer bytes are preserved; only profile headers are advanced for current-profile probes",
        "probe_count": len(results),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "results": results,
    }
    target = ROOT / ".build/pressure-current-results.json"
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({
        "stage0_revision": report["stage0_revision"],
        "source_profile": PROFILE,
        "probe_count": len(results),
        "outcomes": [{"id": item["pressure_id"], "outcome": item["outcome"], "codes": item["diagnostic_codes"]} for item in results],
        "report": str(target.relative_to(ROOT)),
    }, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
