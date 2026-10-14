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
FIXTURE_PATH = ROOT / 'evidence/native-app-readiness-test-digest.json'
ARTIFACT_PATH = ROOT / '.build/native-app-readiness/test-digest-app.vm.json'
RUNNER_SOURCE = ROOT / 'tools/native_app_vm_runner.rs'
RUNNER_MANIFEST = ROOT / '.build/native-app-readiness/vm-capability-runner/Cargo.toml'
RUNNER_TARGET = ROOT / '.build/native-app-readiness/vm-capability-target'
EVIDENCE_PATH = ROOT / 'evidence/native-app-readiness-test-digest-app.json'
ENTRY_MODULE = 'mncs.test.digest'
APP_FUNCTION = 'digest_app'
PURE_FUNCTION = 'build_digest'

sys.path.insert(0, str(ROOT / 'tools'))
import test_vm_native_application as support


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    lockfile = RUNNER_MANIFEST.parent / 'Cargo.lock'
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


def call_request(context: dict) -> dict:
    return {
        'schema_version': '0.1',
        'target': {'module': ENTRY_MODULE, 'function': APP_FUNCTION},
        'arguments': [context],
        'type_arguments': [],
        'step_budget': 3_000_000,
    }


def sample_runner(binary: Path, capability: str, dispatch_alias: str, mode: str,
                  argument: dict) -> tuple[dict, dict, dict | None, dict]:
    with tempfile.TemporaryDirectory(prefix='native-digest-app-') as directory:
        temp = Path(directory)
        input_path = temp / 'request.json'
        output_path = temp / 'result.json'
        request_path = temp / 'execution.json'
        input_path.write_text(json.dumps(argument, sort_keys=True, separators=(',', ':')),
                              encoding='utf-8')
        context = application_context(
            current_interface, input_path, output_path, temp
        )
        request_path.write_text(
            json.dumps(call_request(context), sort_keys=True, separators=(',', ':')),
            encoding='utf-8',
        )
        command = [
            str(binary), str(ARTIFACT_PATH), str(request_path), str(input_path),
            str(output_path), capability, dispatch_alias, mode,
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
            stdout, stderr = process.communicate(timeout=45)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            watcher.join(timeout=1)
            raise TimeoutError('canonical VM capability execution exceeded 45 seconds')
        wall = time.monotonic() - started
        watcher.join(timeout=1)
        samples.append(support.process_sample(process.pid))
        if process.returncode:
            raise AssertionError(f'VM runner exited {process.returncode}: {stderr[-3000:]}')
        document = json.loads(stdout)
        returned = [support.decode_wire(value)
                    for value in document.get('returned_wire', [])]
        actual_wire = None
        if output_path.exists():
            actual_wire = json.loads(output_path.read_text(encoding='utf-8'))
        metrics = support.summarize_samples(samples, wall, first['cpu_seconds'])
        metrics['process_count'] = max(
            (row.get('child_process_count', 0) + 1 for row in samples), default=1
        )
        return document, returned, actual_wire, metrics


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
    success_cases = []
    for index, argument in enumerate(arguments):
        document, returned, actual_wire, metrics = sample_runner(
            binary, capability, dispatch_alias, 'success', argument
        )
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
            'result': expected[index],
            'application_exit': returned[0],
            'artifact_identity': record['artifact_id'],
            'capability_binding': capability_binding,
            'callable_function': record['callable_function'],
            'return_digest': record['return_digest'],
            'effect_operations': operations,
            'effect_outcomes': effect_statuses,
            'effects_digest': record['effects_digest'],
            'usage': record['usage'],
            'resource_limits': record['resource_limits'],
            'metrics': metrics,
        })

    no_cap_document, no_cap_returned, no_cap_output, no_cap_metrics = sample_runner(
        binary, capability, dispatch_alias, 'none', arguments[0]
    )
    no_cap_record = no_cap_document.get('record', {})
    if (no_cap_document.get('outcome', {}).get('kind') != 'capability_denied'
            or no_cap_output is not None
            or no_cap_returned
            or no_cap_record.get('effects', [{}])[0].get('outcome_tag')
            != 'capability_denied'):
        raise AssertionError('missing capability did not fail closed at structured_read')

    fail_document, fail_returned, fail_output, fail_metrics = sample_runner(
        binary, capability, dispatch_alias, 'fail-write', arguments[0]
    )
    fail_record = fail_document.get('record', {})
    if (fail_document.get('outcome', {}).get('kind') != 'completed'
            or len(fail_returned) != 1
            or fail_returned[0].get('exit_code') != 3
            or fail_output is not None
            or [row.get('request', {}).get('operation')
                for row in fail_record.get('effects', [])]
            != ['structured_read', 'structured_write']):
        raise AssertionError('native write-failure path did not return typed exit code 3')

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
        'schema_version': 'mncs.compiler.native-application-proof/1',
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
            'stage0_results': expected,
            'canonical_vm_cases': success_cases,
            'exact_result_agreement': True,
            'capability_admission': {
                'authorized_capability_identity': capability,
                'runtime_dispatch_alias': dispatch_aliases[0],
                'mapping_source': 'mncs.compiler.capability-dispatch/1, cross-checked against admitted SSA',
                'mapped_host_call_count': success_cases[0]['capability_binding']['mapped_host_call_count'],
            },
            'missing_capability': {
                'outcome': no_cap_document.get('outcome'),
                'effects': no_cap_record.get('effects'),
                'artifact_identity': no_cap_record.get('artifact_id'),
                'output_published': no_cap_output is not None,
                'metrics': no_cap_metrics,
            },
            'structured_write_failure': {
                'outcome': fail_document.get('outcome'),
                'application_exit': fail_returned,
                'effects': fail_record.get('effects'),
                'output_published': fail_output is not None,
                'metrics': fail_metrics,
            },
        },
        'feature_evidence': [
            'real mncs-test source module with three imported modules',
            'nominal structured request and result with bounded nested sequences',
            'native pass/fail/skip aggregation and bounded first-failure selection',
            'explicit structured_read and structured_write effects through the VM Provider API',
            'canonical artifact capability identity maps to its exact HostCall runtime alias; missing provider binding fails closed before file access',
            'structured write rejection returns the application typed failure code',
        ],
        'limitations': [
            'The file provider is a bounded test adapter for the canonical VM Provider API; it is not a production MNCS service provider.',
            'The pure digest body is compared to Stage-0; this does not prove full mncs-test command parity or compiler succession.',
            'The provider harness is a separate Rust test program in the Compiler repository; it performs only structured file transport and validates the single app capability mapping from admitted SSA. A production launcher still needs to apply the same verified mapping.',
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
        'cases': [
            {'index': row['case_index'], 'wall_seconds': row['metrics']['wall_seconds'],
             'cpu_seconds': row['metrics']['cpu_seconds'],
             'peak_rss_bytes': row['metrics']['peak_rss_bytes']}
            for row in success_cases
        ],
        'negative_outcomes': [
            no_cap_document.get('outcome'),
            {'outcome': fail_document.get('outcome'),
             'application_exit': fail_returned[0].get('exit_code')},
        ],
    }, separators=(',', ':')))


if __name__ == '__main__':
    main()
