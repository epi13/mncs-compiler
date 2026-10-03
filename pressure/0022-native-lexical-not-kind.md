# Pressure Finding CP-0022

ID: CP-0022

Status: open (filed from CP-0001 tier-D matrix evidence; deliberate-design
consequence, see below — not a regression)

Category: compiler-architecture (lexical vocabulary)

Severity: medium

Frequency: common (any source using bare `!` prefix negation)

## The native scanner has no `not` token kind; Stage-0 emits one

Stage-0 lexes a bare `!` (Profile 0.13+ boolean negation) as a `not` token
with no diagnostic. The native scanner has no such kind: `lexer.punctuation`
maps `!` followed by anything but `=` to kind 7 (`unknown`) with diagnostic
2 (MNL002), and `src/compiler/token-kinds.json` has no `not` entry. The
CP-0001 logical lexer mirrors the single-view rules exactly, so both native
forms diverge from the oracle identically at every bare `!`.

This is the deliberate version-neutral-scanner design, not an accident:
`decl.mncs` documents that "the version-neutral scanner preserves the
historical unknown-token fact for bare `!`; a Profile 0.13+ declaration
body reinterprets that exact byte as prefix negation after its header
established the gate" (`expr_step`, predicated on kind 7, diagnostic 2,
single byte 33, and the header's 0.13 gate). CP-0015 proves parse/proof
conformance for `!` through that reinterpretation. The cost is lexical:
no native-vs-oracle token-stream differential can match exactly on any
source containing bare `!`, and `token-kinds.json` cannot name the oracle
kind. Earlier suites never excited this because their samples avoid bare
`!` (the segment fuzz alphabet contains `!` but seed 257 never generates
it bare).

## Reproduction

Run `python3 tools/test_cp0001.py` (tier D): every ASCII milestone
containing bare `!` records `lex: false` with first-failure stage `lex`
at the first `!` span and pressure CP-0022, while `lex_qualified_modulo_not`
records that every other span is exact (`lex_gap.not_tokens` counts the
classified pairs). The tier-D comparator (`compare_modulo_not`) treats the
native `(7, s, e, 2)` on a single `!` byte as equal to oracle
`('not', s, e, 0)` and raises on any other divergence, so no second gap
can hide behind this one.

## Options (needs an owning decision, not a side quest)

- Keep the version-neutral scanner and canonically document the
  `(7, 2)`-for-`!` fact plus the reinterpretation gate as the native
  lexical contract, with differentials comparing modulo the classified
  pair (the CP-0001 tier-D precedent).
- Or introduce kind 71 `not` in `punctuation`, `token-kinds.json`, and
  both `token_shape` checks, and move the `decl` gate onto `(71, 0)`.
  That changes old-profile `!` rejection shape and every suite that
  keys on `(7, 2)`-for-bang; it needs full-suite revalidation and is
  deliberately out of CP-0001's scope.

## Ownership

Compiler architecture (lexical vocabulary); Stage-0 behavior is the
reference. Until decided, token-stream differentials must carry the
CP-0022 classification the way tier D does.
