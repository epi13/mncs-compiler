# Pressure Finding CP-0018

ID: CP-0018

Status: open

Category: backend

Severity: high

Frequency: common

## Verified native-SSA C11 adapter admits no integer arithmetic

The proof-bound native backend entry point on `mncs-language` main
(`mncs emit-native-ssa-c11`, schema `mncs.native-scalar-ssa/1`, verifier
binding `mncs.compiler.ssa.verify` PASS) accepts only `Constant` and `Call`
instructions. Any `u64` arithmetic in verified native SSA — including the
existing two-module backend canary's `value + 1` — is outside the adapter
envelope, so the compiler cannot migrate its backend vertical off the
test-only structural projection.

## Compiler workload

`mncs-compiler` lowers typed operations to verified value SSA
(`ssa.mncs`, `verify_function`) and must eventually feed backend lowering
with proof/provenance-bound input instead of the current explicitly
unattested structural projection (`mncs.native-scalar-ssa-structural/1`,
consumed by `emit-native-ssa-c11-structural` at locked Stage-0
`a3ac17df`). Language main already provides the verified adapter
(`crates/mncs-codegen/src/c11/native_ssa.rs`, since `cf5a895`), which
checks schema, verifier PASS binding, compiler/stage0 provenance,
signatures, values, edges, and scalar types — but exposes no integer
operation.

## Minimal reproduction

Normalize the verified SSA of the two-module backend fixture
(`mncs 0.18; module b; ... fn f(x:u64)->(r:u64){return x;}` and
`mncs 0.18; module a; use b; ... fn g(flag:bool,x:u64)->(r:u64){
let value:u64=b.f(x); if flag {} else {} return value + 1;}`)
into schema `mncs.native-scalar-ssa/1` with
`verification_status: "pass"`, `verifier: "mncs.compiler.ssa.verify"`,
and feed it to main's `mncs emit-native-ssa-c11`. The `u64` addition
has no representable instruction: `NativeSsaScalarInstruction` is
`Constant | Call` only, so lowering the canary is impossible without
dropping back to the unattested structural schema.

## Current behavior

The verified adapter rejects (by schema) every native function whose
verified SSA contains `TBin` arithmetic. The underlying scalar backend
already models integer operations (`ScalarInst::Integer` with
`ArithmeticIntent`, e.g. `Wrapping`); only the verified native-SSA
adapter lacks the corresponding instruction variant and validation.

## Current workaround

The compiler backend vertical stays on the structural/unattested adapter
at the locked pin (campaign branch `c1ea183`), projecting verified SSA
into explicitly unattested structural input. See
`evidence/campaign-20260928-agent-native-native-backend-vertical.json`.

## Why the workaround is insufficient

The structural document carries no compiler proof or source binding, so
it cannot become authoritative backend input: any projection bug or
fixture skew silently changes emitted code. The verified adapter is the
intended admission path, but its envelope excludes even scalar
arithmetic, let alone finite/enum construction, payload extraction,
sequence repetition, and switch terminators.

## Desired behavior

A verified-adapter instruction variant for integer arithmetic with
operator/intent validation consistent with the scalar backend, so the
existing scalar canary lowers through the proof-bound path with
execution differential against locked Stage-0. Aggregate/nominal
representation, switch terminators, and sequence operations remain the
subsequent envelope boundary after arithmetic lands.

## Likely ownership

backend

## Impact

- Correctness: blocks proof-bound backend admission for any arithmetic-bearing native SSA.
- Safety: keeps the only executable path on unattested input.
- Runtime performance: none (same emitted code shape once admitted).
- Compiler performance: none.
- Ergonomics: compiler/backend differential work must target two adapters.

## Reproduction command

From `mncs-language` at main (`425de20`), run
`cargo test --offline -p mncs-codegen native_ssa`: the adapter's own
tests pass (2/2), admitting `Constant`/`Call` modules with a PASS
binding and refusing unbound facts. The envelope is exactly the
`NativeSsaScalarInstruction::{Constant, Call}` serde enum in
`crates/mncs-codegen/src/c11/native_ssa.rs`, so no JSON document can
express integer arithmetic to main's `mncs emit-native-ssa-c11`.

From `mncs-compiler` at the locked pin, run the imported-nominal
backend fixture through verified SSA, project kinds `{constant, call,
u64-add}` and terminators `{branch, jump, return}` into
`mncs.native-scalar-ssa/1`: the `u64` addition has no representable
instruction, and the adapter accepts the document only with the
arithmetic removed.

## Relations

- Follows the backend direction recorded in `evidence/PARITY.md`
  ("replace the test-only structural projection with a backend input
  tied to compiler proof/provenance").
- Narrower than, and prerequisite to, aggregate/nominal C11 lowering
  for enum construction, payload extraction, sequence repetition, and
  finite/scalar switch terminators.
