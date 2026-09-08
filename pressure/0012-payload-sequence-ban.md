# Pressure Finding CP-0012

ID: CP-0012

Status: open

Category: language | compiler-architecture

Severity: medium

Frequency: occasional

## Finite payloads cannot be sequences; variable-arity nodes need cons-lists

A finite-variant payload field must name "a supported scalar, finite, or
declared record type" (MNE171). Bounded sequences — the natural spelling
for call arguments, parameter lists, statement blocks, match arms — are
rejected as payloads even though records *can* hold sequence fields and
sequences elaborate fine elsewhere.

## Compiler workload

Every variable-arity compiler list (arguments, parameters, results,
fields, variants, statements, clauses, declaration sequences) needed a
representation inside enum payloads.

## Minimal MNCS reproduction

`pressure/repro/enum-sequence-payload.mncs`:

```text
mncs 0.10;
module probe.rec3;
enum E { A { xs: [u64; 2] }, B }
fn f(e: E) -> (result: u64) { return 0; }
```

Stage-0 reports MNE171 on the `[u64; 2]` payload field.

## Current behavior

Payloads accept scalars, finite types, and records (which themselves may
contain sequences, as `record R { xs: [u64; 2], n: u64 }` elaborates).
Only *direct* sequence payloads are refused, so `[u64; 2]` must be
wrapped in a record to travel inside a variant — or, as here, replaced
by a cons-list enum (`ECons { head, tail }`), which is what the compiler
uses for all nine of its list types.

## Current workaround

One cons-list enum per element type (`ExprList`, `FieldList`,
`VariantList`, `StmtList`, `ClauseList`, `EffectList`, `CapList`,
`UseList`, `RecordList`, `EnumList`, `FnList`), each with its own
fuel-loop reversal. Prepend-then-reverse keeps every accumulation O(1)
per element at the cost of one reversal pass per list.

## Why the workaround is insufficient

Cons-lists are a good fit for compiler trees (and would likely be used
anyway), but the workaround has two costs:

- Eleven near-identical list/reverse/fuel-loop triples (~20 lines each)
  that function-level generics cannot abstract over, because the element
  types are distinct enums rather than instances of one `List<T>`.
- Reversal passes are pure overhead forced by prepend accumulation;
  append accumulation would need the same traversal anyway, so the net
  cost is one O(n) pass per list per parse — small but ubiquitous.

The positive side is recorded too: recursive enums *with* scalar/finite/
record payloads elaborate, construct, match, and compile with no
practical limit encountered (25 variants and 6-field payloads verified),
so trees themselves are first-class values. Only direct sequence
payloads are missing.

## Desired behavior

Either accept bounded-sequence payloads (the bound is statically known,
so the termination story is unchanged), or document the intended
collection idiom for variant payloads so compiler code standardizes on
cons-lists versus boxed records deliberately rather than by discovery.

## Likely ownership

language

## Impact

- Correctness: none (workaround is exact).
- Safety: none.
- Runtime performance: one O(n) reversal per list; cons allocation per
  element (value copies, no sharing — see the ownership notes).
- Compiler performance: eleven list-type declarations plus reversals add
  measurable semantic surface (counted in the per-module evidence).
- Memory: spine nodes per element; no sharing between parent/child views.
- Determinism: deterministic.
- Implementation complexity: medium — boilerplate, not subtlety.

## Evidence / reproduction

```text
mncs abi pressure/repro/enum-sequence-payload.mncs  # MNE171
```

## Upstream tracking

- `mncs-language` issue/PR:
- Resolution revision:
- Follow-up evidence in this repository:
