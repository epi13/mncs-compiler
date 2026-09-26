# Compiler pressure CP-0017: poisoned result types change diagnostic recovery

ID: CP-0017

Status: resolved in `mncs-compiler`; focused cases and full semantic twin pass

Category: compiler-architecture (native semantic proof)

Severity: medium; the current semantic differential stops at two common recovery cases

## Compiler workload

The repinned declaration proof suite compares native `decl.prove_unit`
obligations with current Rust Stage-0 diagnostics. Two binary return expressions
under an invalid result type expose the same recovery gap in opposite operand
positions. Both parsers accept the source. Native proof changes which child
mismatch it retains and omits or adds diagnostics relative to Rust.

## Minimal reproductions

```mncs
mncs 0.10;
module t;
fn g(a: u64) -> (r: bogus) { return 0 + a; }
```

```mncs
mncs 0.10;
module t;
fn g(a: u64) -> (r: bogus) { return a + 1; }
```

Replay both against the locked current reference and the compiler's current
Profile 0.18 modules:

```sh
python3 tools/reproduce_semantic_poison.py
```

The sources deliberately retain their historical 0.10 profile. Stage-0 is
revision `709ba00810099e6965bb47dec14ed19e9e1ae6f8`.

## Current result

Pre-fix evidence is preserved in [`evidence/cp0017-before.json`](../evidence/cp0017-before.json)
for `0 + a` and [`evidence/cp0017-left-before.json`](../evidence/cp0017-left-before.json)
for `a + 1`. In the first case Rust emits MNE105 twice, MNE117 on the right
reference, then MNE103 on the enclosing return; the old native proof added
MNE118/MNE119 instead. In the second case Rust emits MNE117 on the left
reference and MNE103 on the enclosing return; the native proof initially
omitted the child MNE117.

## Classification and Commons reconciliation

This is compiler implementation drift, not a missing language capability:
current Rust Stage-0 accepts and classifies both sources, while the existing
MNCS semantic checker produces different obligations. Commons main
`1d3d50bb4cc27c0793b97672ae7dd7f8dbad4de6` contains P1-011 for
position-sensitive literal inference under wrapping operators. That is a
related diagnostic family but a different reproducer and authority boundary;
CP-0017 concerns ordinary `+` under a poisoned function result in the native
compiler. No duplicate Commons pressure was filed.

## Repair and verification

The current change preserves expected-type diagnostics for named operands while
avoiding propagation of a poisoned result type into a left integer literal. If
operand mismatch has already been recorded, it retains enough result typing to
emit the enclosing return mismatch. Ordinary non-poisoned operand mismatches
keep the existing MNE119 path. The focused two-case comparison matches the
exact ordered Rust diagnostic codes and spans in two identical native runs per
source; see [`evidence/semantic-pressure-cp0017-after.json`](../evidence/semantic-pressure-cp0017-after.json).
The full current-pin semantic suite passes twice: 49 cases plus five proof
verdicts, 54 requests per run, digest
`bced8deff2157ebdfe2b151f4e29d34b7e20ca1a74cfa7c69c34689e7f1a5e6c`, and
73,631,716 interpreter steps across both runs. See
[`evidence/sem-results.json`](../evidence/sem-results.json) and
[`evidence/SEM.md`](../evidence/SEM.md).
