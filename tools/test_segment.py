#!/usr/bin/env python3
"""Segment-vertical differential: segment.* (absolute offsets over four
chunks) vs Stage-0 lexical oracle on the concatenated bytes.

Temporary test transport; all scanning executes in MNCS or Stage-0. Python
only moves bytes, splits chunks, and compares against the oracle on every
run (no goldens).

Covers the profile-0.13 migration of segment.mncs (index-name reuse,
`!advance`) and the shared `!`/scalar-match edits in lexer.mncs: every
token of every sample flows through the migrated dispatch.
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

# Segment scope only; `decl` stays excluded while CP-0014 blocks it.
PROBE_MODULES = 'source,lexer,segment'


def integer(n):
    return {'integer': {'type': {'bits': 64, 'signed': False}, 'value': n}}


def blob(data: bytes):
    return {'sequence': {'values': [{'byte': {'value': n}} for n in data]}}


def decode(value):
    if 'record' in value:
        return {k: decode(v) for k, v in value['record']['fields']}
    return next(iter(value.values()))['value']


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

    def run(self, unit, function, args):
        request = {'schema_version': '0.1', 'target': {'module': f'mncs.compiler.{unit}.v1', 'function': function},
                   'arguments': args, 'step_budget': 8000000}
        result = self.send(request)
        assert result['status'] == 'returned', (request, result)
        self.count += 1
        self.steps.append(result['steps'])
        normalized = [request, result['status'], result['returned'], result['failure'], result['steps']]
        self.digest.update(json.dumps(normalized, sort_keys=True).encode())
        return decode(result['returned'][0])

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=30) == 0


def split4(data: bytes):
    n = len(data)
    cuts = [min(n, 64), min(n, 128), min(n, 192)]
    parts, prev = [], 0
    for c in cuts:
        parts.append(data[prev:c])
        prev = c
    parts.append(data[prev:])
    return parts


def random_split(rng, data: bytes):
    n = len(data)
    cuts = sorted(rng.sample(range(n + 1), min(3, n + 1)))
    while len(cuts) < 3:
        cuts.append(n)
    parts, prev = [], 0
    for c in cuts:
        parts.append(data[prev:c])
        prev = c
    parts.append(data[prev:])
    return parts


def valid_random_split(rng, data: bytes):
    for _ in range(50):
        parts = random_split(rng, data)
        if all(len(p) <= 64 for p in parts):
            return parts
    return split4(data)


def samples():
    fixed = [
        b'',
        b'a',
        b'mncs 0.13; module a;',
        b'mncs 0.13; module a.b;',
        b'/*c*/',
        b'/* /* */',
        b'/* unterminated',
        b'// line\nfn f() -> (r: u64) { return 1; }',
        b'-> => && || == != <= >= << >> .. +% -% *% +| -| *|',
        b'+ - * / % & | ^ < > = . : ; , ( ) { } [ ]',
        b'0 07 1.2 3..4 5.6.7',
        b'a' * 64,
        b'ab ' * 40,
        b'fn f(a: u64, b: u64) -> (r: u64) { let x: u64 = a + b * 2; return x; }',
        b' ' * 200,
        b'x' * 256,
    ]
    rng = random.Random(257)
    alphabet = 'ab_09. +-*/%|&^<>=!;:,()\t\n{}fmns'
    out = list(fixed)
    for _ in range(24):
        n = rng.randrange(1, 257)
        out.append(''.join(rng.choice(alphabet) for _ in range(n)).encode())
    return out


NONASCII = [b'mncs 0.13; module caf\xc3\xa9;', b'\xff', b'a\x80b']


def suite():
    probe = Probe()
    try:
        kinds = json.loads(Path('src/compiler/token-kinds.json').read_text())
        inv = {v: int(k) for k, v in kinds.items()}
        word_kinds = {4} | set(range(10, 40))
        rng = random.Random(913)
        n_tokens = 0
        n_texts = 0
        for text in samples():
            n = len(text)
            assert n <= 256
            splits = [split4(text)]
            if n > 1:
                alt = valid_random_split(rng, text)
                if alt != splits[0]:
                    splits.append(alt)
            oracle = probe.send({'oracle': text.decode('ascii')})['lexical']
            assert all(b < 128 for b in text)
            for parts in splits:
                chunks = [blob(p) for p in parts]
                # Storage contract: exact bytes, sentinel, ascii flag, total gate.
                assert probe.run('segment', 'total_valid', chunks + [integer(n)]) is True
                assert probe.run('segment', 'total_valid', chunks + [integer(0)]) is True
                assert probe.run('segment', 'total_valid', chunks + [integer(n + 1)]) is False
                for o in list(range(0, n, 37)) + [n, n + 5]:
                    expect = text[o] if o < n else 256
                    assert probe.run('segment', 'byte_at', chunks + [integer(n), integer(o)]) == expect, (text, o)
                assert probe.run('segment', 'ascii', chunks + [integer(n)]) is True
                # Token walk against the oracle. Every loop is bounded by the
                # oracle token count plus EOF, so a non-advancing token fails
                # instead of hanging the harness; progress itself is asserted.
                diags = {(d['span']['start'], d['span']['end']): d['code'] for d in oracle['diagnostics']}
                cursor, idx = 0, 0
                for _ in range(len(oracle['tokens']) + 1):
                    tok = probe.run('segment', 'next_token', chunks + [integer(n), integer(cursor)])
                    exp = oracle['tokens'][idx] if idx < len(oracle['tokens']) else None
                    if exp is None:
                        assert tok == dict(kind=0, start=n, end=n, diagnostic=0), (text, tok)
                        break
                    span = exp['span']
                    code = diags.get((span['start'], span['end']), None)
                    expect_diag = {'MNL001': 1, 'MNL002': 2}.get(code, 0)
                    assert (tok['kind'], tok['start'], tok['end'], tok['diagnostic']) == \
                        (inv[exp['kind']], span['start'], span['end'], expect_diag), (text, tok, exp)
                    assert tok['end'] > cursor, (text, tok)
                    cursor, idx = tok['end'], idx + 1
                    n_tokens += 1
                else:
                    raise AssertionError(('no EOF', text))
                assert idx == len(oracle['tokens']), (text, idx, len(oracle['tokens']))
                # Significant walk: clean trivia is skipped, but a trivia token
                # carrying a diagnostic is returned (never discarded), exactly
                # like any other lexical failure.
                expected_sig = [t for t in oracle['tokens']
                                if t['kind'] not in ('whitespace', 'line_comment', 'block_comment')
                                or (t['span']['start'], t['span']['end']) in diags]
                seen, cursor = [], 0
                for _ in range(len(expected_sig) + 1):
                    tok = probe.run('segment', 'significant', chunks + [integer(n), integer(cursor)])
                    seen.append(tok)
                    if tok['kind'] == 0:
                        break
                    # A diagnostic token is still consumed: significant
                    # returns (never skips) lexical failure, and the caller
                    # advances past it, exactly as parser.step does.
                    assert tok['end'] > cursor, (text, tok)
                    cursor = tok['end']
                else:
                    raise AssertionError(('no significant terminator', text))
                assert len(seen) == len(expected_sig) + 1, (text, seen, expected_sig)
                for tok, exp in zip(seen, expected_sig):
                    span = exp['span']
                    code = diags.get((span['start'], span['end']), None)
                    expect_diag = {'MNL001': 1, 'MNL002': 2}.get(code, 0)
                    assert (tok['kind'], tok['start'], tok['end'], tok['diagnostic']) == \
                        (inv[exp['kind']], span['start'], span['end'], expect_diag), (text, tok, exp)
                last = seen[-1]
                assert last['kind'] == 0 or last['diagnostic'] != 0, (text, last)
                # Keyword mirror on every word-shaped oracle token.
                for exp in oracle['tokens']:
                    num = inv[exp['kind']]
                    if num in word_kinds:
                        span = exp['span']
                        got = probe.run('segment', 'keyword',
                                        chunks + [integer(n), integer(span['start']), integer(span['end'])])
                        assert got == num, (text, exp, got)
                n_texts += 1
        for text in NONASCII:
            n = len(text)
            parts = split4(text)
            chunks = [blob(p) for p in parts]
            assert probe.run('segment', 'total_valid', chunks + [integer(n)])
            assert probe.run('segment', 'ascii', chunks + [integer(n)]) is False
            first_bad = next(i for i, b in enumerate(text) if b >= 128)
            tok = probe.run('segment', 'next_token', chunks + [integer(n), integer(first_bad)])
            assert tok == dict(kind=7, start=first_bad, end=first_bad + 1, diagnostic=3), (text, tok)
            n_texts += 1
        return {'requests': probe.count, 'texts': n_texts, 'tokens_compared': n_tokens,
                'result_sha256': probe.digest.hexdigest(),
                'execution_steps_total': sum(probe.steps), 'execution_steps_max': max(probe.steps)}
    finally:
        probe.close()


if __name__ == '__main__':
    started = time.monotonic()
    first, second = suite(), suite()
    assert first == second, (first, second)
    report = {'schema_version': 1, 'stage0_revision': json.loads(Path('mncs-language.lock.json').read_text())['revision'],
              'tests': first, 'identical_runs': 2, 'elapsed_seconds': round(time.monotonic() - started, 3),
              'scope': 'segment absolute-offset twin differential vs Stage-0 lexical oracle; reference interpreter execution'}
    (OUT / 'segment-results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
