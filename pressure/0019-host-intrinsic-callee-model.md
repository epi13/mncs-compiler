# Pressure Finding CP-0019

ID: CP-0019

Status: open

Category: compiler-architecture

Severity: high

Frequency: common

## Native proof has no host-intrinsic callee model

Effect-authorized calls to host intrinsics (`structured_write`,
`structured_read`, `fs_entry_kind_at`, …) fail native proof with
obligation kind 32 (unresolved callee) while locked Stage-0 accepts the
same sources. The native signature table only models `fn` declarations;
there is no registry of host-intrinsic signatures, no check that the
calling function carries the authorizing effect/capability headers, and
no callable identity for host targets in typed calls or value SSA.

## Compiler workload

The MNCS family compilation campaign feeds real 0.18 sources through
native `project.compile_project`:

- `mncs-language/examples/source/structured-artifact.mncs` (632 bytes):
  kind 32 at the `structured_write` and `structured_read` call sites;
  Stage-0 oracle accepts.
- `mncs-language/examples/source/fs-metadata.mncs` (708 bytes): kind 32
  at three `fs_entry_kind_at` call sites; Stage-0 oracle accepts.

Both failures are proof-stage (`ProofFailed` carries the call span);
parsing, imports, and all other proof obligations succeed.

## Minimal MNCS reproduction

The 632-byte `structured-artifact.mncs` fixture is itself the minimal
faithful case: shrinking the intrinsic call (fewer arguments, scalar
path/schema types) changes Stage-0's verdict first (`MNP220` arity,
`MNE295` bounded-byte-view path), so the gap cannot be minimized below
the real call shape. A further-reduced shape probe belongs to the
implementing slice, not to this finding.

## Current behavior

`seed_call` in `decl.mncs` resolves callees through the module `fn`
signature table only. Host intrinsics never appear there, so every
intrinsic call drains with kind 32 regardless of effect authorization.

## Current workaround

None in the compiler. Campaign slices avoid host intrinsics; the
imported-call SSA slice covers only module-declared callees.

## Why the workaround is insufficient

Real family sources routinely call host intrinsics for I/O, metadata,
and typed artifact boundaries. Without an intrinsic callee model, whole
modules fail proof at the first intrinsic call and no downstream stage
(CFG, SSA, backend) is reachable for them.

## Desired behavior

Native proof accepts intrinsic calls exactly when Stage-0 does:
known-intrinsic signature/arity checking, effect/capability
authorization against the calling function's headers, Stage-0-matching
diagnostics for unknown intrinsics and unauthorized use, and a stable
callable identity for host targets that reaches typed calls and
verified SSA. No new language feature is proposed; this mirrors
existing Stage-0 behavior in the native compiler.

## Likely ownership

compiler architecture

## Impact

- Correctness: real modules with intrinsic calls cannot complete native proof.
- Safety: authorization semantics must match Stage-0 exactly; a permissive stub would be a soundness hole.
- Runtime performance: none.
- Compiler performance: a small static registry lookup per call; no new passes required.
- Ergonomics: unblocks family sources as compiler dogfood.

## Reproduction command

From `mncs-compiler`, run the family campaign driver over the two
fixtures above and compare native `ProofFailed` (kind 32) against the
locked Stage-0 oracle verdict (valid). Expected after the fix: native
valid with matching diagnostics, and verified SSA for the
intrinsic-calling functions.

## Relations

- Recorded remaining item "imported effect/capability identity and
  coverage" in the project/value-SSA differential scope names the same
  boundary from the imported-call side.
- Distinct from CP-0018 (verified backend adapter envelope), which is
  backend-owned; this finding is compiler-owned proof architecture.
