#!/usr/bin/env python3
"""Gate A: compile and run a real multi-module mncs-test application.

The source files are consumed read-only from the selected mncs-test checkout.
Stage-0's project oracle and the canonical VM execute the same MNCS entry;
Python only transports requests, compares returned values, and records process
measurements and provenance.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = Path('/workspace/mncs-test')
VM_BIN = ROOT / '.build/native-app-readiness/mncs-vm'
PROBE = ROOT / '.build/native-app-readiness/mncs-compiler-stage0-probe'
EVIDENCE = ROOT / 'evidence/native-app-readiness-gate-a.json'
ARTIFACT_PATH = ROOT / '.build/native-app-readiness/native-test-readiness.vm.json'
APP_MODULE = 'tests.native_readiness'
APP_SOURCE_ID = 'tests/native_readiness.mncs'
MODULE_FILES = {
    'mncs.test.assertions': TEST_ROOT / 'native/mncs/test/assertions.mncs',
    'mncs.test.suite': TEST_ROOT / 'native/mncs/test/suite.mncs',
    'mncs.test.generative': TEST_ROOT / 'native/mncs/test/generative.mncs',
    'mncs.test.snapshot': TEST_ROOT / 'native/mncs/test/snapshot.mncs',
}

APP_SOURCE = '''mncs 0.18;
module tests.native_readiness;

use mncs.test.assertions;
use mncs.test.suite;
use mncs.test.generative;
use mncs.test.snapshot;

enum ReadinessState { Ready { checks: u64 }, Deferred { reason: u64 } }
enum ReadinessError {
    InvalidCaseIndex { found: u64 },
    InvalidTestCount { expected: u64, found: u64 }
}
record ReadinessRequest { seed: u64, case_index: u64, expected_tests: u64 }

record ReadinessReport {
    state: ReadinessState,
    state_value: u64,
    summary: SuiteSummary,
    property_case: PropertyCase,
    witness: Witness,
    suite_projection: SuiteProjection,
    test_projection: TestProjection
}

enum ReadinessOutcome {
    Completed { report: ReadinessReport },
    Rejected { error: ReadinessError }
}

fn run(request: ReadinessRequest) -> (result: ReadinessOutcome) {
    if request.case_index > 64 {
        return ReadinessOutcome.Rejected {
            error: ReadinessError.InvalidCaseIndex { found: request.case_index }
        };
    }
    if request.expected_tests != 4 {
        return ReadinessOutcome.Rejected {
            error: ReadinessError.InvalidTestCount { expected: 4, found: request.expected_tests }
        };
    }
    let arithmetic: TestResult = from_assertion(equals_i64(42, 19 +% 23, 1001));
    let generated: PropertyCase = draw_case(request.seed, request.case_index);
    let property_test: TestResult = from_assertion(equals_i64(58, replay(request.seed, request.case_index, 97) as i64, 1004));
    let byte_test: TestResult = from_assertion(equals_bytes<4>([1, 2, 3, 4], [1, 2, 3, 4], 1008));
    let skipped: TestResult = skip(1006);
    let tests: [TestResult; 4] = [arithmetic, property_test, byte_test, skipped];
    let summary: SuiteSummary = summarize<4>(tests);
    let witness: Witness = bytes([1, 2, 3, 4]);
    let suite_projection: SuiteProjection = project(summary);
    let test_projection: TestProjection = project_test(arithmetic);
    let state: ReadinessState = match summary.verdict {
        PASS => ReadinessState.Ready { checks: summary.total },
        FAIL => ReadinessState.Deferred { reason: summary.verdict_code },
        SKIP => ReadinessState.Ready { checks: summary.total },
        UNSUPPORTED => ReadinessState.Deferred { reason: summary.verdict_code }
    };
    let state_value: u64 = match state {
        Ready { checks } => checks,
        Deferred { reason } => reason
    };
    let report: ReadinessReport = ReadinessReport {
        state: state,
        state_value: state_value,
        summary: summary,
        property_case: generated,
        witness: witness,
        suite_projection: suite_projection,
        test_projection: test_projection
    };
    return ReadinessOutcome.Completed { report: report };
}'''


def sha(data: bytes) -> str:
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(['git', *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def process_sample(pid: int) -> dict:
    root = Path(f'/proc/{pid}')
    out = {'rss_bytes': 0, 'rss_high_water_bytes': 0, 'threads': 0,
           'fd_count': 0, 'child_process_count': 0, 'cpu_seconds': 0.0,
           'cgroup_memory_current_bytes': None, 'cgroup_memory_peak_bytes': None}
    try:
        status = {}
        for line in (root / 'status').read_text().splitlines():
            if ':' in line:
                key, value = line.split(':', 1)
                status[key] = value.strip()
        out['rss_bytes'] = int(status.get('VmRSS', '0 kB').split()[0]) * 1024
        out['rss_high_water_bytes'] = int(status.get('VmHWM', '0 kB').split()[0]) * 1024
        out['threads'] = int(status.get('Threads', '0'))
        out['fd_count'] = len(list((root / 'fd').iterdir()))
        children = (root / 'task' / str(pid) / 'children').read_text().split()
        out['child_process_count'] = len(children)
        stat = (root / 'stat').read_text()
        fields = stat[stat.rfind(')') + 2:].split()
        ticks = int(fields[11]) + int(fields[12])
        out['cpu_seconds'] = ticks / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError):
        pass
    for name, key in [('memory.current', 'cgroup_memory_current_bytes'),
                      ('memory.peak', 'cgroup_memory_peak_bytes')]:
        try:
            out[key] = int(Path('/sys/fs/cgroup', name).read_text().strip())
        except (OSError, ValueError):
            pass
    return out


def summarize_samples(samples: list[dict], wall_seconds: float, cpu_start: float) -> dict:
    rows = samples or [{}]
    cpu_end = max((row.get('cpu_seconds', 0.0) for row in rows), default=cpu_start)
    cgroup_peaks = [row.get('cgroup_memory_peak_bytes') for row in rows
                    if row.get('cgroup_memory_peak_bytes') is not None]
    return {
        'wall_seconds': round(wall_seconds, 6),
        'cpu_seconds': round(max(0.0, cpu_end - cpu_start), 6),
        'peak_rss_bytes': max((row.get('rss_high_water_bytes') or row.get('rss_bytes') or 0
                               for row in rows), default=0),
        'peak_cgroup_memory_bytes': max(cgroup_peaks) if cgroup_peaks else None,
        'peak_open_file_descriptors': max((row.get('fd_count') or 0 for row in rows), default=0),
        'peak_threads': max((row.get('threads') or 0 for row in rows), default=0),
        'peak_child_processes': max((row.get('child_process_count') or 0 for row in rows), default=0),
    }


class Probe:
    def __init__(self, cache_dir: Path | None = None):
        env = os.environ.copy()
        env['MNCS_PROBE_MODULES'] = ''
        env['MNCS_PROBE_EXECUTION_MODULES'] = ''
        env['MNCS_PROBE_GENERIC_SEEDS'] = '[]'
        env.pop('MNCS_PROBE_BACKEND', None)
        env['MNCS_PROBE_CACHE_DIR'] = str(cache_dir or ROOT / '.build/native-app-readiness/probe-cache')
        env['MNCS_PROBE_TOOLCHAIN_IDENTITY_ROOT'] = str(ROOT)
        self.stderr = tempfile.TemporaryFile(mode='w+t')
        self.proc = subprocess.Popen([str(PROBE)], cwd=ROOT, env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=self.stderr, text=True, bufsize=1)
        self.samples: list[dict] = []
        self.request_count = 0

    def send(self, request: dict, timeout: float = 90.0) -> tuple[dict, dict]:
        assert self.proc.stdin and self.proc.stdout
        self.samples = []
        before = process_sample(self.proc.pid)
        started = time.monotonic()
        self.proc.stdin.write(json.dumps(request, separators=(',', ':')) + '\n')
        self.proc.stdin.flush()
        deadline = started + timeout
        line = None
        while time.monotonic() < deadline:
            self.samples.append(process_sample(self.proc.pid))
            ready, _, _ = select.select([self.proc.stdout], [], [], 0.02)
            if ready:
                line = self.proc.stdout.readline()
                break
            if self.proc.poll() is not None:
                break
        wall = time.monotonic() - started
        if not line:
            raise RuntimeError(f'Stage-0 probe ended without a response; rc={self.proc.poll()}')
        self.request_count += 1
        metrics = summarize_samples(self.samples, wall, before['cpu_seconds'])
        metrics['request_sequence'] = self.request_count
        return json.loads(line), metrics

    def close(self):
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        rc = self.proc.wait(timeout=10)
        self.stderr.seek(0)
        tail = self.stderr.read()[-4000:]
        self.stderr.close()
        if rc != 0:
            raise RuntimeError(f'Stage-0 probe exit {rc}: {tail}')
        return tail


def vm_to_wire(value: dict) -> dict:
    if 'Integer' in value:
        v = value['Integer']
        return {'integer': {'type': {'bits': v['bits'], 'signed': v['signed']}, 'value': v['value']}}
    if 'Boolean' in value:
        return {'boolean': {'value': value['Boolean']}}
    if 'Byte' in value:
        return {'byte': {'value': value['Byte']}}
    if 'Sequence' in value:
        return {'sequence': {'values': [vm_to_wire(v) for v in value['Sequence']['elements']]}}
    if 'Record' in value:
        return {'record': {'fields': [[k, vm_to_wire(v)] for k, v in value['Record']['fields']]}}
    if 'Finite' in value:
        v = value['Finite']
        return {'finite': {'discriminant': v['discriminant'],
                           'payload': [[k, vm_to_wire(x)] for k, x in v.get('payload', [])]}}
    raise AssertionError(f'unrecognized VM value: {value}')


def decode_wire(value):
    if 'record' in value:
        return {key: decode_wire(item) for key, item in value['record']['fields']}
    if 'finite' in value:
        node = value['finite']
        return {'$v': node['discriminant'],
                '$p': {key: decode_wire(item) for key, item in node.get('payload', [])}}
    if 'sequence' in value:
        return [decode_wire(item) for item in value['sequence']['values']]
    if 'integer' in value:
        return value['integer']['value']
    if 'boolean' in value:
        return value['boolean']['value']
    if 'byte' in value:
        return value['byte']['value']
    raise AssertionError(f'unrecognized oracle value: {value}')


def summarize_report(outcome: dict) -> dict:
    report = outcome['$p']['report']
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


def readiness_request(seed: int, case_index: int, expected_tests: int,
                      identity: str | None = None) -> dict:
    if identity is None:
        identity = ('mncs:0.2:record-type:tests.native_readiness::ReadinessRequest::'
                    'case_index%3Au64%3Bexpected_tests%3Au64%3Bseed%3Au64%3B')
    return {'record': {
        'type_identity': identity,
        'name': 'ReadinessRequest',
        'fields': [
            ['case_index', {'integer': {'type': {'bits': 64, 'signed': False}, 'value': case_index}}],
            ['expected_tests', {'integer': {'type': {'bits': 64, 'signed': False}, 'value': expected_tests}}],
            ['seed', {'integer': {'type': {'bits': 64, 'signed': False}, 'value': seed}}],
        ],
    }}


def call_request(arguments: list[dict] | None = None) -> dict:
    return {
        'schema_version': '0.1',
        'target': {'module': APP_MODULE, 'function': 'run'},
        'arguments': arguments if arguments is not None else [readiness_request(42, 7, 4)],
        'type_arguments': [],
        'step_budget': 2000000,
    }


def variant_discriminant(interface: dict, type_name: str, variant_name: str) -> int:
    finite = next(row for row in interface['types']['finite']
                  if row.get('name') == type_name)
    variant = next(row for row in finite['variants']
                   if row.get('name') == variant_name)
    return variant['discriminant']


def run_vm(artifact: dict, arguments: list[dict] | None = None) -> tuple[dict, dict]:
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT_PATH.write_text(json.dumps(artifact, sort_keys=True, separators=(',', ':')))
    with tempfile.TemporaryDirectory(prefix='mncs-native-readiness-') as tmp:
        args = Path(tmp) / 'args.json'
        type_args = Path(tmp) / 'type-args.json'
        args.write_text(json.dumps(arguments if arguments is not None else
                                    [readiness_request(42, 7, 4)], separators=(',', ':')))
        type_args.write_text('[]')
        command = [str(VM_BIN), 'run', '--artifact', str(ARTIFACT_PATH),
                   '--callable', f'{APP_MODULE}::run', '--args', str(args),
                   '--type-args', str(type_args)]
        proc = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)
        first = process_sample(proc.pid)
        samples = [first]
        def watch():
            while proc.poll() is None:
                samples.append(process_sample(proc.pid))
                time.sleep(0.01)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        started = time.monotonic()
        stdout, stderr = proc.communicate(timeout=45)
        wall = time.monotonic() - started
        watcher.join(timeout=1)
    if proc.returncode != 0:
        raise AssertionError(f'canonical VM failed ({proc.returncode}): {stderr[-4000:]}')
    document = json.loads(stdout)
    measurements = summarize_samples(samples, wall, first['cpu_seconds'])
    measurements['process_count'] = max((r.get('child_process_count', 0) + 1 for r in samples), default=1)
    measurements['stderr'] = stderr[-1000:]
    return document, measurements


def main() -> None:
    if not PROBE.is_file() or not VM_BIN.is_file():
        raise SystemExit(f'missing pinned probe or VM binary: {PROBE}, {VM_BIN}')
    modules = {name: path.read_text() for name, path in MODULE_FILES.items()}
    module_sources = {name: source for name, source in modules.items()}
    request = {
        'schema_version': 'mncs.compiler.application-build-request/1',
        'target': 'mncs-vm-0.1',
        'root_source_id': APP_SOURCE_ID,
        'root_source': APP_SOURCE,
        'entry': {'module': APP_MODULE, 'function': 'run'},
        'modules': module_sources,
        'generic_seeds': [
            {'module': 'mncs.test.assertions', 'function': 'equals_bytes',
             'type_arguments': [{'kind': 'nat', 'value': 4}]},
            {'module': 'mncs.test.suite', 'function': 'summarize',
             'type_arguments': [{'kind': 'nat', 'value': 4}]},
        ],
        'capabilities': [],
        'verification_policy': 'stage0-semantic-check+canonical-vm-artifact/1',
    }
    call = call_request()
    cache_dir = ROOT / '.build/native-app-readiness/gate-a-cache'
    for cache_name in ('application-builds', 'application-build-indexes'):
        cache_entries = cache_dir / cache_name
        cache_entries.mkdir(parents=True, exist_ok=True)
        for path in cache_entries.iterdir():
            if path.is_file() and (path.suffix == '.json' or path.name.endswith('.tmp')):
                path.unlink()
    probe = Probe(cache_dir)
    try:
        built, build_metrics = probe.send({'compile_application': request})
        if built.get('entry', {}).get('status') != 'verified':
            raise AssertionError('application entry was not verified: ' + json.dumps({
                'status': built.get('status'), 'entry': built.get('entry'),
                'obligations': built.get('unresolved_obligations'),
                'diagnostics': built.get('diagnostics')}, indent=2))
        artifact = built.get('artifact', {}).get('value')
        if not isinstance(artifact, dict):
            raise AssertionError('no canonical VM artifact: ' + json.dumps(built, indent=2)[:8000])
        if built.get('schema_version') != 'mncs.compiler.application-build/3':
            raise AssertionError('application producer contract did not advance to /3')
        callable_interface = built.get('entry', {}).get('interface')
        if not isinstance(callable_interface, dict):
            raise AssertionError('verified entry is missing its typed callable interface')
        if (callable_interface.get('status') != 'verified'
                or callable_interface.get('types', {}).get('status') != 'verified'
                or callable_interface.get('types', {}).get('unresolved_references') != []):
            raise AssertionError('callable interface has unresolved type references')
        request_type = next((row for row in callable_interface.get('types', {}).get('records', [])
                             if row.get('name') == 'ReadinessRequest'), None)
        if request_type is None:
            raise AssertionError('callable interface omitted the structured request record')
        referenced_record_names = {
            row.get('name') for row in callable_interface['types'].get('records', [])
        }
        if not {
                'ReadinessRequest', 'ReadinessReport', 'PropertyCase',
                'SuiteSummary', 'SuiteProjection', 'TestProjection', 'Witness',
        }.issubset(referenced_record_names):
            raise AssertionError('callable interface omitted a referenced imported record definition')
        referenced_finite_names = {
            row.get('name') for row in callable_interface['types'].get('finite', [])
        }
        if not {
                'ReadinessOutcome', 'ReadinessError', 'ReadinessState',
                'Verdict', 'SuiteClassification', 'SuiteStatus',
                'TestClassification', 'TestStatus', 'FailureKind',
        }.issubset(referenced_finite_names):
            raise AssertionError('callable interface omitted a referenced imported finite type')
        request_identity = request_type.get('identity', {}).get('0') if isinstance(request_type.get('identity'), dict) else request_type.get('identity')
        if not isinstance(request_identity, str):
            raise AssertionError('request record has no semantic identity')
        interface_signature = callable_interface.get('signature', {})
        if interface_signature.get('function_identity') != (
                f'mncs:0.2:function:{APP_MODULE}::run'):
            raise AssertionError('callable interface changed the selected function identity')
        if interface_signature.get('inputs') != [
                {'name': 'request', 'type': 'ReadinessRequest'}]:
            raise AssertionError('callable interface does not describe the structured request')
        if interface_signature.get('outputs') != [
                {'name': 'result', 'type': 'ReadinessOutcome'}]:
            raise AssertionError('callable interface does not describe the typed result')
        interface_digest_input = json.dumps({
            'signature': callable_interface['signature'],
            'types': callable_interface['types'],
        }, sort_keys=True, separators=(',', ':')).encode()
        if callable_interface['identity'] != sha(interface_digest_input):
            raise AssertionError('callable interface identity does not bind its exact signature and types')
        call = call_request([readiness_request(42, 7, 4, request_identity)])
        if call['arguments'][0]['record']['type_identity'] != request_identity:
            raise AssertionError('invocation argument does not bind the producer interface identity')
        oracle, oracle_metrics = probe.send({'project_oracle': {
            'root': APP_SOURCE,
            'modules': module_sources,
            'calls': [call],
        }})
        if not oracle.get('valid'):
            raise AssertionError(json.dumps(oracle.get('diagnostics'), indent=2))
        oracle_exec = oracle['executions'][0]
        if oracle_exec.get('status') != 'returned':
            raise AssertionError(json.dumps(oracle_exec, indent=2))
        failure_calls = [
            call_request([readiness_request(42, 65, 4, request_identity)]),
            call_request([readiness_request(42, 7, 3, request_identity)]),
        ]
        failure_oracle, failure_oracle_metrics = probe.send({'project_oracle': {
            'root': APP_SOURCE,
            'modules': module_sources,
            'calls': failure_calls,
        }})
        if not failure_oracle.get('valid'):
            raise AssertionError(json.dumps(failure_oracle.get('diagnostics'), indent=2))
        failure_oracle_execs = failure_oracle['executions']
        if any(row.get('status') != 'returned' for row in failure_oracle_execs):
            raise AssertionError(json.dumps(failure_oracle_execs, indent=2))
        specialized_callables = artifact.get('callables', [])
        expected_specializations = {
            ('mncs.test.assertions', 'equals_bytes__spec_'),
            ('mncs.test.suite', 'summarize__spec_'),
        }
        missing_specializations = [
            f'{module}::{prefix}' for module, prefix in expected_specializations
            if not any(row.get('module') == module and row.get('name', '').startswith(prefix)
                       for row in specialized_callables)
        ]
        if missing_specializations:
            raise AssertionError('requested generic exports were not emitted: ' + json.dumps({
                'missing': missing_specializations, 'callables': specialized_callables}, indent=2)[:6000])
        expected_unselected = {
            'export mncs.test.assertions::equals_bytes: no ssa instance',
            'export mncs.test.suite::summarize: no ssa instance',
        }
        if (built.get('status') != 'verified'
                or built.get('unresolved_obligations') != []
                or set(built.get('unselected_unresolved_exports', [])) != expected_unselected):
            raise AssertionError('the selected entry scope must be verified while unselected generic templates stay explicit: ' +
                                 json.dumps({'status': built.get('status'),
                                             'obligations': built.get('unresolved_obligations'),
                                             'unselected': built.get('unselected_unresolved_exports')}, indent=2))
        if built.get('callable_scope', {}).get('kind') != 'entry_and_explicit_generic_seeds':
            raise AssertionError('application response omitted its exact callable scope')
        if built.get('cache', {}).get('status') != 'miss' or built.get('cache', {}).get('publication') != 'stored':
            raise AssertionError('Gate A cold build was not newly verified and published')
        repeated, repeat_metrics = probe.send({'compile_application': request})
        if repeated.get('cache', {}).get('status') != 'hit':
            raise AssertionError('Gate A unchanged build did not reuse the exact verified artifact')
        if repeated.get('artifact', {}).get('identity') != built.get('artifact', {}).get('identity'):
            raise AssertionError('identical source snapshot changed artifact identity')
        specialization_variant = dict(request)
        specialization_variant['generic_seeds'] = request['generic_seeds'] + [{
            'module': 'mncs.test.suite', 'function': 'summarize',
            'type_arguments': [{'kind': 'nat', 'value': 3}],
        }]
        variant, variant_metrics = probe.send({'compile_application': specialization_variant})
        if variant.get('cache', {}).get('key') == built.get('cache', {}).get('key'):
            raise AssertionError('generic specialization assumptions were omitted from the cache key')
        if variant.get('artifact', {}).get('identity') == built.get('artifact', {}).get('identity'):
            raise AssertionError('a changed generic specialization request reused the prior artifact')
        invalid_request = dict(request)
        invalid_request['root_source'] = APP_SOURCE.replace('summary.total', 'summary.total_missing', 1)
        invalid = probe.send({'compile_application': invalid_request})[0]
        if invalid.get('status') != 'rejected':
            raise AssertionError('invalid edit reused/accepted a successful compile: ' + json.dumps(invalid, indent=2)[:6000])
        invalid_codes = [item.get('code') for item in invalid.get('diagnostics', [])]
        if 'MNE162' not in invalid_codes:
            raise AssertionError(f'expected precise unknown-field diagnostic MNE162, got {invalid_codes}')
        unsupported_target = dict(request)
        unsupported_target['target'] = 'mncs-wasm-1.0'
        unavailable = probe.send({'compile_application': unsupported_target})[0]
        if unavailable.get('status') != 'unavailable':
            raise AssertionError('unsupported target did not fail closed: ' + json.dumps(unavailable, indent=2))
        invalid_specialization = dict(request)
        invalid_specialization['generic_seeds'] = [{
            'module': 'mncs.test.suite', 'function': 'missing',
            'type_arguments': [{'kind': 'nat', 'value': 4}],
        }]
        bad_specialization = probe.send({'compile_application': invalid_specialization})[0]
        specialization_codes = [item.get('code') for item in bad_specialization.get('diagnostics', [])]
        if bad_specialization.get('status') != 'rejected' or 'MNE131' not in specialization_codes:
            raise AssertionError('invalid generic export request lacked precise MNE131: ' + json.dumps(
                bad_specialization, indent=2)[:6000])
        malformed_seeds = dict(request)
        malformed_seeds['generic_seeds'] = 'not-an-array'
        malformed_seed_result = probe.send({'compile_application': malformed_seeds})[0]
        if malformed_seed_result.get('error', {}).get('code') != 'MCA109':
            raise AssertionError('malformed specialization input was not rejected as MCA109: ' +
                                 json.dumps(malformed_seed_result, indent=2))
        extra_seed_field = dict(request)
        extra_seed_field['generic_seeds'] = [{
            **request['generic_seeds'][0], 'unrecognized': True,
        }]
        extra_seed_result = probe.send({'compile_application': extra_seed_field})[0]
        if extra_seed_result.get('error', {}).get('code') != 'MCA109':
            raise AssertionError('unknown specialization field was not rejected as MCA109: ' +
                                 json.dumps(extra_seed_result, indent=2))
        over_limit_seeds = dict(request)
        over_limit_seeds['generic_seeds'] = request['generic_seeds'] * 129
        over_limit_result = probe.send({'compile_application': over_limit_seeds})[0]
        if over_limit_result.get('error', {}).get('code') != 'MCA110':
            raise AssertionError('oversized specialization input was not rejected as MCA110: ' +
                                 json.dumps(over_limit_result, indent=2))
        probe_stderr = probe.close()
    except BaseException:
        try:
            probe.proc.kill()
            probe.proc.wait(timeout=5)
        except Exception:
            pass
        raise

    document, vm_metrics = run_vm(artifact, call['arguments'])
    if document.get('outcome') != {'kind': 'completed'}:
        raise AssertionError('VM did not complete: ' + json.dumps(document.get('outcome'), indent=2))
    receipt_artifact_id = document.get('record', {}).get('artifact_id')
    artifact_id = artifact.get('artifact_id')
    if receipt_artifact_id != artifact_id:
        raise AssertionError(f'VM receipt artifact identity mismatch: {receipt_artifact_id} != {artifact_id}')
    actual = [decode_wire(vm_to_wire(value)) for value in document['record']['returned']]
    expected = [decode_wire(value) for value in oracle_exec['returned']]
    if actual != expected:
        raise AssertionError(json.dumps({'actual': actual, 'stage0': expected}, indent=2))
    outcome = actual[0]
    if outcome['$v'] != variant_discriminant(callable_interface, 'ReadinessOutcome', 'Completed'):
        raise AssertionError('valid structured input did not produce Completed')
    report = outcome['$p']['report']
    assert report['summary']['total'] == 4, report
    assert report['summary']['passed'] == 3, report
    assert report['summary']['skipped'] == 1, report
    assert report['summary']['verdict_code'] == 0, report
    assert report['property_case']['seed'] == 42 and report['property_case']['case_index'] == 7, report
    assert report['witness']['length'] == 4 and report['witness']['checksum'] == 30, report
    assert report['state_value'] == 4, report

    typed_failure_results = []
    for index, call_case in enumerate(failure_calls):
        expected_failure = decode_wire(failure_oracle_execs[index]['returned'][0])
        failure_document, failure_vm_metrics = run_vm(artifact, call_case['arguments'])
        if failure_document.get('outcome') != {'kind': 'completed'}:
            raise AssertionError('typed application failure did not return through the VM')
        if failure_document.get('record', {}).get('artifact_id') != artifact_id:
            raise AssertionError('typed failure receipt does not bind the exact artifact')
        failure_actual = decode_wire(vm_to_wire(failure_document['record']['returned'][0]))
        if failure_actual != expected_failure:
            raise AssertionError(json.dumps({'actual': failure_actual,
                                             'stage0': expected_failure}, indent=2))
        if failure_actual['$v'] != variant_discriminant(
                callable_interface, 'ReadinessOutcome', 'Rejected'):
            raise AssertionError('invalid structured input did not produce Rejected')
        error = failure_actual['$p']['error']
        expected_error = ('InvalidCaseIndex' if index == 0 else 'InvalidTestCount')
        if error['$v'] != variant_discriminant(
                callable_interface, 'ReadinessError', expected_error):
            raise AssertionError('typed error did not preserve its declared finite variant')
        expected_payload = ({'found': 65} if index == 0
                            else {'expected': 4, 'found': 3})
        if error['$p'] != expected_payload:
            raise AssertionError('typed error payload does not preserve the rejected request value')
        typed_failure_results.append({
            'input': call_case['arguments'][0],
            'oracle_result': expected_failure,
            'vm_result': failure_actual,
            'artifact_identity': failure_document['record']['artifact_id'],
            'vm_metrics': failure_vm_metrics,
        })

    source_rows = [{'module': name, 'source_id': name, 'sha256': sha(source.encode()),
                    'bytes': len(source.encode())}
                   for name, source in sorted(module_sources.items())]
    source_rows.append({'module': 'root', 'source_id': APP_SOURCE_ID,
                        'sha256': sha(APP_SOURCE.encode()), 'bytes': len(APP_SOURCE.encode())})
    evidence = {
        'schema_version': 1,
        'kind': 'mncs-native-application-readiness-gate-a',
        'observed_at': datetime.now(timezone.utc).isoformat(),
        'compiler': {
            'revision': git('rev-parse', 'HEAD', cwd=ROOT),
            'branch': git('branch', '--show-current', cwd=ROOT),
            'stage0_revision': json.loads((ROOT / 'mncs-language.lock.json').read_text())['revision'],
            'stage0_probe_sha256': sha(PROBE.read_bytes()),
            'vm_revision': git('rev-parse', 'HEAD', cwd=Path('/workspace/mncs-vm')),
            'vm_sha256': sha(VM_BIN.read_bytes()),
            'test_revision': git('rev-parse', 'HEAD', cwd=TEST_ROOT),
        },
        'application': {
            'name': 'mncs-test native readiness suite core',
            'module': APP_MODULE,
            'entry': 'run',
            'contract_schema': built.get('schema_version'),
            'source_snapshot_identity': built.get('source_snapshot', {}).get('identity'),
            'source_files': source_rows,
            'dependency_graph': built.get('dependency_graph'),
            'producer': built.get('producer'),
            'contract_status': built.get('status'),
            'entry_status': built.get('entry', {}).get('status'),
            'callable_interface': callable_interface,
            'execution_compatibility': built.get('execution_target_compatibility'),
            'artifact_schema': built.get('artifact', {}).get('schema_version'),
            'artifact_identity': built.get('artifact', {}).get('identity'),
            'artifact_sha256': sha(json.dumps(artifact, sort_keys=True, separators=(',', ':')).encode()),
            'interface_identity': built.get('artifact', {}).get('interface_identity'),
            'required_capabilities': built.get('artifact', {}).get('required_capabilities'),
            'unresolved_obligations': built.get('unresolved_obligations'),
            'callable_scope': built.get('callable_scope'),
            'selected_exports': artifact.get('exports'),
            'unselected_unresolved_exports': built.get('unselected_unresolved_exports'),
            'generic_seeds': request['generic_seeds'],
            'specialized_callables': [row for row in specialized_callables
                                      if row.get('module') in {'mncs.test.assertions', 'mncs.test.suite'}
                                      and '__spec_' in row.get('name', '')],
        },
        'oracle': {
            'kind': 'Stage-0 ReferenceCompiler + execute_ssa_module project oracle',
            'valid': oracle.get('valid'),
            'diagnostics': oracle.get('diagnostics'),
            'linked_functions': len(oracle.get('program', {}).get('functions', [])),
            'ssa_functions': len(oracle.get('ssa', {}).get('functions', [])),
            'returned_value': summarize_report(expected[0]),
            'returned_value_sha256': sha(json.dumps(expected, sort_keys=True, separators=(',', ':')).encode()),
            'metrics': oracle_metrics,
        },
        'canonical_vm': {
            'outcome': document.get('outcome'),
            'artifact_id': receipt_artifact_id,
            'steps': document.get('usage', {}).get('steps'),
            'returned_value': summarize_report(actual[0]),
            'returned_value_sha256': sha(json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()),
            'metrics': vm_metrics,
        },
        'build_measurements': {
            'first_build': {'cache': built.get('cache'), 'metrics': build_metrics},
            'identical_repeat': {'cache': repeated.get('cache'), 'metrics': repeat_metrics},
            'generic_specialization_change': {
                'cache': variant.get('cache'),
                'artifact_identity': variant.get('artifact', {}).get('identity'),
                'metrics': variant_metrics,
            },
            'identical_repeat_artifact_identity': repeated.get('artifact', {}).get('identity'),
            'artifact_identity_stable': repeated.get('artifact', {}).get('identity') == built.get('artifact', {}).get('identity'),
            'cache_reuse_claimed': True,
        },
        'typed_failure_results': typed_failure_results,
        'negative_cases': {
            'unknown_field': {
                'status': invalid.get('status'),
                'diagnostics': invalid.get('diagnostics'),
                'source_snapshot_identity': invalid.get('source_snapshot', {}).get('identity'),
            },
            'unsupported_target': {'status': unavailable.get('status'), 'error': unavailable.get('error')},
            'invalid_generic_specialization': {
                'status': bad_specialization.get('status'),
                'diagnostics': bad_specialization.get('diagnostics'),
            },
            'malformed_generic_seeds': malformed_seed_result.get('error'),
            'unknown_generic_seed_field': extra_seed_result.get('error'),
            'generic_seed_count_limit': over_limit_result.get('error'),
        },
        'stage0_probe_diagnostics_tail': probe_stderr[-1500:],
        'gate_a': 'pass',
        'self_consumption_matrix_change': 'none; this application does not prove compiler succession',
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + '\n')
    shutil.rmtree(cache_dir, ignore_errors=True)
    print(json.dumps({'gate_a': evidence['gate_a'], 'artifact_identity': built.get('artifact', {}).get('identity'),
                      'contract_status': built.get('status'), 'entry_status': built.get('entry', {}).get('status'),
                      'oracle_matches_vm': actual == expected, 'build': build_metrics,
                      'vm': vm_metrics, 'negative_diagnostic_codes': invalid_codes,
                      'evidence': str(EVIDENCE.relative_to(ROOT))}, indent=2))


if __name__ == '__main__':
    main()
