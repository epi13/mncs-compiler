#!/usr/bin/env python3
"""Cold/warm and invalidation experiments for the native mncs-test application."""
from __future__ import annotations

import copy
import json
import os
import select
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_vm_native_application as app

ROOT = app.ROOT
CACHE_DIR = ROOT / '.build/native-app-readiness/gate-b-cache'
EVIDENCE = ROOT / 'evidence/native-app-readiness-gate-b.json'


def clear_cache() -> None:
    for directory in ('application-builds', 'application-build-indexes'):
        entry_dir = CACHE_DIR / directory
        entry_dir.mkdir(parents=True, exist_ok=True)
        for path in entry_dir.iterdir():
            if path.is_file() and (path.suffix == '.json' or path.name.endswith('.tmp')):
                path.unlink()


def request_for(root: str, modules: dict[str, str]) -> dict:
    return {
        'schema_version': 'mncs.compiler.application-build-request/1',
        'target': 'mncs-vm-0.1',
        'root_source_id': app.APP_SOURCE_ID,
        'root_source': root,
        'entry': {'module': app.APP_MODULE, 'function': 'run'},
        'modules': modules,
        'generic_seeds': [
            {'module': 'mncs.test.assertions', 'function': 'equals_bytes',
             'type_arguments': [{'kind': 'nat', 'value': 4}]},
            {'module': 'mncs.test.suite', 'function': 'summarize',
             'type_arguments': [{'kind': 'nat', 'value': 4}]},
        ],
        'capabilities': [],
        'effect_assumptions': [],
        'optimization_policy': 'default',
        'verification_policy': 'stage0-semantic-check+canonical-vm-artifact/1',
    }


def call_request() -> dict:
    return app.call_request()


def compile_one(probe: app.Probe, request: dict) -> tuple[dict, dict]:
    return probe.send({'compile_application': request})


def ready_probe(cache_dir: Path) -> tuple[app.Probe, dict]:
    probe = app.Probe(cache_dir)
    response, metrics = probe.send({'execution_status': True})
    if not isinstance(response, dict) or 'modules' not in response:
        raise AssertionError('Stage-0 probe did not return its readiness handshake')
    return probe, metrics


def oracle_one(probe: app.Probe, root: str, modules: dict[str, str]) -> tuple[dict, dict, dict]:
    response, metrics = probe.send({'project_oracle': {
        'root': root, 'modules': modules, 'calls': [call_request()]}})
    if not response.get('valid'):
        raise AssertionError(json.dumps(response.get('diagnostics'), indent=2))
    execution = response['executions'][0]
    if execution.get('status') != 'returned':
        raise AssertionError(json.dumps(execution, indent=2))
    return response, execution, metrics


def run_and_compare(artifact: dict, execution: dict) -> tuple[dict, dict, list]:
    document, metrics = app.run_vm(artifact, call_request()['arguments'])
    if document.get('outcome') != {'kind': 'completed'}:
        raise AssertionError(json.dumps(document.get('outcome'), indent=2))
    if document.get('record', {}).get('artifact_id') != artifact.get('artifact_id'):
        raise AssertionError('VM receipt does not bind the exact application artifact')
    actual = [app.decode_wire(app.vm_to_wire(row))
              for row in document['record']['returned']]
    expected = [app.decode_wire(row) for row in execution['returned']]
    if actual != expected:
        raise AssertionError(json.dumps({'actual': actual, 'stage0': expected}, indent=2))
    return document, metrics, actual


def compact_result(report: dict) -> dict:
    report = report['$p']['report']
    summary = report['summary']
    return {
        'state': report['state'],
        'state_value': report['state_value'],
        'suite': {key: summary[key] for key in
                  ['verdict_code', 'total', 'passed', 'failed', 'skipped',
                   'unsupported', 'assertion_count', 'failure_count']},
        'property_case': {key: report['property_case'][key]
                          for key in ['seed', 'case_index', 'value']},
        'witness': report['witness'],
    }


def compact_oracle(execution: dict) -> dict:
    rows = [app.decode_wire(row) for row in execution['returned']]
    return compact_result(rows[0])


def cache_path(key: str) -> Path:
    return CACHE_DIR / 'application-builds' / (key.removeprefix('sha256:') + '.json')


def interrupt_during_miss(request: dict) -> dict:
    env = os.environ.copy()
    env['MNCS_PROBE_MODULES'] = ''
    env['MNCS_PROBE_EXECUTION_MODULES'] = ''
    env['MNCS_PROBE_GENERIC_SEEDS'] = '[]'
    env.pop('MNCS_PROBE_BACKEND', None)
    env['MNCS_PROBE_CACHE_DIR'] = str(CACHE_DIR)
    env['MNCS_PROBE_TOOLCHAIN_IDENTITY_ROOT'] = str(ROOT)
    proc = subprocess.Popen([str(app.PROBE)], cwd=ROOT, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    assert proc.stdin and proc.stdout
    try:
        proc.stdin.write('{"execution_status":true}\n')
        proc.stdin.flush()
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        if not ready or not proc.stdout.readline():
            raise RuntimeError('fresh Stage-0 probe did not reach its request loop')
        proc.stdin.write(json.dumps({'compile_application': request},
                                    separators=(',', ':')) + '\n')
        proc.stdin.flush()
        time.sleep(0.025)
        ready, _, _ = select.select([proc.stdout], [], [], 0)
        if ready:
            response = proc.stdout.readline()
            proc.wait(timeout=5)
            return {'interrupted': False, 'response_received': bool(response),
                    'exit_code': proc.returncode}
        proc.kill()
        proc.wait(timeout=5)
        return {'interrupted': True, 'signal': 'SIGKILL',
                'response_received': False, 'exit_code': proc.returncode,
                'delay_after_request_seconds': 0.025}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def main() -> None:
    if not app.PROBE.is_file() or not app.VM_BIN.is_file():
        raise SystemExit('campaign Stage-0 probe or canonical VM binary is missing')
    clear_cache()
    modules = {name: path.read_text() for name, path in app.MODULE_FILES.items()}
    original_request = request_for(app.APP_SOURCE, modules)
    tails = []

    cold_probe, cold_startup_metrics = ready_probe(CACHE_DIR)
    try:
        oracle, oracle_exec, oracle_metrics = oracle_one(
            cold_probe, app.APP_SOURCE, modules)
        cold, cold_metrics = compile_one(cold_probe, original_request)
        tails.append(cold_probe.close())
    except BaseException:
        if cold_probe.proc.poll() is None:
            cold_probe.proc.kill()
            cold_probe.proc.wait(timeout=5)
        raise
    if cold.get('cache', {}).get('status') != 'miss' or cold.get('cache', {}).get('publication') != 'stored':
        raise AssertionError('cold run was not compiled and atomically published')
    if cold.get('schema_version') != 'mncs.compiler.application-build/4':
        raise AssertionError('incremental experiment did not use the current /3 producer')
    if cold.get('status') != 'verified' or cold.get('entry', {}).get('status') != 'verified':
        raise AssertionError('cold selected application scope was not verified')
    if cold.get('unresolved_obligations') != []:
        raise AssertionError('selected application scope retained unresolved obligations')
    if cold.get('callable_scope', {}).get('kind') != 'entry_and_explicit_generic_seeds':
        raise AssertionError('incremental evidence omitted selected callable scope')
    artifact = cold['artifact']['value']

    # A new process proves the host cache survives session teardown.
    probe, warm_startup_metrics = ready_probe(CACHE_DIR)
    try:
        warm, warm_metrics = compile_one(probe, original_request)
        if warm.get('cache', {}).get('status') != 'hit':
            raise AssertionError('unchanged build did not hit the persistent artifact cache')
        if warm['artifact']['identity'] != cold['artifact']['identity']:
            raise AssertionError('warm reuse changed the artifact identity')

        # An input module outside the verified dependency graph must not force
        # a semantic rebuild. Its source still changes the full snapshot
        # identity returned to consumers.
        unused_modules = dict(modules)
        unused_modules['mncs.test.unused'] = '''mncs 0.18;
module mncs.test.unused;
fn value() -> (result: u64) { return 1; }'''
        unused_request = request_for(app.APP_SOURCE, unused_modules)
        unused_oracle, unused_exec, unused_oracle_metrics = oracle_one(
            probe, app.APP_SOURCE, unused_modules)
        unused, unused_metrics = compile_one(probe, unused_request)
        if (unused.get('cache', {}).get('status') != 'hit'
                or unused.get('cache', {}).get('reuse_basis') != 'verified_dependency_closure'):
            raise AssertionError('an unreferenced module addition did not reuse the verified dependency closure')
        if unused.get('source_snapshot', {}).get('identity') == cold.get('source_snapshot', {}).get('identity'):
            raise AssertionError('dependency-closure reuse returned the stale full snapshot identity')
        if unused['artifact']['identity'] != cold['artifact']['identity']:
            raise AssertionError('unreferenced module addition changed the verified artifact')

        changed_unused_modules = dict(unused_modules)
        changed_unused_modules['mncs.test.unused'] = changed_unused_modules[
            'mncs.test.unused'].replace('return 1', 'return 2', 1)
        changed_unused_request = request_for(app.APP_SOURCE, changed_unused_modules)
        changed_unused, changed_unused_metrics = compile_one(probe, changed_unused_request)
        if (changed_unused.get('cache', {}).get('status') != 'hit'
                or changed_unused.get('cache', {}).get('reuse_basis') != 'verified_dependency_closure'):
            raise AssertionError('an unreferenced module edit invalidated the verified dependency closure')
        if changed_unused.get('source_snapshot', {}).get('identity') == unused.get('source_snapshot', {}).get('identity'):
            raise AssertionError('unreferenced module edit did not produce a new source snapshot identity')
        if changed_unused['artifact']['identity'] != cold['artifact']['identity']:
            raise AssertionError('unreferenced module edit changed the verified artifact')
        _unused_vm, unused_vm_metrics, unused_actual = run_and_compare(
            unused['artifact']['value'], unused_exec)

        # A resolved dependency with a source-only edit still receives a new
        # source snapshot and a fresh semantic check. Its unchanged canonical
        # semantic identity permits reuse of the already verified VM artifact.
        semantic_edit_modules = dict(modules)
        semantic_edit_modules['mncs.test.generative'] += (
            '\n// source-only edit: preserve the verified module semantics\n')
        semantic_edit_request = request_for(app.APP_SOURCE, semantic_edit_modules)
        semantic_edit_oracle, semantic_edit_exec, semantic_edit_oracle_metrics = oracle_one(
            probe, app.APP_SOURCE, semantic_edit_modules)
        semantic_edit, semantic_edit_metrics = compile_one(probe, semantic_edit_request)
        if (semantic_edit.get('cache', {}).get('status') != 'hit'
                or semantic_edit.get('cache', {}).get('reuse_basis') != 'verified_semantic_identity'):
            raise AssertionError('source-only dependency edit did not reuse the matching semantic artifact')
        if semantic_edit.get('cache', {}).get('recomputed_units') != [
                'source_semantic_check', 'semantic_identity_verification']:
            raise AssertionError('semantic reuse omitted the work performed for the changed source')
        if semantic_edit.get('source_snapshot', {}).get('identity') == cold.get(
                'source_snapshot', {}).get('identity'):
            raise AssertionError('semantic reuse returned the stale exact source snapshot')
        if semantic_edit.get('dependency_graph', {}).get('identity') == cold.get(
                'dependency_graph', {}).get('identity'):
            raise AssertionError('semantic reuse returned the stale resolved source graph')
        if semantic_edit.get('dependency_graph', {}).get('semantic_identity') != cold.get(
                'dependency_graph', {}).get('semantic_identity'):
            raise AssertionError('source-only dependency edit changed the canonical semantic identity')
        if semantic_edit['artifact']['identity'] != cold['artifact']['identity']:
            raise AssertionError('source-only dependency edit changed the reused VM artifact')
        _semantic_edit_vm, semantic_edit_vm_metrics, semantic_edit_actual = run_and_compare(
            semantic_edit['artifact']['value'], semantic_edit_exec)

        small_root = app.APP_SOURCE.replace(
            'equals_i64(42, 19 +% 23', 'equals_i64(43, 19 +% 23', 1)
        small_request = request_for(small_root, modules)
        small_oracle, small_exec, small_oracle_metrics = oracle_one(
            probe, small_root, modules)
        small, small_metrics = compile_one(probe, small_request)
        if small.get('cache', {}).get('status') != 'miss':
            raise AssertionError('a changed function reused the prior artifact')
        small_artifact = small['artifact']['value']

        dependency_modules = dict(modules)
        dependency_modules['mncs.test.generative'] = (
            dependency_modules['mncs.test.generative'].replace(
                '6364136223846793005', '6364136223846793006', 1))
        dependency_request = request_for(app.APP_SOURCE, dependency_modules)
        dependency_oracle, dependency_exec, dependency_oracle_metrics = oracle_one(
            probe, app.APP_SOURCE, dependency_modules)
        dependency, dependency_metrics = compile_one(probe, dependency_request)
        if dependency.get('cache', {}).get('status') != 'miss':
            raise AssertionError('changed imported source reused an old artifact')
        if dependency['dependency_graph']['identity'] == cold['dependency_graph']['identity']:
            raise AssertionError('changed dependency did not alter the resolved graph identity')
        if dependency['artifact']['identity'] == cold['artifact']['identity']:
            raise AssertionError('changed dependency returned the prior verified artifact')
        dependency_artifact = dependency['artifact']['value']

        invalid_request = request_for(
            app.APP_SOURCE.replace('summary.total', 'summary.total_missing', 1), modules)
        invalid, invalid_metrics = compile_one(probe, invalid_request)
        invalid_repeat, invalid_repeat_metrics = compile_one(probe, invalid_request)
        invalid_codes = [row.get('code') for row in invalid.get('diagnostics', [])]
        if invalid.get('status') != 'rejected' or 'MNE162' not in invalid_codes:
            raise AssertionError('changed invalid source lacked a precise MNE162 diagnostic')
        if invalid.get('diagnostics') != invalid_repeat.get('diagnostics'):
            raise AssertionError('repeated invalid build diagnostics were not stable')
        if invalid.get('cache', {}).get('publication') != 'not_published':
            raise AssertionError('invalid source entered the success cache')

        unsupported_request = copy.deepcopy(original_request)
        unsupported_request['target'] = 'mncs-wasm-1.0'
        unavailable, unavailable_metrics = compile_one(probe, unsupported_request)
        if unavailable.get('status') != 'unavailable':
            raise AssertionError('unsupported target was not reported unavailable')

        interrupt_request = copy.deepcopy(original_request)
        interrupt_request['verification_policy'] = 'interrupted-publication-probe/1'
        seed, seed_metrics = compile_one(probe, interrupt_request)
        if seed.get('cache', {}).get('publication') != 'stored':
            raise AssertionError('could not seed a unique cache key for interruption')
        interrupt_key = seed['cache']['key']
        target_entry = cache_path(interrupt_key)
        target_entry.unlink()
        index_dir = CACHE_DIR / 'application-build-indexes'
        for index in index_dir.glob('*.json'):
            index.unlink()
        tails.append(probe.close())
    except BaseException:
        if probe.proc.poll() is None:
            probe.proc.kill()
            probe.proc.wait(timeout=5)
        raise

    interrupted = interrupt_during_miss(interrupt_request)
    if not interrupted['interrupted']:
        raise AssertionError('interruption probe completed before cancellation')
    leftovers = list(target_entry.parent.glob(target_entry.stem + '*'))
    index_leftovers = list((CACHE_DIR / 'application-build-indexes').glob('*.tmp'))
    entry_after_cancel_exists = target_entry.exists()
    if entry_after_cancel_exists or leftovers or index_leftovers:
        raise AssertionError(f'interrupted compilation left cache publication: {leftovers + index_leftovers}')

    retry_probe, retry_startup_metrics = ready_probe(CACHE_DIR)
    try:
        retry, retry_metrics = compile_one(retry_probe, interrupt_request)
        if retry.get('cache', {}).get('status') != 'miss' or retry.get('cache', {}).get('publication') != 'stored':
            raise AssertionError('post-interruption build did not recompute and publish')
        tails.append(retry_probe.close())
    except BaseException:
        if retry_probe.proc.poll() is None:
            retry_probe.proc.kill()
            retry_probe.proc.wait(timeout=5)
        raise
    verify_probe, verify_startup_metrics = ready_probe(CACHE_DIR)
    try:
        retry_warm, retry_warm_metrics = compile_one(verify_probe, interrupt_request)
        if retry_warm.get('cache', {}).get('status') != 'hit':
            raise AssertionError('post-interruption artifact did not survive a fresh process')
        tails.append(verify_probe.close())
    except BaseException:
        if verify_probe.proc.poll() is None:
            verify_probe.proc.kill()
            verify_probe.proc.wait(timeout=5)
        raise

    vm_base, vm_base_metrics, base_actual = run_and_compare(artifact, oracle_exec)
    vm_small, vm_small_metrics, small_actual = run_and_compare(small_artifact, small_exec)
    vm_dependency, vm_dependency_metrics, dependency_actual = run_and_compare(
        dependency_artifact, dependency_exec)
    base_report, small_report, dependency_report = (
        base_actual[0]['$p']['report'], small_actual[0]['$p']['report'],
        dependency_actual[0]['$p']['report'])
    assert base_report['summary']['passed'] == 3 and base_report['summary']['skipped'] == 1
    assert small_report['summary']['failed'] == 1 and small_report['state_value'] == 1
    assert dependency_report['summary']['failed'] == 1
    assert dependency_report['summary']['assertion_failures'] == 1
    assert dependency_report['property_case']['value'] != base_report['property_case']['value']

    cache_speedup = cold_metrics['wall_seconds'] / max(warm_metrics['wall_seconds'], 1e-9)
    source_files = [
        {'module': name, 'source_id': name, 'sha256': app.sha(source.encode()),
         'bytes': len(source.encode())}
        for name, source in sorted(modules.items())
    ]
    source_files.append({
        'module': 'root', 'source_id': app.APP_SOURCE_ID,
        'sha256': app.sha(app.APP_SOURCE.encode()), 'bytes': len(app.APP_SOURCE.encode()),
    })
    evidence = {
        'schema_version': 1,
        'kind': 'mncs-native-incremental-build-evidence',
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'compiler': {
            'build_profile': 'cargo build --release',
            'revision': app.git('rev-parse', 'HEAD', cwd=ROOT),
            'branch': app.git('branch', '--show-current', cwd=ROOT),
            'stage0_revision': json.loads((ROOT / 'mncs-language.lock.json').read_text())['revision'],
            'stage0_probe_sha256': app.sha(app.PROBE.read_bytes()),
            'application_module_sha256': app.sha((ROOT / 'tools/stage0-probe/src/application.rs').read_bytes()),
            'probe_dispatch_sha256': app.sha((ROOT / 'tools/stage0-probe/src/main.rs').read_bytes()),
            'gate_a_harness_sha256': app.sha((ROOT / 'tools/test_vm_native_application.py').read_bytes()),
            'gate_b_harness_sha256': app.sha(Path(__file__).read_bytes()),
            'vm_revision': app.git('rev-parse', 'HEAD', cwd=app.VM_ROOT),
            'vm_sha256': app.sha(app.VM_BIN.read_bytes()),
            'test_revision': app.git('rev-parse', 'HEAD', cwd=app.TEST_ROOT),
        },
        'application': {
            'module': app.APP_MODULE, 'entry': 'run',
            'contract_schema': cold.get('schema_version'),
            'callable_scope': cold.get('callable_scope'),
            'unselected_unresolved_exports': cold.get('unselected_unresolved_exports'),
            'callable_interface_identity': cold.get('entry', {}).get('interface', {}).get('identity'),
            'source_snapshot_identity': cold.get('source_snapshot', {}).get('identity'),
            'dependency_graph_identity': cold.get('dependency_graph', {}).get('identity'),
            'source_files': source_files,
            'artifact_identity': cold.get('artifact', {}).get('identity'),
            'build_assumptions': cold.get('build_assumptions'),
        },
        'cache_key_inputs': [
            'exact source snapshot identity, including root and every supplied module ID and byte hash',
            'dependency-closure candidate index binds root source and build assumptions; every resolved module source hash must match before artifact reuse',
            'canonical generic specialization seed identities',
            'Stage-0 toolchain identity and probe executable SHA-256',
            'compiler contract version, canonical VM backend, selected target, and entry',
            'requested capabilities and effect assumptions',
            'optimization and verification policies',
            'the full source snapshot identity is recomputed when an artifact is reused from an unchanged dependency closure',
            'after a fresh source semantic check, the aggregate canonical root and resolved-module semantic identity may reuse the verified VM artifact when exact build assumptions and entry still match',
        ],
        'experiments': {
            'cold_build': {'cache': cold.get('cache'), 'metrics': cold_metrics,
                           'process_startup_metrics': cold_startup_metrics},
            'warm_unchanged_fresh_process': {
                'cache': warm.get('cache'), 'artifact_identity': warm.get('artifact', {}).get('identity'),
                'metrics': warm_metrics, 'process_startup_metrics': warm_startup_metrics,
                'wall_speedup': round(cache_speedup, 2),
                'cpu_speedup': round(cold_metrics['cpu_seconds'] /
                                     max(warm_metrics['cpu_seconds'], 1e-9), 2),
            },
            'unreferenced_module_addition_and_edit': {
                'module_added_and_edited': 'mncs.test.unused',
                'module_source_sha256_after_addition': app.sha(unused_modules['mncs.test.unused'].encode()),
                'module_source_sha256_after_edit': app.sha(changed_unused_modules['mncs.test.unused'].encode()),
                'module_source_bytes_after_addition': len(unused_modules['mncs.test.unused'].encode()),
                'module_source_bytes_after_edit': len(changed_unused_modules['mncs.test.unused'].encode()),
                'cache_after_addition': unused.get('cache'),
                'cache_after_edit': changed_unused.get('cache'),
                'snapshot_identity_before': cold.get('source_snapshot', {}).get('identity'),
                'snapshot_identity_after_addition': unused.get('source_snapshot', {}).get('identity'),
                'snapshot_identity_after_edit': changed_unused.get('source_snapshot', {}).get('identity'),
                'dependency_graph_identity': unused.get('dependency_graph', {}).get('identity'),
                'artifact_identity_before': cold.get('artifact', {}).get('identity'),
                'artifact_identity_after_addition': unused.get('artifact', {}).get('identity'),
                'artifact_identity_after_edit': changed_unused.get('artifact', {}).get('identity'),
                'oracle_valid': unused_oracle.get('valid'),
                'oracle_result': compact_oracle(unused_exec),
                'vm_result': compact_result(unused_actual[0]),
                'compile_metrics_after_addition': unused_metrics,
                'compile_metrics_after_edit': changed_unused_metrics,
                'oracle_metrics': unused_oracle_metrics,
                'vm_metrics': unused_vm_metrics,
            },
            'source_only_dependency_edit': {
                'module': 'mncs.test.generative',
                'source_sha256_before': app.sha(
                    modules['mncs.test.generative'].encode()),
                'source_sha256_after': app.sha(
                    semantic_edit_modules['mncs.test.generative'].encode()),
                'source_snapshot_identity_before': cold.get('source_snapshot', {}).get('identity'),
                'source_snapshot_identity_after': semantic_edit.get('source_snapshot', {}).get('identity'),
                'dependency_graph_identity_before': cold.get('dependency_graph', {}).get('identity'),
                'dependency_graph_identity_after': semantic_edit.get('dependency_graph', {}).get('identity'),
                'semantic_identity_before': cold.get('dependency_graph', {}).get('semantic_identity'),
                'semantic_identity_after': semantic_edit.get('dependency_graph', {}).get('semantic_identity'),
                'cache': semantic_edit.get('cache'),
                'artifact_identity_before': cold['artifact']['identity'],
                'artifact_identity_after': semantic_edit['artifact']['identity'],
                'oracle_valid': semantic_edit_oracle.get('valid'),
                'oracle_result': compact_oracle(semantic_edit_exec),
                'vm_result': compact_result(semantic_edit_actual[0]),
                'oracle_metrics': semantic_edit_oracle_metrics,
                'compile_metrics': semantic_edit_metrics,
                'vm_metrics': semantic_edit_vm_metrics,
            },
            'small_semantic_edit': {
                'source_snapshot_identity': small.get('source_snapshot', {}).get('identity'),
                'cache': small.get('cache'), 'artifact_identity': small.get('artifact', {}).get('identity'),
                'oracle_valid': small_oracle.get('valid'),
                'oracle_result': compact_oracle(small_exec), 'vm_result': compact_result(small_actual[0]),
                'oracle_metrics': small_oracle_metrics, 'compile_metrics': small_metrics,
                'vm_metrics': vm_small_metrics,
                'reused_units': small.get('cache', {}).get('reused_units'),
                'recomputed_units': small.get('cache', {}).get('recomputed_units'),
                'directly_changed_source': app.APP_SOURCE_ID,
            },
            'dependency_change': {
                'changed_module': 'mncs.test.generative',
                'source_snapshot_identity': dependency.get('source_snapshot', {}).get('identity'),
                'cache': dependency.get('cache'),
                'artifact_identity': dependency.get('artifact', {}).get('identity'),
                'graph_identity_before': cold.get('dependency_graph', {}).get('identity'),
                'graph_identity_after': dependency.get('dependency_graph', {}).get('identity'),
                'oracle_valid': dependency_oracle.get('valid'),
                'oracle_result': compact_oracle(dependency_exec), 'vm_result': compact_result(dependency_actual[0]),
                'oracle_metrics': dependency_oracle_metrics,
                'compile_metrics': dependency_metrics, 'vm_metrics': vm_dependency_metrics,
                'reused_units': dependency.get('cache', {}).get('reused_units'),
                'recomputed_units': dependency.get('cache', {}).get('recomputed_units'),
            },
            'invalid_source': {
                'response_status': invalid.get('status'), 'diagnostics': invalid.get('diagnostics'),
                'repeated_diagnostics_equal': invalid.get('diagnostics') == invalid_repeat.get('diagnostics'),
                'cache': invalid.get('cache'), 'repeat_cache': invalid_repeat.get('cache'),
                'metrics': [invalid_metrics, invalid_repeat_metrics],
            },
            'unsupported_target': {
                'response_status': unavailable.get('status'), 'error': unavailable.get('error'),
                'metrics': unavailable_metrics,
            },
            'interrupted_compilation': {
                'unique_policy_key': interrupt_key, 'seed_cache': seed.get('cache'),
                'cancelled_process': interrupted, 'entry_after_cancel_exists': entry_after_cancel_exists,
                'matching_temp_or_entry_paths_after_cancel': [str(p) for p in leftovers],
                'retry_cache': retry.get('cache'), 'retry_metrics': retry_metrics,
                'retry_process_startup_metrics': retry_startup_metrics,
                'fresh_process_cache': retry_warm.get('cache'),
                'fresh_process_metrics': retry_warm_metrics,
                'fresh_process_startup_metrics': verify_startup_metrics,
            },
        },
        'resource_notes': {
            'cgroup_memory_bytes': 'unknown when the sandbox exposes no usable cgroup memory counter',
            'process_metrics': 'sampled from /proc for probe and VM processes; wall and CPU use process deltas',
        },
        'fine_grained_invalidation': {
            'status': 'semantic_identity_and_dependency_closure_artifact_reuse_implemented',
            'detail': (f'Exact source snapshot hits reuse the published artifact. When the root and build assumptions match but a resolved module source hash changes, a fresh valid Stage-0 semantic check computes the canonical root and resolved-module semantic identity; the verified VM artifact is reused only if that identity and the typed callable interface still match. A comment-only imported-module edit took {semantic_edit_metrics["wall_seconds"]:.3f} s and reused the artifact, while a semantic dependency change took {dependency_metrics["wall_seconds"]:.3f} s and rebuilt; the reused artifact result matched the independent Stage-0 oracle. A changed root function or dependency semantic value still recomputes source checking, SSA verification, and artifact generation. Function-level and independently verified module-fact reuse remain unimplemented, so Gate B is partial.'),
        },
        'gate_a': 'pass',
        'gate_b': 'partial',
        'self_consumption_matrix_change': 'none; Stage-1/Stage-2 and the self-consumption matrix remain unchanged',
        'producer_diagnostics_tail': '\n'.join(tails)[-1800:],
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    import shutil
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    print(json.dumps({
        'gate_a': 'pass', 'gate_b': 'partial',
        'cold_cache': cold.get('cache', {}).get('status'),
        'warm_cache': warm.get('cache', {}).get('status'),
        'unreferenced_module_reuse_basis': unused.get('cache', {}).get('reuse_basis'),
        'unreferenced_module_edit_reuse_basis': changed_unused.get('cache', {}).get('reuse_basis'),
        'small_edit_cache': small.get('cache', {}).get('status'),
        'dependency_cache': dependency.get('cache', {}).get('status'),
        'interrupted': interrupted['interrupted'],
        'retry_cache': retry.get('cache', {}).get('status'),
        'cold_wall_seconds': cold_metrics['wall_seconds'],
        'warm_wall_seconds': warm_metrics['wall_seconds'],
        'wall_speedup': round(cache_speedup, 2),
        'cold_cpu_seconds': cold_metrics['cpu_seconds'],
        'warm_cpu_seconds': warm_metrics['cpu_seconds'],
        'evidence': str(EVIDENCE.relative_to(ROOT)),
    }, indent=2))


if __name__ == '__main__':
    main()
