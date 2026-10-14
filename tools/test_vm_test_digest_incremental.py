#!/usr/bin/env python3
"""Measure incremental application builds on the real mncs.test digest module."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import test_vm_native_application as support

TEST_ROOT = Path('/workspace/mncs-test')
STDLIB_ROOT = Path('/workspace/mncs-stdlib')
LANGUAGE_ROOT = Path('/workspace/mncs-language')
VM_ROOT = Path('/workspace/mncs-vm')
ROOT_SOURCE_PATH = TEST_ROOT / 'native/mncs/test/digest.mncs'
MODULE_PATHS = {
    'mncs.test.assertions': TEST_ROOT / 'native/mncs/test/assertions.mncs',
    'mncs.test.suite': TEST_ROOT / 'native/mncs/test/suite.mncs',
    'mncs.std.application.v1': STDLIB_ROOT / 'library/std/application.mncs',
}
ENTRY_MODULE = 'mncs.test.digest'
ENTRY_FUNCTION = 'build_digest'
ENTRY = f'{ENTRY_MODULE}::{ENTRY_FUNCTION}'
CACHE_DIR = ROOT / '.build/native-app-readiness/gate-b-real-digest-cache'
ARTIFACT_PATH = ROOT / '.build/native-app-readiness/digest-incremental.vm.json'
FIXTURE_PATH = ROOT / 'evidence/native-app-readiness-test-digest.json'
EVIDENCE_PATH = ROOT / 'evidence/native-app-readiness-test-digest-incremental.json'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def application_request(root: str, modules: dict[str, str],
                        policy: str = 'stage0-semantic-check+canonical-vm-artifact/1') -> dict:
    return {
        'schema_version': 'mncs.compiler.application-build-request/1',
        'target': 'mncs-vm-0.1',
        'root_source_id': 'native/mncs/test/digest.mncs',
        'root_source': root,
        'entry': {'module': ENTRY_MODULE, 'function': ENTRY_FUNCTION},
        'modules': modules,
        'generic_seeds': [],
        'capabilities': [],
        'verification_policy': policy,
    }


def compile_one(probe: support.Probe, request: dict) -> tuple[dict, dict]:
    response, metrics = probe.send({'compile_application': request}, timeout=60)
    return response, metrics


def make_call(argument: dict) -> dict:
    return {
        'schema_version': '0.1',
        'target': {'module': ENTRY_MODULE, 'function': ENTRY_FUNCTION},
        'arguments': [argument],
        'type_arguments': [],
        'step_budget': 3_000_000,
    }


def run_oracle(probe: support.Probe, root: str, modules: dict[str, str],
               argument: dict) -> tuple[dict, dict]:
    response, metrics = probe.send({'project_oracle': {
        'root': root, 'modules': modules, 'calls': [make_call(argument)],
    }}, timeout=60)
    if not response.get('valid'):
        raise AssertionError(json.dumps(response.get('diagnostics'), indent=2)[:6000])
    execution = response['executions'][0]
    if execution.get('status') != 'returned':
        raise AssertionError(json.dumps(execution, indent=2)[:6000])
    return execution, metrics


def run_vm(artifact: dict, artifact_id: str, argument: dict) -> tuple[list, dict, dict]:
    ARTIFACT_PATH.write_text(json.dumps(artifact, sort_keys=True, separators=(',', ':')))
    with tempfile.TemporaryDirectory(prefix='digest-incremental-call-') as directory:
        temp = Path(directory)
        args_path, type_args_path = temp / 'args.json', temp / 'type-args.json'
        args_path.write_text(json.dumps([argument], separators=(',', ':')))
        type_args_path.write_text('[]')
        command = [str(support.VM_BIN), 'run', '--artifact', str(ARTIFACT_PATH),
                   '--callable', ENTRY, '--args', str(args_path),
                   '--type-args', str(type_args_path)]
        started = time.monotonic()
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        first = support.process_sample(process.pid)
        samples = [first]

        def sample() -> None:
            while process.poll() is None:
                samples.append(support.process_sample(process.pid))
                time.sleep(0.01)

        watcher = threading.Thread(target=sample, daemon=True)
        watcher.start()
        try:
            stdout, stderr = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            watcher.join(timeout=1)
            raise TimeoutError('canonical VM exceeded 45 seconds')
        wall = time.monotonic() - started
        watcher.join(timeout=1)
        samples.append(support.process_sample(process.pid))
    if process.returncode != 0:
        raise AssertionError(f'VM exit {process.returncode}: {stderr[-3000:]}')
    document = json.loads(stdout)
    receipt = document.get('record', {})
    if receipt.get('artifact_id') != artifact_id:
        raise AssertionError('VM receipt did not bind the selected artifact')
    actual = [support.decode_wire(support.vm_to_wire(value))
              for value in receipt.get('returned', [])]
    metrics = support.summarize_samples(samples, wall, first['cpu_seconds'])
    metrics['process_count'] = max(
        (row.get('child_process_count', 0) + 1 for row in samples), default=1)
    return actual, metrics, receipt


def bind_artifact(argument: dict, artifact_id: str) -> dict:
    result = copy.deepcopy(argument)
    top = result['record']['fields']
    run = next(value for name, value in top if name == 'run')['record']['fields']
    artifact = next(value for name, value in run if name == 'artifact_identity')
    artifact['sequence']['values'] = [
        {'byte': {'value': byte}} for byte in artifact_id.encode('utf-8')
    ]
    return result


def set_failure_cap(argument: dict, value: int) -> dict:
    result = copy.deepcopy(argument)
    field = next(item for name, item in result['record']['fields']
                 if name == 'max_failures')
    field['integer']['value'] = value
    return result


def cache_file(key: str) -> Path:
    return CACHE_DIR / 'application-builds' / (key.removeprefix('sha256:') + '.json')


def interrupt_build(request: dict) -> dict:
    env = os.environ.copy()
    env['MNCS_PROBE_MODULES'] = ''
    env['MNCS_PROBE_EXECUTION_MODULES'] = ''
    env['MNCS_PROBE_GENERIC_SEEDS'] = '[]'
    env.pop('MNCS_PROBE_BACKEND', None)
    env['MNCS_PROBE_CACHE_DIR'] = str(CACHE_DIR)
    env['MNCS_PROBE_TOOLCHAIN_IDENTITY_ROOT'] = str(ROOT)
    process = subprocess.Popen([str(support.PROBE)], cwd=ROOT, env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, bufsize=1)
    assert process.stdin and process.stdout
    try:
        process.stdin.write('{"execution_status":true}\n')
        process.stdin.flush()
        ready, _, _ = select.select([process.stdout], [], [], 10)
        if not ready or not process.stdout.readline():
            raise RuntimeError('probe failed to reach its request loop')
        started = time.monotonic()
        process.stdin.write(json.dumps({'compile_application': request},
                                       separators=(',', ':')) + '\n')
        process.stdin.flush()
        time.sleep(0.025)
        ready, _, _ = select.select([process.stdout], [], [], 0)
        if ready:
            response = process.stdout.readline()
            process.wait(timeout=5)
            return {'interrupted': False, 'response_received': bool(response),
                    'exit_code': process.returncode,
                    'elapsed_seconds': round(time.monotonic() - started, 6)}
        process.kill()
        process.wait(timeout=5)
        return {'interrupted': True, 'signal': 'SIGKILL', 'response_received': False,
                'exit_code': process.returncode,
                'elapsed_seconds': round(time.monotonic() - started, 6)}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def main() -> None:
    if not FIXTURE_PATH.is_file() or not support.PROBE.is_file() or not support.VM_BIN.is_file():
        raise SystemExit('Gate A fixture or selected probe/VM is missing')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for name in ('application-builds', 'application-build-indexes'):
        folder = CACHE_DIR / name
        if folder.exists():
            for path in folder.iterdir():
                if path.is_file():
                    path.unlink()
        else:
            folder.mkdir(parents=True)

    modules = {name: path.read_text() for name, path in MODULE_PATHS.items()}
    root = ROOT_SOURCE_PATH.read_text()
    base_request = application_request(root, modules)
    base_arguments = json.loads(FIXTURE_PATH.read_text())['execution']['inputs']
    base_argument = base_arguments[0]

    cold_probe = support.Probe(CACHE_DIR)
    try:
        cold, cold_metrics = compile_one(cold_probe, base_request)
        if (cold.get('status') != 'verified' or cold.get('entry', {}).get('status') != 'verified'
                or cold.get('unresolved_obligations') != []):
            raise AssertionError('cold real digest application was not fully verified')
        if cold.get('cache', {}).get('status') != 'miss' or cold['cache'].get('publication') != 'stored':
            raise AssertionError('cold build did not publish to the empty cache')
        if cold.get('schema_version') != 'mncs.compiler.application-build/4':
            raise AssertionError('unexpected application build contract')
        cold_artifact = cold['artifact']['value']
        cold_id = cold['artifact']['identity']
        cold_argument = bind_artifact(base_argument, cold_id)
        base_execution, base_oracle_metrics = run_oracle(cold_probe, root, modules, cold_argument)
        base_expected = [support.decode_wire(value) for value in base_execution['returned']]
        base_actual, base_vm_metrics, base_receipt = run_vm(cold_artifact, cold_id, cold_argument)
        if base_expected != base_actual:
            raise AssertionError('baseline Stage-0 and VM outputs differ')
        cold_probe.close()
    except BaseException:
        if cold_probe.proc.poll() is None:
            cold_probe.proc.kill()
            cold_probe.proc.wait(timeout=5)
        raise

    warm_probe = support.Probe(CACHE_DIR)
    try:
        warm, warm_metrics = compile_one(warm_probe, base_request)
        if warm.get('cache', {}).get('status') != 'hit' or warm['artifact']['identity'] != cold_id:
            raise AssertionError('unchanged fresh-process build did not reuse the exact artifact')

        edited_root = root.replace(
            'if max_failures > 32 {\n        return 32;',
            'if max_failures > 32 {\n        return 1;', 1)
        if edited_root == root:
            raise AssertionError('small semantic edit did not match the selected root function')
        semantic_request = application_request(edited_root, modules)
        semantic, semantic_metrics = compile_one(warm_probe, semantic_request)
        if semantic.get('cache', {}).get('status') != 'miss':
            raise AssertionError('changed root function reused the previous artifact')
        if semantic.get('artifact', {}).get('identity') == cold_id:
            raise AssertionError('changed root function retained the old artifact identity')
        semantic_argument = bind_artifact(set_failure_cap(base_argument, 40),
                                          semantic['artifact']['identity'])
        original_cap_argument = bind_artifact(set_failure_cap(base_argument, 40), cold_id)
        original_cap_exec, original_cap_oracle_metrics = run_oracle(
            warm_probe, root, modules, original_cap_argument)
        original_cap_expected = [support.decode_wire(value)
                                 for value in original_cap_exec['returned']]
        original_cap_actual, original_cap_vm_metrics, _ = run_vm(
            cold_artifact, cold_id, original_cap_argument)
        if original_cap_expected != original_cap_actual:
            raise AssertionError('original cap behavior disagrees with Stage-0')
        semantic_exec, semantic_oracle_metrics = run_oracle(
            warm_probe, edited_root, modules, semantic_argument)
        semantic_expected = [support.decode_wire(value) for value in semantic_exec['returned']]
        semantic_actual, semantic_vm_metrics, semantic_receipt = run_vm(
            semantic['artifact']['value'], semantic['artifact']['identity'], semantic_argument)
        if semantic_expected != semantic_actual:
            raise AssertionError('changed root function Stage-0/VM results differ')
        if semantic_expected == original_cap_expected:
            raise AssertionError('semantic edit did not change the bounded failure selection')

        edited_modules = dict(modules)
        old_line = 'total: summary.total +% 1,'
        new_line = 'total: summary.total +% 2,'
        edited_modules['mncs.test.suite'] = edited_modules['mncs.test.suite'].replace(old_line, new_line, 1)
        if edited_modules['mncs.test.suite'] == modules['mncs.test.suite']:
            raise AssertionError('dependency edit did not change the suite fold')
        dependency_request = application_request(root, edited_modules)
        dependency, dependency_metrics = compile_one(warm_probe, dependency_request)
        if dependency.get('cache', {}).get('status') != 'miss':
            raise AssertionError('changed dependency reused the old artifact')
        if dependency.get('dependency_graph', {}).get('identity') == cold.get(
                'dependency_graph', {}).get('identity'):
            raise AssertionError('dependency edit did not change the resolved source graph')
        if dependency['artifact']['identity'] == cold_id:
            raise AssertionError('dependency edit retained the old artifact identity')
        dependency_argument = bind_artifact(base_argument, dependency['artifact']['identity'])
        dependency_exec, dependency_oracle_metrics = run_oracle(
            warm_probe, root, edited_modules, dependency_argument)
        dependency_expected = [support.decode_wire(value)
                              for value in dependency_exec['returned']]
        dependency_actual, dependency_vm_metrics, dependency_receipt = run_vm(
            dependency['artifact']['value'], dependency['artifact']['identity'], dependency_argument)
        if dependency_expected != dependency_actual:
            raise AssertionError('dependency edit Stage-0/VM results differ')
        if dependency_expected[0]['summary']['total'] != 8:
            raise AssertionError('dependency behavior change did not affect the real digest summary')

        invalid_root = root.replace('request.max_failures)', 'request.max_failures_missing)', 1)
        invalid_request = application_request(invalid_root, modules)
        invalid, invalid_metrics = compile_one(warm_probe, invalid_request)
        invalid_again, invalid_again_metrics = compile_one(warm_probe, invalid_request)
        codes = [row.get('code') for row in invalid.get('diagnostics', [])]
        if invalid.get('status') != 'rejected' or 'MNE162' not in codes:
            raise AssertionError('invalid edit did not produce precise MNE162')
        if invalid.get('diagnostics') != invalid_again.get('diagnostics'):
            raise AssertionError('invalid edit diagnostics changed across retries')
        if invalid.get('cache', {}).get('publication') != 'not_published':
            raise AssertionError('invalid source was published as a successful fact')

        interrupted_request = application_request(
            root, modules, policy='interrupted-real-digest-build/1')
        interrupt_seed, interrupt_seed_metrics = compile_one(warm_probe, interrupted_request)
        interrupt_key = interrupt_seed['cache']['key']
        target = cache_file(interrupt_key)
        target.unlink()
        for index in (CACHE_DIR / 'application-build-indexes').glob('*.json'):
            index.unlink()
        warm_probe.close()
    except BaseException:
        if warm_probe.proc.poll() is None:
            warm_probe.proc.kill()
            warm_probe.proc.wait(timeout=5)
        raise

    interrupted = interrupt_build(interrupted_request)
    leftovers = list(target.parent.glob(target.stem + '*'))
    index_temps = list((CACHE_DIR / 'application-build-indexes').glob('*.tmp'))
    entry_after_cancel = target.exists()
    if not interrupted['interrupted'] or entry_after_cancel or leftovers or index_temps:
        raise AssertionError(f'interrupted build published incomplete facts: {interrupted}, {leftovers}, {index_temps}')

    retry_probe = support.Probe(CACHE_DIR)
    try:
        retry, retry_metrics = compile_one(retry_probe, interrupted_request)
        if retry.get('cache', {}).get('status') != 'miss' or retry.get('cache', {}).get('publication') != 'stored':
            raise AssertionError('retry after interruption did not recompute a complete result')
        retry_probe.close()
    except BaseException:
        if retry_probe.proc.poll() is None:
            retry_probe.proc.kill()
            retry_probe.proc.wait(timeout=5)
        raise
    verify_probe = support.Probe(CACHE_DIR)
    try:
        recovered, recovered_metrics = compile_one(verify_probe, interrupted_request)
        if recovered.get('cache', {}).get('status') != 'hit' or recovered['artifact']['identity'] != retry['artifact']['identity']:
            raise AssertionError('fresh-process retry did not reuse the complete artifact')
        verify_probe.close()
    except BaseException:
        if verify_probe.proc.poll() is None:
            verify_probe.proc.kill()
            verify_probe.proc.wait(timeout=5)
        raise

    source_rows = [{'module': name, 'path': str(path), 'sha256': sha(path.read_bytes())}
                   for name, path in sorted(MODULE_PATHS.items())]
    source_rows.append({'module': ENTRY_MODULE, 'path': str(ROOT_SOURCE_PATH),
                        'sha256': sha(ROOT_SOURCE_PATH.read_bytes())})
    source_variants = {
        'root_baseline_sha256': sha(root.encode()),
        'root_semantic_edit_sha256': sha(edited_root.encode()),
        'root_invalid_edit_sha256': sha(invalid_root.encode()),
        'suite_baseline_sha256': sha(modules['mncs.test.suite'].encode()),
        'suite_dependency_edit_sha256': sha(edited_modules['mncs.test.suite'].encode()),
    }
    evidence = {
        'schema_version': 'mncs.compiler.native-application-incremental-proof/1',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'application': {'entry': ENTRY, 'root_source_id': 'native/mncs/test/digest.mncs',
                        'modules': source_rows, 'target': 'mncs-vm-0.1'},
        'producer': {
            'compiler_revision': git(ROOT, 'rev-parse', 'HEAD'),
            'language_revision': git(LANGUAGE_ROOT, 'rev-parse', 'HEAD'),
            'test_revision': git(TEST_ROOT, 'rev-parse', 'HEAD'),
            'stdlib_revision': git(STDLIB_ROOT, 'rev-parse', 'HEAD'),
            'vm_revision': git(VM_ROOT, 'rev-parse', 'HEAD'),
            'probe_sha256': sha(support.PROBE.read_bytes()),
            'vm_sha256': sha(support.VM_BIN.read_bytes()),
        },
        'source_variants': source_variants,
        'measured_benefit': {
            'cold_build_wall_seconds': cold_metrics['wall_seconds'],
            'warm_build_wall_seconds': warm_metrics['wall_seconds'],
            'warm_speedup_factor': round(cold_metrics['wall_seconds'] / warm_metrics['wall_seconds'], 3),
        },
        'experiments': {
            'cold': {'status': cold['status'], 'cache': cold['cache'],
                     'source_snapshot_identity': cold['source_snapshot']['identity'],
                     'dependency_graph_identity': cold['dependency_graph']['identity'],
                     'semantic_identity': cold['dependency_graph']['semantic_identity'],
                     'interface_identity': cold['artifact']['interface_identity'],
                     'artifact_id': cold_id, 'metrics': cold_metrics},
            'warm_unchanged_fresh_process': {'cache': warm['cache'],
                                              'source_snapshot_identity': warm['source_snapshot']['identity'],
                                              'artifact_id': warm['artifact']['identity'],
                                              'metrics': warm_metrics},
            'small_semantic_edit': {
                'edit': 'clamp_cap returns 1 instead of 32 above its bound',
                'cache': semantic['cache'],
                'source_snapshot_identity': semantic['source_snapshot']['identity'],
                'dependency_graph_identity': semantic['dependency_graph']['identity'],
                'semantic_identity': semantic['dependency_graph']['semantic_identity'],
                'interface_identity': semantic['artifact']['interface_identity'],
                'artifact_id': semantic['artifact']['identity'],
                'metrics': semantic_metrics, 'stage0_metrics': semantic_oracle_metrics,
                'vm_metrics': semantic_vm_metrics, 'stage0_result': semantic_expected,
                'vm_result': semantic_actual, 'original_cap_result': original_cap_expected,
                'original_oracle_metrics': original_cap_oracle_metrics,
                'original_vm_metrics': original_cap_vm_metrics,
                'artifact_receipt': semantic_receipt.get('artifact_id'),
            },
            'dependency_change': {
                'module': 'mncs.test.suite', 'edit': 'summary.total increment changed from 1 to 2',
                'cache': dependency['cache'],
                'source_snapshot_identity': dependency['source_snapshot']['identity'],
                'semantic_identity': dependency['dependency_graph']['semantic_identity'],
                'interface_identity': dependency['artifact']['interface_identity'],
                'artifact_id': dependency['artifact']['identity'],
                'graph_identity': dependency['dependency_graph']['identity'],
                'metrics': dependency_metrics, 'stage0_metrics': dependency_oracle_metrics,
                'vm_metrics': dependency_vm_metrics, 'stage0_result': dependency_expected,
                'vm_result': dependency_actual,
                'artifact_receipt': dependency_receipt.get('artifact_id'),
            },
            'invalid_source': {'diagnostic_codes': codes,
                               'source_snapshot_identity': invalid['source_snapshot']['identity'],
                               'diagnostics': invalid['diagnostics'],
                               'retry_diagnostics_equal': invalid['diagnostics'] == invalid_again['diagnostics'],
                               'cache': invalid['cache'], 'metrics': invalid_metrics,
                               'repeat_metrics': invalid_again_metrics},
            'interrupted_compilation': {
                'seed_metrics': interrupt_seed_metrics, 'interruption': interrupted,
                'published_entry_after_cancel': entry_after_cancel,
                'temporary_entries_after_cancel': [str(path) for path in leftovers + index_temps],
                'source_snapshot_identity': interrupt_seed['source_snapshot']['identity'],
                'artifact_id': interrupt_seed['artifact']['identity'],
                'retry_cache': retry['cache'], 'retry_metrics': retry_metrics,
                'fresh_process_recovery_cache': recovered['cache'],
                'fresh_process_recovery_metrics': recovered_metrics,
                'recovered_artifact_id': recovered['artifact']['identity'],
            },
            'baseline_execution': {'stage0_metrics': base_oracle_metrics,
                                   'vm_metrics': base_vm_metrics,
                                   'stage0_result': base_expected,
                                   'vm_result': base_actual,
                                   'artifact_receipt': base_receipt.get('artifact_id')},
        },
        'limitations': [
            'Changed function and dependency builds still recompute source semantic checking, SSA verification, and VM artifact generation.',
            'The cache reuses exact complete artifacts for unchanged source and does not yet reuse independently verified changed-function facts.',
            'cgroup memory counters were unavailable in this environment.',
        ],
    }
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    print(json.dumps({
        'status': 'passed', 'evidence': str(EVIDENCE_PATH),
        'cold': cold_metrics, 'warm': warm_metrics,
        'semantic_edit': semantic_metrics, 'dependency_change': dependency_metrics,
        'invalid_source': invalid['diagnostics'], 'interruption': interrupted,
        'retry': retry_metrics, 'recovered': recovered_metrics,
    }, separators=(',', ':')))


if __name__ == '__main__':
    main()
