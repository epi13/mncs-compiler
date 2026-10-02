# Pressure Finding CP-0019

ID: CP-0019

Status: resolved in compiler through verified SSA (current profile)

## Resolution (2026-10-02)

Native proof now models the five compiler operations real family sources
call (`fs_entry_kind_at`, `fs_entry_size_at`, `fs_entry_mtime_at`,
`structured_read`, `structured_write`): parser reservation in call-head
position with parse-time arity, proof in expected → authority → operand
order twinning MNE257/MNE258/MNE261/MNE262 and MNE287–MNE297 as native
obligation kinds 53–67, exactly-one-effect plus declared-capability
authorization mirroring Stage-0 `check_host_authority`, canonical
`mncs:0.2:operation::<name>` identities on typed `TOp.TOp` nodes, kind-9
value-SSA instructions, and SSA verification (slot, arity, identity,
source, result, inputs). The slice also carries the ungated exact→view
borrow (`[E; N]` into `[E; up_to M]` when `N <= M`), applied at name
elaboration with re-annotation to the expected view type plus the
let/return backstop, mirroring Stage-0
`exact_view_borrow_dimensions`.

Evidence (locked Stage-0 `a3ac17df`, Profile 0.18):

- `evidence/campaign-20261002-project-results.json`: operation section,
  5 verified functions, 7 corruption rejections, unauthorized twin
  kind 53 / MNE257 at identical spans; canonical digest `2fb25256…`.
- `evidence/campaign-20261002-semantic-results.json`: 131 semantic
  cases plus 11 proof verdicts pass twice identically (28 operation
  cases, all Stage-0-agreeing).
- `evidence/campaign-20261002-family-results.json`: both pulling
  real sources now reach verified SSA —
  `examples/source/structured-artifact.mncs` (632 bytes, 2 functions)
  and `examples/source/fs-metadata.mncs` (708 bytes, 3 functions),
  unmodified, Stage-0 valid.

Remaining, explicitly out of this slice:

- Backend/runtime lowering for kind-9 operations is unverified; no
  executable operation support is claimed (backend ownership, adjacent
  to CP-0018).
- Operations below their `introduced_profile` are not gated natively:
  `fs_entry_kind_at` at 0.10 and `structured_read` at 0.17 elaborate
  natively while Stage-0 rejects them at parse (MNP204/MNP217). Current
  profiles agree (0.12 admits `fs_entry_kind_at`; 0.18 admits all five).
  Old-profile parse gating is a separate parser slice with no
  real-family pull today.
- Deferred view behaviors stay unimplemented until evidence pulls them:
  view widen (0.15+), view narrow (0.14+), call-result/projection/
  repeat → view borrow at non-name sites, and the opaque `TSeq`
  operand edge.

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
