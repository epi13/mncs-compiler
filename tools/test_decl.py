#!/usr/bin/env python3
"""Declaration-vertical differential: decl.parse_unit vs Stage-0 oracle plus
decl.check_unit symbol/resolve/IR verdicts. Temporary test transport; all
parsing, checking, and lowering execute in MNCS or Stage-0. Python only
moves bytes and compares against the oracle on every run (no goldens).

Kept small on purpose: this focused corpus guards key properties with a
twin determinism run. The retained backend is the normal fast path when
available; the reference interpreter remains an independent fallback.
"""
import backend_policy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP_TARGET = Path(os.environ.get("MNCS_BOOTSTRAP_TARGET_DIR", ROOT / ".bootstrap" / "target"))
os.chdir(ROOT)
OUT = ROOT / '.build'
OUT.mkdir(exist_ok=True)
# Native self-ingestion results can contain more than a thousand linked
# declaration nodes; keep the Python wire decoder above that structural depth.
sys.setrecursionlimit(10000)

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
        return ('not', norm_expr(p['value'], src), (p['start'], p['end']))
    if v == 4:
        return ('rep', norm_expr(p['value'], src),
                src[p['count_start']:p['count_end']].decode(),
                (p['start'], p['end']))
    if v == 9:
        arms = []
        for arm in flist(p['arms'], 1):
            pattern = arm['pattern']
            bindings = [(src[b['field_start']:b['field_end']].decode(),
                          src[b['name_start']:b['name_end']].decode())
                         for b in flist(pattern['bindings'], 1)]
            arms.append((('finite-pattern',
                          src[pattern['qualifier_start']:pattern['qualifier_end']].decode() if pattern['has_qualifier'] else None,
                          (pattern['qualifier_start'], pattern['qualifier_end']) if pattern['has_qualifier'] else None,
                          src[pattern['variant_start']:pattern['variant_end']].decode(),
                          (pattern['variant_start'], pattern['variant_end']),
                          pattern['has_payload'], pattern['ignore_payload'], bindings,
                          (pattern['start'], pattern['end'])),
                         norm_expr(arm['result'], src)))
        return ('match', norm_expr(p['subject'], src), arms, (p['start'], p['end']))
    if v == 10:
        fields = [(src[f['field_start']:f['field_end']].decode(), norm_expr(f['value'], src))
                  for f in flist(p['fields'], 1)]
        type_name = src[p['type_start']:p['type_end']].decode()
        variant = src[p['variant_start']:p['variant_end']].decode()
        if not fields:
            return ('path', type_name, (p['type_start'], p['type_end']),
                    [(variant, (p['variant_start'], p['variant_end']))], (p['start'], p['end']))
        return ('enum', type_name, variant, fields, (p['start'], p['end']))
    # Keep these discriminants aligned with decl.Expr after the Profile 0.18
    # UnaryNot, Repeat, and ScalarMatch variants were added ahead of Binary.
    # RecordConstruct (12), Index (13), Cast (14), and Slice (15) are
    # appended last so existing discriminants hold.
    if v == 12:
        return ('rec', src[p['type_start']:p['type_end']].decode(),
                norm_expr(p['base'], src) if p['has_base'] else None,
                [(src[f['field_start']:f['field_end']].decode(), norm_expr(f['value'], src))
                 for f in flist(p['fields'], 1)],
                (p['start'], p['end']))
    if v == 13:
        return ('idx', norm_expr(p['base'], src), norm_expr(p['index'], src),
                (p['start'], p['end']))
    if v == 14:
        return ('cast', norm_expr(p['value'], src),
                src[p['type_start']:p['type_end']].decode(),
                (p['start'], p['end']))
    if v == 15:
        return ('slice', norm_expr(p['base'], src), norm_expr(p['start_index'], src),
                norm_expr(p['end_index'], src), (p['start'], p['end']))
    if v == 16:
        return ('seq', [norm_expr(x, src) for x in flist(p['elements'], 1)],
                (p['start'], p['end']))
    if v == 6:
        return ('bin', OPNAME[p['op']], norm_expr(p['left'], src), norm_expr(p['right'], src),
                (p['start'], p['end']))
    if v == 7:
        return ('call', src[p['name_start']:p['name_end']].decode(),
                [src[t['start']:t['end']].decode() for t in flist(p['targs'], 1)],
                [norm_expr(a, src) for a in flist(p['args'], 1)],
                (p['start'], p['end']))
    if v == 8:
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
        return ('call', e[1], e[2], [canon_proj(a) for a in e[3]], e[4])
    if isinstance(e, tuple) and e and e[0] == 'rec':
        return ('rec', e[1], canon_proj(e[2]),
                [(f, canon_proj(v)) for f, v in e[3]], e[4])
    if isinstance(e, tuple) and e and e[0] == 'not':
        return ('not', canon_proj(e[1]), e[2])
    if isinstance(e, tuple) and e and e[0] == 'enum':
        return ('enum', e[1], e[2],
                [(f, canon_proj(v)) for f, v in e[3]], e[4])
    if isinstance(e, tuple) and e and e[0] == 'rep':
        return ('rep', canon_proj(e[1]), e[2], e[3])
    if isinstance(e, tuple) and e and e[0] == 'idx':
        return ('idx', canon_proj(e[1]), canon_proj(e[2]), e[3])
    if isinstance(e, tuple) and e and e[0] == 'cast':
        return ('cast', canon_proj(e[1]), e[2], e[3])
    if isinstance(e, tuple) and e and e[0] == 'slice':
        return ('slice', canon_proj(e[1]), canon_proj(e[2]), canon_proj(e[3]), e[4])
    if isinstance(e, tuple) and e and e[0] == 'seq':
        return ('seq', [canon_proj(x) for x in e[1]], e[2])
    if isinstance(e, list):
        return [canon_proj(x) for x in e]
    if isinstance(e, tuple) and e and e[0] == 'let':
        return ('let', e[1], e[2], canon_proj(e[3]))
    if isinstance(e, tuple) and e and e[0] == 'if':
        return ('if', canon_proj(e[1]), [canon_proj(x) for x in e[2]], [canon_proj(x) for x in e[3]])
    if isinstance(e, tuple) and e and e[0] == 'ret':
        return ('ret', canon_proj(e[1]))
    if isinstance(e, tuple) and e and e[0] == 'iter':
        return ('iter', e[1], canon_proj(e[2]), e[3], e[4], e[5],
                canon_proj(e[6]), [canon_proj(x) for x in e[7]], e[8],
                canon_proj(e[9]))
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
    if v == 4:
        return ('iter', src[p['name_start']:p['name_end']].decode(),
                norm_expr(p['over_source'], src) if p['has_over_source'] else None,
                src[p['bound_start']:p['bound_end']].decode(),
                src[p['state_start']:p['state_end']].decode(),
                src[p['type_start']:p['type_end']].decode(),
                norm_expr(p['initial'], src),
                [norm_stmt(x, src) for x in flist(p['body'], 1)],
                src[p['next_start']:p['next_end']].decode(),
                norm_expr(p['next_value'], src))
    raise AssertionError(v)


def ast_expr_span(e):
    tag = next(iter(e.keys()))
    b = e[tag]
    if tag in ('Name', 'Boolean'):
        return b['text']['span'] if tag == 'Boolean' else b['span']
    if tag == 'Integer':
        return b['text']['span']
    return b['span']


def onorm_expr(e, src=None):
    tag = next(iter(e.keys()))
    b = e[tag]
    if tag == 'Name':
        return ('name', b['text'])
    if tag == 'Integer':
        return ('int', b['value'], (b['text']['span']['start'], b['text']['span']['end']))
    if tag == 'Boolean':
        return ('bool', b['value'])
    if tag == 'Binary':
        return ('bin', b['op'], onorm_expr(b['left'], src), onorm_expr(b['right'], src),
                (b['span']['start'], b['span']['end']))
    if tag == 'Call':
        return ('call', b['function']['text'],
                [a['text']['text'] for a in b.get('generic_args', [])],
                [onorm_expr(a, src) for a in b['arguments']],
                (b['span']['start'], b['span']['end']))
    if tag == 'FieldProject':
        f = b['field']
        return ('proj', onorm_expr(b['base'], src), (f['text'], (f['span']['start'], f['span']['end'])),
                (b['span']['start'], b['span']['end']))
    if tag == 'FiniteVariant':
        t, v = b['type_name'], b['variant']
        if b.get('fields'):
            return ('enum', t['text'], v['text'],
                    [(f[0]['text'], onorm_expr(f[1], src)) for f in b['fields']],
                    (b['span']['start'], b['span']['end']))
        return ('path', t['text'], (t['span']['start'], t['span']['end']),
                [(v['text'], (v['span']['start'], v['span']['end']))],
                (b['span']['start'], b['span']['end']))
    if tag == 'QualifiedPath':
        segs = b['segments']
        return ('path', segs[0]['text'], (segs[0]['span']['start'], segs[0]['span']['end']),
                [(s['text'], (s['span']['start'], s['span']['end'])) for s in segs[1:]],
                (b['span']['start'], b['span']['end']))
    if tag == 'RecordLiteral':
        return ('rec', b['type_name']['text'],
                onorm_expr(b['base'], src) if b.get('base') else None,
                [(f[0]['text'], onorm_expr(f[1], src)) for f in b['fields']],
                (b['span']['start'], b['span']['end']))
    if tag == 'Match':
        arms = []
        for arm in b['arms']:
            variant = arm['variant']
            qualifier = arm.get('type_name')
            value_start = ast_expr_span(arm['value'])['start']
            pattern_start = qualifier['span']['start'] if qualifier else variant['span']['start']
            pattern_end = variant['span']['end']
            if src is not None:
                arrow = src.find(b'=>', pattern_start, value_start)
                if arrow >= 0:
                    pattern_end = arrow
                    while pattern_end > pattern_start and src[pattern_end - 1] in b' \t\r\n':
                        pattern_end -= 1
            has_payload = bool(arm.get('bindings')) or bool(arm.get('ignore_payload'))
            arms.append((('finite-pattern',
                          qualifier['text'] if qualifier else None,
                          (qualifier['span']['start'], qualifier['span']['end']) if qualifier else None,
                          variant['text'], (variant['span']['start'], variant['span']['end']),
                          has_payload, bool(arm.get('ignore_payload')),
                          [(f['text'], name['text']) for f, name in arm.get('bindings', [])],
                          (pattern_start, pattern_end)),
                         onorm_expr(arm['value'], src)))
        return ('match', onorm_expr(b['value'], src), arms,
                (b['span']['start'], b['span']['end']))
    if tag == 'Not':
        return ('not', onorm_expr(b['value'], src),
                (b['span']['start'], b['span']['end']))
    if tag == 'SequenceRepeat':
        return ('rep', onorm_expr(b['element'], src), b['count']['text'],
                (b['span']['start'], b['span']['end']))
    if tag == 'SequenceLiteral':
        return ('seq', [onorm_expr(x, src) for x in b['elements']],
                (b['span']['start'], b['span']['end']))
    if tag == 'Index':
        return ('idx', onorm_expr(b['base'], src), onorm_expr(b['index'], src),
                (b['span']['start'], b['span']['end']))
    if tag == 'Slice':
        return ('slice', onorm_expr(b['base'], src), onorm_expr(b['start'], src),
                onorm_expr(b['end'], src), (b['span']['start'], b['span']['end']))
    if tag == 'Cast':
        return ('cast', onorm_expr(b['value'], src), b['target_type']['text'],
                (b['span']['start'], b['span']['end']))
    return ('OTHER', tag)


def onorm_stmt(s, src=None):
    tag = next(iter(s.keys()))
    b = s[tag]
    if tag == 'Let':
        return ('let', b['name']['text'], b['value_type']['text'], onorm_expr(b['value'], src))
    if tag == 'If':
        return ('if', onorm_expr(b['condition'], src),
                [onorm_stmt(x, src) for x in b['then_body']],
                [onorm_stmt(x, src) for x in b['else_body']])
    if tag == 'Fail':
        return ('fail', b['mode']['text'])
    if tag == 'Return':
        return ('ret', onorm_expr(b['value'], src))
    if tag == 'BoundedIteration':
        return ('iter', b['name']['text'],
                onorm_expr(b['over_source'], src) if b.get('over_source') else None,
                b['bound']['text'],
                b['state']['text'], b['state_type']['text'],
                onorm_expr(b['initial'], src),
                [onorm_stmt(x, src) for x in b['body']],
                b['next_state']['text'], onorm_expr(b['next_value'], src))
    return ('OTHER', tag)


def strip_spans(e):
    if isinstance(e, tuple) and e and e[0] == 'rec':
        return ('rec', e[1], strip_spans(e[2]),
                [(f, strip_spans(v)) for f, v in e[3]])
    if isinstance(e, tuple) and e and e[0] in ('bin', 'call', 'proj', 'pathproj', 'int', 'idx', 'cast', 'rep', 'not', 'seq'):
        return (e[0],) + tuple(strip_spans(x) for x in e[1:-1])
    if isinstance(e, tuple) and e and e[0] == 'enum':
        return ('enum', e[1], e[2], [(f, strip_spans(v)) for f, v in e[3]])
    if isinstance(e, list):
        return [strip_spans(x) for x in e]
    return e


def deep(e):
    if isinstance(e, tuple) and e and e[0] == 'match':
        return ('match', deep(e[1]), [(pattern, deep(value)) for pattern, value in e[2]])
    if isinstance(e, tuple) and e and e[0] in ('bin', 'call', 'proj', 'pathproj', 'int', 'rec', 'idx', 'cast', 'rep', 'not', 'enum', 'seq'):
        return strip_spans(e)
    if isinstance(e, tuple) and e and e[0] == 'let':
        return ('let', e[1], e[2], deep(e[3]))
    if isinstance(e, tuple) and e and e[0] == 'if':
        return ('if', deep(e[1]), [deep(x) for x in e[2]], [deep(x) for x in e[3]])
    if isinstance(e, tuple) and e and e[0] == 'ret':
        return ('ret', deep(e[1]))
    if isinstance(e, tuple) and e and e[0] == 'iter':
        return ('iter', e[1], deep(e[2]), e[3], e[4], e[5], deep(e[6]),
                [deep(x) for x in e[7]], e[8], deep(e[9]))
    return e


POS = [
    'mncs 0.10; module t; fn f(a: u64, b: u64) -> (result: u64) { return a + b; }',
    '// Kinds — never spans — cross unit boundaries.\nmncs 0.18; module t; fn f() -> (r: u64) { return 0; }',
    # Qualified finite match patterns share one normalized representation in
    # the top-level parser and the explicit nested-match state machine.
    'mncs 0.10; module t; enum F { No, Yes } fn f(s: u64) -> (r: u64) { return match s { No => 1, Yes => 0 }; }',
    'mncs 0.6; module t; enum F { No, Yes } fn f(s: u64) -> (r: u64) { return match s { F.No => 1, F.Yes => 0 }; }',
    'mncs 0.9; module t; enum F { No, Yes } fn f(s: u64) -> (r: u64) { return match s { pkg.F.No => 1, pkg.F.Yes => 0 }; }',
    'mncs 0.18; module t; enum F { Item { value: u64 }, No } fn f(s: u64) -> (r: u64) { return match s { F.Item { value: n } => n, F.No => 0 }; }',
    'mncs 0.18; module t; enum F { Item { value: u64 }, No } fn f(s: u64) -> (r: u64) { return match s { F.Item { value } => value, F.No => 0 }; }',
    'mncs 0.18; module t; enum F { Item { value: u64 }, No } fn f(s: u64) -> (r: u64) { return match s { F.Item { .. } => 1, F.No => 0 }; }',
    'mncs 0.18; module t; enum F { _, No } fn f(s: u64) -> (r: u64) { return match s { F._ => 1, F.No => 0 }; }',
    'mncs 0.18; module t; enum F { No, Yes } fn f(s: u64) -> (r: u64) { return match s { F.No => match s { F.Yes => 1, _ => 0 }, F.Yes => 0 }; }',
    # The explicit nested-match machine shares the regular expression prefix
    # gates: unary `!` must remain valid after a chained arm-result operator.
    'mncs 0.18; module t; enum F { No, Item { left: u64, right: u64 } } fn g<P: Nat, N: Nat>(x: u64, y: u64) -> (r: bool) { return true; } fn f<P: Nat, N: Nat>(s: F) -> (r: bool) { return match s { No => true, Item { left: l, right: r } => l < 3 && r < 4 && g<P, N>(l, r) && !(g<P, N>(l, r)) }; }',
    'mncs 0.18; module t; enum F { No, Yes } fn f(s: F, a: bool, b: bool) -> (r: bool) { return match s { No => true, Yes => match s { No => a && b && !(a), Yes => true } }; }',
    # `next` is a contextual field name from Profile 0.13 in record literals,
    # finite payload constructors, and payload bindings.
    'mncs 0.18; module t; record R { next: u64 } fn f(v: u64) -> (r: R) { return R { next: v }; }',
    'mncs 0.18; module t; enum E { Item { next: u64 }, No } fn f(v: u64) -> (r: E) { return E.Item { next: v }; }',
    'mncs 0.18; module t; enum E { Item { next: u64 }, No } fn f(x: E) -> (r: u64) { return match x { Item { next: n } => n, No => 0 }; }',
    'mncs 0.10; module t; use a.b as c; record R { x: u64 } fn f(v: R) -> (r: u64) { return v.x; }',
    # Multi-segment paths stop at the first non-dot token without rescanning
    # that terminal token through the remainder of the defensive path bound.
    'mncs 0.18; module demo.alpha.beta.gamma; use demo.source.segment.lexer as lib; fn f(x: u64) -> (r: u64) { return x; }',
    'mncs 0.10; module t; fn f(a: u64) -> (r: u64) { if a == 1 { return 1; } else { return 2; } return 0; }',
    # An inner if without `else` must finish without consuming the parent's
    # closing brace; the block parser reprocesses that token on its frame stack.
    'mncs 0.18; module t; fn f(a: u64, b: u64) -> (r: u64) { if a == 1 { if b == 1 { return 2; } } return 0; }',
    # Generic-aware declaration surface: params, bounds, type-argument
    # calls, nested sequences, `<` disambiguation, and backtracking.
    'mncs 0.18; module t; fn f<N: Nat>(x: u64) -> (r: u64) { return x; }',
    'mncs 0.18; module t; fn h<F: Type -> Type, T>(v: u64) -> (r: u64) { return v; }',
    'mncs 0.18; module t; use a.b as c; fn f<N: Nat>(x: u64) -> (r: u64) { return c.g<N>(x); }',
    'mncs 0.18; module t; fn g<N: Nat>(x: u64) -> (r: u64) { return x; } fn f(x: u64) -> (r: u64) { return g<4>(x); }',
    'mncs 0.18; module t; fn f<N: Nat>(p: [[byte; up_to N]; up_to 4]) -> (r: u64) { return 0; }',
    'mncs 0.18; module t; fn f(a: u64, b: u64) -> (r: bool) { return a < b; }',
    'mncs 0.18; module t; fn f(a: u64, b: u64) -> (r: bool) { return a<1+2>(b); }',
    # Record construction literals and `..base` updates, including trailing
    # commas, projections over literals, and nested literal values.
    'mncs 0.18; module t; record R { x: u64 } fn f() -> (r: u64) { let v: R = R { x: 1 }; return v.x; }',
    'mncs 0.18; module t; record R { x: u64, y: u64 } fn f() -> (r: u64) { let v: R = R { x: 1, y: 2 }; return v.x; }',
    'mncs 0.18; module t; record R { x: u64, y: u64 } fn f(v: R) -> (r: u64) { let w: R = R { ..v, x: 2 }; return w.x; }',
    'mncs 0.18; module t; record R { x: u64 } fn f(v: R) -> (r: u64) { let w: R = R { ..v }; return w.x; }',
    # A leading `..base` disambiguates a qualified nominal path as a record
    # update under Stage-0's parse tree.
    'mncs 0.18; module t; use a.b as m; record R { x: u64, y: u64 } fn f(parameter: R, ty: R) -> (r: R) { return m.R { ..parameter, y: ty.x }; }',
    'mncs 0.18; module t; record R { x: u64 } fn f() -> (r: u64) { let v: R = R { x: 1, }; return v.x; }',
    'mncs 0.18; module t; record R { x: u64 } fn f(v: R) -> (r: u64) { return R { ..v, x: v.x }; }',
    'mncs 0.18; module t; record S { y: u64 } record R { x: u64 } fn f(s: S) -> (r: u64) { let v: R = R { x: s.y }; return v.x; }',
    # Repeat expressions retain an enclosing record-construction frame,
    # including a cast in the repeated element.
    'mncs 0.18; module t; record R { bytes: [byte; up_to 4] } fn f() -> (r: R) { return R { bytes: [0 as byte; 4] }; }',
    # Sequence literals share the explicit bracket machine with repeats,
    # including old profiles, empty/trailing-comma lists, nested lists,
    # postfix indexing, and record-field values.
    'mncs 0.10; module t; fn f() -> (r: u64) { return [1, 2]; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { return []; }',
    'mncs 0.13; module t; fn f() -> (r: u64) { return [1, 2,]; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { return [1 as byte, 2 + 3 as byte,]; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { return [[1, 2], [3, 4]]; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { return [1, 2][0]; }',
    'mncs 0.18; module t; record R { bytes: [byte; up_to 2] } fn f() -> (r: R) { return R { bytes: [0 as byte, 1 as byte] }; }',
    # Bounded iteration: `up_to` and `over` headers, body statements,
    # nesting, sequencing after `if`, and record carried state.
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { next s = s; } return s; }',
    # A qualified projection at the end of a carry initializer is followed
    # by the loop body brace. Constructor lookahead must leave that brace to
    # the block parser, while still accepting empty finite payloads.
    'mncs 0.18; module t; fn f(left: u64, right: u64) -> (r: bool) { iterate i up_to 2 carrying same: bool = left.len == right.len { next same = same; } return same; }',
    'mncs 0.18; module t; enum F { No } fn f() -> (r: F) { return F.No {}; }',
    'mncs 0.18; module t; fn f(xs: u64) -> (r: u64) { iterate i over xs carrying s: u64 = 0 { next s = s; } return s; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { iterate i over g(x) carrying s: u64 = x { let y: u64 = i; next s = y; } return s; }',
    'mncs 0.18; module t; fn f(a: u64) -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { if a == 1 { return 1; } else { return 2; } next s = s; } return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { iterate j up_to 2 carrying t: u64 = s { next t = t; } next s = t; } return s; }',
    'mncs 0.18; module t; fn f(a: u64) -> (r: u64) { if a == 1 { return 1; } iterate i up_to 4 carrying s: u64 = 0 { next s = s; } return s; }',
    'mncs 0.18; module t; fn f(a: u64) -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { if a == 1 { return 1; } next s = s; } return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { next s = s; } iterate j up_to 2 carrying t: u64 = s { next t = t; } return t; }',
    'mncs 0.18; module t; fn f(a: u64) -> (r: u64) { if a == 1 { iterate i up_to 4 carrying s: u64 = 0 { next s = s; } return s; } return 0; }',
    'mncs 0.18; module t; record R { x: u64 } fn f(v: R) -> (r: u64) { iterate i up_to 4 carrying s: R = v { next s = s; } return s.x; }',
    # Sequence index and `as` casts over open postfix chains, including
    # chained index, cast/b operator association, and group-cast reentry.
    'mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: u64) { return x[i]; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: u64) { return x[i] as u64; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return x as u64; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: u64) { return g(x)[i]; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64, j: u64) -> (r: u64) { return x[i][j]; }',
    # Range slices share the postfix index machine and retain base/lower/upper
    # expressions, including binary endpoints and an enclosing iteration.
    'mncs 0.7; module t; fn f(x: u64, a: u64, b: u64) -> (r: u64) { return x[a + 1..b + 2]; }',
    'mncs 0.7; module t; fn f(x: u64, a: u64, b: u64) -> (r: u64) { iterate i over x[a..b] carrying s: u64 = 0 { next s = s; } return s; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: u64) { return x[i].y; }',
    'mncs 0.18; module t; fn f(a: u64, b: u64) -> (r: u64) { return a + b as u64; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: bool) { return x[i] as u64 < 128; }',
    'mncs 0.18; module t; fn f(a: u64, b: u64) -> (r: u64) { return (a + b) as u64; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return (x as u64) as u64; }',
    'mncs 0.18; module t; record R { x: u64 } fn f(v: R, i: u64) -> (r: u64) { return v.x[i]; }',
    'mncs 0.18; module t; record R { x: u64 } fn f(x: u64) -> (r: u64) { return x as R; }',
    'mncs 0.10; module t; fn f(x: u64, i: u64) -> (r: u64) { return x[i]; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: u64) { return [x; 2][i]; }',
    'mncs 0.18; module t; use a.b as c; fn f(x: u64) -> (r: u64) { return c.g(x) as u64; }',
    # Operators inside construct field values reduce before the closing
    # brace on both sides.
    'mncs 0.18; module t; record R { x: u64 } fn f(s: u64) -> (r: u64) { return R { x: s + 1 }; }',
    'mncs 0.18; module t; record R { x: u64, y: u64 } fn f(s: u64, t: u64) -> (r: u64) { return R { x: s, y: t + 1 }; }',
    'mncs 0.18; module t; record R { x: bool } fn f(b: bool) -> (r: bool) { return R { x: !b }; }',
    'mncs 0.18; module t; enum F { V { x: u64 } } fn f(a: u64, b: u64) -> (r: u64) { return F.V { x: a + b }; }',
    # Multi-segment qualified finite payload construction retains the
    # qualified type span and final variant identity from Profile 0.13.
    'mncs 0.18; module t; enum E { V { value: u64 } } fn f(x: u64) -> (r: u64) { return pkg.E.V { value: x }; }',
    'mncs 0.18; module t; enum F { No, Yes } fn f() -> (r: u64) { return F.No; }',
]

NEG = [
    'mncs 0.10; module t;',
    'mncs 0.18; module ;',
    # A dotted path with no following identifier keeps Stage-0's first-error
    # span at the token after the separator.
    'mncs 0.18; module demo.root; use demo.alpha. as dep; fn f(x: u64) -> (r: u64) { return x; }',
    'mncs 0.10; module t; record R {}',
    'mncs 0.10; module t; fn f() -> (r: u64) { return 1 + ; }',
    'mncs 0.18; module t; enum F { No } fn f(s: u64) -> (r: u64) { return match s { F. => 0 }; }',
    'mncs 0.18; module t; enum F { No } fn f(s: u64) -> (r: u64) { return match s { F..No => 0 }; }',
    'mncs 0.18; module t; enum F { No, Yes } fn f(s: F, a: bool, b: bool) -> (r: bool) { return match s { No => true, Yes => a && b && !(a }; }',
    'mncs 0.5; module t; enum F { No, Yes } fn f(s: u64) -> (r: u64) { return match s { F.No => 1, F.Yes => 0 }; }',
    'mncs 0.6; module t; enum F { No, Yes } fn f(s: u64) -> (r: u64) { return match s { pkg.F.No => 1, pkg.F.Yes => 0 }; }',
    'mncs 0.18; module t; enum F { No } fn f(s: bool) -> (r: u64) { return match s { true.No => 1, false => 0 }; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return pkg..E.V { value: x }; }',
    # A finite payload brace must begin with an empty payload or a field
    # label and colon; otherwise the opening brace remains the expression
    # delimiter reported by Stage-0.
    'mncs 0.18; module t; enum F { Item { value: u64 } } fn f(x: u64) -> (r: F) { return F.Item { value }; }',
    'mncs 0.18; module t; use a.b as m; record R { x: u64 } fn f(x: u64) -> (r: R) { return m.R { .., x: x }; }',
    # Generic first-error spans: duplicate params (MNP186), constraint
    # validation (MNP187), and the Profile 0.10 gate (MNP184/MNP189).
    'mncs 0.18; module t; fn f<T, T>(x: u64) -> (r: u64) { return x; }',
    'mncs 0.18; module t; fn f<N: Foo>(x: u64) -> (r: u64) { return x; }',
    'mncs 0.09; module t; fn f<T>(x: u64) -> (r: u64) { return x; }',
    'mncs 0.09; module t; use a.b as c; fn f(x: u64) -> (r: u64) { return c.g<N>(x); }',
    # Record-literal first-error spans: missing value/name/base land where
    # Stage-0 reports them; a late `..` and a non-literal brace fail at the
    # brace or dots on both sides.
    'mncs 0.18; module t; record R { x: u64 } fn f() -> (r: u64) { let v: R = R { x: }; return v.x; }',
    'mncs 0.18; module t; record R { x: u64 } fn f() -> (r: u64) { let v: R = R { x }; return v.x; }',
    'mncs 0.18; module t; record R { x: u64 } fn f() -> (r: u64) { let v: R = R { .. }; return v.x; }',
    'mncs 0.18; module t; record R { x: u64 } fn f(v: R) -> (r: u64) { let w: R = R { x: 1, ..v }; return w.x; }',
    'mncs 0.18; module t; record R { x: u64 } fn f() -> (r: u64) { let v: R = R {}; return v.x; }',
    'mncs 0.18; module t; record R { x: u64 } fn f() -> (r: u64) { let v: R = R { 1: 2 }; return v.x; }',
    # Stage-0 ends a record literal before any `.`, so a projection directly
    # off a literal fails at the dot on both sides.
    'mncs 0.18; module t; record R { x: u64 } fn f(v: R) -> (r: u64) { return R { ..v, x: v.x }.x; }',
    'mncs 0.18; module t; record S { y: u64 } record R { x: u64 } fn f() -> (r: u64) { let v: R = R { x: S { y: 1 }.y }; return v.x; }',
    # Iterate first-error spans: a body must end with `next`; a `next`
    # outside a body fails at the keyword; header truncations fail at the
    # offending token on both sides.
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { } return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { next s = s; return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { if s == 1 { next s = s; } } return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 { next s = s; } return s; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { iterate i up_to x carrying s: u64 = 0 { next s = s; } return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate up_to 4 carrying s: u64 = 0 { next s = s; } return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { next s = ; } return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { next s = s } return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { next s = s; return s; } return s; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { iterate i over carrying s: u64 = 0 { next s = s; } return s; }',
    # Index/cast first-error spans: empty/unclosed/multi indexes, missing
    # cast targets, and the chain-closing rules (no index over groups or
    # casts, no chained casts) fail at the delimiter on both sides.
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return x[]; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: u64) { return x[i; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: u64) { return x[i, i]; }',
    'mncs 0.18; module t; record R { bytes: [byte; up_to 4] } fn f() -> (r: R) { return R { bytes: [0 as byte;] }; }',
    # List separators cannot repeat or mix with the Profile 0.13 repeat
    # separator; lists are already part of Profile 0.10.
    'mncs 0.18; module t; fn f() -> (r: u64) { return [1,, 2]; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { return [1, 2; 3]; }',
    'mncs 0.10; module t; fn f() -> (r: u64) { return [1; 2]; }',
    'mncs 0.18; module t; fn f() -> (r: u64) { return [1, 2; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return x as; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return x as 5; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: u64) { return (x)[i]; }',
    'mncs 0.18; module t; fn f(a: u64, b: u64, i: u64) -> (r: u64) { return (a + b)[i]; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return x as u64 as u64; }',
    'mncs 0.18; module t; fn f(x: u64, i: u64, j: u64) -> (r: u64) { return x as u64[i]; }',
    # Grouped names cannot acquire a field projection or call suffix in
    # Stage-0; native postfix handling must preserve that distinction.
    'mncs 0.18; module t; fn f(a: u64) -> (r: u64) { return (a).x; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return (f)(x); }',
    'mncs 0.6; module t; fn f(x: u64, a: u64, b: u64) -> (r: u64) { return x[a..b]; }',
    'mncs 0.7; module t; fn f(x: u64, a: u64) -> (r: u64) { return x[a..]; }',
    'mncs 0.7; module t; fn f(x: u64, a: u64, b: u64, c: u64) -> (r: u64) { return x[a..b..c]; }',
    'mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return as u64; }',
    # Truncated nested blocks still report Stage-0's EOF span.
    'mncs 0.18; module t; fn f(a: u64, b: u64) -> (r: u64) { if a == 1 { if b == 1 { return 2; } } return 0;',
    # Truncated operators inside construct fields fail at the delimiter.
    'mncs 0.18; module t; record R { x: u64 } fn f(a: u64) -> (r: u64) { return R { x: a + }; }',
    'mncs 0.18; module t; record R { x: u64, y: u64 } fn f(a: u64, b: u64) -> (r: u64) { return R { x: a + , y: b }; }',
]

# check_unit verdicts: (source, expected stage). Stages: 1 duplicate symbol,
# 2 resolve/span walk, 4 fully verified (0/3 are parse/never-IR-fail paths).
CHECKS = [
    ('mncs 0.10; module t; fn f(a: u64) -> (r: u64) { return a + 1; }', 4),
    ('mncs 0.10; module t; fn f() -> (r: u64) { return 0; } fn f() -> (r: u64) { return 1; }', 1),
    ('mncs 0.10; module t; fn f() -> (r: u64) { return g(1); }', 2),
    # Generic signatures verify; alias-qualified calls stay stage 2 in
    # single-unit scope (imports resolve at the project layer).
    ('mncs 0.18; module t; fn f<N: Nat>(x: u64) -> (r: u64) { return x; }', 4),
    ('mncs 0.18; module t; use a.b as c; fn f(x: u64) -> (r: u64) { return c.g(x); }', 2),
    # Iterate bodies resolve and lower structurally at check_unit; loop
    # checking itself stays an explicit prove_unit failure (kind-2 obl).
    ('mncs 0.18; module t; fn f() -> (r: u64) { iterate i up_to 4 carrying s: u64 = 0 { next s = s; } return s; }', 4),
    # Index and cast resolve and lower structurally at check_unit; their
    # checking stays an explicit prove_unit failure like records/loops.
    ('mncs 0.18; module t; fn f(x: u64, i: u64) -> (r: u64) { return x[i]; }', 4),
    ('mncs 0.18; module t; fn f(x: u64) -> (r: u64) { return x as u64; }', 4),
    # Slice operands are resolved and structurally lowered; bounded-view
    # typing/proof remains a separate deeper frontier.
    ('mncs 0.7; module t; fn f(x: u64, a: u64, b: u64) -> (r: u64) { return x[a..b]; }', 4),
]

# Self-ingestion: real compiler modules the native parser must consume
# whole, with oracle agreement on declaration facts.
SELF_INGEST = ['src/compiler/segment.mncs', 'src/compiler/parser.mncs', 'src/compiler/source.mncs',
               'src/compiler/decl.mncs',
               'src/compiler/flow.mncs', 'src/compiler/lexer.mncs', 'src/compiler/ssa.mncs',
               'src/compiler/kernel.mncs', 'src/compiler/project.mncs']
SELF_INGEST_PAGE_BOUNDS = {'src/compiler/ssa.mncs': 217}

SOURCE_BOUND = max(len(text.encode()) for text in POS + NEG + [item[0] for item in CHECKS])


def source_bytes(text):
    raw = text.encode() if isinstance(text, str) else bytes(text)
    assert len(raw) <= SOURCE_BOUND
    return raw + b' ' * (SOURCE_BOUND - len(raw))


def nat_arg(value):
    return {'kind': 'nat', 'value': value}


# CP-0021: declaration/proof entry points consume the logical paged source.
# Unit suites transport each padded view as one canonical single-page
# composition (stride 1024, total = view length).
PAGE_BOUND = 1024
STRIDE_BOUND = 1024


def pages_value(chunks):
    return {'sequence': {'values': [blob(chunk) for chunk in chunks]}}


def logical_args(data, stride=STRIDE_BOUND):
    total = len(data)
    assert total <= STRIDE_BOUND
    pages = [data] if total else []
    return [pages_value(pages), integer(stride), integer(total)]


EXECUTION_STEP_BUDGET = 8_000_000


class Probe:
    def __init__(self, page_bound=PAGE_BOUND):
        env = dict(os.environ)
        reference_interpreter = env.get("MNCS_PROBE_BACKEND") == "reference_interpreter"
        if reference_interpreter:
            env.pop("MNCS_PROBE_BACKEND", None)
        env['MNCS_PROBE_MODULES'] = 'source,lexer,parser,segment,decl'
        env['MNCS_PROBE_EXECUTION_MODULES'] = 'mncs.compiler.decl.v1'
        if not reference_interpreter:
            env.setdefault('MNCS_PROBE_BACKEND', backend_policy.resolve('decl'))
        env['MNCS_PROBE_GENERIC_SEEDS'] = json.dumps([
            {'module': 'mncs.compiler.decl.v1', 'function': function,
             'type_arguments': [nat_arg(page_bound), nat_arg(STRIDE_BOUND)]}
            for function in ['parse_unit', 'check_unit', 'prove_unit']
        ])
        env.setdefault('MNCS_PROBE_CACHE_DIR', str(ROOT / '.build' / 'probe-cache'))
        self.proc = subprocess.Popen(
            [env.get('MNCS_PROBE_BIN', str(BOOTSTRAP_TARGET / "release" / "mncs-compiler-stage0-probe"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=ROOT, env=env
        )
        self.count = 0
        self.steps = []
        self.digest = hashlib.sha256()
        self.page_bound = page_bound

    def send(self, request):
        self.proc.stdin.write(json.dumps(request) + '\n')
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f'probe terminated: {self.proc.poll()}'
        return json.loads(line)

    def run(self, unit, function, args, step_budget=EXECUTION_STEP_BUDGET):
        request = {'schema_version': '0.1', 'target': {'module': f'mncs.compiler.{unit}.v1', 'function': function},
                   'arguments': args, 'type_arguments': [nat_arg(self.page_bound), nat_arg(STRIDE_BOUND)], 'step_budget': step_budget}
        result = self.send(request)
        assert result['status'] == 'returned', (function, result)
        self.count += 1
        self.steps.append(result['steps'])
        # Semantic-only digest: steps are cost telemetry (backend-coupled:
        # native reports 1/request, interpreters count SSA steps) and are
        # recorded separately as execution_steps_total/max.
        self.digest.update(json.dumps([request, result['returned']], sort_keys=True).encode())
        return decode(result['returned'][0])

    def close(self):
        self.proc.stdin.close()
        assert self.proc.wait(timeout=30) == 0


def check_unit_case(probe, text):
    return probe.run('decl', 'check_unit', logical_args(source_bytes(text)))


def check_pos_parse(got, oracle, data, text):
    """POS structural assertions shared by the live suite and VM drivers."""
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
        want_gen = [(g['name']['text'], g['constraint']['text'] if g.get('constraint') else None)
                    for g in wf.get('generic_params', [])]
        have_gen = [(src[p['name_start']:p['name_end']].decode(),
                     src[p['bound_start']:p['bound_end']].decode() if p['has_bound'] else None)
                    for p in flist(s['generics'], 1)]
        assert want_gen == have_gen, (text, have_gen, want_gen)
        assert ([norm_field(f, src) for f in flist(s['params'], 1)] ==
                [(p['name']['text'], p['value_type']['text']) for p in wf['inputs']]), text
        assert ([norm_field(f, src) for f in flist(s['results'], 1)] ==
                [(p['name']['text'], p['value_type']['text']) for p in wf['outputs']]), text
        wstmts = canon_proj([onorm_stmt(x, src) for x in wf['body']['statements']] +
                            [('ret', onorm_expr(wf['body']['returned_value'], src))])
        hstmts = canon_proj([norm_stmt(x, src) for x in flist(hf['body']['body'], 1)] +
                            [('ret', norm_expr(hf['body']['ret'], src))])
        assert [deep(x) for x in wstmts] == [deep(x) for x in hstmts], (text, hstmts, wstmts)


def check_neg_parse(got, oracle, text):
    """NEG first-error-span assertions shared by the live suite and VM drivers."""
    odiags = oracle['diagnostics'] or []
    assert odiags, text
    assert not got['ok'], text
    ospan = (odiags[0]['span']['start'], odiags[0]['span']['end'])
    assert (got['err_start'], got['err_end']) == ospan, (text, got, ospan)


def check_check_unit(got, text, stage):
    """check_unit verdict assertions shared by the live suite and VM drivers."""
    assert got['stage'] == stage, (text, got)
    if stage == 4:
        assert got['ok'] and got['ir_ok'] and got['fn_count'] >= 1 and got['expr_count'] >= 1, (text, got)
    else:
        assert not got['ok'], (text, got)


def check_self_ingest(got, oracle, data, relpath):
    """Self-ingestion oracle-agreement assertions shared with VM drivers."""
    assert got['ok'], (relpath, got['err_start'], got['err_end'])
    assert not oracle['diagnostics'], (relpath, oracle['diagnostics'])
    want = [(f['name']['text'], [g['name']['text'] for g in f.get('generic_params', [])])
            for f in oracle['ast']['functions']]
    have = [(data[s['name_start']:s['name_end']].decode(),
             [data[p['name_start']:p['name_end']].decode() for p in flist(s['generics'], 1)])
            for s in (h['sig'] for h in flist(got['fns'], 1))]
    assert want == have, (relpath, have, want)
    return {'module': relpath, 'bytes': len(data), 'functions': len(want)}


def self_ingest_case(probe, relpath):
    data = (ROOT / relpath).read_bytes()
    pages = [data[i:i + STRIDE_BOUND] for i in range(0, len(data), STRIDE_BOUND)]
    args = [pages_value(pages), integer(STRIDE_BOUND), integer(len(data))]
    got = probe.run('decl', 'parse_unit', args)
    oracle = probe.send({'oracle': data.decode()})
    return check_self_ingest(got, oracle, data, relpath)


def declaration_capacity_case(probe):
    """Retain complete source order past the 1024-node list traversal bound."""
    source = 'mncs 0.18; module t;\n' + ''.join(
        f'fn f{i:04d}() -> (result: u64) {{ return 0; }}\n'
        for i in range(1122)) + 'record R { value: u64 }\n'
    data = source.encode()
    pages = [data[i:i + STRIDE_BOUND] for i in range(0, len(data), STRIDE_BOUND)]
    args = [pages_value(pages), integer(STRIDE_BOUND), integer(len(data))]
    got = probe.run('decl', 'parse_unit', args)
    oracle = probe.send({'oracle': source})
    item = check_self_ingest(got, oracle, data, 'inline:1025-top-level-declarations')
    native_records = len(flist(got['records'], 1))
    oracle_records = len(oracle['ast']['record_types'])
    assert native_records == oracle_records == 1, (native_records, oracle_records)
    item['declarations'] = 1123
    item['records'] = native_records
    assert item['functions'] == 1122, item
    return item


def isolated_self_ingest_case(relpath, page_bound=PAGE_BOUND):
    """Keep each large compiler module in its own retained-backend arena."""
    probe = Probe(page_bound=page_bound)
    try:
        execution_status = probe.send({'execution_status': True})
        if execution_status.get('backend') == 'cranelift':
            assert execution_status['retained_sessions'] == 1, execution_status
        result = self_ingest_case(probe, relpath)
        return result, probe.digest.hexdigest(), probe.count, list(probe.steps)
    finally:
        probe.close()


def self_ingest_page_bound(relpath):
    configured = SELF_INGEST_PAGE_BOUNDS.get(relpath, PAGE_BOUND)
    if configured >= PAGE_BOUND:
        return configured
    source_bytes = (ROOT / relpath).stat().st_size
    required_pages = (source_bytes + STRIDE_BOUND - 1) // STRIDE_BOUND
    # The SSA source has a tuned P bound to keep its retained arena compact.
    # Preserve that tuning while allowing the real source to grow, with a
    # small page margin so each compiler change does not stale the fixture.
    return max(configured, required_pages + 16)


def suite():
    probe = Probe()
    try:
        execution_status = probe.send({'execution_status': True})
        if execution_status.get('backend') == 'cranelift':
            assert execution_status['backend'] == 'cranelift', execution_status
            assert execution_status['retained_sessions'] == 1, execution_status
        for text in POS:
            data = source_bytes(text)
            source_text = data.decode()
            got = probe.run('decl', 'parse_unit', logical_args(data))
            oracle = probe.send({'oracle': source_text})
            check_pos_parse(got, oracle, data, text)
        for text in NEG:
            data = source_bytes(text)
            source_text = data.decode()
            got = probe.run('decl', 'parse_unit', logical_args(data))
            oracle = probe.send({'oracle': source_text})
            check_neg_parse(got, oracle, text)
        for text, stage in CHECKS:
            got = check_unit_case(probe, text)
            check_check_unit(got, text, stage)
        capacity_frontier = declaration_capacity_case(probe)
        base_count = probe.count
        base_steps = list(probe.steps)
        base_digest = probe.digest.hexdigest()
    finally:
        probe.close()
    # Whole-module compiler parses allocate persistent native-SSA values.
    # Reboot between modules so arena growth from one source cannot hide the
    # next module's actual parser frontier.
    ingested, ingestion_digests, ingestion_counts, ingestion_steps = [], [], [], []
    for path in SELF_INGEST:
        page_bound = self_ingest_page_bound(path)
        item, digest, count, steps = isolated_self_ingest_case(path, page_bound=page_bound)
        item['page_bound'] = page_bound
        # True means this stage was separately compared; null means it has
        # not been proven for this module yet.
        item['stages'] = {
            'parse': True,
            'function_signature_names_and_generics': True,
            'check': None,
            'proof': None,
            'flow': None,
            'ssa': None,
            'canonical_vm': None,
        }
        ingested.append(item)
        ingestion_digests.append(digest)
        ingestion_counts.append(count)
        ingestion_steps.extend(steps)
    result_digest = hashlib.sha256(json.dumps(
        [base_digest, ingestion_digests], sort_keys=True).encode()).hexdigest()
    all_steps = base_steps + ingestion_steps
    return {'requests': base_count + sum(ingestion_counts), 'pos': len(POS), 'neg': len(NEG), 'checks': len(CHECKS),
            'declaration_capacity_frontier': capacity_frontier,
            'self_ingest': ingested,
            'result_sha256': result_digest,
            'execution_steps_total': sum(all_steps), 'execution_steps_max': max(all_steps),
            'execution_mode': ('retained_' + (execution_status['backend'] or 'unknown')) if execution_status['retained_sessions'] else 'reference_interpreter',
            'retained_execution_sessions': execution_status['retained_sessions']}


if __name__ == '__main__':
    started = time.monotonic()
    first, second = suite(), suite()
    assert first == second, (first, second)
    report = {'schema_version': 1, 'stage0_revision': json.loads(Path('mncs-language.lock.json').read_text())['revision'],
              'tests': first, 'identical_runs': 2, 'elapsed_seconds': round(time.monotonic() - started, 3),
              'scope': 'decl.parse_unit structural + first-error-span differential vs Stage-0; decl.check_unit symbol/resolve/IR verdicts; execution mode and retained-session counts are recorded per run'}
    (OUT / 'decl-results.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))
