# Pressure Finding CP-0010

ID: CP-0010

Status: open

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
- Resolution revision:
- Follow-up evidence in this repository:
