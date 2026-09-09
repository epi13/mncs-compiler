#!/usr/bin/env python3
"""Twin differential: decl.prove_unit obligations vs Stage-0 diagnostics.

For each corpus source: run the self-hosted semantic proof through the
stage0 probe and compare its FAIL obligations (kind, span) against the
Stage-0 oracle diagnostics (code, span) in order. UNKNOWN obligations
(overflow, div-zero, contracts) must never surface as oracle diagnostics.
Also checks sabotage/soundness verdicts and run-to-run determinism.
"""
import hashlib
import json
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

KIND_TO_MNE = {
    1: 'MNE102', 2: 'MNE117', 3: 'MNE118', 4: 'MNE122', 5: 'MNE110',
    6: 'MNE132', 7: 'MNE119', 8: 'MNE120', 9: 'MNE121', 10: 'MNE181',
    11: 'MNE190', 12: 'MNE191', 15: 'MNE134', 17: 'MNE111', 18: 'MNE101',
    19: 'MNE105', 20: 'MNE114', 21: 'MNE113', 22: 'MNE162', 23: 'MNE161',
    24: 'MNE116', 25: 'MNE135', 26: 'MNE133', 27: 'MNE103', 28: 'MNE115',
    29: 'MNE153', 30: 'MNE171', 31: 'MNE173', 32: 'MNE131', 33: 'MNE152',
    34: 'MNE121', 35: 'MNE163', 36: 'MNE173', 37: 'MNE104',
}
UNKNOWN_KINDS = {13, 14, 16}

# (name, source, expected UNKNOWN kinds present in our obligations)
CASES = [
    ('clean-add',
     'mncs 0.10; module t; fn f(a: u64, b: u64) -> (result: u64) { return a + b; }',
     {13}),
    ('clean-div-nonlit',
     'mncs 0.10; module t; fn f(a: u64, b: u64) -> (result: u64) { return a / b; }',
     {14}),
    ('clean-div-lit',
     'mncs 0.10; module t; fn f(a: u64) -> (result: u64) { return a / 2; }',
     set()),
    ('clean-if',
     'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { if a == 1 { return 1; } else { return 2; } return 0; }',
     set()),
    ('clean-call',
     'mncs 0.10; module t; fn h(x: u64) -> (r: u64) { return x; } fn g(a: u64) -> (r: u64) { return h(a); }',
     set()),
    ('clean-record-proj',
     'mncs 0.10; module t; record R { x: u64 } fn f(v: R) -> (r: u64) { return v.x; }',
     set()),
    ('clean-let-bool-shift',
     'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { let b: bool = a == 1; let c: u64 = a << 2; return c; }',
     set()),
    ('clean-fail',
     'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { if a == 0 { fail isolated; } return a; }',
     set()),
    ('sig-body-order',
     'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { return a + true; } fn g(a: bogus) -> (r: u64) { return 0; }',
     set()),
    ('sig-body-order-swapped',
     'mncs 0.10; module t; fn g(a: bogus) -> (r: u64) { return 0; } fn f(a: u64) -> (r: u64) { return a + true; }',
     set()),
    ('dup-param-no-redup',
     'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { return a + true; } fn g(a: u64, a: bool) -> (r: u64) { return 0; }',
     set()),
    ('dup-param-first-wins',
     'mncs 0.10; module t; fn g(a: u64, a: bool) -> (r: bool) { return a; }',
     set()),
    ('result-bogus-double',
     'mncs 0.10; module t; fn g() -> (r: bogus) { return 0; }',
     set()),
    ('name-vs-poison-silent-tail',
     'mncs 0.10; module t; fn g(a: u64) -> (r: bogus) { return a; }',
     set()),
    ('let-bool-vs-poison',
     'mncs 0.10; module t; fn g() -> (r: u64) { let y: bogus = true; return 0; }',
     set()),
    ('binop-left-poison',
     'mncs 0.10; module t; fn g(a: u64) -> (r: bogus) { return a + 1; }',
     {13}),
    ('binop-right-poison',
     'mncs 0.10; module t; fn g(a: u64) -> (r: bogus) { return 0 + a; }',
     set()),
    ('let-name-vs-poison',
     'mncs 0.10; module t; fn g(a: u64) -> (r: u64) { let y: bogus = a; return 0; }',
     set()),
    ('call-arg-poison-param',
     'mncs 0.10; module t; fn h(x: bogus) -> (r: u64) { return 0; } fn g() -> (r: u64) { return h(1); }',
     set()),
    ('poison-arg-clean-param',
     'mncs 0.10; module t; fn h(x: u64) -> (r: u64) { return 0; } fn g() -> (r: u64) { let y: bogus = 0; return h(y); }',
     set()),
    ('poison-arg-poison-param',
     'mncs 0.10; module t; fn h(x: bogus) -> (r: u64) { return 0; } fn g() -> (r: u64) { let y: bogus = 0; return h(y); }',
     set()),
    ('call-vs-poison-result',
     'mncs 0.10; module t; fn h() -> (r: u64) { return 0; } fn g() -> (r: bogus) { return h(); }',
     set()),
    ('unbound-tail-name',
     'mncs 0.10; module t; fn g() -> (r: u64) { return nosuch; }',
     set()),
    ('poison-let-use',
     'mncs 0.10; module t; fn g() -> (r: u64) { let y: bogus = 0; let z: u64 = y; return 0; }',
     set()),
    ('tail-poison-name-silent',
     'mncs 0.10; module t; fn g() -> (r: bogus) { let y: bogus = 0; return y; }',
     set()),
    ('mismatch-fatal-119',
     'mncs 0.10; module t; fn f(a: u64, b: bool) -> (r: u64) { return (a + b) + nosuchvar; }',
     set()),
    ('cmp-bool-fatal',
     'mncs 0.10; module t; fn f(a: bool) -> (r: bool) { return a == true; }',
     set()),
    ('byte-arith-fatal',
     'mncs 0.10; module t; fn f(a: byte, b: byte) -> (r: byte) { return a + b; }',
     set()),
    ('binop-result-threaded',
     'mncs 0.10; module t; fn f(a: u64, b: u64) -> (r: bool) { return a + b; }',
     {13}),
    ('poison-operands-arith',
     'mncs 0.10; module t; fn g() -> (r: u64) { let y: bogus = 0; return y + 1; }',
     set()),
    ('poison-operands-same-poison',
     'mncs 0.10; module t; fn g() -> (r: u64) { let y: bogus = 0; return y + y; }',
     set()),
    ('poison-operands-cmp',
     'mncs 0.10; module t; fn g() -> (r: bool) { let y: bogus = 0; return y == y; }',
     set()),
    ('poison-operands-boolop',
     'mncs 0.10; module t; fn g() -> (r: bool) { let y: bogus = true; return y && y; }',
     set()),
    ('poison-cond',
     'mncs 0.10; module t; fn g() -> (r: u64) { let y: bogus = 0; if y { return 1; } else { return 2; } return 0; }',
     set()),
    ('double-result-skips-body',
     'mncs 0.10; module t; fn g() -> (a: u64, b: u64) { return 0; }',
     set()),
    ('arity',
     'mncs 0.10; module t; fn h(a: u64) -> (r: u64) { return a; } fn g() -> (r: u64) { return h(); }',
     set()),
    ('callee',
     'mncs 0.10; module t; fn g() -> (r: u64) { return nosuchfn(1); }',
     set()),
    ('unreachable-in-branch',
     'mncs 0.10; module t; fn g(a: u64) -> (r: u64) { if a == 1 { return 1; return 2; } else { return 3; } return 0; }',
     set()),
    ('join-after-both-return',
     'mncs 0.10; module t; fn g(a: u64) -> (r: u64) { if a == 1 { return 1; } else { return 2; } let z: u64 = 3; return z; }',
     set()),
    ('bad-fail-mode',
     'mncs 0.10; module t; fn g(a: u64) -> (r: u64) { if a == 0 { fail bogus; } return a; }',
     set()),
    ('dup-fn',
     'mncs 0.10; module t; fn f() -> (r: u64) { return 0; } fn f() -> (r: u64) { return 1; }',
     set()),
    ('shadow-let',
     'mncs 0.10; module t; fn g(a: u64) -> (r: u64) { let x: u64 = a; let x: u64 = 1; return x; }',
     set()),
    ('proj-bad-field',
     'mncs 0.10; module t; record R { x: u64 } fn f(v: R) -> (r: u64) { return v.y; }',
     set()),
    ('proj-bad-base',
     'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { return a.x; }',
     set()),
    ('if-cond-type',
     'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { if a { return 1; } else { return 2; } return 0; }',
     set()),
    ('effect-auth-pass2',
     'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { return a + true; } fn g() -> (r: u64) effect io authorized_by net { return 0; }',
     set()),
    ('effect-auth-clean',
     'mncs 0.10; module t; fn g() -> (r: u64) capability net effect io authorized_by net { return 0; }',
     set()),
    ('result-one-pass2',
     'mncs 0.10; module t; fn a() -> (r: u64) { return nosuch; } fn b() -> (x: u64, y: u64) { return 0; }',
     set()),
    ('sig-multi-family',
     'mncs 0.10; module t; fn g(a: u64, a: bool) -> (r: u64) capability c capability c effect io authorized_by net { return nosuch; }',
     set()),
]


def integer(n):
    return {'integer': {'type': {'bits': 64, 'signed': False}, 'value': n}}


def blob(data: bytes):
    return {'sequence': {'values': [{'byte': {'value': n}} for n in data]}}


def decode(value):
    if 'record' in value:
        return {k: decode(v) for k, v in value['record']['fields']}
    if 'finite' in value:
        f = value['finite']
        return {'$v': f['discriminant'], '$p': {k: decode(v) for k, v in f.get('payload', [])}}
    if 'sequence' in value:
        return [decode(v) for v in value['sequence']['values']]
    if 'boolean' in value:
        return value['boolean']['value']
    return next(iter(value.values()))['value']


def split4(data: bytes):
    n = len(data)
    cuts = [min(n, 64), min(n, 128), min(n, 192)]
    parts, prev = [], 0
    for c in cuts:
        parts.append(data[prev:c])
        prev = c
    parts.append(data[prev:])
    return parts


def flist(v, cons):
    out = []
    while v['$v'] == cons:
        out.append(v['$p']['head'])
        v = v['$p']['tail']
    return out


class Probe:
    def __init__(self):
        self.proc = subprocess.Popen(['.bootstrap/target/debug/mncs-compiler-stage0-probe'],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=ROOT)
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
        assert result['status'] == 'returned', (function, result)
        self.count += 1
        self.steps.append(result['steps'])
        self.digest.update(json.dumps([request, result['returned'], result['steps']], sort_keys=True).encode())
        return decode(result['returned'][0])

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=60) == 0


def prove_case(probe, text):
    data = text.encode()
    assert len(data) <= 256, text
    args = [blob(p) for p in split4(data)] + [integer(len(data))]
    return probe.run('decl', 'prove_unit', args)


def suite():
    probe = Probe()
    details = []
    try:
        for name, text, want_unknown in CASES:
            oracle = probe.send({'oracle': text})
            # Scope: MNE elaboration diagnostics only. MNB body-graph codes
            # (e.g. unreachable blocks after a both-return join) belong to
            # lowering validation, which prove_unit does not model.
            odiags = [(d['code'], d['span']['start'], d['span']['end'])
                      for d in probe.send({'elaborate': text}) if d['code'].startswith('MNE')]
            got = prove_case(probe, text)
            obls = flist(got['obls'], 1)
            fails = [(KIND_TO_MNE[o['kind']], o['start'], o['end']) for o in obls if o['status'] == 1]
            unknowns = {o['kind'] for o in obls if o['status'] == 2}
            assert all(o['kind'] in KIND_TO_MNE for o in obls if o['status'] == 1), (name, obls)
            assert all(k in UNKNOWN_KINDS for k in unknowns), (name, unknowns)
            assert want_unknown <= unknowns, (name, want_unknown, unknowns)
            assert fails == odiags, (name, fails, odiags)
            assert got['ok'] == (odiags == []), (name, got['ok'], odiags)
            expect_n = 0 if 'MNE104' in [c for c, _, _ in odiags] else len(oracle['ast']['functions'])
            assert got['fn_count'] == expect_n, (name, got)
            details.append({'case': name, 'fails': len(fails), 'unknowns': sorted(unknowns),
                            'fn_count': got['fn_count']})
        # Intrinsic-proof adversarial verdicts: all sabotage rejected, sound sample passes.
        data = b'mncs 0.10; module t;'
        args = [blob(p) for p in split4(data)] + [integer(len(data))]
        assert probe.run('decl', 'sabotage_depth', args[:4]) is False
        assert probe.run('decl', 'sabotage_call_arity', args) is False
        assert probe.run('decl', 'sabotage_bin_mismatch', args) is False
        assert probe.run('decl', 'sabotage_final_type', args) is False
        assert probe.run('decl', 'sound_sample', args) is True
        return {'requests': probe.count, 'cases': len(CASES),
                'result_sha256': probe.digest.hexdigest(),
                'execution_steps_total': sum(probe.steps), 'execution_steps_max': max(probe.steps),
                'details': details}
    finally:
        probe.close()


if __name__ == '__main__':
    import os
    os.chdir(ROOT)
    started = time.monotonic()
    first, second = suite(), suite()
    assert first == second, (first, second)
    report = {'schema_version': 1,
              'stage0_revision': json.loads(Path('mncs-language.lock.json').read_text())['revision'],
              'tests': first, 'identical_runs': 2, 'elapsed_seconds': round(time.monotonic() - started, 3),
              'scope': 'decl.prove_unit FAIL-obligation differential vs Stage-0 diagnostics; UNKNOWN obligations never surface; sabotage/soundness verdicts'}
    Path('.build').mkdir(exist_ok=True)
    Path('.build/sem-results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(f"{len(CASES)} semantic cases + 5 proof verdicts passed twice identically.")
