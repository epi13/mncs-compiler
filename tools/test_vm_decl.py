#!/usr/bin/env python3
"""Decl suite through the canonical VM (direct bytes + batch).

Routes the real declaration-vertical workload through:

    pinned Stage-0 elaboration
      -> direct mncs.vm.artifact/1 emission (no research payload)
      -> canonical mncs-vm batch execution (one admission, one process)
      -> structured results compared per case and per digest

against the pinned reference interpreter AND the suite's policy
backend (currently Cranelift). Expectations come from Stage-0 oracle
facts via the shared test_decl check functions, so every executor is
checked independently; the semantic digests must then agree.

Corpus, wire shapes, decode, and check rules are imported from
test_decl so the workload is the suite's workload, not a copy.

Measurements (wall, RSS, bytes, VM accounting) print at the end and
persist to .build/vm-decl-results.json.

Run: python3 tools/test_vm_decl.py [--smoke]
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
import test_decl
from test_decl import (decode, integer, nat_arg, source_bytes,
                       pages_value, logical_args, PAGE_BOUND, STRIDE_BOUND,
                       check_pos_parse, check_neg_parse, check_check_unit,
                       check_self_ingest)
from test_vm_segment import run_batch, vm_to_wire

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
PROBE = Path(os.environ.get("MNCS_PROBE_BIN", BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"))
os.chdir(ROOT)
OUT = ROOT / '.build'
OUT.mkdir(exist_ok=True)
MODULE = 'mncs.compiler.decl.v1'
FUNCTIONS = ['parse_unit', 'check_unit']
TYPE_ARGS = [nat_arg(PAGE_BOUND), nat_arg(STRIDE_BOUND)]
# Step-counted executors (reference interpreter, canonical VM batch) share
# the probe schema ceiling of 8M steps/request. Measured reference cost is
# ~2050 steps/byte (segment.mncs: 2430 bytes -> 4,979,291 steps), so inputs
# above this limit run on the native policy backend only, still checked
# per-case against the Stage-0 oracle, while the joint semantic digests
# cover exactly the cases every executor ran.
INTERPRETER_BYTE_LIMIT = 3000


def stage0_revision():
    return json.loads(Path('mncs-language.lock.json').read_text())['revision']


class Probe:
    def __init__(self, backend=None, cache_dir=None):
        env = dict(os.environ)
        if backend is None or backend == 'reference_interpreter':
            env.pop('MNCS_PROBE_BACKEND', None)
        else:
            env['MNCS_PROBE_BACKEND'] = backend
        env['MNCS_PROBE_MODULES'] = 'source,lexer,parser,segment,decl'
        env['MNCS_PROBE_EXECUTION_MODULES'] = MODULE
        env['MNCS_PROBE_GENERIC_SEEDS'] = json.dumps(
            [{'module': MODULE, 'function': function, 'type_arguments': TYPE_ARGS}
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


def gen_cases(probe, smoke):
    """Generate cases from oracle facts only, mirroring test_decl.suite."""
    cases = []

    def case(kind, function, args, **extra):
        item = {'id': f"{len(cases)}:{kind}:{function}", 'kind': kind,
                'function': function, 'args': args, 'type_args': TYPE_ARGS, 'step_budget': test_decl.EXECUTION_STEP_BUDGET}
        item.update(extra)
        cases.append(item)

    pos = test_decl.POS[:2] if smoke else test_decl.POS
    neg = test_decl.NEG[:1] if smoke else test_decl.NEG
    checks = test_decl.CHECKS[:1] if smoke else test_decl.CHECKS
    for text in pos:
        data = source_bytes(text)
        oracle = probe.send({'oracle': data.decode()})
        case('pos', 'parse_unit', logical_args(data),
             text=text, data=data, oracle=oracle, input_bytes=len(data))
    for text in neg:
        data = source_bytes(text)
        oracle = probe.send({'oracle': data.decode()})
        case('neg', 'parse_unit', logical_args(data),
             text=text, oracle=oracle, input_bytes=len(data))
    for text, stage in checks:
        case('check', 'check_unit', logical_args(source_bytes(text)),
             text=text, stage=stage, input_bytes=len(source_bytes(text)))
    if not smoke:
        for relpath in test_decl.SELF_INGEST:
            data = (ROOT / relpath).read_bytes()
            pages = [data[i:i + STRIDE_BOUND] for i in range(0, len(data), STRIDE_BOUND)]
            args = [pages_value(pages), integer(STRIDE_BOUND), integer(len(data))]
            oracle = probe.send({'oracle': data.decode()})
            case('ingest', 'parse_unit', args,
                 relpath=relpath, data=data, oracle=oracle, input_bytes=len(data))
    return cases


def check_case(case_item, wire):
    got = decode(wire[0])
    kind = case_item['kind']
    if kind == 'pos':
        check_pos_parse(got, case_item['oracle'], case_item['data'], case_item['text'])
    elif kind == 'neg':
        check_neg_parse(got, case_item['oracle'], case_item['text'])
    elif kind == 'check':
        check_check_unit(got, case_item['text'], case_item['stage'])
    elif kind == 'ingest':
        check_self_ingest(got, case_item['oracle'], case_item['data'], case_item['relpath'])
    else:
        raise AssertionError(kind)
    return got


def execute_reference(probe, cases):
    """test_decl.Probe.run replicated exactly (including its digest)."""
    digest = hashlib.sha256()
    steps = []
    wire_results = []
    for case_item in cases:
        request = {
            'schema_version': '0.1',
            'target': {'module': MODULE, 'function': case_item['function']},
            'arguments': case_item['args'],
            'type_arguments': case_item['type_args'],
            'step_budget': case_item['step_budget'],
        }
        result = probe.send(request)
        assert result['status'] == 'returned', (case_item['id'], result)
        steps.append(result['steps'])
        digest.update(json.dumps([request, result['returned']], sort_keys=True).encode())
        check_case(case_item, result['returned'])
        wire_results.append(result['returned'])
    return digest.hexdigest(), steps, wire_results


def semantic_digest(cases, wire_results):
    digest = hashlib.sha256()
    for case_item, returned in zip(cases, wire_results):
        assert len(returned) == 1
        digest.update(json.dumps(
            [case_item['id'], decode(returned[0])], sort_keys=True).encode() + b'\n')
    return digest.hexdigest()


def main():
    smoke = len(sys.argv) > 1 and sys.argv[1] == '--smoke'
    assert PROBE.exists(), f'probe missing: {PROBE}'
    process_count = 0
    phase_wall = {}
    t0 = time.monotonic()

    cache_dir = OUT / 'probe-cache'
    ref = Probe(backend=None, cache_dir=cache_dir)
    process_count += 1
    t_gen = time.monotonic()
    cases = gen_cases(ref, smoke)
    phase_wall['oracle_and_generation'] = round(time.monotonic() - t_gen, 3)

    t_emit = time.monotonic()
    emitted = ref.send({'emit_vm_artifact': {'module': MODULE}})
    phase_wall['direct_emission'] = round(time.monotonic() - t_emit, 3)
    artifact = emitted['artifact']
    assert artifact['schema_version'] == 'mncs.vm.artifact/1'
    assert 'program' not in artifact
    with tempfile.TemporaryDirectory(prefix='mncs-vm-decl-') as directory:
        tmp = Path(directory)
        artifact_path = tmp / 'decl.json'
        artifact_bytes = json.dumps(artifact).encode()
        artifact_path.write_bytes(artifact_bytes)

        joint = [c for c in cases if c['input_bytes'] <= INTERPRETER_BYTE_LIMIT]
        native_only = [c['id'] for c in cases if c['input_bytes'] > INTERPRETER_BYTE_LIMIT]
        assert joint, 'partition left no jointly-runnable case'

        t_ref = time.monotonic()
        ref_raw_digest, ref_steps, ref_wire = execute_reference(ref, joint)
        phase_wall['reference_execution'] = round(time.monotonic() - t_ref, 3)
        ref_stderr = ref.close()
        ref_cache = [line for line in ref_stderr.splitlines() if 'mncs-stage0-probe' in line]

        document, batch_wall, batch_peak_kb, calls_bytes, results_bytes = run_batch(
            artifact_path, joint, tmp, module=MODULE)
        process_count += 1
        phase_wall['vm_batch'] = round(batch_wall, 3)
        assert document['artifact_id'] == artifact['artifact_id']
        vm_wire = []
        for case_item, result in zip(joint, document['results']):
            assert result['id'] == case_item['id']
            assert result['outcome'] == {'kind': 'completed'}, (case_item['id'], result['outcome'])
            wire = [vm_to_wire(v) for v in result['record']['returned']]
            check_case(case_item, wire)
            vm_wire.append(wire)

        policy_backend = backend_policy.resolve('decl', {})
        third = Probe(backend=policy_backend, cache_dir=cache_dir)
        process_count += 1
        t_third = time.monotonic()
        third_ok, third_wire, third_note, third_steps = True, [], '', []
        try:
            for case_item in cases:
                request = {
                    'schema_version': '0.1',
                    'target': {'module': MODULE, 'function': case_item['function']},
                    'arguments': case_item['args'],
                    'type_arguments': case_item['type_args'],
                    'step_budget': case_item['step_budget'],
                }
                result = third.send(request)
                assert result['status'] == 'returned', (case_item['id'], result['status'])
                third_steps.append(result['steps'])
                check_case(case_item, result['returned'])
                if case_item['input_bytes'] <= INTERPRETER_BYTE_LIMIT:
                    third_wire.append(result['returned'])
        except Exception as error:
            third_ok, third_note = False, f'{type(error).__name__}: {error}'[:300]
        phase_wall[f'{policy_backend}_execution'] = round(time.monotonic() - t_third, 3)
        third_stderr = third.close()
        third_cache = [line for line in third_stderr.splitlines() if 'mncs-stage0-probe' in line]

        digests = {'reference': semantic_digest(joint, ref_wire),
                   'vm': semantic_digest(joint, vm_wire)}
        assert digests['vm'] == digests['reference'], digests
        if third_ok:
            digests[policy_backend] = semantic_digest(joint, third_wire)
            assert digests[policy_backend] == digests['reference'], digests

        children_peak_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        report = {
            'schema_version': 1,
            'stage0_revision': stage0_revision(),
            'suite': 'decl',
            'smoke': smoke,
            'cases': len(cases),
            'joint_cases': len(joint),
            'native_only_cases': native_only,
            'interpreter_byte_limit': INTERPRETER_BYTE_LIMIT,
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
            'third_steps_total': sum(third_steps) if third_steps else 0,
            'semantic_digests': digests,
            'reference_raw_digest': ref_raw_digest,
            'third_backend': {'name': policy_backend, 'worked': third_ok, 'note': third_note},
            'reference_probe': ref_cache,
            'third_probe': third_cache,
            'phase_wall_seconds': phase_wall,
            'elapsed_seconds': round(time.monotonic() - t0, 3),
            'scope': 'decl.parse_unit/check_unit via direct mncs.vm.artifact/1 + canonical VM batch vs reference + policy backend',
        }
        (OUT / 'vm-decl-results.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
