# Pressure Finding CP-0010

ID: CP-0010

Status: resolved upstream; native parser gap tracked by CP-0015

> Historical description and reproductions below preserve the original
> finding. See the current reconciliation at the end of this file and the
> pressure index for live status.


Category: language

Severity: medium

Frequency: common

## No scalar dispatch: match takes only finite/bool shapes, no integer patterns

`match` arms must open with an identifier, `true`, or `false` pattern
(MNP084 on an integer literal pattern). There is no match on `u64`
values of any kind — no integer patterns, no range patterns, no guard
syntax observed. Opcode-style dispatch (token kinds, operator precedences,
error codes) therefore compiles to long `if`/`else` chains over `u64`
equality, exactly as the existing lexer does for punctuation and as the
expression parser does for its 24-operator precedence table.

## Compiler workload

Every compiler table is keyed by small integers: token kind to
precedence (`prec`), byte to character class, keyword bytes to keyword
kind, operator kind to IR mnemonic. All of them are `if`-chains in the
current core (see `prec` in `src/compiler/decl.mncs`).

## Minimal MNCS reproduction

`pressure/repro/match-u64-pattern.mncs`:

```text
fn f(x: u64) -> (result: u64) { return match x { 0 => 1, _ => 2 }; }
```

Stage-0 reports MNP084 at the `0` pattern (plus recovery cascades).

## Current behavior

The grammar's match arms accept finite-variant patterns, bare variant
names, and boolean literals only. Scalar scrutinees have no pattern
form. Related boolean gap: there is no logical-negation operator either
(`!` is MNL002; see the CP-0004 update note), so negative conditions
also spell out through `select` or inverted comparisons.

## Current workaround

`if`-chains over `u64 ==` comparisons, exactly mirroring what the
workaround would be anywhere else. Correct but linear, repetitive, and
unchecked for exhaustiveness: unlike `match` on finite types (which
rejects missing variants with MNE140), nothing verifies that an
if-chain over 24 operators covers all of them.

## Why the workaround is insufficient

- Exhaustiveness is the main loss: adding an operator to the lexer table
  without updating `prec` (or vice versa) fails silently at differential
  tests rather than at elaboration.
- Linear if-chains over opcode spaces cost one comparison per candidate
  at every dispatch site, in both interpreter steps and generated
  semantic/HIR size (each arm is checked arithmetic plus a branch).
- The codebase now contains two dispatch idioms (`match` for trees,
  `if`-chains for scalars) with different safety properties, and the
  boundary between them is an accident of the pattern grammar.

## Desired behavior

Some total scalar-dispatch form with exhaustiveness checking — integer
patterns, or a `select`-chain with a coverage obligation, or documented
guidance that scalar dispatch is intentionally left to `if`-chains (in
which case the pressure is editorial, not technical).

## Likely ownership

language

## Impact

- Correctness: silent non-exhaustiveness at dispatch sites.
- Safety: none.
- Runtime performance: linear dispatch where a jump table belongs.
- Compiler performance: larger semantic artifacts per dispatch site.
- Memory: none measurable.
- Determinism: deterministic.
- Implementation complexity: low per site, high in aggregate review cost.

## Evidence / reproduction

```text
mncs abi pressure/repro/match-u64-pattern.mncs  # MNP084 at the `0` pattern
```

## Upstream tracking

- `mncs-language` issue/PR:
- Resolution revision: profile 0.13 (scalar integer match with `_` totality)
- Follow-up evidence in this repository:

## Re-evaluation (Stage-0 `a7a8c05`, 2026-09-12): resolved

Probed on the current pin: `match x { 0 => 1, _ => 2 }` over `u64`
elaborates at 0.13; a missing `_` default is MNE140 (non-exhaustive
scalar match) — the exhaustiveness check the original pressure asked
for, delivered as an elaboration property. The 0.10 control still
yields MNP084, so old-profile behavior is preserved.

Proven in real compiler code: `lexer.mncs` `punctuation` is now a
nested scalar match (10 two-byte groups with single-kind inner
defaults, 12 single kinds, `_ => 7` outer default), and `decl.mncs`
`prec` (24 operator rows), `is_operand_start`, and `is_value_name` are
total scalar matches. Every punctuation token of the 1586-token
frontend differential flows through the migrated dispatch
(`tools/test_frontend.py` green), and interpreter steps dropped ~9%,
so the branch-chain lowering is measurably cheaper than the if-chains
it replaced. At the time, `decl.mncs` tables awaited elaboration proof after
CP-0014. The current compiler source now loads under Profile 0.18 and its
declaration and semantic twins pass; parsing integer-match input in a user
unit remains a separate CP-0015 gap. History preserved above: the MNP084
refusal, the if-chain workaround, and the silent non-exhaustiveness
that motivated totality.

## Current reconciliation (2026-09-25)

Current Stage-0 accepts the preserved total integer-match fixture at profile 0.18. The native compiler parser rejects this current syntax, recorded independently under CP-0015.
