#!/usr/bin/env python3
"""One bounded Stage-1 stress probe; records a frontier without adding syntax.

Run after cleanup validation:
python3 tools/test_vm_stage1_frontier.py .build/vm-efficiency/decl-after.json
"""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import time

import test_vm_decl as decl
from test_vm_segment import run_batch, vm_to_wire


def main():
    artifact = Path(sys.argv[1]).resolve()
    path = Path('src/compiler/source.mncs')
    data = path.read_bytes()
    pages = [data[i:i + decl.STRIDE_BOUND] for i in range(0, len(data), decl.STRIDE_BOUND)]
    args = [decl.pages_value(pages), decl.integer(decl.STRIDE_BOUND), decl.integer(len(data))]
    request = {'schema_version': '0.1', 'target': {'module': decl.MODULE, 'function': 'parse_unit'},
               'arguments': args, 'type_arguments': decl.TYPE_ARGS,
               'step_budget': decl.test_decl.EXECUTION_STEP_BUDGET}
    case = {'id': 'stage1:source', 'function': 'parse_unit', 'args': args,
            'type_args': decl.TYPE_ARGS, 'step_budget': request['step_budget']}
    values, times, steps = {}, {}, {}
    for backend in ['reference_interpreter', 'cranelift']:
        probe = decl.Probe(backend=backend, cache_dir=Path('.build/probe-cache'))
        t = time.monotonic()
        result = probe.send(request)
        times[backend] = time.monotonic() - t
        assert result['status'] == 'returned', result
        values[backend] = decl.decode(result['returned'][0])
        steps[backend] = result['steps']
        if backend == 'reference_interpreter':
            oracle = probe.send({'oracle': data.decode()})
            assert not oracle['diagnostics'], oracle['diagnostics']
        probe.close()
    with tempfile.TemporaryDirectory(prefix='mncs-vm-stage1-') as directory:
        document, wall, rss, _, _ = run_batch(artifact, [case], Path(directory), module=decl.MODULE)
    result = document['results'][0]
    assert result['outcome'] == {'kind': 'completed'}, result['outcome']
    values['vm'] = decl.decode(vm_to_wire(result['record']['returned'][0]))
    steps['vm'] = result['record']['usage']['steps']
    times['vm_batch'] = wall
    assert values['vm'] == values['reference_interpreter'] == values['cranelift']
    got = values['vm']
    assert not got['ok'], 'frontier moved; update this stress witness deliberately'
    start, end = got['err_start'], got['err_end']
    report = {
        'stage0_revision': decl.stage0_revision(), 'source': str(path), 'source_bytes': len(data),
        'source_sha256': hashlib.sha256(data).hexdigest(), 'request_step_budget': request['step_budget'],
        'vm_outcome': result['outcome'], 'vm_peak_rss_kb': rss, 'steps': steps,
        'wall_seconds': times, 'frontier': {'start': start, 'end': end,
        'line': data[:start].count(b'\n') + 1, 'token': data[start:end].decode(),
        'context': data[max(0, start - 40):end + 70].decode()},
        'semantic_digests': {k: hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()
                             for k, v in values.items()},
        'new_vm_envelope_blocker': False,
        'scope': 'real source.mncs self-ingestion: matching structured Stage-1 source frontier; no feature implementation',
    }
    Path('.build/vm-efficiency/stage1-frontier.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
