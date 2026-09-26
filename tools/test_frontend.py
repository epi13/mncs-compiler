#!/usr/bin/env python3
"""Temporary test transport; lexical/parser decisions execute in MNCS or Stage-0.
No source implementation, tokenization, parsing, or production hashing lives here.
"""
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
OUT = ROOT / '.build'
OUT.mkdir(exist_ok=True)


def integer(n):
    return {'integer': {'type': {'bits': 64, 'signed': False}, 'value': n}}


def blob(text):
    values = text.encode() if isinstance(text, str) else text
    return {'sequence': {'values': [{'byte': {'value': n}} for n in values]}}


def decode(value):
    if 'record' in value:
        return {k: decode(v) for k, v in value['record']['fields']}
    return next(iter(value.values()))['value']


# Frontend scope: source/lexer/parser/kernel/segment only. Declaration and
# semantic behavior have dedicated suites with their own differential corpus;
# this suite isolates the lexical and header surface.
PROBE_MODULES = 'source,lexer,parser,kernel,segment'


class Probe:
    def __init__(self):
        env = dict(os.environ)
        env['MNCS_PROBE_MODULES'] = PROBE_MODULES
        self.proc = subprocess.Popen(['.bootstrap/target/debug/mncs-compiler-stage0-probe'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self.count = 0
        self.steps = []
        self.digest = hashlib.sha256()

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + '\n')
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f'probe terminated: {self.proc.poll()}'
        return json.loads(line)

    def run(self, unit, function, args, status='returned'):
        request = {'schema_version': '0.1', 'target': {'module': f'mncs.compiler.{unit}.v1', 'function': function},
                   'arguments': args, 'step_budget': 1000000}
        result = self.send(request)
        assert result['status'] == status, (request, result)
        self.count += 1
        self.steps.append(result['steps'])
        normalized = [request, result['status'], result['returned'], result['failure'], result['steps']]
        self.digest.update(json.dumps(normalized, sort_keys=True).encode())
        if status == 'returned':
            return decode(result['returned'][0])
        return result

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=30) == 0


def ref_line_col(data: bytes, offset: int):
    """Spec transcription of SourceSpan::at (ASCII domain): 1-based line
    from newline count, column from bytes since the last newline."""
    offset = min(offset, len(data))
    line = data.count(b'\n', 0, offset) + 1
    line_start = data.rfind(b'\n', 0, offset) + 1
    return (line, offset - line_start + 1)


def suite():
    probe = Probe()
    try:
        fixtures = json.loads(Path('tests/fixtures/frontend.json').read_text())
        kinds = json.loads(Path('src/compiler/token-kinds.json').read_text())
        samples = fixtures['lexical'] + ['a' * 64, ' ' * 64, '/' * 64, '/*' * 32, '1' * 64]
        # Single ASCII bytes exercise the entire classification domain, including NUL.
        samples += [chr(i) for i in range(128)]
        rng = random.Random(130)
        alphabet = 'ab_09. +-*/%|&^<>=!;:\t\n'
        samples += [''.join(rng.choice(alphabet) for _ in range(rng.randrange(1, 65))) for _ in range(40)]
        tokens = 0
        for sample_index, text in enumerate(samples):
            assert len(text.encode()) <= 64
            oracle = probe.send({'oracle': text})['lexical']
            if sample_index < len(fixtures['lexical']) + 5:
                summary = probe.run('kernel', 'lex_summary', [blob(text)])
                diagnostics = oracle['diagnostics']
                assert summary == dict(code=1 if diagnostics else 0, cursor=len(text.encode()), tokens=len(oracle['tokens']),
                    trivia=sum(t['kind'] in ['whitespace', 'line_comment', 'block_comment'] for t in oracle['tokens']),
                    errors=len(diagnostics), first_start=diagnostics[0]['span']['start'] if diagnostics else 0,
                    first_end=diagnostics[0]['span']['end'] if diagnostics else 0), (text, summary)
            cursor = 0
            observed_diagnostics = []
            for expected in oracle['tokens']:
                token = probe.run('lexer', 'next_token', [blob(text), integer(cursor)])
                span = expected['span']
                assert (kinds[str(token['kind'])], token['start'], token['end']) == (expected['kind'], span['start'], span['end']), (text, token, expected)
                if token['diagnostic']:
                    observed_diagnostics.append((f"MNL{token['diagnostic']:03}", token['start'], token['end']))
                # Oracle-anchored rendering: every token start carries Stage-0's
                # own (line, column); the MNCS renderer must agree exactly.
                span = expected['span']
                rendered = probe.run('source', 'line_col', [blob(text), integer(span['start'])])
                assert (rendered['line'], rendered['col']) == (span['line'], span['column']), (text, expected, rendered)
                assert probe.run('kernel', 'token_shape', [blob(text)] + [integer(n) for n in [cursor, token['kind'], token['start'], token['end'], token['diagnostic']]])
                cursor = token['end']
                tokens += 1
            assert cursor == len(text.encode())
            assert observed_diagnostics == [(d['code'], d['span']['start'], d['span']['end']) for d in oracle['diagnostics']]
            eof = probe.run('lexer', 'next_token', [blob(text), integer(cursor)])
            assert eof == dict(kind=0, start=cursor, end=cursor, diagnostic=0)
            # Exhaustive rendering cross-check (strided): every probed
            # offset must match the SourceSpan::at transcription, including
            # line starts, newline bytes, EOF, and past-the-end clamping.
            data = text.encode() if isinstance(text, str) else bytes(text)
            for offset in sorted(set([0, len(data), len(data) + 5] + list(range(0, len(data) + 1, 7)))):
                rendered = probe.run('source', 'line_col', [blob(text), integer(offset)])
                assert (rendered['line'], rendered['col']) == ref_line_col(data, offset), (text, offset, rendered)
        for case in fixtures['headers']:
            text = case['text']
            fact = probe.run('kernel', 'parse_header', [blob(text)])
            assert fact['code'] == case['code'], (case, fact)
            oracle = probe.send({'oracle': text})
            if case['code'] == 0:
                assert fact['state'] == 8
                module_node = oracle['cst']['root']['children'][1]
                assert fact['next_offset'] == module_node['span']['end']
                oracle = probe.send({'oracle': text[:fact['next_offset']] + ' fn x(v:u64)->(r:u64){return v;}'})
                if oracle['ast'] is None:
                    # Stage-0 discards the AST here and reports envelope rejection;
                    # since the re-pin it additionally reports a parse-stage
                    # profile gate (MNP008) for unknown versions. Require the
                    # envelope diagnostic rather than stage exclusivity.
                    assert any(d['stage'] == 'envelope' for d in oracle['diagnostics']), (case, oracle['diagnostics'])
                    for key in ['version', 'module']:
                        assert [fact[f'{key}_start'], fact[f'{key}_end']] == case[f'{key}_span']
                    continue
                for key in ['version', 'module']:
                    span = oracle['ast']['language_version' if key == 'version' else 'module']['span']
                    assert (fact[f'{key}_start'], fact[f'{key}_end']) == (span['start'], span['end'])
                module_node = oracle['cst']['root']['children'][1]
                assert fact['next_offset'] == module_node['span']['end']
            elif case['code'] != 8:
                assert oracle['diagnostics'], case
                first = oracle['diagnostics'][0]['span']
                assert (fact['start'], fact['end']) == (first['start'], first['end']), (case, fact, first)
        # Representation/guard checks, including malformed caller inputs.
        for text, offset, expected in [('', 1, 4), ('a', 2, 4), ('a', 2**64-1, 4), (b'\xff', 0, 3)]:
            assert probe.run('lexer', 'next_token', [blob(text), integer(offset)])['diagnostic'] == expected
        for a, b, expected in [('', '', True), ('ab', 'ab', True), ('ab', 'ac', False), ('a', 'ab', False), ('ab', 'a', False)]:
            assert probe.run('kernel', 'same_source', [blob(a), blob(b)]) == expected
        for values in [[0, 4, 0, 0, 0], [0, 4, 0, 2, 0], [0, 9, 0, 1, 0], [0, 7, 0, 1, 0], [0, 4, 0, 1, 1], [1, 0, 1, 1, 0]]:
            expected = values == [1, 0, 1, 1, 0]
            assert probe.run('kernel', 'token_shape', [blob('a')] + [integer(n) for n in values]) == expected
        for start, end, expected in [(0, 1, True), (1, 1, True), (1, 0, False), (0, 2, False)]:
            assert probe.run('source', 'span_valid', [blob('a'), integer(start), integer(end)]) == expected
        # No silent truncation of an over-capacity source at the executor ABI.
        probe.run('kernel', 'parse_header', [blob('a' * 65)], status='invalid_request')
        assert probe.run('kernel', 'lex_summary', [blob('é')])['code'] == 3
        return {'requests': probe.count, 'lexical_sources': len(samples), 'tokens_compared': tokens,
                'header_cases': len(fixtures['headers']), 'result_sha256': probe.digest.hexdigest(),
                'execution_steps_total': sum(probe.steps), 'execution_steps_max': max(probe.steps)}
    finally:
        probe.close()


if __name__ == '__main__':
    started = time.monotonic()
    first, second = suite(), suite()
    assert first == second, (first, second)
    report = {'schema_version': 1, 'stage0_revision': json.loads(Path('mncs-language.lock.json').read_text())['revision'],
              'tests': first, 'identical_runs': 2, 'elapsed_seconds': round(time.monotonic() - started, 3),
              'scope': 'bounded ASCII lexical differential; header syntax/spans; reference interpreter execution, not native backend parity'}
    (OUT / 'frontend-results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
