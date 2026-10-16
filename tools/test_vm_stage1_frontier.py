#!/usr/bin/env python3
"""Three-executor differential for the bounded decl::parse_unit frontier.

The test emits one fresh canonical VM artifact from the current compiler
sources, then runs selected hand-picked cases and a larger valid/invalid slice
of the declaration parser corpus through that artifact, the pinned reference
interpreter, and Cranelift. Stage-0 supplies the independent parse oracle for
each source.
"""
import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_vm_decl as decl
from test_vm_segment import run_batch, vm_to_wire, test_vm_emit

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / 'src' / 'compiler'


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

# Extend the initial hand-picked parser boundary cases with a curated slice of
# the production declaration suite. These remain parser-only inputs; their
# bodies are never type checked or executed by this frontier.
CASES.extend(
    (f"decl-suite-pos-{index:02d}", decl.test_decl.POS[index], "pos")
    for index in (2, 5, 8, 10, 12, 20, 22, 28, 30, 34, 38, 42, 46, 59, 74)
)
CASES.extend(
    (f"decl-suite-neg-{index:02d}", decl.test_decl.NEG[index], "neg")
    for index in (2, 5, 7, 13, 14, 18, 21, 28, 37, 44, 52, 54)
)


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


def host_memory_snapshot():
    values = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            key, raw = line.split(':', 1)
            if key in {'MemTotal', 'MemAvailable'}:
                values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    if set(values) != {'MemTotal', 'MemAvailable'}:
        return None
    return {
        'total_bytes': values['MemTotal'],
        'available_bytes': values['MemAvailable'],
    }


def main():
    cases = []
    source_identity = []
    host_memory_before = host_memory_snapshot()
    child_usage_before = resource.getrusage(resource.RUSAGE_CHILDREN)
    whole_test_started = time.monotonic()
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
    compiler_revision = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], text=True).strip()
    compiler_branch = subprocess.check_output(
        ['git', 'branch', '--show-current'], text=True).strip()
    stage0_probe_sha256 = hashlib.sha256(probe_path.read_bytes()).hexdigest()
    vm_path = test_vm_emit.VM_BIN
    vm_binary_sha256 = hashlib.sha256(vm_path.read_bytes()).hexdigest()
    vm_repository = vm_path.parents[2]
    vm_revision = subprocess.check_output(
        ['git', '-C', str(vm_repository),
         'rev-parse', 'HEAD'], text=True).strip()
    module_sources = {
        name: hashlib.sha256((SOURCE_ROOT / f'{name}.mncs').read_bytes()).hexdigest()
        for name in ('source', 'lexer', 'parser', 'segment', 'decl')
    }
    child_usage_after = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu_seconds = (
        child_usage_after.ru_utime - child_usage_before.ru_utime
        + child_usage_after.ru_stime - child_usage_before.ru_stime
    )
    total_wall_seconds = time.monotonic() - whole_test_started
    cgroup_memory = {}
    for name in ('memory.current', 'memory.max', 'memory.peak'):
        try:
            cgroup_memory[name] = (Path('/sys/fs/cgroup') / name).read_text().strip()
        except OSError:
            cgroup_memory[name] = None
    report = {
        'schema_version': 1,
        'kind': 'mncs-compiler-decl-parse-canonical-vm-frontier',
        'compiler_revision_tested': compiler_revision,
        'compiler_branch': compiler_branch,
        'stage0_revision': decl.stage0_revision(),
        'selected_modules': ['source', 'lexer', 'parser', 'segment', 'decl'],
        'module_source_sha256': module_sources,
        'test_harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'probe_sha256': stage0_probe_sha256,
        'vm_revision': vm_revision,
        'vm_binary_sha256': vm_binary_sha256,
        'language_source_sha256': hashlib.sha256(
            (Path('src/compiler/decl.mncs').read_bytes())).hexdigest(),
        'artifact_id': artifact['artifact_id'],
        'cases': source_identity,
        'valid_case_count': sum(kind == 'pos' for _, _, kind in CASES),
        'invalid_case_count': sum(kind == 'neg' for _, _, kind in CASES),
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
            'whole_frontier': round(total_wall_seconds, 6),
        },
        'resource_measurements': {
            'child_cpu_seconds': round(child_cpu_seconds, 6),
            'child_peak_rss_kib': child_usage_after.ru_maxrss,
            'host_memory_before': host_memory_before,
            'cgroup_memory_files': cgroup_memory,
            'cgroup_peak_attributable_to_test': False,
            'method': 'resource.getrusage for child processes; /proc/meminfo; cgroup readings are ambient and not attributed to this test',
        },
        'canonical_vm_peak_rss_kb': vm_rss_kb,
        'canonical_vm_batch_bytes': {'calls': calls_bytes, 'results': results_bytes},
        'scope': (f"{len(CASES)} decl.parse_unit cases from hand-picked boundaries and the "
                  'production declaration parser suite agree across Stage-0 structural/diagnostic '
                  'checks, the reference interpreter, Cranelift, and one freshly emitted canonical '
                  'VM artifact. This is a function frontier only; it is not full decl-module or '
                  'succession evidence.'),
        'stage1_claimed': False,
        'stage2_claimed': False,
    }
    report_path = Path('.build/vm-efficiency/stage1-frontier.json')
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2) + '\n')

    observed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    evidence = {
        'schema_version': 'mncs.compiler.self-consumption-frontier/1',
        'kind': 'decl-parse-canonical-vm-expanded-corpus',
        'observed_at': observed_at,
        'status': 'passed',
        'claim_boundary': report['scope'],
        'compiler': {
            'tested_source_revision': compiler_revision,
            'branch': compiler_branch,
            'selected_worktree': str(ROOT),
            'selected_modules': report['selected_modules'],
            'module_source_sha256': module_sources,
            'decl_source_sha256': report['language_source_sha256'],
            'test_harness': 'tools/test_vm_stage1_frontier.py',
            'test_harness_sha256': report['test_harness_sha256'],
        },
        'toolchains': {
            'stage0_probe': str(probe_path),
            'stage0_probe_sha256': stage0_probe_sha256,
            'stage0_revision': report['stage0_revision'],
            'vm_binary': str(vm_path),
            'vm_binary_sha256': vm_binary_sha256,
            'vm_repository': str(vm_repository),
            'vm_revision': vm_revision,
        },
        'workload': {
            'module': decl.MODULE,
            'function': 'parse_unit<1024,1024>',
            'artifact_id': artifact['artifact_id'],
            'selected_case_count': len(CASES),
            'valid_case_count': report['valid_case_count'],
            'invalid_case_count': report['invalid_case_count'],
            'cases': source_identity,
        },
        'verification': {
            'stage0_structural_and_diagnostic_checks': 'passed_for_all_cases',
            'executor_case_counts': report['executor_case_counts'],
            'semantic_digests': semantic_digests,
            'steps': report['steps'],
            'language_verified_facts_reused': 0,
            'stage1_claimed': False,
            'stage2_claimed': False,
        },
        'measurements': {
            'timing_seconds': report['timing_seconds'],
            'resource_measurements': report['resource_measurements'],
            'canonical_vm_peak_rss_kib': vm_rss_kb,
            'canonical_vm_batch_bytes': report['canonical_vm_batch_bytes'],
        },
        'raw_test_report': report,
    }
    evidence_path = ROOT / 'evidence/self-host-decl-parse-expanded-canonical-vm-20261016.json'
    evidence_path.write_text(json.dumps(evidence, indent=2) + '\n')
    report['evidence_artifact'] = evidence_path.relative_to(ROOT).as_posix()
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
