#!/usr/bin/env python3
"""Check pinned diagnostics and a supported composite-type control."""
import json
from pathlib import Path
import os
import subprocess
import time

os.chdir(Path(__file__).resolve().parents[1])
ROOT = Path.cwd()
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
LOCK = json.loads((ROOT / 'mncs-language.lock.json').read_text())
started = time.monotonic()
results = []
mncs = os.environ.get('MNCS_CLI_BIN', str(BOOTSTRAP_TARGET / "release" / "mncs"))
for name, code in [('source-65', 'MNE105'), ('bool-equality', 'MNE121'), ('unbounded-scan', 'MNP106'), ('leading-comment-envelope', 'MNE002')]:
    case_started = time.monotonic()
    result = subprocess.run([mncs, 'abi', f'pressure/repro/{name}.mncs'], capture_output=True, text=True, timeout=30)
    diagnostics = json.loads(result.stdout)['diagnostics']
    assert result.returncode == 1 and code in [d['code'] for d in diagnostics], (name, result)
    results.append({'repro': name, 'exit_code': result.returncode, 'diagnostics': diagnostics, 'elapsed_seconds': round(time.monotonic() - case_started, 4)})
case_started = time.monotonic()
result = subprocess.run([mncs, 'abi', 'tests/fixtures/token-sequence.mncs'], capture_output=True, text=True, timeout=30)
assert result.returncode == 0, result.stdout
results.append({'control': 'record sequence', 'status': 'elaborated; runtime/backend not tested by this control', 'elapsed_seconds': round(time.monotonic() - case_started, 4)})
report = {
    'schema_version': 1,
    'stage0_revision': LOCK['revision'],
    'stage0_reference_mode': os.environ.get('MNCS_PROBE_REFERENCE_MODE', 'locked'),
    'elapsed_seconds': round(time.monotonic() - started, 4),
    'scope': 'pinned diagnostic pressure reproductions plus one supported record-sequence control',
    'results': results,
}
target = ROOT / 'evidence/campaign-20260925-pressure-suite-results.json'
target.write_text(json.dumps(report, indent=2) + '\n')
print('Four expected rejection probes and one supported control passed.')
