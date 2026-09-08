# Pressure Finding CP-0008

ID: CP-0008

Status: open

Category: language | backend | compiler-architecture

Severity: blocking

Frequency: pervasive

## Finite payloads do not cross module boundaries

A `record` value travels freely between modules: foreign records can appear
in signatures, be constructed (`alias.Type { ... }`), projected (`v.field`),
and matched. A `finite` (enum) value does not:

1. Foreign payload-variant *construction* (`alias.Type.Variant { ... }`)
   parses as a record literal and fails elaboration with MNE154
   ("record literal names an unknown record type"). Only same-module
   `Type.Variant { ... }` (two segments) constructs payload variants, and
   only payload-free `alias.Type.VARIANT` reinterprets through elaboration.
2. A *locally defined* finite variant whose payload field type names a
   *foreign* finite type elaborates (no MNE diagnostic) but fails the
   backend check: construction reports MNB063 ("payload operand type does
   not match the declared payload field type") and payload projection
   inside `match` reports MNB066 ("payload projection result type does not
   match the declared field type"), even when the operand type is exactly
   the declared field type.

Net rule: a finite payload can only be built and destructured in the
module that homes the payload's type. Cross-module tree code can pass
trees opaquely and test variant tags, but cannot look inside.

## Compiler workload

The declaration core needs one recursive syntax tree (`Expr` inside
`Stmt`, `Call` arguments, binary operands) shared by parsing, name
resolution, type checking, and lowering. The natural design homes `Expr`
in an expression module and `Stmt` in a declaration module. Five
functions constructing `Stmt.Let`/`Return`/`If` with `expr.Expr`
payloads failed with MNB063 despite exact types; investigation showed any
local finite with a foreign finite payload field is affected.

## Minimal MNCS reproduction

`pressure/repro/finite-payload-home.mncs` (homes `E`) and
`pressure/repro/finite-payload-consumer.mncs` (defines `S` with a `t.E`
payload, constructs it, and matches on it):

```text
mncs 0.10;
module probe.u2;
use probe.u1 as t;
enum S { W { v: t.E }, Z }
fn build(s: u64) -> (result: S) {
    let e: t.E = t.mk(s, s);
    return S.W { v: e };
}
fn read(x: S) -> (result: u64) {
    return match x { W { v: e } => 1, Z => 0 };
}
```

Stage-0 reports MNB063 on `build` (construction) and MNB066 on `read`
(projection while binding `v`, even though the binding is unused).
Replacing the payload construction with a same-module constructor call
and dropping the payload binding removes both diagnostics, proving the
payload use, not the surrounding code, is the trigger.

## Current behavior

Elaboration accepts the definition and the construction (types line up),
then the backend rejects both directions of payload use. Record payloads
of foreign finite type work in the same positions (`Frame { cond: Expr }`
elaborates and lowers), so the failure is specific to *finite*
construction/projection with foreign *finite* operand types — most likely
a nominal-identity mismatch between the imported payload identity and the
declared field identity at lowering time.

## Current workaround

All tree types (`Expr`, `Stmt`, declarations, clause/effect facts, and
every list) plus all tree-walking stages (expression parsing, declaration
parsing, and later resolution/checking/lowering) live in one module,
`mncs.compiler.decl.v1`. The module header documents this homing rule.
Byte/token/record-only layers (`source`, `lexer`, `segment`) stay
separate because records cross module boundaries without restriction.
Consumer modules may only pass trees opaquely.

## Why the workaround is insufficient

- The core module grows without bound: parser plus upcoming resolution,
  checking, and lowering stages share one compilation unit, concentrating
  checked-obligation and semantic/HIR/SSA cost (see CP-0007) instead of
  distributing it across separately attributable modules.
- Separate compilation and per-stage cost attribution, both needed for
  the long-term fact-graph direction (RFC 0002), are unavailable for the
  most important compiler data: the trees themselves.
- The failure is silent at elaboration time (MNE-clean) and appears only
  as a backend diagnostic, so module decomposition looks viable until
  lowering — exactly the wrong stage to discover an architectural
  constraint.

## Desired behavior

One of: finite construction and payload projection work across module
boundaries for identical nominal types (import identity equals home
identity at lowering); or the grammar/elaboration rejects foreign finite
payload types early (at definition or first use) with a dedicated
diagnostic instead of passing elaboration and failing at lowering; or
explicit documentation of the intended module-visibility rule for finite
payloads so compiler architecture can rely on it.

## Likely ownership

language | backend

## Impact

- Correctness: no unsoundness observed; valid programs are rejected.
- Safety: none (rejection, not miscompilation).
- Runtime performance: none.
- Compiler performance: forces a monolithic core module; concentrates
  Stage-0 compile cost and defeats per-stage attribution.
- Memory: larger single-module semantic artifacts than a decomposed
  design would produce.
- Determinism: deterministic rejection; no nondeterminism observed.
- Implementation complexity: high — every future tree stage must live in
  the same module, and the boundary between "tree code" and "fact code"
  must be policed by convention rather than the module system.

## Evidence / reproduction

```text
mncs abi pressure/repro/finite-payload-consumer.mncs
# MNB063 on construction, MNB066 on payload binding; diagnostics quote the
# exact operation paths (functions[1].body..., functions[2].body...).
```

Bisected from five MNB063 failures in `src/compiler/decl.mncs` helper
functions (`parse_let`, `parse_return_stmt`, `block_else_close`,
`block_await_else`, `block_if_frame`) that construct declaration-local
`Stmt` variants with `expr.Expr` payloads; merging the expression layer
into the declaration module removed all five. Related grammar limit:
three-segment `alias.Type.Variant { ... }` never reaches elaboration as a
finite construction (parses as a record literal); see the finding body.

## Upstream tracking

- `mncs-language` issue/PR:
- Resolution revision:
- Follow-up evidence in this repository:
