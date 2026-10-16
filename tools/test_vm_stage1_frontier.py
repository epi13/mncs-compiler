#!/usr/bin/env python3
"""Three-executor differential for the bounded decl::parse_unit frontier.

The test emits one fresh canonical VM artifact from the current compiler
sources, then runs a small valid/invalid declaration corpus through that
artifact, the pinned reference interpreter, and Cranelift. Stage-0 supplies
the independent parse oracle for each source.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import test_vm_decl as decl
from test_vm_segment import run_batch, vm_to_wire


CASES = [
    ('bounded-slice-postfix',
     'mncs 0.18; module t; fn f(x: u64, a: u64, b: u64) -> (r: u64) '
     '{ return x[a + 1..b + 2]; }', 'pos'),
    ('nested-if-parent-close',
     'mncs 0.18; module t; fn f(a: u64, b: u64) -> (r: u64) '
     '{ if a == 1 { if b == 1 { return 2; } } return 0; }', 'pos'),
    ('generic-signature-and-call',
     'mncs 0.18; module t; fn g<N: Nat>(x: u64) -> (r: u64) { return x; } '
     'fn f(x: u64) -> (r: u64) { return g<4>(x); }', 'pos'),
    ('record-update',
     'mncs 0.18; module t; record R { x: u64, y: u64 } '
     'fn f(v: R) -> (r: u64) { let w: R = R { ..v, x: 2 }; return w.x; }', 'pos'),
    ('first-error-span',
     'mncs 0.18; module t; fn f() -> (r: u64) { return 1 + ; }', 'neg'),
]


def request_for(text):
    data = text.encode()
    assert len(data) <= decl.STRIDE_BOUND, (len(data), decl.STRIDE_BOUND)
    args = [decl.pages_value([data]), decl.integer(decl.STRIDE_BOUND),
            decl.integer(len(data))]
    return data, {
        'schema_version': '0.1',
        'target': {'module': decl.MODULE, 'function': 'parse_unit'},
        'arguments': args,
        'type_arguments': decl.TYPE_ARGS,
        'step_budget': decl.test_decl.EXECUTION_STEP_BUDGET,
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def main():
    cases = []
    source_identity = []
    reference = decl.Probe(backend='reference_interpreter',
                           cache_dir=Path('.build/probe-cache'))
    reference_values, reference_times, reference_steps = [], [], []
    t0 = time.monotonic()
    for case_id, text, kind in CASES:
        data, request = request_for(text)
        oracle_result = reference.send({'oracle': text})
        oracle = oracle_result
        case = {'id': f'decl.parse_unit:{case_id}', 'function': 'parse_unit',
                'args': request['arguments'], 'type_args': decl.TYPE_ARGS,
                'step_budget': request['step_budget']}
        t = time.monotonic()
        result = reference.send(request)
        reference_times.append(time.monotonic() - t)
        assert result['status'] == 'returned', (case_id, result)
        value = decl.decode(result['returned'][0])
        if kind == 'pos':
            assert not oracle['diagnostics'], (case_id, oracle['diagnostics'])
            decl.test_decl.check_pos_parse(value, oracle, data, text)
        else:
            decl.test_decl.check_neg_parse(value, oracle, text)
        reference_values.append(value)
        reference_steps.append(result['steps'])
        cases.append(case)
        source_identity.append({'id': case['id'], 'kind': kind,
                                'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})

    emitted = reference.send({'emit_vm_artifact': {'module': decl.MODULE}})
    artifact = emitted['artifact']
    assert artifact['schema_version'] == 'mncs.vm.artifact/1'
    reference_wall = time.monotonic() - t0
    reference.close()

    cranelift = decl.Probe(backend='cranelift', cache_dir=Path('.build/probe-cache'))
    cranelift_values, cranelift_times, cranelift_steps = [], [], []
    for (case_id, text, _), case in zip(CASES, cases):
        _, request = request_for(text)
        t = time.monotonic()
        result = cranelift.send(request)
        cranelift_times.append(time.monotonic() - t)
        assert result['status'] == 'returned', (case_id, result)
        value = decl.decode(result['returned'][0])
        assert value == reference_values[len(cranelift_values)], case_id
        cranelift_values.append(value)
        cranelift_steps.append(result['steps'])
    cranelift.close()

    with tempfile.TemporaryDirectory(prefix='mncs-vm-decl-frontier-') as directory:
        directory = Path(directory)
        artifact_path = directory / 'decl-current.json'
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + '\n')
        document, vm_wall, vm_rss_kb, calls_bytes, results_bytes = run_batch(
            artifact_path, cases, directory, module=decl.MODULE)

    vm_values, vm_steps = [], []
    for index, (case_id, text, kind) in enumerate(CASES):
        result = document['results'][index]
        assert result['id'] == cases[index]['id'], result
        assert result['outcome'] == {'kind': 'completed'}, (case_id, result['outcome'])
        value = decl.decode(vm_to_wire(result['record']['returned'][0]))
        assert value == reference_values[index] == cranelift_values[index], case_id
        vm_values.append(value)
        vm_steps.append(result['record']['usage']['steps'])
    # Positive and negative results above were independently checked against
    # Stage-0 before the artifact was emitted. The shared result equality then
    # carries those checks to both native executors.
    semantic_digests = {
        'reference_interpreter': digest(reference_values),
        'cranelift': digest(cranelift_values),
        'canonical_vm': digest(vm_values),
    }
    assert len(set(semantic_digests.values())) == 1, semantic_digests
    probe_path = Path(decl.PROBE)
    report = {
        'schema_version': 1,
        'kind': 'mncs-compiler-decl-parse-canonical-vm-frontier',
        'stage0_revision': decl.stage0_revision(),
        'compiler_revision_before_delivery': subprocess.check_output(
            ['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'probe_sha256': hashlib.sha256(probe_path.read_bytes()).hexdigest(),
        'language_source_sha256': hashlib.sha256(
            (Path('src/compiler/decl.mncs').read_bytes())).hexdigest(),
        'artifact_id': artifact['artifact_id'],
        'cases': source_identity,
        'executor_case_counts': {
            'reference_interpreter': len(reference_values),
            'cranelift': len(cranelift_values),
            'canonical_vm': len(vm_values),
        },
        'semantic_digests': semantic_digests,
        'steps': {
            'reference_interpreter': reference_steps,
            'cranelift': cranelift_steps,
            'canonical_vm': vm_steps,
        },
        'timing_seconds': {
            'reference_interpreter_cases': reference_times,
            'reference_interpreter_total_including_oracles_and_emit': reference_wall,
            'cranelift_cases': cranelift_times,
            'canonical_vm_batch': vm_wall,
        },
        'canonical_vm_peak_rss_kb': vm_rss_kb,
        'canonical_vm_batch_bytes': {'calls': calls_bytes, 'results': results_bytes},
        'scope': ('Five decl.parse_unit cases (four valid constructs and one fresh syntax error) '
                  'agree across Stage-0 structural/diagnostic checks, the reference interpreter, '
                  'Cranelift, and a freshly emitted canonical VM artifact. This is a function '
                  'frontier only; it is not full decl-module or succession evidence.'),
        'stage1_claimed': False,
        'stage2_claimed': False,
    }
    report_path = Path('.build/vm-efficiency/stage1-frontier.json')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
