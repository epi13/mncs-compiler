#!/usr/bin/env python3
"""Declaration-vertical differential: decl.parse_unit vs Stage-0 oracle plus
decl.check_unit symbol/resolve/IR verdicts. Temporary test transport; all
parsing, checking, and lowering execute in MNCS or Stage-0. Python only
moves bytes and compares against the oracle on every run (no goldens).

Kept small on purpose: reference-interpreter steps cost ~20s per unit
request, so the broad corpus lives in evidence/DECL.md while this suite
guards the key properties with a twin determinism run.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
OUT = ROOT / '.build'
OUT.mkdir(exist_ok=True)

OPNAME = {50: 'add', 51: 'sub', 52: 'mul', 53: 'div', 54: 'mod',
          91: 'add_wrap', 92: 'sub_wrap', 93: 'mul_wrap',
          94: 'add_sat', 95: 'sub_sat', 96: 'mul_sat',
          55: 'bitwise_and', 56: 'bitwise_or', 57: 'bitwise_xor',
          88: 'shl', 89: 'shr', 82: 'and', 83: 'or',
          84: 'eq', 85: 'ne', 58: 'lt', 86: 'le', 59: 'gt', 87: 'ge'}


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


def norm_field(f, src: bytes):
    return (src[f['name_start']:f['name_end']].decode(), src[f['type_start']:f['type_end']].decode())


def norm_expr(e, src: bytes):
    v, p = e['$v'], e['$p']
    if v == 0:
        return ('name', src[p['start']:p['end']].decode())
    if v == 1:
        return ('int', p['value'], (p['start'], p['end']))
    if v == 2:
        return ('bool', p['value'])
    if v == 3:
        return ('bin', OPNAME[p['op']], norm_expr(p['left'], src), norm_expr(p['right'], src),
                (p['start'], p['end']))
    if v == 4:
        return ('call', src[p['name_start']:p['name_end']].decode(),
                [norm_expr(a, src) for a in flist(p['args'], 1)],
                (p['start'], p['end']))
    if v == 5:
        return ('proj' if not p['path'] else 'pathproj', norm_expr(p['base'], src),
                (src[p['field_start']:p['field_end']].decode(), (p['field_start'], p['field_end'])),
                (p['start'], p['end']))
    raise AssertionError(v)


def canon_proj(e):
    """Name-based projection chains -> ('path', base, bspan, [(field, span)], total).

    Stage-0 parses `v.x` as FiniteVariant and `v.x.y` as QualifiedPath with
    facts identical to our nested Project nodes; the node tag is our IR
    choice, so both sides canonicalize to the same segment/span tuple.
    Non-name bases (e.g. calls) stay structural on both sides.
    """
    if isinstance(e, tuple) and e and e[0] in ('proj', 'pathproj'):
        nodes, segs, cur = [], [], e
        while isinstance(cur, tuple) and cur and cur[0] in ('proj', 'pathproj'):
            _, base, field, span = cur
            nodes.append(span)
            segs.append(field)
            cur = base
        if isinstance(cur, tuple) and cur and cur[0] == 'name':
            return ('path', cur[1], (nodes[-1][0], segs[-1][1][0] - 1),
                    list(reversed(segs)), nodes[0])
        return (e[0], canon_proj(cur), e[2], e[3])
    if isinstance(e, tuple) and e and e[0] == 'bin':
        return ('bin', e[1], canon_proj(e[2]), canon_proj(e[3]), e[4])
    if isinstance(e, tuple) and e and e[0] == 'call':
        return ('call', e[1], [canon_proj(a) for a in e[2]], e[3])
    if isinstance(e, list):
        return [canon_proj(x) for x in e]
    if isinstance(e, tuple) and e and e[0] == 'let':
        return ('let', e[1], e[2], canon_proj(e[3]))
    if isinstance(e, tuple) and e and e[0] == 'if':
        return ('if', canon_proj(e[1]), [canon_proj(x) for x in e[2]], [canon_proj(x) for x in e[3]])
    if isinstance(e, tuple) and e and e[0] == 'ret':
        return ('ret', canon_proj(e[1]))
    return e


def norm_stmt(s, src: bytes):
    v, p = s['$v'], s['$p']
    if v == 0:
        return ('let', src[p['name_start']:p['name_end']].decode(),
                src[p['type_start']:p['type_end']].decode(), norm_expr(p['value'], src))
    if v == 1:
        return ('if', norm_expr(p['cond'], src),
                [norm_stmt(x, src) for x in flist(p['then_b'], 1)],
                [norm_stmt(x, src) for x in flist(p['else_b'], 1)])
    if v == 2:
        return ('fail', src[p['mode_start']:p['mode_end']].decode())
    if v == 3:
        return ('ret', norm_expr(p['value'], src))
    raise AssertionError(v)


def onorm_expr(e):
    tag = next(iter(e.keys()))
    b = e[tag]
    if tag == 'Name':
        return ('name', b['text'])
    if tag == 'Integer':
        return ('int', b['value'], (b['text']['span']['start'], b['text']['span']['end']))
    if tag == 'Boolean':
        return ('bool', b['value'])
    if tag == 'Binary':
        return ('bin', b['op'], onorm_expr(b['left']), onorm_expr(b['right']),
                (b['span']['start'], b['span']['end']))
    if tag == 'Call':
        return ('call', b['function']['text'], [onorm_expr(a) for a in b['arguments']],
                (b['span']['start'], b['span']['end']))
    if tag == 'FieldProject':
        f = b['field']
        return ('proj', onorm_expr(b['base']), (f['text'], (f['span']['start'], f['span']['end'])),
                (b['span']['start'], b['span']['end']))
    if tag == 'FiniteVariant':
        t, v = b['type_name'], b['variant']
        return ('path', t['text'], (t['span']['start'], t['span']['end']),
                [(v['text'], (v['span']['start'], v['span']['end']))],
                (b['span']['start'], b['span']['end']))
    if tag == 'QualifiedPath':
        segs = b['segments']
        return ('path', segs[0]['text'], (segs[0]['span']['start'], segs[0]['span']['end']),
                [(s['text'], (s['span']['start'], s['span']['end'])) for s in segs[1:]],
                (b['span']['start'], b['span']['end']))
    return ('OTHER', tag)


def onorm_stmt(s):
    tag = next(iter(s.keys()))
    b = s[tag]
    if tag == 'Let':
        return ('let', b['name']['text'], b['value_type']['text'], onorm_expr(b['value']))
    if tag == 'If':
        return ('if', onorm_expr(b['condition']),
                [onorm_stmt(x) for x in b['then_body']],
                [onorm_stmt(x) for x in b['else_body']])
    if tag == 'Fail':
        return ('fail', b['mode']['text'])
    if tag == 'Return':
        return ('ret', onorm_expr(b['value']))
    return ('OTHER', tag)


def strip_spans(e):
    if isinstance(e, tuple) and e and e[0] in ('bin', 'call', 'proj', 'pathproj', 'int'):
        return (e[0],) + tuple(strip_spans(x) for x in e[1:-1])
    if isinstance(e, list):
        return [strip_spans(x) for x in e]
    return e


def deep(e):
    if isinstance(e, tuple) and e and e[0] in ('bin', 'call', 'proj', 'pathproj', 'int'):
        return strip_spans(e)
    if isinstance(e, tuple) and e and e[0] == 'let':
        return ('let', e[1], e[2], deep(e[3]))
    if isinstance(e, tuple) and e and e[0] == 'if':
        return ('if', deep(e[1]), [deep(x) for x in e[2]], [deep(x) for x in e[3]])
    if isinstance(e, tuple) and e and e[0] == 'ret':
        return ('ret', deep(e[1]))
    return e


POS = [
    'mncs 0.10; module t; fn f(a: u64, b: u64) -> (result: u64) { return a + b; }',
    'mncs 0.10; module t; use a.b as c; record R { x: u64 } fn f(v: R) -> (r: u64) { return v.x; }',
    'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { if a == 1 { return 1; } else { return 2; } return 0; }',
]

NEG = [
    'mncs 0.10; module t;',
    'mncs 0.10; module t; record R {}',
    'mncs 0.10; module t; fn f() -> (r: u64) { return 1 + ; }',
]

# check_unit verdicts: (source, expected stage). Stages: 1 duplicate symbol,
# 2 resolve/span walk, 4 fully verified (0/3 are parse/never-IR-fail paths).
CHECKS = [
    ('mncs 0.10; module t; fn f(a: u64) -> (r: u64) { return a + 1; }', 4),
    ('mncs 0.10; module t; fn f() -> (r: u64) { return 0; } fn f() -> (r: u64) { return 1; }', 1),
    ('mncs 0.10; module t; fn f() -> (r: u64) { return g(1); }', 2),
]


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
        assert self.proc.wait(timeout=30) == 0


def check_unit_case(probe, text):
    data = text.encode()
    assert len(data) <= 256, text
    args = [blob(p) for p in split4(data)] + [integer(len(data))]
    return probe.run('decl', 'check_unit', args)


def suite():
    probe = Probe()
    try:
        for text in POS:
            data = text.encode()
            assert len(data) <= 256, text
            args = [blob(p) for p in split4(data)] + [integer(len(data))]
            got = probe.run('decl', 'parse_unit', args)
            oracle = probe.send({'oracle': text})
            assert not oracle['diagnostics'], (text, oracle['diagnostics'])
            assert got['ok'], (text, got['err_start'], got['err_end'])
            a = oracle['ast']
            src = data
            want_uses = [(u['module']['text'], u['alias']['text'] if u.get('alias') else None) for u in a['uses']]
            have_uses = [(src[u['module_start']:u['module_end']].decode(),
                          src[u['alias_start']:u['alias_end']].decode() if u['has_alias'] else None)
                         for u in flist(got['uses'], 1)]
            assert want_uses == have_uses, (text, have_uses, want_uses)
            want_recs = [(r['name']['text'], [(f['name']['text'], f['value_type']['text']) for f in r['fields']])
                         for r in a['record_types']]
            have_recs = [(src[r['name_start']:r['name_end']].decode(),
                          [norm_field(f, src) for f in flist(r['fields'], 1)]) for r in flist(got['records'], 1)]
            assert want_recs == have_recs, (text, have_recs, want_recs)
            assert len(a['functions']) == len(flist(got['fns'], 1)), text
            for wf, hf in zip(a['functions'], flist(got['fns'], 1)):
                s = hf['sig']
                assert src[s['name_start']:s['name_end']].decode() == wf['name']['text'], text
                assert ([norm_field(f, src) for f in flist(s['params'], 1)] ==
                        [(p['name']['text'], p['value_type']['text']) for p in wf['inputs']]), text
                assert ([norm_field(f, src) for f in flist(s['results'], 1)] ==
                        [(p['name']['text'], p['value_type']['text']) for p in wf['outputs']]), text
                wstmts = canon_proj([onorm_stmt(x) for x in wf['body']['statements']] +
                                    [('ret', onorm_expr(wf['body']['returned_value']))])
                hstmts = canon_proj([norm_stmt(x, src) for x in flist(hf['body']['body'], 1)] +
                                    [('ret', norm_expr(hf['body']['ret'], src))])
                assert [deep(x) for x in wstmts] == [deep(x) for x in hstmts], (text, hstmts, wstmts)
        for text in NEG:
            data = text.encode()
            assert len(data) <= 256, text
            args = [blob(p) for p in split4(data)] + [integer(len(data))]
            got = probe.run('decl', 'parse_unit', args)
            oracle = probe.send({'oracle': text})
            odiags = oracle['diagnostics'] or []
            assert odiags, text
            assert not got['ok'], text
            ospan = (odiags[0]['span']['start'], odiags[0]['span']['end'])
            assert (got['err_start'], got['err_end']) == ospan, (text, got, ospan)
        for text, stage in CHECKS:
            got = check_unit_case(probe, text)
            assert got['stage'] == stage, (text, got)
            if stage == 4:
                assert got['ok'] and got['ir_ok'] and got['fn_count'] >= 1 and got['expr_count'] >= 1, (text, got)
            else:
                assert not got['ok'], (text, got)
        return {'requests': probe.count, 'pos': len(POS), 'neg': len(NEG), 'checks': len(CHECKS),
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
              'scope': 'decl.parse_unit structural + first-error-span differential vs Stage-0; decl.check_unit symbol/resolve/IR verdicts; reference interpreter execution'}
    (OUT / 'decl-results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
