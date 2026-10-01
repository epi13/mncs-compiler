# Pressure Finding CP-0020

ID: CP-0020

Status: resolved in mncs-compiler (revalidated by the committed suite)

Category: compiler-architecture

Severity: high

Frequency: common

## Match lowering reversed block-parameter environments, misaligning arm-to-join edges for heterogeneous-type envs

`prepare_block_environment` in `src/compiler/ssa.mncs` mapped incoming
bindings to block parameters by prepending, so the returned environment
listed bindings youngest-last while every consumer assumed source order.
Arm-to-join edge arguments are built with `match_context_ids` over the
arm-exit environment, but join parameters are built over the source
environment, so the two lists paired positionally only when the
environment was type-homogeneous (or palindromic). Any scalar or finite
match whose live environment held two or more bindings of differing
types failed native SSA verification: the verifier correctly rejected
the misaligned edge (`argument_types_match` pairs `u64` against
`TNamed`, etc.).

The stack half of `match_context_parameters` already reversed its
accumulator back (`reverse_value_stack`); the environment half was the
odd one out.

## Compiler workload

Lowering record field projections inside scalar-match arms
(`fn in_match(p: Point, c: u64) -> (r: u64) { return match c { 0 => p.x,
_ => p.y }; }`) verified 4 of 5 projection functions and rejected the
match composition. Bisection showed the projection instruction was
innocent: `fn rec_env(p: Point, c: u64) -> (r: u64) { return match c {
0 => 1, _ => 2 }; }` — a match with a record-typed parameter and no
projection at all — also failed verification while the `u64`-only twin
passed.

Wire-shape comparison of the failing vs passing function showed
identical structure: the only difference was the arm jump arguments
`[result, c-copy, p-copy]` pairing against join parameters
`[result, p-param, c-param]`.

## Minimal MNCS reproduction

No records needed; two bindings of differing types suffice:

```mncs
mncs 0.18; module demo.mixed;
fn mixed_count(value: u64, flag: bool) -> (r: u64) { return match value { 0 => 10, _ => 20 }; }
```

Pre-fix this fails native SSA verification; post-fix it verifies.
Committed as `mixed_count` in the scalar-match section of
`tools/test_project.py`, with a finite-match twin `mixed_match` in the
enum section and a record-env composition `in_match` in the projection
section.

## Current behavior

Fixed at the single shared choke point: `prepare_block_environment`
now returns `reverse_environment(built.env)`, restoring source binding
order for the mapped environment. This repairs scalar-match, finite-match,
and flow-block (`Branch`/`Jump`) edges uniformly, and also restores
correct youngest-first name resolution for shadowing across block
boundaries (previously reversed).

## Revalidation

- `tools/test_project.py`: full suite green, run twice deterministically;
  enum 11 + scalar 6 + projection 5 verified functions, all corruption
  rejections still reject.
- `tools/test_sem.py`: 103 semantic cases + 9 proof verdicts green.
- `test_decl`, `test_flow`, `test_frontend`, `test_segment`,
  `test_pressure`, `test_imported_nominal_ssa`: all green.
- Environment obligation
  `mncs-compiler:verification-executor/mncs-compiler.value-ssa-differential`
  green through session `ses_e7fd4be3ea5731b5`; canonical digest moves
  `a3c5a13f…` (39 requests) to `da46e431…` (40 requests: new coverage
  plus corrected edge-argument order).
- Family campaign round 2: same stage classifications as round 1 on all
  9 slices (no regressions; remaining blocks are CP-0019 kind 32,
  cre1-combine ParseFailed@313, and the 1024-byte ceiling).

## Why the homogeneous fixtures masked it

All pre-existing match fixtures carried single-type environments
(`u64`-only, or a lone enum subject), where reversed edge order still
pairs equal types. The verifier was right to reject; the lowering was
wrong to reverse.
