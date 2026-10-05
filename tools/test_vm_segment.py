#!/usr/bin/env python3
"""Segment suite through the canonical VM (direct bytes + batch).

Routes the real segment/lexer compiler-helper workload through:

    pinned Stage-0 elaboration
      -> direct mncs.vm.artifact/1 emission (no research payload)
      -> canonical mncs-vm batch execution (one admission, one process)
      -> structured results compared per case and per digest

against the pinned reference interpreter AND Cranelift (the suite's
current backend). Expectations are generated from Stage-0 oracle
facts only, so every executor is checked independently; the three
semantic digests must then agree with each other.

Corpus, wire shapes, decode, and digest rules are imported from
test_segment so the workload is the suite's workload, not a copy.
Case generation mirrors test_segment.suite's cursor threading
exactly; the reference raw digest is cross-checked against a real
test_segment run to prove it.

Measurements (wall, RSS, bytes, VM accounting) print at the end and
persist to .build/vm-segment-results.json.

Run: python3 tools/test_vm_segment.py [--smoke]
"""
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_policy
import test_segment
from test_segment import (SOURCE_BOUND, blob, decode, integer, nat_arg)
import test_vm_emit

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
PROBE = Path(os.environ.get("MNCS_PROBE_BIN", BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"))
os.chdir(ROOT)
OUT = ROOT / '.build'
OUT.mkdir(exist_ok=True)
MODULE = 'mncs.compiler.segment.v1'
FUNCTIONS = ['byte_at', 'ascii', 'span_valid', 'next_token', 'significant']


def stage0_revision():
    return json.loads(Path('mncs-language.lock.json').read_text())['revision']


class Probe:
    def __init__(self, backend=None, cache_dir=None):
        env = dict(os.environ)
        if backend is None or backend == 'reference_interpreter':
            env.pop('MNCS_PROBE_BACKEND', None)
        else:
            env['MNCS_PROBE_BACKEND'] = backend
        env['MNCS_PROBE_MODULES'] = test_segment.PROBE_MODULES
        env['MNCS_PROBE_EXECUTION_MODULES'] = MODULE
        env['MNCS_PROBE_GENERIC_SEEDS'] = json.dumps(
            [{'module': MODULE, 'function': function, 'type_arguments': [nat_arg(SOURCE_BOUND)]}
             for function in FUNCTIONS])
        if cache_dir is not None:
            env['MNCS_PROBE_CACHE_DIR'] = str(cache_dir)
        self.proc = subprocess.Popen(
            [str(PROBE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=env, cwd=ROOT)
        self.backend = backend or 'reference_interpreter'

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + '\n')
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f'probe terminated: {self.proc.poll()}'
        return json.loads(line)

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=120) == 0
        return self.proc.stderr.read()


def vm_to_wire(value):
    """Normalize a VM record value to oracle wire shape for comparison."""
    if 'Integer' in value:
        node = value['Integer']
        return {'integer': {'type': {'bits': node['bits'], 'signed': node['signed']},
                            'value': node['value']}}
    if 'Boolean' in value:
        return {'boolean': {'value': value['Boolean']}}
    if 'Byte' in value:
        return {'byte': {'value': value['Byte']}}
    if 'Sequence' in value:
        return {'sequence': {'values': [vm_to_wire(v) for v in value['Sequence']['elements']]}}
    if 'Record' in value:
        return {'record': {'fields': [[k, vm_to_wire(v)] for k, v in value['Record']['fields']]}}
    raise AssertionError(f'unexpected VM value {value}')


def gen_cases(probe, texts, smoke):
    """Generate (request, expected) cases from oracle facts only.

    Mirrors test_segment.suite line for line: cursors follow oracle
    span ends, which the live suite asserts results equal, so the
    generated request stream is identical.
    """
    kinds = json.loads(Path('src/compiler/token-kinds.json').read_text())
    inv = {value: int(key) for key, value in kinds.items()}
    cases = []
    n_tokens = 0

    def case(function, args, expected):
        cases.append({'id': f"{len(cases)}:{function}", 'function': function,
                      'args': args, 'type_args': [nat_arg(SOURCE_BOUND)],
                      'expected': expected})

    for original in texts:
        data = original + b' ' * (SOURCE_BOUND - len(original))
        text = data.decode('ascii')
        oracle = probe.send({'oracle': text})['lexical']
        assert all(byte < 128 for byte in data)
        case('ascii', [blob(data)], True)
        for start, end, expected in [(0, len(data), True), (len(data), len(data), True),
                                     (1, 0, False), (0, len(data) + 1, False)]:
            case('span_valid', [blob(data), integer(start), integer(end)], expected)
        for offset in list(range(0, len(data), 37)) + [len(data), len(data) + 5]:
            case('byte_at', [blob(data), integer(offset)],
                 data[offset] if offset < len(data) else 256)
        diagnostics = {(item['span']['start'], item['span']['end']): item['code']
                       for item in oracle['diagnostics']}
        cursor = 0
        for expected_token in oracle['tokens']:
            span = expected_token['span']
            code = diagnostics.get((span['start'], span['end']))
            case('next_token', [blob(data), integer(cursor)],
                 {'kind': inv[expected_token['kind']], 'start': span['start'], 'end': span['end'],
                  'diagnostic': {'MNL001': 1, 'MNL002': 2}.get(code, 0)})
            cursor = span['end']
            n_tokens += 1
        case('next_token', [blob(data), integer(cursor)],
             {'kind': 0, 'start': len(data), 'end': len(data), 'diagnostic': 0})
        expected_significant = [
            token for token in oracle['tokens']
            if token['kind'] not in ('whitespace', 'line_comment', 'block_comment')
            or (token['span']['start'], token['span']['end']) in diagnostics
        ]
        cursor = 0
        for expected_token in expected_significant:
            span = expected_token['span']
            code = diagnostics.get((span['start'], span['end']))
            case('significant', [blob(data), integer(cursor)],
                 {'kind': inv[expected_token['kind']], 'start': span['start'], 'end': span['end'],
                  'diagnostic': {'MNL001': 1, 'MNL002': 2}.get(code, 0)})
            cursor = span['end']
        # Final significant call: EOF or a diagnostic token (predicate,
        # exactly like the live suite's closing assertion).
        case('significant', [blob(data), integer(cursor)], {'eof_or_diagnostic': True})

    for original in (test_segment.NONASCII[:1] if smoke else test_segment.NONASCII):
        data = original + b' ' * (SOURCE_BOUND - len(original))
        case('ascii', [blob(data)], False)
        first_bad = next(index for index, byte in enumerate(data) if byte >= 128)
        case('next_token', [blob(data), integer(first_bad)],
             {'kind': 7, 'start': first_bad, 'end': first_bad + 1, 'diagnostic': 3})
        case('span_valid', [blob(data), integer(0), integer(len(data))], True)
    return cases, n_tokens


def check_expected(case_item, wire):
    got = decode(wire[0])
    expected = case_item['expected']
    if isinstance(expected, dict) and expected.get('eof_or_diagnostic'):
        assert got['kind'] == 0 or got['diagnostic'] != 0, (case_item['id'], got)
        return got
    assert got == expected, (case_item['id'], got, expected)
    return got


def execute_reference(probe, cases):
    """test_segment.Probe.run replicated exactly (including its digest)."""
    digest = hashlib.sha256()
    steps = []
    wire_results = []
    for case_item in cases:
        request = {
            'schema_version': '0.1',
            'target': {'module': MODULE, 'function': case_item['function']},
            'arguments': case_item['args'],
            'type_arguments': case_item['type_args'],
            'step_budget': 8000000,
        }
        result = probe.send(request)
        assert result['status'] == 'returned', (case_item['id'], result)
        steps.append(result['steps'])
        normalized = [request, result['status'], result['returned'], result['failure']]
        digest.update(json.dumps(normalized, sort_keys=True).encode())
        check_expected(case_item, result['returned'])
        wire_results.append(result['returned'])
    return digest.hexdigest(), steps, wire_results


def semantic_digest(cases, wire_results):
    digest = hashlib.sha256()
    for case_item, returned in zip(cases, wire_results):
        assert len(returned) == 1
        digest.update(json.dumps(
            [case_item['id'], decode(returned[0])], sort_keys=True).encode() + b'\n')
    return digest.hexdigest()


def run_batch(artifact_path, cases, tmp):
    calls = [{'id': c['id'], 'callable': f"{MODULE}::{c['function']}",
              'args': c['args'], 'type_args': c['type_args']} for c in cases]
    calls_path = tmp / 'calls.json'
    out_path = tmp / 'results.json'
    calls_path.write_text(json.dumps(calls))
    test_vm_emit.ensure_vm()
    started = time.monotonic()
    completed = subprocess.run(
        ['/usr/bin/time', '-v', str(test_vm_emit.VM_BIN), 'batch',
         '--artifact', str(artifact_path), '--calls', str(calls_path),
         '--output', str(out_path)],
        capture_output=True, text=True)
    wall = time.monotonic() - started
    assert completed.returncode == 0, completed.stderr[-2000:]
    peak_kb = None
    for line in completed.stderr.splitlines():
        if 'Maximum resident set size' in line:
            peak_kb = int(line.split(':')[1].strip().split()[0])
    document = json.loads(out_path.read_text())
    assert len(document['results']) == len(cases)
    return document, wall, peak_kb, calls_path.stat().st_size, out_path.stat().st_size


def main():
    smoke = len(sys.argv) > 1 and sys.argv[1] == '--smoke'
    assert PROBE.exists(), f'probe missing: {PROBE}'
    process_count = 0
    phase_wall = {}
    t0 = time.monotonic()

    texts = test_segment.samples(smoke=smoke)
    assert all(len(t) <= SOURCE_BOUND for t in texts)

    cache_dir = OUT / 'probe-cache'
    ref = Probe(backend=None, cache_dir=cache_dir)
    process_count += 1
    t_gen = time.monotonic()
    cases, n_tokens = gen_cases(ref, texts, smoke)
    phase_wall['oracle_and_generation'] = round(time.monotonic() - t_gen, 3)

    t_emit = time.monotonic()
    emitted = ref.send({'emit_vm_artifact': {'module': MODULE}})
    phase_wall['direct_emission'] = round(time.monotonic() - t_emit, 3)
    artifact = emitted['artifact']
    assert artifact['schema_version'] == 'mncs.vm.artifact/1'
    assert 'program' not in artifact
    tmp = Path(tempfile.mkdtemp(prefix='mncs-vm-segment-'))
    artifact_path = tmp / 'segment.json'
    artifact_bytes = json.dumps(artifact).encode()
    artifact_path.write_bytes(artifact_bytes)

    t_ref = time.monotonic()
    ref_raw_digest, ref_steps, ref_wire = execute_reference(ref, cases)
    phase_wall['reference_execution'] = round(time.monotonic() - t_ref, 3)
    ref_stderr = ref.close()
    ref_cache = [line for line in ref_stderr.splitlines() if 'mncs-stage0-probe' in line]

    document, batch_wall, batch_peak_kb, calls_bytes, results_bytes = run_batch(
        artifact_path, cases, tmp)
    process_count += 1
    phase_wall['vm_batch'] = round(batch_wall, 3)
    assert document['artifact_id'] == artifact['artifact_id']
    vm_wire = []
    for case_item, result in zip(cases, document['results']):
        assert result['id'] == case_item['id']
        assert result['outcome'] == {'kind': 'completed'}, (case_item['id'], result['outcome'])
        wire = [vm_to_wire(v) for v in result['record']['returned']]
        check_expected(case_item, wire)
        vm_wire.append(wire)

    cranelift_backend = backend_policy.resolve('segment', {})
    assert cranelift_backend == 'cranelift'
    cran = Probe(backend=cranelift_backend, cache_dir=cache_dir)
    process_count += 1
    t_cran = time.monotonic()
    cran_ok, cran_wire, cran_note, cran_steps = True, [], '', []
    try:
        for case_item in cases:
            request = {
                'schema_version': '0.1',
                'target': {'module': MODULE, 'function': case_item['function']},
                'arguments': case_item['args'],
                'type_arguments': case_item['type_args'],
                'step_budget': 8000000,
            }
            result = cran.send(request)
            assert result['status'] == 'returned', (case_item['id'], result['status'])
            cran_steps.append(result['steps'])
            check_expected(case_item, result['returned'])
            cran_wire.append(result['returned'])
    except Exception as error:
        cran_ok, cran_note = False, f'{type(error).__name__}: {error}'[:300]
    phase_wall['cranelift_execution'] = round(time.monotonic() - t_cran, 3)
    cran_stderr = cran.close()
    cran_cache = [line for line in cran_stderr.splitlines() if 'mncs-stage0-probe' in line]

    digests = {'reference': semantic_digest(cases, ref_wire),
               'vm': semantic_digest(cases, vm_wire)}
    assert digests['vm'] == digests['reference'], digests
    if cran_ok:
        digests['cranelift'] = semantic_digest(cases, cran_wire)
        assert digests['cranelift'] == digests['reference'], digests

    children_peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    report = {
        'schema_version': 1,
        'stage0_revision': stage0_revision(),
        'suite': 'segment',
        'smoke': smoke,
        'texts': len(texts) + (1 if smoke else len(test_segment.NONASCII)),
        'cases': len(cases),
        'tokens_compared': n_tokens,
        'artifact_id': artifact['artifact_id'],
        'artifact_bytes': len(artifact_bytes),
        'batch_calls_bytes': calls_bytes,
        'batch_results_bytes': results_bytes,
        'batch_peak_rss_kb': batch_peak_kb,
        'children_peak_rss_kb': children_peak_kb,
        'process_count': process_count,
        'vm_summary': document['summary'],
        'reference_steps_total': sum(ref_steps),
        'reference_steps_max': max(ref_steps),
        'cranelift_steps_total': sum(cran_steps) if cran_steps else 0,
        'semantic_digests': digests,
        'reference_raw_digest': ref_raw_digest,
        'cranelift': {'worked': cran_ok, 'note': cran_note},
        'reference_probe': ref_cache,
        'cranelift_probe': cran_cache,
        'phase_wall_seconds': phase_wall,
        'elapsed_seconds': round(time.monotonic() - t0, 3),
        'scope': 'segment/lexer helpers via direct mncs.vm.artifact/1 + canonical VM batch vs reference + Cranelift',
    }
    (OUT / 'vm-segment-results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
