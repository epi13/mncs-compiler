#!/usr/bin/env python3
"""Run the current MNCS header parser through reference, Cranelift, and VM.

The workload exercises kernel.parse_header, whose MNCS implementation calls
parser.header. Stage-0's project oracle validates each result's diagnostics
and source spans before a direct canonical VM artifact is executed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import resource
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_frontend as frontend
import test_vm_native_application as native_app
import test_vm_emit
from test_vm_segment import run_batch, vm_to_wire

ROOT = Path(__file__).resolve().parents[1]
MODULE = 'mncs.compiler.kernel.v1'
FUNCTION = 'parse_header'
STEP_BUDGET = 1_000_000
EVIDENCE = ROOT / 'evidence' / 'self-host-parser-header-canonical-vm-20261016.json'
OUT = ROOT / '.build' / 'vm-parser-header-frontier.json'

# Resolve the sibling VM from the workspace root so registered linked
# worktrees nested below the Compiler checkout use the same canonical VM.
test_vm_emit.VM_REPO = native_app.VM_ROOT
test_vm_emit.VM_BIN = native_app.VM_BIN


def open_probe(backend: str | None) -> frontend.Probe:
    """Create the existing frontend probe with one explicit execution backend."""
    prior = os.environ.get('MNCS_PROBE_BACKEND')
    if backend is None:
        os.environ['MNCS_PROBE_BACKEND'] = 'reference_interpreter'
    else:
        os.environ['MNCS_PROBE_BACKEND'] = backend
    try:
        return frontend.Probe()
    finally:
        if prior is None:
            os.environ.pop('MNCS_PROBE_BACKEND', None)
        else:
            os.environ['MNCS_PROBE_BACKEND'] = prior


def request_for(raw: bytes) -> dict:
    bound = len(raw)
    return {
        'schema_version': '0.1',
        'target': {'module': MODULE, 'function': FUNCTION},
        'arguments': [frontend.blob(raw)],
        'type_arguments': [frontend.nat_arg(bound)],
        'step_budget': STEP_BUDGET,
    }


def decode_result(result: dict, case_id: str) -> tuple[dict, int, dict]:
    if result.get('status') != 'returned':
        raise AssertionError(f'{case_id}: parser returned {result.get("status")}: {result}')
    value = frontend.decode(result['returned'][0])
    return value, result.get('steps', 0), result['returned'][0]


def check_against_stage0(probe: frontend.Probe, case: dict, fact: dict,
                         oracle: dict) -> None:
    expected_code = case['expected_code']
    if fact['code'] != expected_code:
        raise AssertionError(f'{case["id"]}: expected header code {expected_code}, got {fact}')
    if expected_code == 8:
        return
    if expected_code == 0:
        if fact['state'] != 8:
            raise AssertionError(f'{case["id"]}: successful header did not reach terminal state')
        module_node = oracle.get('cst', {}).get('root', {}).get('children', [None, None])[1]
        if module_node is None:
            raise AssertionError(f'{case["id"]}: Stage-0 omitted the module CST node')
        if fact['next_offset'] != module_node['span']['end']:
            raise AssertionError(f'{case["id"]}: Stage-0 module boundary mismatch')
        prefix = case['raw'][:fact['next_offset']].decode('utf-8')
        full_oracle = probe.send({
            'oracle': prefix + ' fn x(v:u64)->(r:u64){return v;}'})
        if full_oracle.get('ast') is None:
            if not any(diagnostic.get('stage') == 'envelope'
                       for diagnostic in full_oracle.get('diagnostics', [])):
                raise AssertionError(f'{case["id"]}: missing Stage-0 envelope diagnostic')
            for name in ('version', 'module'):
                expected_span = case.get(f'{name}_span')
                if expected_span is not None and [
                        fact[f'{name}_start'], fact[f'{name}_end']] != expected_span:
                    raise AssertionError(f'{case["id"]}: Stage-0 {name} span mismatch')
            return
        for name, ast_key in (('version', 'language_version'), ('module', 'module')):
            span = full_oracle['ast'][ast_key]['span']
            if (fact[f'{name}_start'], fact[f'{name}_end']) != (span['start'], span['end']):
                raise AssertionError(f'{case["id"]}: Stage-0 {name} span mismatch')
    else:
        diagnostics = oracle.get('diagnostics', [])
        if not diagnostics:
            raise AssertionError(f'{case["id"]}: Stage-0 accepted invalid header fixture')
        span = diagnostics[0]['span']
        if (fact['start'], fact['end']) != (span['start'], span['end']):
            raise AssertionError(f'{case["id"]}: Stage-0 first diagnostic span mismatch')


def semantic_digest(case_ids: list[str], values: list[dict]) -> str:
    digest = hashlib.sha256()
    for case_id, value in zip(case_ids, values):
        digest.update(json.dumps([case_id, value], sort_keys=True).encode() + b'\n')
    return digest.hexdigest()


def cgroup_memory_snapshot() -> dict:
    values = {}
    for name in ('memory.current', 'memory.max', 'memory.peak'):
        try:
            values[name] = (Path('/sys/fs/cgroup') / name).read_text().strip()
        except OSError:
            values[name] = None
    return values


def main() -> None:
    if not test_vm_emit.PROBE.is_file():
        raise SystemExit(f'Stage-0 probe is missing: {test_vm_emit.PROBE}')
    if not test_vm_emit.VM_BIN.is_file():
        raise SystemExit(f'canonical VM binary is missing: {test_vm_emit.VM_BIN}')

    fixtures = json.loads((ROOT / 'tests/fixtures/frontend.json').read_text())['headers']
    cases = []
    for index, fixture in enumerate(fixtures):
        raw = frontend.fixed_bytes(fixture['text'])
        cases.append({
            'id': f'frontend-header-{index:02d}',
            'raw': raw,
            'expected_code': fixture['code'],
            'version_span': fixture.get('version_span'),
            'module_span': fixture.get('module_span'),
        })
    extended = b'mncs 0.18; module x;'
    extended = extended + b' ' * (65 - len(extended))
    cases.append({
        'id': 'extended-header-65-byte-bound',
        'raw': extended,
        'expected_code': 0,
        'version_span': None,
        'module_span': None,
    })

    before_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    memory_before = cgroup_memory_snapshot()
    started = time.monotonic()
    phase_seconds = {}
    reference_values, reference_steps, oracle_checks = [], [], []

    reference = open_probe(None)
    try:
        status = reference.send({'execution_status': True})
        t0 = time.monotonic()
        for case in cases:
            raw = case['raw']
            text = raw.decode('utf-8')
            oracle = reference.send({'oracle': text})
            request = request_for(raw)
            value, steps, _ = decode_result(reference.send(request), case['id'])
            check_against_stage0(reference, case, value, oracle)
            reference_values.append(value)
            reference_steps.append(steps)
            oracle_checks.append({
                'id': case['id'],
                'expected_code': case['expected_code'],
                'bytes': len(raw),
                'sha256': hashlib.sha256(raw).hexdigest(),
                'stage0_diagnostics': len(oracle.get('diagnostics', [])),
            })
        phase_seconds['stage0_oracles_and_reference_execution'] = round(
            time.monotonic() - t0, 6)

        t0 = time.monotonic()
        emitted = reference.send({'emit_vm_artifact': {'module': MODULE}})
        artifact = emitted.get('artifact')
        if not isinstance(artifact, dict) or artifact.get('schema_version') != 'mncs.vm.artifact/1':
            raise AssertionError(f'direct emitter returned no canonical VM artifact: {emitted}')
        rows = [entry for entry in artifact.get('generic_entrypoints', [])
                if entry.get('generic_function') == FUNCTION]
        if {tuple(entry.get('args_spellings', [])) for entry in rows} != {('64',), ('65',)}:
            raise AssertionError(f'artifact omitted required header specializations: {rows}')
        phase_seconds['direct_vm_emission'] = round(time.monotonic() - t0, 6)
    finally:
        reference.close()

    cranelift = open_probe('cranelift')
    cranelift_values, cranelift_steps = [], []
    try:
        status_cranelift = cranelift.send({'execution_status': True})
        if status_cranelift.get('backend') != 'cranelift':
            raise AssertionError(f'frontend backend policy did not select Cranelift: {status_cranelift}')
        t0 = time.monotonic()
        for case in cases:
            value, steps, _ = decode_result(
                cranelift.send(request_for(case['raw'])), case['id'])
            if value != reference_values[len(cranelift_values)]:
                raise AssertionError(f'{case["id"]}: Cranelift disagrees with reference interpreter')
            cranelift_values.append(value)
            cranelift_steps.append(steps)
        phase_seconds['cranelift_execution'] = round(time.monotonic() - t0, 6)
    finally:
        cranelift.close()

    vm_cases = []
    for case in cases:
        raw = case['raw']
        vm_cases.append({
            'id': case['id'],
            'function': FUNCTION,
            'args': [frontend.blob(raw)],
            'type_args': [frontend.nat_arg(len(raw))],
            'step_budget': STEP_BUDGET,
        })
    vm_values, vm_steps = [], []
    with tempfile.TemporaryDirectory(prefix='mncs-vm-parser-header-') as directory:
        temp = Path(directory)
        artifact_path = temp / 'kernel-parser-header.vm.json'
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + '\n')
        document, vm_wall, vm_rss_kib, calls_bytes, results_bytes = run_batch(
            artifact_path, vm_cases, temp, module=MODULE)
        phase_seconds['canonical_vm_batch'] = round(vm_wall, 6)
        for index, (case, result) in enumerate(zip(cases, document['results'])):
            if result.get('id') != case['id'] or result.get('outcome') != {'kind': 'completed'}:
                raise AssertionError(f'{case["id"]}: canonical VM did not complete: {result}')
            returned = result['record']['returned']
            if len(returned) != 1:
                raise AssertionError(f'{case["id"]}: canonical VM returned wrong value count')
            value = frontend.decode(vm_to_wire(returned[0]))
            if value != reference_values[index]:
                raise AssertionError(f'{case["id"]}: canonical VM disagrees with reference interpreter')
            if value != cranelift_values[index]:
                raise AssertionError(f'{case["id"]}: canonical VM disagrees with Cranelift')
            vm_values.append(value)
            vm_steps.append(result['record']['usage']['steps'])

    case_ids = [case['id'] for case in cases]
    semantic_digests = {
        'reference_interpreter': semantic_digest(case_ids, reference_values),
        'cranelift': semantic_digest(case_ids, cranelift_values),
        'canonical_vm': semantic_digest(case_ids, vm_values),
    }
    if len(set(semantic_digests.values())) != 1:
        raise AssertionError(f'executor semantic digests differ: {semantic_digests}')

    compiler_revision = subprocess.check_output(
        ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    compiler_branch = subprocess.check_output(
        ['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip()
    stage0_revision = json.loads((ROOT / 'mncs-language.lock.json').read_text())['revision']
    stage0_probe_sha256 = hashlib.sha256(test_vm_emit.PROBE.read_bytes()).hexdigest()
    vm_sha256 = hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest()
    vm_repo = test_vm_emit.VM_BIN.parents[2]
    vm_revision = subprocess.check_output(
        ['git', '-C', str(vm_repo), 'rev-parse', 'HEAD'], text=True).strip()
    source_hashes = {
        name: hashlib.sha256((ROOT / 'src/compiler' / f'{name}.mncs').read_bytes()).hexdigest()
        for name in ('source', 'lexer', 'parser', 'kernel', 'segment')
    }
    after_children = resource.getrusage(resource.RUSAGE_CHILDREN)
    child_cpu = (
        after_children.ru_utime - before_children.ru_utime
        + after_children.ru_stime - before_children.ru_stime
    )
    whole_wall = round(time.monotonic() - started, 6)
    harness_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report = {
        'schema_version': 1,
        'kind': 'mncs-compiler-parser-header-canonical-vm-frontier',
        'compiler_revision_tested': compiler_revision,
        'compiler_branch': compiler_branch,
        'stage0_revision': stage0_revision,
        'stage0_probe_sha256': stage0_probe_sha256,
        'selected_modules': ['source', 'lexer', 'parser', 'kernel', 'segment'],
        'module_source_sha256': source_hashes,
        'target_module': MODULE,
        'target_function': FUNCTION,
        'test_harness_sha256': harness_sha256,
        'artifact_identity': artifact['artifact_id'],
        'specializations': [entry.get('args_spellings') for entry in rows],
        'cases': oracle_checks,
        'case_count': len(cases),
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
        'timing_seconds': {**phase_seconds, 'whole_frontier': whole_wall},
        'resource_measurements': {
            'child_cpu_seconds': round(child_cpu, 6),
            'child_peak_rss_kib': after_children.ru_maxrss,
            'canonical_vm_peak_rss_kib': vm_rss_kib,
            'canonical_vm_calls_bytes': calls_bytes,
            'canonical_vm_results_bytes': results_bytes,
            'cgroup_before': memory_before,
            'cgroup_after': cgroup_memory_snapshot(),
            'cgroup_measurements_are_ambient': True,
        },
        'scope': ('The current kernel.parse_header callable executes parser.header for all 18 '
                  'frontend header fixtures and an extended 65-byte input. Stage-0 structural '
                  'and diagnostic checks, reference interpreter, Cranelift, and one fresh '
                  'canonical VM artifact agree. This is a selected helper frontier, not full '
                  'parser-module verification or Stage-1/Stage-2 succession.'),
        'stage1_claimed': False,
        'stage2_claimed': False,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2) + '\n')
    evidence = {
        'schema_version': 'mncs.compiler.self-consumption-frontier/1',
        'kind': 'parser-header-canonical-vm-current-source',
        'observed_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'status': 'passed',
        'claim_boundary': report['scope'],
        'compiler': {
            'tested_source_revision': compiler_revision,
            'branch': compiler_branch,
            'selected_worktree': str(ROOT),
            'selected_modules': report['selected_modules'],
            'module_source_sha256': source_hashes,
            'target_module': MODULE,
            'target_function': FUNCTION,
            'test_harness': 'tools/test_vm_parser_frontier.py',
            'test_harness_sha256': harness_sha256,
        },
        'toolchains': {
            'stage0_probe': str(test_vm_emit.PROBE),
            'stage0_probe_sha256': stage0_probe_sha256,
            'stage0_revision': stage0_revision,
            'vm_binary': str(test_vm_emit.VM_BIN),
            'vm_binary_sha256': vm_sha256,
            'vm_revision': vm_revision,
        },
        'verification': {
            'executor_case_counts': report['executor_case_counts'],
            'semantic_digests': semantic_digests,
            'stage0_checks': 'all fixture diagnostic and span assertions passed',
            'stage1_claimed': False,
            'stage2_claimed': False,
        },
        'measurements': {
            'timing_seconds': report['timing_seconds'],
            'resource_measurements': report['resource_measurements'],
        },
        'raw_test_report': report,
    }
    EVIDENCE.write_text(json.dumps(evidence, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
