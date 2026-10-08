#!/usr/bin/env python3
"""Three-executor regression for the current bounded-slice frontier.

The test emits a fresh canonical VM artifact from the current source through
the pinned Stage-0 probe. A previously emitted artifact can predate the
compiler changes under test and is not suitable for this frontier witness.
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
    text = ('mncs 0.18; module t; '
            'fn f(x: u64, a: u64, b: u64) -> (r: u64) { '
            'return x[a + 1..b + 2]; }')
    data = text.encode()
    pages = [data[i:i + decl.STRIDE_BOUND] for i in range(0, len(data), decl.STRIDE_BOUND)]
    args = [decl.pages_value(pages), decl.integer(decl.STRIDE_BOUND), decl.integer(len(data))]
    request = {'schema_version': '0.1', 'target': {'module': decl.MODULE, 'function': 'parse_unit'},
               'arguments': args, 'type_arguments': decl.TYPE_ARGS,
               'step_budget': decl.test_decl.EXECUTION_STEP_BUDGET}
    case = {'id': 'stage1:nested-if-parent-close', 'function': 'parse_unit', 'args': args,
            'type_args': decl.TYPE_ARGS, 'step_budget': request['step_budget']}
    values, times, steps = {}, {}, {}
    reference = decl.Probe(backend='reference_interpreter', cache_dir=Path('.build/probe-cache'))
    t = time.monotonic()
    result = reference.send(request)
    times['reference_interpreter'] = time.monotonic() - t
    assert result['status'] == 'returned', result
    values['reference_interpreter'] = decl.decode(result['returned'][0])
    steps['reference_interpreter'] = result['steps']
    oracle = reference.send({'oracle': data.decode()})
    assert not oracle['diagnostics'], oracle['diagnostics']
    emitted = reference.send({'emit_vm_artifact': {'module': decl.MODULE}})
    artifact = emitted['artifact']
    assert artifact['schema_version'] == 'mncs.vm.artifact/1'
    reference.close()

    cranelift = decl.Probe(backend='cranelift', cache_dir=Path('.build/probe-cache'))
    t = time.monotonic()
    result = cranelift.send(request)
    times['cranelift'] = time.monotonic() - t
    assert result['status'] == 'returned', result
    values['cranelift'] = decl.decode(result['returned'][0])
    steps['cranelift'] = result['steps']
    cranelift.close()
    with tempfile.TemporaryDirectory(prefix='mncs-vm-stage1-') as directory:
        artifact_path = Path(directory) / 'decl-current.json'
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + '\n')
        document, wall, rss, _, _ = run_batch(artifact_path, [case], Path(directory), module=decl.MODULE)
    result = document['results'][0]
    assert result['outcome'] == {'kind': 'completed'}, result['outcome']
    values['vm'] = decl.decode(vm_to_wire(result['record']['returned'][0]))
    steps['vm'] = result['record']['usage']['steps']
    times['vm_batch'] = wall
    assert values['vm'] == values['reference_interpreter'] == values['cranelift'], {
        'vm_ok': values['vm'].get('ok'),
        'reference_ok': values['reference_interpreter'].get('ok'),
        'cranelift_ok': values['cranelift'].get('ok'),
        'artifact_id': artifact['artifact_id'],
    }
    got = values['vm']
    assert got['ok'], 'current parser rejected the bounded slice witness'
    report = {
        'stage0_revision': decl.stage0_revision(), 'source': 'inline:bounded-slice-postfix', 'source_bytes': len(data),
        'source_sha256': hashlib.sha256(data).hexdigest(), 'request_step_budget': request['step_budget'],
        'artifact_id': artifact['artifact_id'],
        'vm_outcome': result['outcome'], 'vm_peak_rss_kb': rss, 'steps': steps,
        'wall_seconds': times, 'native_parse_ok': got['ok'],
        'semantic_digests': {k: hashlib.sha256(json.dumps(v, sort_keys=True).encode()).hexdigest()
                             for k, v in values.items()},
        'new_vm_envelope_blocker': False,
        'scope': 'bounded-slice postfix AST: Stage-0, reference, freshly emitted canonical VM, and Cranelift agree',
    }
    Path('.build/vm-efficiency/stage1-frontier.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
