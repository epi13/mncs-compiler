#!/usr/bin/env python3
"""Compile and execute mncs.test.digest through the canonical VM capability API."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]


def sibling_repository(name: str) -> Path:
    short_name = name.removeprefix('mncs-').replace('-', '_').upper()
    override = os.environ.get(f'MNCS_{short_name}_ROOT')
    candidates = ([Path(override)] if override else [])
    candidates.extend([Path('/workspace') / name, *(parent / name for parent in ROOT.parents)])
    seen = set()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_dir():
            continue
        try:
            top_level = Path(subprocess.check_output(
                ['git', '-C', str(resolved), 'rev-parse', '--show-toplevel'],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()).resolve()
        except (OSError, subprocess.CalledProcessError):
            continue
        if top_level == resolved:
            return resolved
    raise FileNotFoundError(f'cannot locate registered sibling repository {name}')


TEST_ROOT = sibling_repository('mncs-test')
STDLIB_ROOT = sibling_repository('mncs-stdlib')
LANGUAGE_ROOT = sibling_repository('mncs-language')
VM_ROOT = sibling_repository('mncs-vm')
ROOT_SOURCE_PATH = TEST_ROOT / 'native/mncs/test/digest.mncs'
MODULE_PATHS = {
    'mncs.test.assertions': TEST_ROOT / 'native/mncs/test/assertions.mncs',
    'mncs.test.suite': TEST_ROOT / 'native/mncs/test/suite.mncs',
    'mncs.std.application.v1': STDLIB_ROOT / 'library/std/application.mncs',
}
FIXTURE_PATH = ROOT / 'evidence/native-app-readiness-test-digest.json'
ARTIFACT_PATH = ROOT / '.build/native-app-readiness/test-digest-app.vm.json'
RUNNER_SOURCE = ROOT / 'tools/native_app_vm_runner.rs'
RUNNER_MANIFEST = ROOT / '.build/native-app-readiness/vm-capability-runner-batch/Cargo.toml'
RUNNER_TARGET = ROOT / '.build/native-app-readiness/vm-capability-target-batch'
EVIDENCE_PATH = ROOT / 'evidence/native-app-readiness-test-digest-app.json'
ENTRY_MODULE = 'mncs.test.digest'
APP_FUNCTION = 'digest_app'
PURE_FUNCTION = 'build_digest'

sys.path.insert(0, str(ROOT / 'tools'))
import test_vm_native_application as support


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def effect_summaries(record: dict) -> list[dict]:
    return [
        {
            'sequence': row.get('sequence'),
            'capability': row.get('request', {}).get('capability'),
            'operation': row.get('request', {}).get('operation'),
            'outcome_tag': row.get('outcome_tag'),
        }
        for row in record.get('effects', [])
    ]


def git(path: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(path), *args], text=True).strip()


def byte_sequence(text: str) -> dict:
    return {'sequence': {'values': [{'byte': {'value': byte}} for byte in text.encode()]}}


def unsigned(value: int) -> dict:
    return {'integer': {'type': {'bits': 64, 'signed': False}, 'value': value}}


def wire_record(name: str, fields: dict, identity: str) -> dict:
    return {
        'record': {
            'name': name,
            'type_identity': identity,
            'fields': [[key, value] for key, value in sorted(fields.items())],
        },
    }


def compile_request(root: str, modules: dict[str, str]) -> dict:
    return {
        'schema_version': 'mncs.compiler.application-build-request/1',
        'target': 'mncs-vm-0.1',
        'root_source_id': 'native/mncs/test/digest.mncs',
        'root_source': root,
        'entry': {'module': ENTRY_MODULE, 'function': APP_FUNCTION},
        'modules': modules,
        'generic_seeds': [],
        'capabilities': ['test_artifact'],
        'verification_policy': 'stage0-semantic-check+canonical-vm-artifact/1',
    }


def oracle_call(argument: dict) -> dict:
    return {
        'schema_version': '0.1',
        'target': {'module': ENTRY_MODULE, 'function': PURE_FUNCTION},
        'arguments': [argument],
        'type_arguments': [],
        'step_budget': 3_000_000,
    }


def runner_manifest() -> str:
    return f'''[package]
name = "mncs-compiler-native-app-vm-runner"
version = "0.1.0"
edition = "2021"

[workspace]

[[bin]]
name = "mncs-compiler-native-app-vm-runner"
path = "{RUNNER_SOURCE}"

[dependencies]
mncs-model = {{ path = "{LANGUAGE_ROOT / 'crates/mncs-model'}" }}
mncs-vm = {{ path = "{VM_ROOT}" }}
serde_json = "1"
'''


def build_runner() -> tuple[Path, str, str]:
    RUNNER_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    RUNNER_MANIFEST.write_text(runner_manifest(), encoding='utf-8')
    lockfile = RUNNER_MANIFEST.parent / 'Cargo.lock'
    if not lockfile.exists():
        lockfile.write_bytes((VM_ROOT / 'Cargo.lock').read_bytes())
    env = os.environ.copy()
    env['CARGO_TARGET_DIR'] = str(RUNNER_TARGET)
    proc = subprocess.run(
        ['cargo', 'build', '--offline', '--release', '--manifest-path', str(RUNNER_MANIFEST)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if proc.returncode:
        raise RuntimeError(f'VM capability runner build failed: {proc.stderr[-5000:]}')
    binary = RUNNER_TARGET / 'release/mncs-compiler-native-app-vm-runner'
    return binary, sha(binary.read_bytes()), sha(lockfile.read_bytes())


def application_context(interface: dict, input_path: Path, output_path: Path,
                        current_dir: Path) -> dict:
    context_type = next(
        row for row in interface['types']['records']
        if row.get('name') == 'ApplicationContext'
    )
    identity = context_type['identity']
    return wire_record('ApplicationContext', {
        'application_identity': {'sequence': {'values': [
            {'byte': {'value': 0}} for _ in range(32)
        ]}},
        'argv': {'sequence': {'values': [
            byte_sequence(str(input_path)),
            byte_sequence(str(output_path)),
        ]}},
        'argv_count': unsigned(2),
        'current_dir': byte_sequence(str(current_dir)),
        'environment': {'sequence': {'values': []}},
        'environment_count': unsigned(0),
        'stdin': byte_sequence(''),
    }, identity)


def call_request(context: dict, step_budget: int = 3_000_000) -> dict:
    return {
        'schema_version': '0.1',
        'target': {'module': ENTRY_MODULE, 'function': APP_FUNCTION},
        'arguments': [context],
        'type_arguments': [],
        'step_budget': step_budget,
    }


def sample_runner_batch(binary: Path, capability: str, dispatch_alias: str,
                        plans: list[dict], *, authorized_identity: str | None = None,
                        artifact_path: Path = ARTIFACT_PATH) -> tuple[dict, list[dict], list[dict | None], dict, list[dict]]:
    with tempfile.TemporaryDirectory(prefix='native-digest-app-batch-') as directory:
        temp = Path(directory)
        manifest_calls = []
        case_paths = []
        for index, plan in enumerate(plans):
            call_root = temp / f'call-{index:02d}'
            call_root.mkdir()
            input_path = call_root / 'request.json'
            output_path = call_root / 'result.json'
            request_path = call_root / 'execution.json'
            input_path.write_text(
                json.dumps(plan['argument'], sort_keys=True, separators=(',', ':')),
                encoding='utf-8',
            )
            context = application_context(
                current_interface, input_path, output_path, call_root
            )
            request_path.write_text(
                json.dumps(
                    call_request(context, plan.get('step_budget', 3_000_000)),
                    sort_keys=True,
                    separators=(',', ':'),
                ),
                encoding='utf-8',
            )
            manifest_calls.append({
                'request_path': str(request_path),
                'input_path': str(input_path),
                'output_path': str(output_path),
                'provider_mode': plan['provider_mode'],
            })
            case_paths.append({
                'input_path': str(input_path),
                'output_path': str(output_path),
                'request_path': str(request_path),
            })
        manifest_path = temp / 'batch-request.json'
        manifest_path.write_text(json.dumps({
            'schema_version': 'mncs.compiler.native-application-batch-request/1',
            'calls': manifest_calls,
        }, sort_keys=True, separators=(',', ':')), encoding='utf-8')
        command = [
            str(binary), str(artifact_path), authorized_identity or capability,
            dispatch_alias, 'batch',
            str(manifest_path),
        ]
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
            stdout, stderr = process.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            watcher.join(timeout=1)
            raise TimeoutError('canonical VM application batch exceeded 90 seconds')
        wall = time.monotonic() - started
        watcher.join(timeout=1)
        samples.append(support.process_sample(process.pid))
        if process.returncode:
            raise AssertionError(f'VM batch runner exited {process.returncode}: {stderr[-3000:]}')
        document = json.loads(stdout)
        rows = document.get('calls', [])
        if (document.get('schema_version')
                != 'mncs.compiler.native-application-batch-result/1'
                or document.get('call_count') != len(plans)
                or len(rows) != len(plans)):
            raise AssertionError(f'VM batch result shape is incomplete: {document}')
        call_results = [row.get('result', {}) for row in rows]
        actual_outputs = []
        for paths in case_paths:
            output_path = Path(paths['output_path'])
            actual_outputs.append(
                json.loads(output_path.read_text(encoding='utf-8'))
                if output_path.exists() else None
            )
        metrics = support.summarize_samples(samples, wall, first['cpu_seconds'])
        metrics['process_count'] = max(
            (row.get('child_process_count', 0) + 1 for row in samples), default=1
        )
        return document, call_results, actual_outputs, metrics, case_paths


def expect_batch_rejection(binary: Path, capability: str, dispatch_alias: str,
                           expected_diagnostic: str, argument: dict, *,
                           authorized_identity: str | None = None,
                           artifact_path: Path = ARTIFACT_PATH) -> dict:
    plan = [{'provider_mode': 'success', 'argument': argument}]
    try:
        sample_runner_batch(
            binary, capability, dispatch_alias, plan,
            authorized_identity=authorized_identity,
            artifact_path=artifact_path,
        )
    except AssertionError as error:
        if expected_diagnostic not in str(error):
            raise AssertionError(
                f'runner rejected for an unexpected reason; expected '
                f'{expected_diagnostic!r}: {error}'
            ) from error
        return {'rejected': True, 'diagnostic': expected_diagnostic}
    raise AssertionError(f'runner accepted input that should fail: {expected_diagnostic}')


def reject_artifact_identity(binary: Path, capability: str,
                             dispatch_alias: str) -> dict:
    with tempfile.TemporaryDirectory(prefix='native-digest-app-bad-artifact-') as directory:
        artifact_path = Path(directory) / 'bad-artifact.json'
        artifact = json.loads(ARTIFACT_PATH.read_text(encoding='utf-8'))
        artifact['artifact_id'] = 'sha256:' + '0' * 64
        artifact_path.write_text(
            json.dumps(artifact, sort_keys=True, separators=(',', ':')),
            encoding='utf-8',
        )
        process = subprocess.run(
            [
                str(binary), str(artifact_path), capability, dispatch_alias,
                'batch', str(Path(directory) / 'unused-manifest.json'),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if (process.returncode == 0
                or not any(fragment in process.stderr for fragment in (
                    'artifact identity mismatch', 'IdentityMismatch',
                ))):
            raise AssertionError(
                f'VM accepted or misdiagnosed a tampered artifact identity: '
                f'rc={process.returncode}, stderr={process.stderr[-1200:]}'
            )
        return {'rejected': True, 'diagnostic': 'IdentityMismatch'}


def main() -> None:
    global current_interface
    if not support.PROBE.is_file() or not RUNNER_SOURCE.is_file():
        raise SystemExit('pinned Stage-0 probe or VM runner source is missing')
    if not FIXTURE_PATH.is_file():
        raise SystemExit(f'pure digest reference evidence missing: {FIXTURE_PATH}')
    root = ROOT_SOURCE_PATH.read_text(encoding='utf-8')
    modules = {name: path.read_text(encoding='utf-8') for name, path in MODULE_PATHS.items()}
    fixture = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    arguments = fixture['execution']['inputs']
    request = compile_request(root, modules)
    cache_root = ROOT / '.build/native-app-readiness'
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='digest-app-cold-', dir=cache_root) as cache:
        probe = support.Probe(Path(cache))
        try:
            built, build_metrics = probe.send({'compile_application': request}, timeout=90)
            current_interface = built.get('entry', {}).get('interface', {})
            if (built.get('schema_version') != 'mncs.compiler.application-build/4'
                    or built.get('status') != 'verified'
                    or built.get('unresolved_obligations') != []
                    or current_interface.get('status') != 'verified'):
                raise AssertionError(json.dumps({
                    'status': built.get('status'),
                    'entry': built.get('entry'),
                    'unresolved_obligations': built.get('unresolved_obligations'),
                    'diagnostics': built.get('diagnostics'),
                }, indent=2)[:9000])
            signature = current_interface.get('signature', {})
            effects = sorted((row.get('kind'), row.get('capability'))
                             for row in signature.get('effects', []))
            if effects != [
                ('structured_read', 'test_artifact'),
                ('structured_write', 'test_artifact'),
            ]:
                raise AssertionError(f'entry effects differ from contract: {effects}')
            artifact = built['artifact']['value']
            artifact_id = built['artifact']['identity']
            if artifact.get('artifact_id') != artifact_id:
                raise AssertionError('artifact identity does not match compiler response')
            required_capabilities = artifact.get('requirements', {}).get('capabilities', [])
            if len(required_capabilities) != 1:
                raise AssertionError(f'expected one resolved capability identity: {required_capabilities}')
            capability = required_capabilities[0]
            dispatch_aliases = sorted({
                row.get('capability') for row in signature.get('effects', [])
                if row.get('capability')
            })
            if dispatch_aliases != ['test_artifact']:
                raise AssertionError(f'expected one source dispatch alias: {dispatch_aliases}')
            capability_dispatch = built.get('capability_dispatch', {})
            dispatch_rows = capability_dispatch.get('bindings', [])
            matching_dispatch_rows = [
                row for row in dispatch_rows
                if row.get('capability_identity') == capability
                and row.get('runtime_key_kind') == 'source-alias'
            ]
            if (
                capability_dispatch.get('schema_version')
                != 'mncs.compiler.capability-dispatch/1'
                or capability_dispatch.get('status') != 'verified'
                or sorted(row.get('operation') for row in matching_dispatch_rows)
                != ['structured_read', 'structured_write']
                or {row.get('runtime_capability_key') for row in matching_dispatch_rows}
                != set(dispatch_aliases)
                or any(
                    not row.get('instruction_identity')
                    or not row.get('function_identity')
                    for row in matching_dispatch_rows
                )
            ):
                raise AssertionError(
                    f'compiler capability dispatch contract is incomplete: {capability_dispatch}'
                )
            dispatch_alias = dispatch_aliases[0]
            if built.get('cache', {}).get('status') != 'miss':
                raise AssertionError('cold application build unexpectedly reused a cache entry')
            if current_interface.get('types', {}).get('status') != 'verified':
                raise AssertionError('application callable interface is unresolved')
            APP_ARTIFACT_BYTES = json.dumps(
                artifact, sort_keys=True, separators=(',', ':')
            ).encode()
            application_closure_refs = [
                ref for ref in artifact.get('source', {}).get('lowering_refs', [])
                if isinstance(ref, str) and ref.startswith('application-ssa-closure/1:')
            ]
            if len(application_closure_refs) != 1:
                raise AssertionError(
                    'application artifact must identify exactly one projected SSA closure'
                )
            ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
            ARTIFACT_PATH.write_bytes(APP_ARTIFACT_BYTES)
            calls = [oracle_call(argument) for argument in arguments]
            oracle, oracle_metrics = probe.send({'project_oracle': {
                'root': root, 'modules': modules, 'calls': calls,
            }}, timeout=90)
            if (not oracle.get('valid')
                    or any(row.get('status') != 'returned'
                           for row in oracle.get('executions', []))):
                raise AssertionError(json.dumps({
                    'valid': oracle.get('valid'),
                    'diagnostics': oracle.get('diagnostics'),
                    'executions': oracle.get('executions'),
                }, indent=2)[:9000])
            expected_wires = [row['returned'][0] for row in oracle['executions']]
            expected = [support.decode_wire(value) for value in expected_wires]
        finally:
            probe.close()

    binary, runner_sha, runner_lock_sha = build_runner()
    batch_plans = [
        {'case': 'success-0', 'provider_mode': 'success',
         'argument': arguments[0], 'oracle_index': 0},
        {'case': 'missing-capability', 'provider_mode': 'none',
         'argument': arguments[0]},
        {'case': 'invalid-typed-input', 'provider_mode': 'success',
         'argument': {'integer': {
             'type': {'bits': 64, 'signed': False}, 'value': 7,
         }}},
        {'case': 'write-failure', 'provider_mode': 'fail-write',
         'argument': arguments[0]},
        {'case': 'step-budget-interruption', 'provider_mode': 'success',
         'argument': arguments[0], 'step_budget': 1},
    ]
    batch_plans.extend(
        {'case': f'success-{index}', 'provider_mode': 'success',
         'argument': argument, 'oracle_index': index}
        for index, argument in enumerate(arguments[1:], start=1)
    )
    batch_document, batch_results, batch_outputs, batch_metrics, batch_paths = sample_runner_batch(
        binary, capability, dispatch_alias, batch_plans
    )
    runtime_ids = {row.get('record', {}).get('runtime_id') for row in batch_results}
    if (len(runtime_ids) != 1
            or None in runtime_ids
            or batch_document.get('runtime_id') not in runtime_ids
            or batch_document.get('artifact_id') != artifact_id
            or batch_metrics.get('process_count') != 1
            or len({path['input_path'] for path in batch_paths}) != len(batch_paths)
            or len({path['output_path'] for path in batch_paths}) != len(batch_paths)
            or len({path['request_path'] for path in batch_paths}) != len(batch_paths)):
        raise AssertionError('application batch did not retain one VM session with isolated call paths')

    mismatched_capability = expect_batch_rejection(
        binary, capability, dispatch_alias,
        'artifact does not require authorized capability', arguments[0],
        authorized_identity=capability + ':mismatch',
    )
    mismatched_alias = expect_batch_rejection(
        binary, capability, dispatch_alias + ':mismatch',
        'application contract dispatch alias differs from admitted SSA', arguments[0],
    )
    mismatched_artifact = reject_artifact_identity(
        binary, capability, dispatch_alias
    )

    success_cases = []
    for batch_index, plan in enumerate(batch_plans):
        if 'oracle_index' not in plan:
            continue
        index = plan['oracle_index']
        argument = plan['argument']
        document = batch_results[batch_index]
        returned = [support.decode_wire(value)
                    for value in document.get('returned_wire', [])]
        actual_wire = batch_outputs[batch_index]
        record = document.get('record', {})
        capability_binding = document.get('capability_binding', {})
        operations = [row.get('request', {}).get('operation')
                      for row in record.get('effects', [])]
        effect_statuses = [row.get('outcome_tag')
                           for row in record.get('effects', [])]
        if (document.get('outcome', {}).get('kind') != 'completed'
                or record.get('artifact_id') != artifact_id
                or record.get('admitted_capabilities') != dispatch_aliases
                or capability_binding.get('authorized_capability_identity') != capability
                or capability_binding.get('runtime_dispatch_alias') not in dispatch_aliases
                or capability_binding.get('contract_dispatch_alias') != dispatch_alias
                or capability_binding.get('mapped_host_call_count', 0) < 1
                or operations != ['structured_read', 'structured_write']
                or effect_statuses != ['completed', 'completed']
                or len(returned) != 1 or returned[0].get('exit_code') != 0
                or actual_wire != expected_wires[index]
                or support.decode_wire(actual_wire) != expected[index]):
            raise AssertionError(json.dumps({
                'outcome': document.get('outcome'),
                'record': record,
                'returned': returned,
                'actual_wire': actual_wire,
                'expected_wire': expected_wires[index],
            }, indent=2)[:12000])
        success_cases.append({
            'case_index': index,
            'input_sha256': sha(json.dumps(
                argument, sort_keys=True, separators=(',', ':')
            ).encode()),
            'output_sha256': sha(json.dumps(
                actual_wire, sort_keys=True, separators=(',', ':')
            ).encode()),
            'application_exit': returned[0],
            'artifact_identity': record['artifact_id'],
            'capability_binding': capability_binding,
            'callable_function': record['callable_function'],
            'return_digest': record['return_digest'],
            'effect_operations': operations,
            'effect_outcomes': effect_statuses,
            'stage0_output_sha256': sha(json.dumps(
                expected_wires[index], sort_keys=True, separators=(',', ':')
            ).encode()),
            'effects_digest': record['effects_digest'],
            'usage': record['usage'],
            'resource_limits': record['resource_limits'],
            'retained_session_call_index': batch_index,
        })

    no_cap_document = batch_results[1]
    no_cap_returned = [support.decode_wire(value)
                       for value in no_cap_document.get('returned_wire', [])]
    no_cap_output = batch_outputs[1]
    no_cap_record = no_cap_document.get('record', {})
    if (no_cap_document.get('outcome', {}).get('kind') != 'capability_denied'
            or no_cap_output is not None
            or no_cap_returned
            or no_cap_record.get('effects', [{}])[0].get('outcome_tag')
            != 'capability_denied'):
        raise AssertionError('missing capability did not fail closed at structured_read')

    invalid_type_document = batch_results[2]
    invalid_type_output = batch_outputs[2]
    if (invalid_type_document.get('outcome', {}).get('kind') == 'completed'
            or invalid_type_output is not None):
        raise AssertionError('application accepted or published output for an invalid typed request')

    fail_document = batch_results[3]
    fail_returned = [support.decode_wire(value)
                     for value in fail_document.get('returned_wire', [])]
    fail_output = batch_outputs[3]
    fail_record = fail_document.get('record', {})
    if (fail_document.get('outcome', {}).get('kind') != 'completed'
            or len(fail_returned) != 1
            or fail_returned[0].get('exit_code') != 3
            or fail_output is not None
            or [row.get('request', {}).get('operation')
                for row in fail_record.get('effects', [])]
            != ['structured_read', 'structured_write']):
        raise AssertionError('native write-failure path did not return typed exit code 3')

    interrupted_document = batch_results[4]
    interrupted_output = batch_outputs[4]
    if (interrupted_document.get('outcome', {}).get('kind') != 'budget_exhausted'
            or interrupted_output is not None):
        raise AssertionError('step-limited application call was not recorded as interrupted')

    sources = [
        {'module': name, 'path': str(path), 'sha256': sha(path.read_bytes())}
        for name, path in sorted(MODULE_PATHS.items())
    ]
    sources.append({
        'module': ENTRY_MODULE,
        'path': str(ROOT_SOURCE_PATH),
        'sha256': sha(ROOT_SOURCE_PATH.read_bytes()),
    })
    producer = {
        'compiler_revision': git(ROOT, 'rev-parse', 'HEAD'),
        'compiler_status': git(ROOT, 'status', '--short'),
        'language_revision': git(LANGUAGE_ROOT, 'rev-parse', 'HEAD'),
        'test_revision': git(TEST_ROOT, 'rev-parse', 'HEAD'),
        'test_status': git(TEST_ROOT, 'status', '--short'),
        'stdlib_revision': git(STDLIB_ROOT, 'rev-parse', 'HEAD'),
        'stdlib_status': git(STDLIB_ROOT, 'status', '--short'),
        'vm_revision': git(VM_ROOT, 'rev-parse', 'HEAD'),
        'vm_status': git(VM_ROOT, 'status', '--short'),
        'vm_binary_sha256': sha(support.VM_BIN.read_bytes()),
        'stage0_probe_sha256': sha(support.PROBE.read_bytes()),
        'stage0_project_oracle_metrics': oracle_metrics,
        'vm_runner_source_sha256': sha(RUNNER_SOURCE.read_bytes()),
        'vm_runner_binary_sha256': runner_sha,
        'vm_runner_lockfile_sha256': runner_lock_sha,
    }
    evidence = {
        'schema_version': 'mncs.compiler.native-application-proof/2',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'objective': 'Execute the real mncs.test.digest application through the verified compiler artifact and canonical VM provider boundary',
        'application': {
            'root_source_id': 'native/mncs/test/digest.mncs',
            'entry': f'{ENTRY_MODULE}::{APP_FUNCTION}',
            'module_sources': sources,
            'target': 'mncs-vm-0.1',
            'required_capability': capability,
            'runtime_dispatch_alias': dispatch_aliases[0],
            'effect_contract': signature.get('effects', []),
            'input_schema': 'mncs.test-digest-request/1',
            'output_schema': 'mncs.test-digest/1',
        },
        'producer': producer,
        'build': {
            'request_schema': request['schema_version'],
            'result_schema': built['schema_version'],
            'status': built['status'],
            'unresolved_obligations': built['unresolved_obligations'],
            'diagnostics': built.get('diagnostics', []),
            'cache': built.get('cache'),
            'metrics': build_metrics,
            'artifact_identity': artifact_id,
            'artifact_file_sha256': sha(APP_ARTIFACT_BYTES),
            'artifact_bytes': len(APP_ARTIFACT_BYTES),
            'application_ssa_provenance': {
                'full_selected_ssa_identity': artifact.get('source', {}).get('selected_ssa_identity'),
                'full_selected_ssa_fingerprint': artifact.get('source', {}).get('selected_ssa_fingerprint'),
                'projected_closure_ref': application_closure_refs[0],
            },
            'artifact_size_comparison': {
                'baseline_artifact_identity': 'sha256:95cf2d3e3361b7089336c82e0cd4870dfe74775547831eed90879555d494f919',
                'baseline_compiler_revision': '24dda63ae59e0505969c490604e14348dab1f7d7',
                'baseline_bytes': 1161949,
                'current_bytes': len(APP_ARTIFACT_BYTES),
                'reduction_bytes': 1161949 - len(APP_ARTIFACT_BYTES),
                'reduction_percent': round((1161949 - len(APP_ARTIFACT_BYTES)) * 100 / 1161949, 4),
                'comparison': 'same selected mncs-test digest_app source snapshot and mncs-vm-0.1 target; closure projection added to the application emitter',
            },
            'callable_interface': current_interface,
            'callable_interface_identity': current_interface.get('identity'),
            'capability_dispatch': capability_dispatch,
        },
        'execution': {
            'stage0_result_sha256': [
                sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())
                for value in expected_wires
            ],
            'canonical_vm_cases': success_cases,
            'exact_result_agreement': True,
            'retained_session_batch': {
                'schema_version': batch_document['schema_version'],
                'artifact_identity': artifact_id,
                'runtime_identity': batch_document['runtime_id'],
                'admitted_artifact_count': 1,
                'calls_in_one_process': len(batch_results),
                'unique_request_input_output_paths': True,
                'call_order': [plan['case'] for plan in batch_plans],
                'per_call_resource_usage': [
                    row.get('record', {}).get('usage') for row in batch_results
                ],
                'host_metrics': batch_metrics,
            },
            'capability_admission': {
                'authorized_capability_identity': capability,
                'runtime_dispatch_alias': dispatch_aliases[0],
                'mapping_source': 'mncs.compiler.capability-dispatch/1, cross-checked against admitted SSA',
                'mapped_host_call_count': success_cases[0]['capability_binding']['mapped_host_call_count'],
            },
            'missing_capability': {
                'session_call_index': 1,
                'outcome': no_cap_document.get('outcome'),
                'effects': effect_summaries(no_cap_record),
                'artifact_identity': no_cap_record.get('artifact_id'),
                'runtime_identity': no_cap_record.get('runtime_id'),
                'output_published': no_cap_output is not None,
            },
            'invalid_typed_input': {
                'session_call_index': 2,
                'outcome': invalid_type_document.get('outcome'),
                'effects': effect_summaries(invalid_type_document.get('record', {})),
                'artifact_identity': invalid_type_document.get('record', {}).get('artifact_id'),
                'output_published': invalid_type_output is not None,
            },
            'structured_write_failure': {
                'session_call_index': 3,
                'outcome': fail_document.get('outcome'),
                'application_exit_code': fail_returned[0].get('exit_code'),
                'effects': effect_summaries(fail_record),
                'runtime_identity': fail_record.get('runtime_id'),
                'output_published': fail_output is not None,
            },
            'step_budget_interruption': {
                'session_call_index': 4,
                'outcome': interrupted_document.get('outcome'),
                'effects': effect_summaries(interrupted_document.get('record', {})),
                'output_published': interrupted_output is not None,
            },
            'mismatched_capability_identity': mismatched_capability,
            'mismatched_dispatch_alias': mismatched_alias,
            'incorrect_artifact_identity': mismatched_artifact,
        },
        'feature_evidence': [
            'real mncs-test source module with three imported modules',
            'nominal structured request and result with bounded nested sequences',
            'native pass/fail/skip aggregation and bounded first-failure selection',
            'explicit structured_read and structured_write effects through the VM Provider API',
            'canonical artifact capability identity maps to its exact HostCall runtime alias; missing provider binding fails closed before file access',
            'structured write rejection returns the application typed failure code',
            'one admitted application artifact and retained VM session serve repeated isolated calls, including calls after provider failures',
            'missing and mismatched capability identities, mismatched dispatch aliases, and a tampered artifact identity fail closed',
            'invalid typed input and an exhausted per-call step budget publish no result, while later calls in the same retained session still succeed',
        ],
        'limitations': [
            'The file provider is a bounded test adapter for the canonical VM Provider API; it is not a production MNCS service provider.',
            'The pure digest body is compared to Stage-0; this does not prove full mncs-test command parity or compiler succession.',
            'The provider harness is a separate Rust test program in the Compiler repository; it performs bounded structured file transport and validates the capability mapping from admitted SSA.',
            'This proof does not execute through Forge, Environment, or canonical Store; the selected Environment session exposes no Forge application-execution binding.',
        ],
    }
    EVIDENCE_PATH.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n',
                             encoding='utf-8')
    print(json.dumps({
        'status': 'passed',
        'evidence': str(EVIDENCE_PATH),
        'artifact_identity': artifact_id,
        'interface_identity': current_interface.get('identity'),
        'build_metrics': build_metrics,
        'case_count': len(success_cases),
        'retained_session_batch_metrics': batch_metrics,
        'negative_outcomes': [
            no_cap_document.get('outcome'),
            {'outcome': fail_document.get('outcome'),
             'application_exit': fail_returned[0].get('exit_code')},
            invalid_type_document.get('outcome'),
            interrupted_document.get('outcome'),
            mismatched_capability,
            mismatched_alias,
            mismatched_artifact,
        ],
    }, separators=(',', ':')))


if __name__ == '__main__':
    main()
