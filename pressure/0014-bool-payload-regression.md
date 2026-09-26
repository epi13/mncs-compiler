# Pressure Finding CP-0014

ID: CP-0014

Status: resolved upstream in mncs-language 709ba008

> Historical description and reproductions below preserve the original
> finding. See the current reconciliation at the end of this file and the
> pressure index for live status.


Category: language

Severity: blocking

Frequency: pervasive (every elaboration of the declaration core)

## `bool` finite-payload fields regressed from accepted to MNE171

A finite-variant payload field typed `bool` elaborated under the previous
Stage-0 pin (`6906d0b1eee7a03f808aff4ab12eb3d2338ff6b4`, profile 0.10) and
is rejected under the current pin (`a7a8c054015971c148d1bbaa797c64362c4073e2`)
at every profile (verified 0.10 and 0.13):

```text
MNE171 variant payload field type does not name a supported scalar, finite,
       record, or bounded-sequence type   (at the `bool` spelling)
```

The single MNE171 cascades into MNE177 (payload binding) and MNE172
(constructor) at every use site, so one root cause reads as dozens of
diagnostics on real code.

## Compiler workload

`src/compiler/decl.mncs` uses `bool` payloads pervasively for exact
compiler state: `Expr.Integer.overflow`, `Expr.Boolean.value`,
`Expr.Project.path`, `FnBody.has_ret`, plus `ok` flags threaded through
every declaration/parse record consumed by payload-bearing variants.
The whole declaration and semantic verticals (parsing, symbol facts,
resolve/span walks, stack-IR lowering, bidirectional proof, typed IR)
are unreachable: the test-transport probe elaborates `decl.mncs` at
startup and fails closed before any request runs.

## Minimal MNCS reproduction

`pressure/repro/bool-payload.mncs` (profile 0.10, no imports):

```text
mncs 0.10;
module probe.bool_payload;
enum Flag { Yes { set: bool }, No }
fn is_set(f: Flag) -> (result: bool) {
    return match f {
        Yes { set: s } => s,
        No => false
    };
}
fn make(s: bool) -> (result: Flag) {
    return Flag.Yes { set: s };
}
```

```sh
.bootstrap/target/debug/mncs abi pressure/repro/bool-payload.mncs
# MNE171 at the `bool` field type (span 61 65), then MNE177 at the
# match binding and MNE172 at the constructor — both cascades of the
# dropped field, not independent errors.
```

`u64` and `byte` payloads elaborate in the same positions on the same
pin; only `bool` is refused.

## Current behavior

The payload gate in `mncs-compiler/src/frontend.rs` (current pin)
resolves the field type with `profile_type(...)` and accepts it only
when no MNE105 probe diagnostic fires **and** the resolved type is not
`BodyType::Named(_)`:

```text
let supported = probe_diagnostics.iter().all(|d| d.code != "MNE105")
    && !matches!(payload_ty, BodyType::Named(_));
```

`bool` is the one scalar whose resolved type is `BodyType::Named("bool")`
(`profile_scalar_supported` explicitly admits it), so the `Named`
exclusion — introduced with the bounded-sequence/canonicalization
refactor — rejects exactly `bool` while admitting every other scalar.
At the previous pin the gate was `profile_scalar_supported(...).is_none()
&& !finite && !record`, which admits `bool`.

No profile document announces the removal: profiles 0.1–0.16 retain
`bool` as a core scalar, the 0.6 payload-sums section gives no
bool exclusion, and RFC 0036 promises older profiles keep their
historical semantics. The language's own suites pin bool *record*
fields (e.g. `module_imports.rs`) but contain no bool *payload* case,
which is how the regression shipped green.

## Current workaround

None viable in-compiler. Replacing every `bool` payload with a `u64`
0/1 code (or a two-variant finite boolean) would touch every tree type
and every construction/match site in `decl.mncs` (~100 sites), destroy
the readability of predicates the 0.13 migration is about to simplify
with `!`/`==`, and enshrine a workaround for what the previous pin
accepted. The declaration/semantic suites stay red until upstream
resolves this; the frontend suite runs on the five unaffected modules
(see the probe's module allowlist below).

## Why the workaround is insufficient

- Any in-compiler rewrite is pure churn against a regression: the
  previous pin proves the language can and did support this.
- The failure is silent at the design stage (bool *record* fields,
  bool locals, bool returns all still work) and appears only when a
  payload names `bool` — exactly the modeling a compiler needs for
  flags carried inside tree nodes.
- Cascades (MNE177/MNE172) misdirect diagnosis toward match/constructor
  spellings that are correct.

## Desired behavior

One of: `bool` payload fields elaborate again on all profiles that
admitted them (the `Named` exclusion exempts the scalar `bool`, matching
`profile_scalar_supported`); or a profile document explicitly removes
`bool` from the payload universe with a migration rule and a compat pin
proving old profiles keep it; or a dedicated diagnostic that names the
exclusion instead of the generic supported-type list. The first option
restores the documented contract with the smallest change.

## Likely ownership

language

## Impact

- Correctness: valid programs (including this compiler's own core)
  are rejected; no unsoundness observed.
- Safety: none (rejection, not miscompilation).
- Runtime performance: none.
- Compiler performance: blocks all declaration-scale validation on the
  current pin; frontend-only validation proceeds on five modules.
- Memory: none measurable.
- Determinism: deterministic rejection.
- Implementation complexity: total blockage of the decl/sem verticals,
  zero workaround cost accepted.

## Evidence / reproduction

- Root-plus-cascade triple on the current pin: command above.
- Previous-pin acceptance: `evidence/decl-results-pre-campaign.json` and
  `evidence/sem-results-pre-campaign.json` at revision `6906d0b1...` elaborate the
  same `bool`-payload `decl.mncs` (e.g. `overflow: bool`,
  `value: bool`, `path: bool`); the gate change arrived with the
  bounded-sequence/canonicalization refactor (language history:
  `git log -S 'matches!(payload_ty' -- crates/mncs-compiler/src/frontend.rs`).
- Regression scope probe: `u64`/`byte` payloads pass, `bool` fails, at
  both 0.10 and 0.13 on the current pin.

## Upstream tracking

- `mncs-language` issue/PR:
- Resolution revision:
- Follow-up evidence in this repository:

## Current reconciliation (2026-09-25)

Current Stage-0 accepts the preserved bool-payload reproduction under profile 0.18. The generic language regression tests and compiler integration case are in `mncs-language` commit 709ba00810099e6965bb47dec14ed19e9e1ae6f8.
