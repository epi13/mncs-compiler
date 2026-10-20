#!/usr/bin/env python3
"""Cold/warm and invalidation experiments for the native mncs-test application."""
from __future__ import annotations

import copy
import json
import os
import shutil
import select
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_vm_native_application as app

ROOT = app.ROOT
CACHE_DIR = ROOT / '.build/native-app-readiness/gate-b-cache'
EVIDENCE = ROOT / 'evidence' / (
    f"native-app-readiness-gate-b-"
    f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}-{os.getpid()}.json"
)


def clear_cache() -> None:
    for directory in ('application-builds', 'application-build-indexes'):
        entry_dir = CACHE_DIR / directory
        entry_dir.mkdir(parents=True, exist_ok=True)
        for path in entry_dir.iterdir():
            if path.is_file() and (path.suffix == '.json' or path.name.endswith('.tmp')):
                path.unlink()


def request_for(root: str, modules: dict[str, str]) -> dict:
    return {
        'schema_version': 'mncs.compiler.application-build-request/5',
        'target': 'mncs-vm-0.1',
        'root_source_id': app.APP_SOURCE_ID,
        'root_source': root,
        'callable_roots': [{'module': app.APP_MODULE, 'function': 'run'}],
        'modules': modules,
        'test_selection': {'kind': 'all_source_cases'},
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


def ready_probe(cache_dir: Path, executable: Path | None = None) -> tuple[app.Probe, dict]:
    probe = app.Probe(cache_dir, executable=executable)
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


def require_verification_work(
    label: str,
    build: dict,
    *,
    frontend_invocations: int,
    dependency_closure: str,
    ssa_verification: str,
    backend_artifact: str,
) -> dict:
    cache = build.get('cache', {})
    work = cache.get('verification_work', {})
    if (cache.get('schema_version') != 'mncs.compiler.application-build-cache-observation/5'
            or work.get('schema_version') != 'mncs.compiler.application-build-verification-work/3'):
        raise AssertionError(f'{label} omitted the versioned verification-work observation')
    if work.get('front_end_invocations') != frontend_invocations:
        raise AssertionError(f'{label} frontend invocation count is inaccurate: {work}')
    expected_stage = 'recomputed_by_full_frontend' if frontend_invocations else 'not_run'
    for stage in ('source_parsing', 'declaration_resolution'):
        if work.get(stage) != expected_stage:
            raise AssertionError(f'{label} has an unexpected {stage} state: {work}')
    if not isinstance(work.get('reused_semantic_facts'), list):
        raise AssertionError(f'{label} omitted the Language fact-reference list: {work}')
    if (not isinstance(work.get('reused_declaration_facts'), list)
            or not isinstance(work.get('recomputed_declaration_facts'), list)):
        raise AssertionError(f'{label} omitted Language declaration-fact work: {work}')
    expected_signature_stage = (
        'not_run' if not frontend_invocations else
        'recomputed_with_language_declaration_fact_reuse'
        if work['reused_declaration_facts'] else expected_stage
    )
    if work.get('signature_verification') != expected_signature_stage:
        raise AssertionError(
            f'{label} has an unexpected signature verification state: {work}')
    expected_body_stage = (
        'not_run' if not frontend_invocations else
        'recomputed_with_language_fact_reuse' if work['reused_semantic_facts'] else
        'recomputed_by_full_frontend'
    )
    if work.get('function_body_verification') != expected_body_stage:
        raise AssertionError(f'{label} has an unexpected function body verification state: {work}')
    if (not isinstance(work.get('recomputed_semantic_facts'), list)
            or type(work.get('produced_semantic_fact_count')) is not int):
        raise AssertionError(f'{label} omitted actual Language fact work: {work}')
    if (work.get('semantic_dependency_closure') != dependency_closure
            or work.get('ssa_verification') != ssa_verification
            or work.get('backend_artifact') != backend_artifact):
        raise AssertionError(f'{label} reports inaccurate cache or backend work: {work}')
    if frontend_invocations:
        if type(work.get('resolved_module_count')) is not int or work['resolved_module_count'] < 1:
            raise AssertionError(f'{label} omitted resolved module work count: {work}')
        if build.get('status') == 'rejected':
            if work.get('front_end_outcome') != 'rejected':
                raise AssertionError(f'{label} lost its fresh frontend rejection: {work}')
        elif (work.get('front_end_outcome') != 'verified'
              or type(work.get('program_function_count')) is not int
              or work['program_function_count'] < 1):
            raise AssertionError(f'{label} omitted verified frontend work counts: {work}')
    return work


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
    if cold.get('schema_version') != 'mncs.compiler.application-build/8':
        raise AssertionError('incremental experiment did not use application-build/8')
    if cold.get('status') != 'verified' or cold.get('entry', {}).get('status') != 'verified':
        raise AssertionError('cold selected application scope was not verified')
    if cold.get('unresolved_obligations') != []:
        raise AssertionError('selected application scope retained unresolved obligations')
    cold_scope = cold.get('callable_scope', {})
    cold_roots = {
        (row.get('module'), row.get('function'))
        for row in cold_scope.get('callable_roots', [])
    }
    if (cold_scope.get('kind')
            != 'source_test_case_selection_and_explicit_callable_roots_and_generic_seeds'
            or cold_roots != {(app.APP_MODULE, 'run')}
            or cold_scope.get('generic_seed_functions') != [
                ['mncs.test.assertions', 'equals_bytes'],
                ['mncs.test.suite', 'summarize'],
            ]
            or cold_scope.get('test_case_selection', {}).get('selection')
            != {'kind': 'all_source_cases'}):
        raise AssertionError('incremental evidence omitted its source-test callable scope')
    artifact = cold['artifact']['value']

    # A new process proves the host cache survives session teardown.
    probe, warm_startup_metrics = ready_probe(CACHE_DIR)
    try:
        warm, warm_metrics = compile_one(probe, original_request)
        if warm.get('cache', {}).get('status') != 'hit':
            raise AssertionError('unchanged build did not hit the persistent artifact cache')
        if warm['artifact']['identity'] != cold['artifact']['identity']:
            raise AssertionError('warm reuse changed the artifact identity')

        # A root-only source edit must find the candidate from the previous
        # process, verify the current source, then reuse the same executable
        # artifact when the selected closure and interfaces remain unchanged.
        root_comment_source = app.APP_SOURCE + (
            '\n// source-only root edit: preserve the verified application semantics\n'
        )
        root_comment_request = request_for(root_comment_source, modules)
        root_comment_cache_dir = CACHE_DIR / 'root-comment-isolated'
        shutil.rmtree(root_comment_cache_dir, ignore_errors=True)
        root_seed_probe, root_seed_startup_metrics = ready_probe(root_comment_cache_dir)
        try:
            root_seed, root_seed_metrics = compile_one(root_seed_probe, original_request)
            if (root_seed.get('cache', {}).get('status') != 'miss'
                    or root_seed.get('cache', {}).get('publication') != 'stored'):
                raise AssertionError('root snapshot fixture did not seed a fresh artifact')
            root_seed_probe_tail = root_seed_probe.close()
        except BaseException:
            if root_seed_probe.proc.poll() is None:
                root_seed_probe.proc.kill()
                root_seed_probe.proc.wait(timeout=5)
            raise

        root_comment_probe, root_comment_startup_metrics = ready_probe(root_comment_cache_dir)
        try:
            root_comment_oracle, root_comment_exec, root_comment_oracle_metrics = oracle_one(
                root_comment_probe, root_comment_source, modules)
            root_comment, root_comment_metrics = compile_one(
                root_comment_probe, root_comment_request)
            root_comment_work = root_comment.get('cache', {}).get('verification_work', {})
            if (root_comment.get('status') != 'verified'
                    or root_comment.get('cache', {}).get('status') != 'hit'
                    or root_comment.get('cache', {}).get('reuse_basis')
                    != 'verified_application_executable_closure'
                    or root_comment.get('artifact', {}).get('identity')
                    != root_seed.get('artifact', {}).get('identity')
                    or root_comment.get('source_snapshot', {}).get('identity')
                    == root_seed.get('source_snapshot', {}).get('identity')):
                raise AssertionError(
                    'fresh-process root comment edit did not reuse the verified executable closure')
            if (root_comment.get('cache', {}).get('recomputed_units') != [
                    'source_semantic_check', 'application_executable_closure_verification']
                    or root_comment_work.get('front_end_invocations') != 1
                    or root_comment_work.get('backend_artifact') != 'reused'
                    or root_comment_work.get('ssa_verification')
                    != 'reused_verified_vm_artifact'
                    or root_comment.get('dependency_graph', {}).get('semantic_identity')
                    != cold.get('dependency_graph', {}).get('semantic_identity')):
                raise AssertionError(
                    'root comment reuse did not preserve fresh semantic verification work')
            _root_comment_vm, root_comment_vm_metrics, root_comment_actual = run_and_compare(
                root_comment['artifact']['value'], root_comment_exec)
            if compact_oracle(root_comment_exec) != compact_result(root_comment_actual[0]):
                raise AssertionError('fresh-process root comment reuse changed VM behavior')
            root_comment_probe_tail = root_comment_probe.close()
        except BaseException:
            if root_comment_probe.proc.poll() is None:
                root_comment_probe.proc.kill()
                root_comment_probe.proc.wait(timeout=5)
            raise

        # A byte-distinct compiler executable is a different producer even
        # when it contains the same code. Sharing a cache directory must not
        # let it reuse a result admitted under the old compiler identity.
        with tempfile.TemporaryDirectory(prefix='native-app-probe-identity-') as raw_dir:
            variant_probe_path = Path(raw_dir) / 'mncs-compiler-stage0-probe'
            shutil.copy2(app.PROBE, variant_probe_path)
            with variant_probe_path.open('ab') as variant_file:
                variant_file.write(b'\0compiler-identity-cache-isolation')
            variant_probe, variant_startup_metrics = ready_probe(
                CACHE_DIR, executable=variant_probe_path)
            try:
                variant_oracle, variant_exec, variant_oracle_metrics = oracle_one(
                    variant_probe, app.APP_SOURCE, modules)
                variant_build, variant_build_metrics = compile_one(
                    variant_probe, original_request)
                if (variant_build.get('cache', {}).get('status') != 'miss'
                        or variant_build.get('cache', {}).get('reused_units') != []
                        or variant_build.get('cache', {}).get('recomputed_units') != [
                            'source_semantic_check', 'ssa_verification',
                            'verified_vm_artifact']):
                    raise AssertionError(
                        'different compiler executable identity reused a prior application build')
                original_probe_identity = cold.get('producer', {}).get('probe', {}).get(
                    'probe_executable_sha256')
                variant_probe_identity = variant_build.get('producer', {}).get('probe', {}).get(
                    'probe_executable_sha256')
                if (not original_probe_identity or not variant_probe_identity
                        or original_probe_identity == variant_probe_identity):
                    raise AssertionError('compiler identity isolation fixture did not change probe identity')
                _variant_vm, variant_vm_metrics, variant_actual = run_and_compare(
                    variant_build['artifact']['value'], variant_exec)
                if compact_oracle(variant_exec) != compact_result(variant_actual[0]):
                    raise AssertionError('identity-isolated build changed application semantics')
                variant_probe_tail = variant_probe.close()
            except BaseException:
                if variant_probe.proc.poll() is None:
                    variant_probe.proc.kill()
                    variant_probe.proc.wait(timeout=5)
                raise
            different_compiler_identity = {
                'original_probe_sha256': original_probe_identity,
                'variant_probe_sha256': variant_probe_identity,
                'cache_key_original': cold.get('cache', {}).get('key'),
                'cache_key_variant': variant_build.get('cache', {}).get('key'),
                'cache_status': variant_build.get('cache', {}).get('status'),
                'reused_units': variant_build.get('cache', {}).get('reused_units'),
                'recomputed_units': variant_build.get('cache', {}).get('recomputed_units'),
                'artifact_identity_original': cold['artifact']['identity'],
                'artifact_identity_variant': variant_build['artifact']['identity'],
                'oracle_valid': variant_oracle.get('valid'),
                'oracle_result': compact_oracle(variant_exec),
                'vm_result': compact_result(variant_actual[0]),
                'semantic_match': compact_oracle(variant_exec) == compact_result(variant_actual[0]),
                'oracle_metrics': variant_oracle_metrics,
                'compile_metrics': variant_build_metrics,
                'vm_metrics': variant_vm_metrics,
                'process_startup_metrics': variant_startup_metrics,
                'producer_stderr_tail': variant_probe_tail[-1200:],
            }

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
                or semantic_edit.get('cache', {}).get('reuse_basis')
                != 'verified_application_executable_closure'):
            raise AssertionError('source-only dependency edit did not reuse the matching semantic artifact')
        if semantic_edit.get('cache', {}).get('recomputed_units') != [
                'source_semantic_check', 'application_executable_closure_verification']:
            raise AssertionError('executable-closure reuse omitted the changed-source verification work')
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

        # A semantic edit to an uncalled function in an otherwise reachable
        # imported module must keep the full semantic identity fresh without
        # rebuilding the selected executable closure.
        unrelated_function_modules = dict(modules)
        generative_source = unrelated_function_modules['mncs.test.generative']
        old_shrink = 'fn shrink(value: u64) -> (result: u64) {\n    return value / 2;\n}'
        new_shrink = 'fn shrink(value: u64) -> (result: u64) {\n    return value / 3;\n}'
        if generative_source.count(old_shrink) != 1:
            raise AssertionError('unreachable function invalidation fixture no longer matches source')
        unrelated_function_modules['mncs.test.generative'] = generative_source.replace(
            old_shrink, new_shrink, 1)
        unrelated_function_request = request_for(app.APP_SOURCE, unrelated_function_modules)
        unrelated_function_oracle, unrelated_function_exec, unrelated_function_oracle_metrics = oracle_one(
            probe, app.APP_SOURCE, unrelated_function_modules)
        unrelated_function_build, unrelated_function_metrics = compile_one(
            probe, unrelated_function_request)
        if (unrelated_function_build.get('status') != 'verified'
                or unrelated_function_build.get('cache', {}).get('status') != 'hit'
                or unrelated_function_build.get('cache', {}).get('reuse_basis')
                != 'verified_application_executable_closure'
                or unrelated_function_build.get('cache', {}).get('recomputed_units') != [
                    'source_semantic_check', 'application_executable_closure_verification']
                or unrelated_function_build.get('dependency_graph', {}).get('semantic_identity')
                == cold.get('dependency_graph', {}).get('semantic_identity')
                or unrelated_function_build.get('dependency_graph', {}).get(
                    'application_executable_closure_identity')
                != cold.get('dependency_graph', {}).get('application_executable_closure_identity')
                or unrelated_function_build.get('artifact', {}).get('identity')
                != cold.get('artifact', {}).get('identity')):
            raise AssertionError('unreachable function edit did not reuse only the verified executable closure')
        _unrelated_vm, unrelated_function_vm_metrics, unrelated_function_actual = run_and_compare(
            unrelated_function_build['artifact']['value'], unrelated_function_exec)
        if compact_oracle(unrelated_function_exec) != compact_result(unrelated_function_actual[0]):
            raise AssertionError('unreachable function cache reuse changed application semantics')

        # Language facts also guard authority assumptions. Changing an
        # uncalled function's capability and effect surface must reverify that
        # body while retaining independent facts from the same modules.
        effect_cache_dir = CACHE_DIR / 'effect-capability-isolated'
        shutil.rmtree(effect_cache_dir, ignore_errors=True)
        effect_probe, effect_startup_metrics = ready_probe(effect_cache_dir)
        try:
            effect_seed, effect_seed_metrics = compile_one(effect_probe, original_request)
            if (effect_seed.get('status') != 'verified'
                    or effect_seed.get('artifact', {}).get('identity')
                    != cold.get('artifact', {}).get('identity')):
                raise AssertionError('effect/capability fixture could not seed current Language facts')

            generative_source = modules['mncs.test.generative']
            old_shrink_declaration = 'fn shrink(value: u64) -> (result: u64) {'
            capability_shrink_declaration = (
                'fn shrink(value: u64) -> (result: u64)\n'
                '    capability test_artifact\n'
                '{'
            )
            if generative_source.count(old_shrink_declaration) != 1:
                raise AssertionError('capability fact invalidation fixture no longer matches source')
            capability_modules = dict(modules)
            capability_modules['mncs.test.generative'] = generative_source.replace(
                old_shrink_declaration, capability_shrink_declaration, 1)
            capability_request = request_for(app.APP_SOURCE, capability_modules)
            capability_oracle, capability_exec, capability_oracle_metrics = oracle_one(
                effect_probe, app.APP_SOURCE, capability_modules)
            capability_build, capability_metrics = compile_one(effect_probe, capability_request)
            capability_work = capability_build.get('cache', {}).get('verification_work', {})
            capability_recomputed = set(capability_work.get('recomputed_semantic_facts', []))
            capability_reasons = capability_work.get('semantic_fact_recomputation_reasons', [])
            capability_shrink = next((row for row in capability_reasons
                                      if str(row.get('subject', '')).endswith(
                                          'mncs.test.generative::shrink')), None)
            if (capability_build.get('status') != 'verified'
                    or capability_work.get('front_end_invocations') != 1
                    or capability_work.get('function_body_verification')
                    != 'recomputed_with_language_fact_reuse'
                    or not capability_work.get('reused_semantic_facts')
                    or not any(subject.endswith('mncs.test.generative::shrink')
                               for subject in capability_recomputed)
                    or capability_shrink is None
                    or capability_shrink.get('reason') != 'capability_assumptions_changed'):
                raise AssertionError(
                    'capability edit did not selectively reverify its Language body fact: '
                    f'{capability_work}')
            _capability_vm, capability_vm_metrics, capability_actual = run_and_compare(
                capability_build['artifact']['value'], capability_exec)
            if compact_oracle(capability_exec) != compact_result(capability_actual[0]):
                raise AssertionError('capability-fact reuse changed canonical VM behavior')

            effect_source = capability_modules['mncs.test.generative']
            old_capability_declaration = (
                'fn shrink(value: u64) -> (result: u64)\n'
                '    capability test_artifact\n'
                '{'
            )
            effect_shrink_declaration = (
                'fn shrink(value: u64) -> (result: u64)\n'
                '    capability test_artifact\n'
                '    effect structured_read authorized_by test_artifact\n'
                '{'
            )
            if effect_source.count(old_capability_declaration) != 1:
                raise AssertionError('effect fact invalidation fixture no longer matches source')
            effect_modules = dict(capability_modules)
            effect_modules['mncs.test.generative'] = effect_source.replace(
                old_capability_declaration, effect_shrink_declaration, 1)
            effect_request = request_for(app.APP_SOURCE, effect_modules)
            effect_oracle, effect_exec, effect_oracle_metrics = oracle_one(
                effect_probe, app.APP_SOURCE, effect_modules)
            effect_build, effect_metrics = compile_one(effect_probe, effect_request)
            effect_work = effect_build.get('cache', {}).get('verification_work', {})
            effect_recomputed = set(effect_work.get('recomputed_semantic_facts', []))
            effect_reasons = effect_work.get('semantic_fact_recomputation_reasons', [])
            effect_shrink = next((row for row in effect_reasons
                                  if str(row.get('subject', '')).endswith(
                                      'mncs.test.generative::shrink')), None)
            if (effect_build.get('status') != 'verified'
                    or effect_work.get('front_end_invocations') != 1
                    or effect_work.get('function_body_verification')
                    != 'recomputed_with_language_fact_reuse'
                    or not effect_work.get('reused_semantic_facts')
                    or not any(subject.endswith('mncs.test.generative::shrink')
                               for subject in effect_recomputed)
                    or effect_shrink is None
                    or effect_shrink.get('reason') != 'effect_assumptions_changed'):
                raise AssertionError(
                    'effect edit did not selectively reverify its Language body fact: '
                    f'{effect_work}')
            _effect_vm, effect_vm_metrics, effect_actual = run_and_compare(
                effect_build['artifact']['value'], effect_exec)
            if compact_oracle(effect_exec) != compact_result(effect_actual[0]):
                raise AssertionError('effect-fact reuse changed canonical VM behavior')
            effect_capability_reuse = {
                'seed_cache': effect_seed.get('cache'),
                'seed_compile_metrics': effect_seed_metrics,
                'process_startup_metrics': effect_startup_metrics,
                'capability_edit': {
                    'source_sha256': app.sha(capability_modules[
                        'mncs.test.generative'].encode()),
                    'cache': capability_build.get('cache'),
                    'reused_semantic_facts': capability_work.get('reused_semantic_facts'),
                    'recomputed_semantic_facts': capability_work.get('recomputed_semantic_facts'),
                    'recomputation_reasons': capability_reasons,
                    'artifact_identity': capability_build.get('artifact', {}).get('identity'),
                    'oracle_valid': capability_oracle.get('valid'),
                    'oracle_result': compact_oracle(capability_exec),
                    'vm_result': compact_result(capability_actual[0]),
                    'oracle_matches_vm': compact_oracle(capability_exec)
                    == compact_result(capability_actual[0]),
                    'oracle_metrics': capability_oracle_metrics,
                    'compile_metrics': capability_metrics,
                    'vm_metrics': capability_vm_metrics,
                },
                'effect_edit': {
                    'source_sha256': app.sha(effect_modules[
                        'mncs.test.generative'].encode()),
                    'cache': effect_build.get('cache'),
                    'reused_semantic_facts': effect_work.get('reused_semantic_facts'),
                    'recomputed_semantic_facts': effect_work.get('recomputed_semantic_facts'),
                    'recomputation_reasons': effect_reasons,
                    'artifact_identity': effect_build.get('artifact', {}).get('identity'),
                    'oracle_valid': effect_oracle.get('valid'),
                    'oracle_result': compact_oracle(effect_exec),
                    'vm_result': compact_result(effect_actual[0]),
                    'oracle_matches_vm': compact_oracle(effect_exec)
                    == compact_result(effect_actual[0]),
                    'oracle_metrics': effect_oracle_metrics,
                    'compile_metrics': effect_metrics,
                    'vm_metrics': effect_vm_metrics,
                },
            }
            tails.append(effect_probe.close())
        except BaseException:
            if effect_probe.proc.poll() is None:
                effect_probe.proc.kill()
                effect_probe.proc.wait(timeout=5)
            raise

        # The executable closure remains the same after an unrelated exported
        # signature edit, but the artifact also advertises the whole program
        # interface. That broader artifact contract must force regeneration.
        unrelated_signature_modules = dict(modules)
        generative_source = unrelated_signature_modules['mncs.test.generative']
        old_signature = 'fn shrink(value: u64) -> (result: u64) {\n    return value / 2;\n}'
        new_signature = 'fn shrink(value: u32) -> (result: u32) {\n    return value / 2;\n}'
        if generative_source.count(old_signature) != 1:
            raise AssertionError('unrelated exported signature fixture no longer matches source')
        unrelated_signature_modules['mncs.test.generative'] = generative_source.replace(
            old_signature, new_signature, 1)
        unrelated_signature_request = request_for(app.APP_SOURCE, unrelated_signature_modules)
        unrelated_signature_oracle, unrelated_signature_exec, unrelated_signature_oracle_metrics = oracle_one(
            probe, app.APP_SOURCE, unrelated_signature_modules)
        unrelated_signature_build, unrelated_signature_metrics = compile_one(
            probe, unrelated_signature_request)
        if (unrelated_signature_build.get('status') != 'verified'
                or unrelated_signature_build.get('cache', {}).get('status') != 'miss'
                or unrelated_signature_build.get('cache', {}).get('recomputed_units') != [
                    'source_semantic_check', 'ssa_verification', 'verified_vm_artifact']
                or unrelated_signature_build.get('dependency_graph', {}).get(
                    'application_executable_closure_identity')
                != cold.get('dependency_graph', {}).get(
                    'application_executable_closure_identity')
                or unrelated_signature_build.get('entry', {}).get('interface', {}).get(
                    'identity')
                != cold.get('entry', {}).get('interface', {}).get('identity')
                or unrelated_signature_build.get('artifact', {}).get('interface_identity')
                == cold.get('artifact', {}).get('interface_identity')
                or unrelated_signature_build.get('artifact', {}).get('identity')
                == cold.get('artifact', {}).get('identity')):
            raise AssertionError('unrelated exported signature did not refresh whole-program artifact provenance')
        _unrelated_signature_vm, unrelated_signature_vm_metrics, unrelated_signature_actual = run_and_compare(
            unrelated_signature_build['artifact']['value'], unrelated_signature_exec)
        if compact_oracle(unrelated_signature_exec) != compact_result(unrelated_signature_actual[0]):
            raise AssertionError('interface-refreshed application differs from the independent oracle')

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

        # Changing an imported nominal record's exported shape must invalidate
        # the dependent application interface and artifact. The application
        # consumes PropertyCase through its typed return closure, so the
        # updated constructor remains a valid independent oracle workload.
        interface_modules = dict(modules)
        generative_source = interface_modules['mncs.test.generative']
        old_record = '''record PropertyCase {
    seed: u64,
    case_index: u64,
    value: u64
}'''
        new_record = '''record PropertyCase {
    seed: u64,
    case_index: u64,
    value: u64,
    generation: u64
}'''
        old_constructor = 'PropertyCase { seed: seed, case_index: case_index, value: drawn }'
        new_constructor = (
            'PropertyCase { seed: seed, case_index: case_index, value: drawn, '
            'generation: seed }'
        )
        if generative_source.count(old_record) != 1 or generative_source.count(old_constructor) != 1:
            raise AssertionError('nominal interface invalidation fixture no longer matches source')
        interface_modules['mncs.test.generative'] = (
            generative_source.replace(old_record, new_record, 1)
            .replace(old_constructor, new_constructor, 1)
        )
        interface_request = request_for(app.APP_SOURCE, interface_modules)
        interface_oracle, interface_exec, interface_oracle_metrics = oracle_one(
            probe, app.APP_SOURCE, interface_modules)
        interface_build, interface_metrics = compile_one(probe, interface_request)
        if (interface_build.get('status') != 'verified'
                or interface_build.get('cache', {}).get('status') != 'miss'
                or interface_build.get('cache', {}).get('reused_units') != []
                or interface_build.get('cache', {}).get('recomputed_units') != [
                    'source_semantic_check', 'ssa_verification', 'verified_vm_artifact']):
            raise AssertionError('exported nominal interface change reused dependent application work')
        interface_before = cold.get('entry', {}).get('interface', {}).get('identity')
        interface_after = interface_build.get('entry', {}).get('interface', {}).get('identity')
        semantic_before = cold.get('dependency_graph', {}).get('semantic_identity')
        semantic_after = interface_build.get('dependency_graph', {}).get('semantic_identity')
        if (not interface_before or not interface_after or interface_before == interface_after
                or not semantic_before or semantic_before == semantic_after
                or interface_build.get('artifact', {}).get('identity') == cold['artifact']['identity']):
            raise AssertionError('exported nominal interface change left its dependent identity unchanged')
        _interface_vm, interface_vm_metrics, interface_actual = run_and_compare(
            interface_build['artifact']['value'], interface_exec)
        if compact_oracle(interface_exec) != compact_result(interface_actual[0]):
            raise AssertionError('interface-invalidated application differs from the independent oracle')

        # Adding a real import and cross-module call changes the executable
        # closure. Rebuild the graph and artifact, then compare its behavior
        # to the independent project oracle.
        closure_modules = dict(modules)
        closure_modules['mncs.test.readiness_marker'] = '''mncs 0.18;
module mncs.test.readiness_marker;
fn readiness_marker(value: u64) -> (result: u64) { return value; }'''
        closure_source = app.APP_SOURCE.replace(
            'use mncs.test.snapshot;',
            'use mncs.test.snapshot;\nuse mncs.test.readiness_marker;',
            1,
        ).replace(
            'state_value: state_value,',
            'state_value: readiness_marker(state_value),',
            1,
        )
        if closure_source == app.APP_SOURCE or 'readiness_marker(state_value)' not in closure_source:
            raise AssertionError('module-closure fixture did not add its import and call')
        closure_request = request_for(closure_source, closure_modules)
        closure_oracle, closure_exec, closure_oracle_metrics = oracle_one(
            probe, closure_source, closure_modules)
        closure_build, closure_metrics = compile_one(probe, closure_request)
        if (closure_build.get('status') != 'verified'
                or closure_build.get('cache', {}).get('status') != 'miss'
                or closure_build.get('cache', {}).get('reused_units') != []
                or closure_build.get('cache', {}).get('recomputed_units') != [
                    'source_semantic_check', 'ssa_verification', 'verified_vm_artifact']):
            raise AssertionError('imported executable closure change reused stale build work')
        closure_resolutions = closure_build.get(
            'dependency_graph', {}).get('details', {}).get('resolutions', [])
        if (not any(row.get('requested_module') == 'mncs.test.readiness_marker'
                    for row in closure_resolutions)
                or closure_build.get('dependency_graph', {}).get('identity')
                == cold.get('dependency_graph', {}).get('identity')
                or closure_build.get('artifact', {}).get('identity')
                == cold.get('artifact', {}).get('identity')):
            raise AssertionError('import change did not reconstruct dependency and artifact identities')
        _closure_vm, closure_vm_metrics, closure_actual = run_and_compare(
            closure_build['artifact']['value'], closure_exec)
        if compact_oracle(closure_exec) != compact_result(closure_actual[0]):
            raise AssertionError('changed module closure differs from the independent oracle')

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

    # Run the changed generic after cache scenarios that rely on the original
    # executable index. Publishing a different selected closure can update the
    # index candidate used by later requests, even though each build remains
    # correct for its own source snapshot.
    generic_specialization_modules = dict(modules)
    suite_source = generic_specialization_modules['mncs.test.suite']
    old_fold = 'next summary = observe(summary, tests[i]);'
    new_fold = 'next summary = observe(summary, tests[tests.len - i - 1]);'
    if suite_source.count(old_fold) != 1:
        raise AssertionError('generic specialization invalidation fixture no longer matches source')
    generic_specialization_modules['mncs.test.suite'] = suite_source.replace(
        old_fold, new_fold, 1)
    generic_specialization_request = request_for(
        app.APP_SOURCE, generic_specialization_modules)
    generic_cache_dir = CACHE_DIR / 'generic-specialization-isolated'
    shutil.rmtree(generic_cache_dir, ignore_errors=True)
    generic_probe, generic_startup_metrics = ready_probe(generic_cache_dir)
    try:
        generic_specialization_oracle, generic_specialization_exec, generic_specialization_oracle_metrics = oracle_one(
            generic_probe, app.APP_SOURCE, generic_specialization_modules)
        generic_seed_build, generic_seed_metrics = compile_one(
            generic_probe, original_request)
        if (generic_seed_build.get('status') != 'verified'
                or generic_seed_build.get('artifact', {}).get('identity')
                != cold.get('artifact', {}).get('identity')):
            raise AssertionError('generic specialization fixture could not seed prior Language facts')
        generic_specialization_build, generic_specialization_metrics = compile_one(
            generic_probe, generic_specialization_request)
        if (generic_specialization_build.get('status') != 'verified'
                or generic_specialization_build.get('cache', {}).get('status') != 'miss'
                or generic_specialization_build.get('cache', {}).get('reused_units') != []
                or generic_specialization_build.get('cache', {}).get('recomputed_units') != [
                    'source_semantic_check', 'ssa_verification', 'verified_vm_artifact']):
            raise AssertionError('edited imported generic specialization reused stale build work')
        generic_work = generic_specialization_build.get('cache', {}).get(
            'verification_work', {})
        generic_reused = set(generic_work.get('reused_semantic_facts', []))
        generic_recomputed = set(generic_work.get('recomputed_semantic_facts', []))
        if not generic_reused or not generic_recomputed:
            raise AssertionError(
                'generic specialization edit did not selectively reuse and recompute Language facts')
        if (len(generic_recomputed) != 3
                or not any('mncs.test.suite::summarize' in subject
                           and '__spec_' not in subject
                           for subject in generic_recomputed)
                or not any('mncs.test.suite::summarize__spec_' in subject
                           for subject in generic_recomputed)
                or 'mncs:0.2:body:tests.native_readiness::run' not in generic_recomputed):
            raise AssertionError(
                'generic edit did not reverify its body, specialization, and consumer')
        if (generic_specialization_build.get('dependency_graph', {}).get('semantic_identity')
                == cold.get('dependency_graph', {}).get('semantic_identity')
                or generic_specialization_build.get('dependency_graph', {}).get(
                    'application_executable_closure_identity')
                == cold.get('dependency_graph', {}).get(
                    'application_executable_closure_identity')
                or generic_specialization_build.get('artifact', {}).get('identity')
                == cold.get('artifact', {}).get('identity')):
            raise AssertionError('generic specialization edit left semantic or artifact identity unchanged')
        _generic_specialization_vm, generic_specialization_vm_metrics, generic_specialization_actual = run_and_compare(
            generic_specialization_build['artifact']['value'], generic_specialization_exec)
        if compact_oracle(generic_specialization_exec) != compact_result(generic_specialization_actual[0]):
            raise AssertionError('generic specialization edit differs from the independent oracle')
        tails.append(generic_probe.close())
    except BaseException:
        if generic_probe.proc.poll() is None:
            generic_probe.proc.kill()
            generic_probe.proc.wait(timeout=5)
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

    cold_work = require_verification_work(
        'cold build', cold, frontend_invocations=1,
        dependency_closure='recomputed_by_full_frontend',
        ssa_verification='recomputed', backend_artifact='generated',
    )
    require_verification_work(
        'warm exact-snapshot build', warm, frontend_invocations=0,
        dependency_closure='not_run', ssa_verification='not_run',
        backend_artifact='reused',
    )
    for label, result in (('unreferenced module addition', unused),
                          ('unreferenced module edit', changed_unused)):
        require_verification_work(
            label, result, frontend_invocations=0,
            dependency_closure='reused_verified_dependency_closure',
            ssa_verification='not_run', backend_artifact='reused',
        )
    for label, result in (('comment-only dependency edit', semantic_edit),
                          ('unreachable function body edit', unrelated_function_build)):
        require_verification_work(
            label, result, frontend_invocations=1,
            dependency_closure='recomputed_by_full_frontend',
            ssa_verification='reused_verified_vm_artifact', backend_artifact='reused',
        )
    for label, result in (
        ('reachable root body edit', small),
        ('reachable dependency edit', dependency),
        ('generic specialization edit', generic_specialization_build),
        ('unrelated exported signature edit', unrelated_signature_build),
        ('imported nominal interface edit', interface_build),
        ('imported call and closure change', closure_build),
        ('compiler identity change', variant_build),
        ('interrupted build retry', retry),
    ):
        require_verification_work(
            label, result, frontend_invocations=1,
            dependency_closure='recomputed_by_full_frontend',
            ssa_verification='recomputed', backend_artifact='generated',
        )
    for label, result in (('invalid source', invalid), ('repeated invalid source', invalid_repeat)):
        require_verification_work(
            label, result, frontend_invocations=1,
            dependency_closure='recomputed_by_full_frontend',
            ssa_verification='not_run', backend_artifact='not_generated',
        )
    retry_warm_work = require_verification_work(
        'fresh-process retry cache readback', retry_warm, frontend_invocations=0,
        dependency_closure='not_run', ssa_verification='not_run',
        backend_artifact='reused',
    )

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
    worktree_changes = app.git('status', '--short', cwd=ROOT).splitlines()
    evidence = {
        'schema_version': 1,
        'kind': 'mncs-native-incremental-build-evidence',
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'compiler': {
            'build_profile': 'cargo build --release',
            'revision': app.git('rev-parse', 'HEAD', cwd=ROOT),
            'branch': app.git('branch', '--show-current', cwd=ROOT),
            'worktree_head_at_measurement': app.git('rev-parse', 'HEAD', cwd=ROOT),
            'worktree_changes_at_measurement': worktree_changes,
            'worktree_dirty_at_measurement': bool(worktree_changes),
            'stage0_revision': json.loads((ROOT / 'mncs-language.lock.json').read_text())['revision'],
            'stage0_probe_sha256': app.sha(app.PROBE.read_bytes()),
            'application_module_sha256': app.sha((ROOT / 'tools/stage0-probe/src/application.rs').read_bytes()),
            'probe_dispatch_sha256': app.sha((ROOT / 'tools/stage0-probe/src/main.rs').read_bytes()),
            'gate_a_harness_sha256': app.sha((ROOT / 'tools/test_vm_native_application.py').read_bytes()),
            'gate_b_harness_sha256': app.sha(Path(__file__).read_bytes()),
            'vm_revision': app.git('rev-parse', 'HEAD', cwd=app.VM_ROOT),
            'vm_build_profile': os.environ.get('MNCS_NATIVE_APP_VM_PROFILE', 'unspecified'),
            'vm_binary_path': str(app.VM_BIN),
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
            'dependency-closure candidate index binds root source ID and build assumptions; exact root and resolved dependency hashes permit pre-frontend reuse, while changed source requires a fresh frontend check before executable-closure artifact reuse',
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
            'fresh_process_root_comment_edit': {
                'root_source_sha256_before': app.sha(app.APP_SOURCE.encode()),
                'root_source_sha256_after': app.sha(root_comment_source.encode()),
                'source_snapshot_identity_before': root_seed.get('source_snapshot', {}).get('identity'),
                'source_snapshot_identity_after': root_comment.get('source_snapshot', {}).get('identity'),
                'semantic_identity_before': root_seed.get('dependency_graph', {}).get('semantic_identity'),
                'semantic_identity_after': root_comment.get('dependency_graph', {}).get('semantic_identity'),
                'cache': root_comment.get('cache'),
                'verification_work': root_comment_work,
                'artifact_identity_before': root_seed.get('artifact', {}).get('identity'),
                'artifact_identity_after': root_comment.get('artifact', {}).get('identity'),
                'oracle_valid': root_comment_oracle.get('valid'),
                'oracle_result': compact_oracle(root_comment_exec),
                'vm_result': compact_result(root_comment_actual[0]),
                'oracle_matches_vm': compact_oracle(root_comment_exec)
                == compact_result(root_comment_actual[0]),
                'compile_metrics': root_comment_metrics,
                'vm_metrics': root_comment_vm_metrics,
                'oracle_metrics': root_comment_oracle_metrics,
                'process_startup_metrics': root_comment_startup_metrics,
                'seed_compile_metrics': root_seed_metrics,
                'seed_process_startup_metrics': root_seed_startup_metrics,
                'seed_producer_stderr_tail': root_seed_probe_tail[-1200:],
                'producer_stderr_tail': root_comment_probe_tail[-1200:],
            },
            'different_compiler_executable_identity': different_compiler_identity,
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
            'generic_specialization_edit': {
                'changed_module': 'mncs.test.suite',
                'changed_generic_function': 'summarize<N=4>',
                'changed_body': 'reverse the bounded TestResult fold order',
                'prior_fact_seed_cache': generic_seed_build.get('cache'),
                'prior_fact_seed_metrics': generic_seed_metrics,
                'source_sha256_before': app.sha(modules['mncs.test.suite'].encode()),
                'source_sha256_after': app.sha(
                    generic_specialization_modules['mncs.test.suite'].encode()),
                'generic_seeds': generic_specialization_request.get('generic_seeds'),
                'semantic_identity_before': cold.get('dependency_graph', {}).get('semantic_identity'),
                'semantic_identity_after': generic_specialization_build.get(
                    'dependency_graph', {}).get('semantic_identity'),
                'executable_closure_identity_before': cold.get('dependency_graph', {}).get(
                    'application_executable_closure_identity'),
                'executable_closure_identity_after': generic_specialization_build.get(
                    'dependency_graph', {}).get('application_executable_closure_identity'),
                'cache': generic_specialization_build.get('cache'),
                'artifact_identity_before': cold.get('artifact', {}).get('identity'),
                'artifact_identity_after': generic_specialization_build.get(
                    'artifact', {}).get('identity'),
                'oracle_valid': generic_specialization_oracle.get('valid'),
                'oracle_result': compact_oracle(generic_specialization_exec),
                'vm_result': compact_result(generic_specialization_actual[0]),
                'oracle_matches_vm': compact_oracle(generic_specialization_exec)
                == compact_result(generic_specialization_actual[0]),
                'oracle_metrics': generic_specialization_oracle_metrics,
                'compile_metrics': generic_specialization_metrics,
                'vm_metrics': generic_specialization_vm_metrics,
                'process_startup_metrics': generic_startup_metrics,
            },
            'effect_capability_fact_invalidation': effect_capability_reuse,
            'unreachable_function_edit': {
                'changed_module': 'mncs.test.generative',
                'changed_function': 'shrink',
                'source_sha256_before': app.sha(modules['mncs.test.generative'].encode()),
                'source_sha256_after': app.sha(
                    unrelated_function_modules['mncs.test.generative'].encode()),
                'full_semantic_identity_before': cold.get('dependency_graph', {}).get('semantic_identity'),
                'full_semantic_identity_after': unrelated_function_build.get(
                    'dependency_graph', {}).get('semantic_identity'),
                'executable_closure_identity_before': cold.get('dependency_graph', {}).get(
                    'application_executable_closure_identity'),
                'executable_closure_identity_after': unrelated_function_build.get(
                    'dependency_graph', {}).get('application_executable_closure_identity'),
                'cache': unrelated_function_build.get('cache'),
                'artifact_identity_before': cold.get('artifact', {}).get('identity'),
                'artifact_identity_after': unrelated_function_build.get('artifact', {}).get('identity'),
                'oracle_valid': unrelated_function_oracle.get('valid'),
                'oracle_result': compact_oracle(unrelated_function_exec),
                'vm_result': compact_result(unrelated_function_actual[0]),
                'oracle_matches_vm': compact_oracle(unrelated_function_exec)
                == compact_result(unrelated_function_actual[0]),
                'oracle_metrics': unrelated_function_oracle_metrics,
                'compile_metrics': unrelated_function_metrics,
                'vm_metrics': unrelated_function_vm_metrics,
            },
            'unrelated_exported_signature_change': {
                'changed_module': 'mncs.test.generative',
                'changed_function': 'shrink',
                'source_sha256_before': app.sha(modules['mncs.test.generative'].encode()),
                'source_sha256_after': app.sha(
                    unrelated_signature_modules['mncs.test.generative'].encode()),
                'executable_closure_identity_before': cold.get('dependency_graph', {}).get(
                    'application_executable_closure_identity'),
                'executable_closure_identity_after': unrelated_signature_build.get(
                    'dependency_graph', {}).get('application_executable_closure_identity'),
                'entry_interface_identity_before': cold.get('entry', {}).get(
                    'interface', {}).get('identity'),
                'entry_interface_identity_after': unrelated_signature_build.get(
                    'entry', {}).get('interface', {}).get('identity'),
                'artifact_interface_identity_before': cold.get('artifact', {}).get(
                    'interface_identity'),
                'artifact_interface_identity_after': unrelated_signature_build.get(
                    'artifact', {}).get('interface_identity'),
                'cache': unrelated_signature_build.get('cache'),
                'artifact_identity_before': cold.get('artifact', {}).get('identity'),
                'artifact_identity_after': unrelated_signature_build.get(
                    'artifact', {}).get('identity'),
                'oracle_valid': unrelated_signature_oracle.get('valid'),
                'oracle_result': compact_oracle(unrelated_signature_exec),
                'vm_result': compact_result(unrelated_signature_actual[0]),
                'oracle_matches_vm': compact_oracle(unrelated_signature_exec)
                == compact_result(unrelated_signature_actual[0]),
                'oracle_metrics': unrelated_signature_oracle_metrics,
                'compile_metrics': unrelated_signature_metrics,
                'vm_metrics': unrelated_signature_vm_metrics,
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
            'exported_nominal_interface_change': {
                'changed_module': 'mncs.test.generative',
                'changed_exported_record': 'PropertyCase.generation: u64',
                'source_sha256_before': app.sha(modules['mncs.test.generative'].encode()),
                'source_sha256_after': app.sha(interface_modules['mncs.test.generative'].encode()),
                'interface_identity_before': interface_before,
                'interface_identity_after': interface_after,
                'semantic_identity_before': semantic_before,
                'semantic_identity_after': semantic_after,
                'cache': interface_build.get('cache'),
                'artifact_identity_before': cold['artifact']['identity'],
                'artifact_identity_after': interface_build['artifact']['identity'],
                'oracle_valid': interface_oracle.get('valid'),
                'oracle_result': compact_oracle(interface_exec),
                'vm_result': compact_result(interface_actual[0]),
                'oracle_matches_vm': compact_oracle(interface_exec) == compact_result(interface_actual[0]),
                'oracle_metrics': interface_oracle_metrics,
                'compile_metrics': interface_metrics,
                'vm_metrics': interface_vm_metrics,
            },
            'imported_module_closure_change': {
                'added_module': 'mncs.test.readiness_marker',
                'added_cross_module_call': 'readiness_marker(state_value)',
                'dependency_graph_identity_before': cold.get('dependency_graph', {}).get('identity'),
                'dependency_graph_identity_after': closure_build.get('dependency_graph', {}).get('identity'),
                'dependency_resolutions_after': closure_resolutions,
                'cache': closure_build.get('cache'),
                'artifact_identity_before': cold.get('artifact', {}).get('identity'),
                'artifact_identity_after': closure_build.get('artifact', {}).get('identity'),
                'oracle_valid': closure_oracle.get('valid'),
                'oracle_result': compact_oracle(closure_exec),
                'vm_result': compact_result(closure_actual[0]),
                'oracle_matches_vm': compact_oracle(closure_exec) == compact_result(closure_actual[0]),
                'oracle_metrics': closure_oracle_metrics,
                'compile_metrics': closure_metrics,
                'vm_metrics': closure_vm_metrics,
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
        'verification_work_observation': {
            'schema_version': 'mncs.compiler.application-build-verification-work/3',
            'cold_build': cold_work,
            'warm_exact_snapshot': warm.get('cache', {}).get('verification_work'),
            'source_only_dependency_edit': semantic_edit.get('cache', {}).get('verification_work'),
            'generic_specialization_edit': generic_specialization_build.get(
                'cache', {}).get('verification_work'),
            'unreachable_function_body_edit': unrelated_function_build.get(
                'cache', {}).get('verification_work'),
            'effect_capability_fact_invalidation': effect_capability_reuse,
            'reachable_dependency_edit': dependency.get('cache', {}).get('verification_work'),
            'invalid_source': invalid.get('cache', {}).get('verification_work'),
            'fresh_process_readback': retry_warm_work,
            'changed_snapshot_semantic_facts_reused': dependency.get(
                'cache', {}).get('verification_work', {}).get(
                    'reused_semantic_facts', []),
            'interpretation': 'Changed snapshots invoke parsing, module resolution, and whole-program declaration checking. Language-owned verified function-body facts are separately guard-checked, with independent bodies reused and changed bodies or dependents reverified. SSA and backend work are recorded independently.',
        },
        'fine_grained_invalidation': {
            'status': 'language_function_body_fact_reuse_verified',
            'detail': (f'Changed snapshots invoke parsing, module resolution, and declaration verification, then reuse only Language facts whose subject, verifier, policy, profile, callable interface, semantic dependencies, specialization, and effect/capability assumptions still match. A dependency edit reused {len(dependency.get("cache", {}).get("verification_work", {}).get("reused_semantic_facts", []))} independent body facts and reverified {len(dependency.get("cache", {}).get("verification_work", {}).get("recomputed_semantic_facts", []))} dependent bodies in {dependency_metrics["wall_seconds"]:.3f} s; canonical VM execution matched the Stage-0 oracle. Effect and capability changes independently reverified the affected Language body fact while reusing other bodies; both updated applications matched the canonical VM and Stage-0 oracle. A reachable root edit took {small_metrics["wall_seconds"]:.3f} s, an unreachable body edit reused independent facts and reverified only the changed body, and a fresh-process root comment edit reused {len(root_comment_work.get("reused_semantic_facts", []))} facts. Nominal edits reverified affected users; a generic body edit reverified its declaration, concrete specialization, and dependent caller while retaining independent facts. SSA/backend identity remains separate and is regenerated when executable semantics change.'),
        },
        'gate_a': 'pass',
        'gate_b': 'pass',
        'self_consumption_matrix_change': 'none; Stage-1/Stage-2 and the self-consumption matrix remain unchanged',
        'producer_diagnostics_tail': '\n'.join(tails)[-1800:],
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    shutil.rmtree(CACHE_DIR, ignore_errors=True)
    print(json.dumps({
        'gate_a': 'pass', 'gate_b': 'pass',
        'cold_cache': cold.get('cache', {}).get('status'),
        'warm_cache': warm.get('cache', {}).get('status'),
        'unreferenced_module_reuse_basis': unused.get('cache', {}).get('reuse_basis'),
        'unreferenced_module_edit_reuse_basis': changed_unused.get('cache', {}).get('reuse_basis'),
        'small_edit_cache': small.get('cache', {}).get('status'),
        'dependency_cache': dependency.get('cache', {}).get('status'),
        'exported_interface_cache': interface_build.get('cache', {}).get('status'),
        'generic_specialization_cache': generic_specialization_build.get('cache', {}).get('status'),
        'capability_fact_recomputed': any(
            row.get('reason') == 'capability_assumptions_changed'
            for row in effect_capability_reuse['capability_edit']['recomputation_reasons']),
        'effect_fact_recomputed': any(
            row.get('reason') == 'effect_assumptions_changed'
            for row in effect_capability_reuse['effect_edit']['recomputation_reasons']),
        'exported_interface_identity_changed': interface_before != interface_after,
        'unreachable_function_cache': unrelated_function_build.get('cache', {}).get('status'),
        'unrelated_signature_cache': unrelated_signature_build.get('cache', {}).get('status'),
        'interrupted': interrupted['interrupted'],
        'retry_cache': retry.get('cache', {}).get('status'),
        'cold_wall_seconds': cold_metrics['wall_seconds'],
        'warm_wall_seconds': warm_metrics['wall_seconds'],
        'wall_speedup': round(cache_speedup, 2),
        'cold_cpu_seconds': cold_metrics['cpu_seconds'],
        'warm_cpu_seconds': warm_metrics['cpu_seconds'],
        'cold_frontend_invocations': cold_work['front_end_invocations'],
        'cold_resolved_modules': cold_work['resolved_module_count'],
        'cold_program_functions': cold_work['program_function_count'],
        'changed_snapshot_frontend_invocations': small['cache']['verification_work']['front_end_invocations'],
        'changed_snapshot_semantic_facts_reused': small['cache']['verification_work']['reused_semantic_facts'],
        'changed_snapshot_semantic_fact_count': len(
            small['cache']['verification_work']['reused_semantic_facts']),
        'evidence': str(EVIDENCE.relative_to(ROOT)),
    }, indent=2))


if __name__ == '__main__':
    main()
