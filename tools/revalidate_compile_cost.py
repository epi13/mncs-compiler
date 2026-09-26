#!/usr/bin/env python3
"""Repeat the historical linked IR-size workload at the current Stage-0 pin."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
os.chdir(ROOT)
LOCK = json.loads((ROOT / "mncs-language.lock.json").read_text())
MNCS = Path(os.environ.get("MNCS_CLI_BIN", BOOTSTRAP_TARGET / "release" / "mncs"))
OUT = ROOT / ".build/current-compile-cost"
OUT.mkdir(parents=True, exist_ok=True)
ENV = dict(os.environ, MNCS_LIBRARY_PATH=str(ROOT / "src"))


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(index):
    output_dir = OUT / f"run-{index}"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(MNCS), "compile", "src/compiler/kernel.mncs",
        "--emit", "semantic,hir,ssa", "--output-dir", str(output_dir),
    ]
    start = time.monotonic()
    proc = subprocess.run(command, cwd=ROOT, env=ENV, capture_output=True,
                          text=True, timeout=3600, check=False)
    elapsed = time.monotonic() - start
    files = [
        {"path": str(path.relative_to(output_dir)), "bytes": path.stat().st_size,
         "sha256": digest(path)}
        for path in sorted(output_dir.rglob("*")) if path.is_file()
    ]
    try:
        stdout = json.loads(proc.stdout) if proc.stdout.strip() else None
    except json.JSONDecodeError:
        stdout = None
    return {
        "exit_code": proc.returncode,
        "elapsed_seconds": round(elapsed, 3),
        "files": files,
        "stdout_schema_version": stdout.get("schema_version") if isinstance(stdout, dict) else None,
        "stdout_status": stdout.get("status") if isinstance(stdout, dict) else None,
        "diagnostic_code_counts": dict(sorted(Counter(
            item.get("code") for item in (stdout or {}).get("diagnostics", [])
            if isinstance(item, dict) and item.get("code")
        ).items())) if isinstance(stdout, dict) else {},
        "stderr": proc.stderr.strip()[:2000],
    }


first = run(1)
second = run(2)
if first["exit_code"] or second["exit_code"]:
    raise SystemExit(json.dumps({"first": first, "second": second}, indent=2))
if [(f["path"], f["bytes"], f["sha256"]) for f in first["files"]] != [
    (f["path"], f["bytes"], f["sha256"]) for f in second["files"]
]:
    raise SystemExit("repeated current Stage-0 compile outputs differ")
report = {
    "schema_version": 1,
    "stage0_revision": LOCK["revision"],
    "source_profile": LOCK["source_profile"],
    "command": "mncs compile src/compiler/kernel.mncs --emit semantic,hir,ssa --output-dir <run-dir>",
    "identical_runs": 2,
    "runs": [first, second],
    "scope": "Stage-0 linked semantic/HIR/SSA artifact sizes and repeat hashes; not native backend or peak-memory evidence.",
}
target = ROOT / "evidence/campaign-20260925-compile-cost-results.json"
target.write_text(json.dumps(report, indent=2) + "\n")
print(json.dumps(report, indent=2))
