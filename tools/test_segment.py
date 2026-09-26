#!/usr/bin/env python3
"""Generic-source segment/lexer differential against the Stage-0 oracle.

The suite exercises the compiler-owned segment adapters and shared lexer over
one exact bounded source sequence. Rust/Python only transport bytes and
compare observations; they do not implement tokenization.
"""
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
os.chdir(ROOT)
OUT = ROOT / '.build'
OUT.mkdir(exist_ok=True)
PROBE_MODULES = 'source,lexer,segment'
SOURCE_BOUND = 256


def integer(n):
    return {'integer': {'type': {'bits': 64, 'signed': False}, 'value': n}}


def nat_arg(value):
    return {'kind': 'nat', 'value': value}


def blob(data):
    raw = data.encode() if isinstance(data, str) else bytes(data)
    assert len(raw) <= SOURCE_BOUND
    raw += b' ' * (SOURCE_BOUND - len(raw))
    return {'sequence': {'values': [{'byte': {'value': n}} for n in raw]}}


def decode(value):
    if 'record' in value:
        return {k: decode(v) for k, v in value['record']['fields']}
    return next(iter(value.values()))['value']


class Probe:
    def __init__(self):
        env = dict(os.environ)
        if env.get("MNCS_PROBE_BACKEND") == "reference_interpreter":
            env.pop("MNCS_PROBE_BACKEND", None)
        env['MNCS_PROBE_MODULES'] = PROBE_MODULES
        env['MNCS_PROBE_EXECUTION_MODULES'] = 'mncs.compiler.segment.v1'
        env.setdefault('MNCS_PROBE_BACKEND', 'cranelift')
        env['MNCS_PROBE_GENERIC_SEEDS'] = json.dumps([
            {'module': 'mncs.compiler.segment.v1', 'function': function,
             'type_arguments': [nat_arg(SOURCE_BOUND)]}
            for function in ['byte_at', 'ascii', 'span_valid', 'next_token', 'significant']
        ])
        self.proc = subprocess.Popen(
            [env.get('MNCS_PROBE_BIN', str(BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env
        )
        self.count = 0
        self.steps = []
        self.digest = hashlib.sha256()

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + '\n')
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f'probe terminated: {self.proc.poll()}'
        return json.loads(line)

    def run(self, function, args):
        request = {
            'schema_version': '0.1',
            'target': {'module': 'mncs.compiler.segment.v1', 'function': function},
            'arguments': args,
            'type_arguments': [nat_arg(SOURCE_BOUND)],
            'step_budget': 8000000,
        }
        result = self.send(request)
        assert result['status'] == 'returned', (function, result)
        self.count += 1
        self.steps.append(result['steps'])
        normalized = [request, result['status'], result['returned'], result['failure'], result['steps']]
        self.digest.update(json.dumps(normalized, sort_keys=True).encode())
        return decode(result['returned'][0])

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=60) == 0


def samples():
    fixed = [
        b'', b'a', b'mncs 0.13; module a;', b'mncs 0.13; module a.b;',
        b'/*c*/', b'/* /* */', b'/* unterminated',
        b'// line\nfn f() -> (r: u64) { return 1; }',
        b'-> => && || == != <= >= << >> .. +% -% *% +| -| *|',
        b'+ - * / % & | ^ < > = . : ; , ( ) { } [ ]', b'0 07 1.2 3..4 5.6.7',
        b'a' * 64, b'ab ' * 40,
        b'fn f(a: u64, b: u64) -> (r: u64) { let x: u64 = a + b * 2; return x; }',
        b' ' * 200, b'x' * SOURCE_BOUND,
    ]
    rng = random.Random(257)
    alphabet = 'ab_09. +-*/%|&^<>=!;:,()\t\n{}fmns'
    return fixed + [
        ''.join(rng.choice(alphabet) for _ in range(rng.randrange(1, SOURCE_BOUND + 1))).encode()
        for _ in range(24)
    ]


NONASCII = [b'mncs 0.13; module caf\xc3\xa9;', b'\xff', b'a\x80b']


def suite():
    probe = Probe()
    try:
        execution_status = probe.send({'execution_status': True})
        if execution_status.get('backend') == 'cranelift':
            assert execution_status['retained_sessions'] == 1, execution_status
        kinds = json.loads(Path('src/compiler/token-kinds.json').read_text())
        inv = {value: int(key) for key, value in kinds.items()}
        n_tokens = 0
        n_texts = 0
        for original in samples():
            data = original + b' ' * (SOURCE_BOUND - len(original))
            text = data.decode('ascii')
            oracle = probe.send({'oracle': text})['lexical']
            assert all(byte < 128 for byte in data)
            assert probe.run('ascii', [blob(data)]) is True
            for start, end, expected in [(0, len(data), True), (len(data), len(data), True),
                                          (1, 0, False), (0, len(data) + 1, False)]:
                assert probe.run('span_valid', [blob(data), integer(start), integer(end)]) is expected
            for offset in list(range(0, len(data), 37)) + [len(data), len(data) + 5]:
                expected = data[offset] if offset < len(data) else 256
                assert probe.run('byte_at', [blob(data), integer(offset)]) == expected

            diagnostics = {
                (item['span']['start'], item['span']['end']): item['code']
                for item in oracle['diagnostics']
            }
            cursor = 0
            token_index = 0
            for _ in range(len(oracle['tokens']) + 1):
                token = probe.run('next_token', [blob(data), integer(cursor)])
                expected_token = oracle['tokens'][token_index] if token_index < len(oracle['tokens']) else None
                if expected_token is None:
                    assert token == {'kind': 0, 'start': len(data), 'end': len(data), 'diagnostic': 0}
                    break
                span = expected_token['span']
                code = diagnostics.get((span['start'], span['end']))
                expected_diagnostic = {'MNL001': 1, 'MNL002': 2}.get(code, 0)
                assert (token['kind'], token['start'], token['end'], token['diagnostic']) == (
                    inv[expected_token['kind']], span['start'], span['end'], expected_diagnostic
                ), (data, token, expected_token)
                assert token['end'] > cursor, (data, token)
                cursor = token['end']
                token_index += 1
                n_tokens += 1
            else:
                raise AssertionError(('no EOF', data))
            assert token_index == len(oracle['tokens']), (data, token_index, len(oracle['tokens']))

            expected_significant = [
                token for token in oracle['tokens']
                if token['kind'] not in ('whitespace', 'line_comment', 'block_comment')
                or (token['span']['start'], token['span']['end']) in diagnostics
            ]
            seen = []
            cursor = 0
            for _ in range(len(expected_significant) + 1):
                token = probe.run('significant', [blob(data), integer(cursor)])
                seen.append(token)
                if token['kind'] == 0:
                    break
                assert token['end'] > cursor, (data, token)
                cursor = token['end']
            else:
                raise AssertionError(('no significant EOF', data))
            assert len(seen) == len(expected_significant) + 1
            for token, expected_token in zip(seen, expected_significant):
                span = expected_token['span']
                code = diagnostics.get((span['start'], span['end']))
                expected_diagnostic = {'MNL001': 1, 'MNL002': 2}.get(code, 0)
                assert (token['kind'], token['start'], token['end'], token['diagnostic']) == (
                    inv[expected_token['kind']], span['start'], span['end'], expected_diagnostic
                ), (data, token, expected_token)
            assert seen[-1]['kind'] == 0 or seen[-1]['diagnostic'] != 0
            n_texts += 1

        for original in NONASCII:
            data = original + b' ' * (SOURCE_BOUND - len(original))
            assert probe.run('ascii', [blob(data)]) is False
            first_bad = next(index for index, byte in enumerate(data) if byte >= 128)
            token = probe.run('next_token', [blob(data), integer(first_bad)])
            assert token == {'kind': 7, 'start': first_bad, 'end': first_bad + 1, 'diagnostic': 3}
            assert probe.run('span_valid', [blob(data), integer(0), integer(len(data))]) is True
            n_texts += 1
        return {
            'requests': probe.count,
            'texts': n_texts,
            'tokens_compared': n_tokens,
            'result_sha256': probe.digest.hexdigest(),
            'execution_steps_total': sum(probe.steps),
            'execution_steps_max': max(probe.steps),
            'execution_mode': 'retained_cranelift' if execution_status['retained_sessions'] else 'reference_interpreter',
            'retained_execution_sessions': execution_status['retained_sessions'],
        }
    finally:
        probe.close()


if __name__ == '__main__':
    started = time.monotonic()
    first, second = suite(), suite()
    assert first == second, (first, second)
    report = {
        'schema_version': 1,
        'stage0_revision': json.loads(Path('mncs-language.lock.json').read_text())['revision'],
        'stage0_reference_mode': os.environ.get('MNCS_PROBE_REFERENCE_MODE', 'locked'),
        'tests': first,
        'identical_runs': 2,
        'elapsed_seconds': round(time.monotonic() - started, 3),
        'scope': 'generic source, byte/span adapters, segment token walk, significant-token walk vs Stage-0 lexical facts',
    }
    (OUT / 'segment-results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
