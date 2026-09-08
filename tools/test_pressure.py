#!/usr/bin/env python3
"""Check pinned diagnostics and a supported composite-type control."""
import json
from pathlib import Path
import subprocess
import os
os.chdir(Path(__file__).resolve().parents[1])
results = []
for name, code in [('source-65', 'MNE105'), ('bool-equality', 'MNE121'), ('unbounded-scan', 'MNP106'), ('leading-comment-envelope', 'MNE002')]:
    result = subprocess.run(['.bootstrap/target/debug/mncs', 'abi', f'pressure/repro/{name}.mncs'], capture_output=True, text=True, timeout=30)
    diagnostics = json.loads(result.stdout)['diagnostics']
    assert result.returncode == 1 and code in [d['code'] for d in diagnostics], (name, result)
    results.append({'repro': name, 'exit_code': result.returncode, 'diagnostics': diagnostics})
result = subprocess.run(['.bootstrap/target/debug/mncs', 'abi', 'tests/fixtures/token-sequence.mncs'], capture_output=True, text=True, timeout=30)
assert result.returncode == 0, result.stdout
results.append({'control': 'record sequence', 'status': 'elaborated; runtime/backend not tested by this control'})
Path('.build').mkdir(exist_ok=True)
Path('.build/pressure-results.json').write_text(json.dumps(results, indent=2)+'\n')
print('Four expected rejection probes and one supported control passed.')
