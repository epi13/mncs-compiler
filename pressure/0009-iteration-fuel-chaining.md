# Pressure Finding CP-0009

ID: CP-0009

Status: partially resolved (2026-09-12)

Category: language | compiler-architecture

Severity: medium (was high)

Frequency: pervasive

## Bounded-iteration fuel must be chained by hand

Two small rules combine to force an awkward looping idiom everywhere a
compiler pass needs more than a trivial bound:

1. Counted iteration `iterate k up_to N` requires a literal `1 <= N <= 32`
   (MNE142), even in profile 0.10.
2. Every `iterate` in one function needs a distinct iteration identity
   (MNE146); reusing `i` across sequential loops is an error.

Traversal iteration `iterate i over seq` is bounded by the sequence's
runtime length (up to 64 for the bounded source chunks), so any pass that
must advance up to 256 logical bytes — let alone drain an operator stack
while consuming tokens — chains four to eight traversals with distinct
names (`bi0..bi7`, `ex0..ex7` plus a counted `up_to 32` supplement),
threading state through fresh bindings at each link. The declaration
core contains dozens of such chains.

## Compiler workload

Lexing a 256-byte logical source, shunting-yard expression parsing (each
step shifts one token or reduces one operator, so steps can exceed the
byte count), block parsing with frame pops that consume no input, and
every list reversal all need fuel beyond any single loop form.

## Minimal MNCS reproduction

`pressure/repro/counted-bound-256.mncs` (`up_to 256` rejected with
MNE142) and `pressure/repro/iteration-identity-dup.mncs` (two sequential
`iterate i` loops rejected with MNE146).

## Current behavior

The limits are presumably deliberate (termination Stratification), but
they interact poorly: the only way to spell "repeat up to N<=256 times"
is to chain traversals over the input chunks, coupling every loop's fuel
to the physical chunking of its input. A pass over derived data (an
operator stack, a worklist) with no chunk sequence to iterate must borrow
fuel from an unrelated sequence or split its bound into `up_to 32`
slices. Fuel accounting (proving `2 * total + 1` steps fit in eight
chunk links plus a counted supplement) is done by hand in comments and
re-verified by nothing.

## Current workaround

Chained traversal links with distinct identities per function, recorded
as an explicit fuel argument in each loop's header comment:
`src/compiler/segment.mncs` (`scan_fuel`, `significant`), expression
parsing (eight links plus a counted link), block parsing (eight links).

## Why the workaround is insufficient

- Every loop pays its full bound in interpreter steps even when it stops
  early (see CP-0003): a 256-byte source costs ~256 no-op iterations per
  token request, dominating execution-step budgets.
- Chaining couples loop fuel to input chunking: eight links over four
  chunks is correct only because each link iterates runtime length and
  the sum covers the byte count. A refactor that changes chunking must
  re-audit every chain by hand.
- Distinct identities per link (`bi0..bi7`, `qi0..qi35`) are pure
  bookkeeping with no semantic content; a single slip duplicates an
  identity and fails elaboration far from the logic error.

## Desired behavior

Without prescribing syntax: some way to spell "repeat this total step at
most K times" for K in the low hundreds with one loop and one identity,
or loop combinators whose fuel is derived from a declared numeric bound
rather than borrowed from an input sequence's length — while keeping the
termination guarantees the current rules protect.

## Likely ownership

language

## Impact

- Correctness: chains are correct but fragile; fuel insufficiency fails
  silently (fuel exhaustion looks like end-of-input).
- Safety: none.
- Runtime performance: no-op iterations dominate reference-interpreter
  step counts (measured per-request maxima in `evidence/`).
- Compiler performance: more interpreter steps per compile request;
  larger generated semantic artifacts per loop.
- Memory: carried state is copied per link; no aliasing.
- Determinism: deterministic.
- Implementation complexity: high — fuel arithmetic pervades every pass.

## Evidence / reproduction

```text
mncs abi pressure/repro/counted-bound-256.mncs     # MNE142
mncs abi pressure/repro/iteration-identity-dup.mncs # MNE146
```

Step counts: `evidence/decl-results.json` records per-request maxima for
the chained passes.

## Upstream tracking

- `mncs-language` issue/PR:
- Resolution revision: profile 0.13 (sequential reuse, 1..=1024 bounds, 2-level nesting)
- Follow-up evidence in this repository:

## Re-evaluation (Stage-0 `a7a8c05`, 2026-09-12): partially resolved

Probed on the current pin: `iterate k up_to 256` elaborates at 0.13;
`up_to 1025` is refused (MNE142, ceiling now 1024); two sequential
`iterate i` loops elaborate (MNE146 now scope-based with hygienic
`name#2` recording). The 0.10 controls still fail exactly as documented,
so old-profile behavior is preserved.

Proven in real compiler code: the 0.13 migration renamed every
fuel-chain group in `segment.mncs` (ai/si/gi families) and all ~70
groups in `decl.mncs` (ex/vi/ni/hi/ti/fi/ri/pi/qi/bi/ci/ui/si/ei/ki/di/
wi/ai/li/gi/mi/oi/oj/ok/sl/el/dg/rd/ed/pm/rs/cp/fx/nc/rl/fd/fo/rc/sw/tl/
ta/sg/fk/pb/kk/fh/ec/vx/pf/gs/ty/tr/sm/tv/tc/tq/tp/us/kc/…) to a single
`i` per function — 317 sites in `decl.mncs` alone, plus the carried-`i`
collision (`fin`) and the position-index group (`int_value`) handled
explicitly. `segment.mncs` links are proven by the new
`tools/test_segment.py` differential (pending final run); `decl.mncs`
is parse-checked with elaboration proof staged after CP-0014.

What remains: fuel is still borrowed from input chunking for bounds
past 1024 and for derived-data passes (operator stacks, worklists);
fuel-sufficiency arguments still live in comments; silent exhaustion
still looks like end-of-input; no-op iterations still cost full steps
(see CP-0003). The pressure is now about *large* bounds and *derived*
fuel, not everyday loop naming.
