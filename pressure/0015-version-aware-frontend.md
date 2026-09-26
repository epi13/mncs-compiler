# Pressure Finding CP-0015

ID: CP-0015

Status: partially resolved; `next` fields work, four Profile 0.18 forms remain unsupported

> Historical description and reproductions below preserve the original
> finding. See the current reconciliation at the end of this file and the
> pressure index for live status.


Category: language | compiler-architecture

Severity: high

Frequency: common (every 0.13 source using new syntax, including this
compiler's own migrated modules)

## The MNCS frontend cannot yet parse the 0.13 syntax it now emits

Profile 0.13 admitted new surface syntax behind the declared header
version. Stage-0's lexer and parser are version-aware: they read the
`mncs 0.13;` header and change tokenization/parsing accordingly. The
MNCS frontend layers are version-unaware byte scanners:

| 0.13 syntax | Stage-0 on 0.13 input | MNCS frontend today |
| --- | --- | --- |
| bare `!` | `not` token, no diagnostic | kind 7 + diagnostic 2 (MNL002 shape) |
| `-5` in operand position | single negative-literal atom | binary minus; expression parse fails where the oracle succeeds |
| `[v; N]` repeat literal | admitted (MNE184/MNE256 on malformed) | `;` in sequence literal is a parse error (old MNP157 shape) |
| `next` as field name | ordinary member name | record/finite field parse refusal (old MNP127 shape) |
| integer `match` arms + `_` | admitted with MNE140 totality | arm parse refusal (old MNP084 shape) |

At the time this finding was first written, only the `!` row had direct
oracle evidence and the other native refusals were predictions from
`decl.mncs`. The historical 709ba008 run confirmed all five initial
refusals; see [`evidence/profile-surface-results.json`](../evidence/profile-surface-results.json).

## Compiler workload

The Phase-1 migration modernized five frontend modules to profile 0.13,
using `!` (`lexer.mncs`, `kernel.mncs`, `segment.mncs`) and scalar
`match` (`lexer.mncs` `punctuation`). The declaration core still needs
the same migration (`not()` → `!` at ~135 sites, `prec` → scalar
match). The result: the MNCS compiler's own sources are 0.13 programs
that the MNCS frontend cannot fully lex/parse. Bootstrap is unaffected
(Stage-0 parses everything), but self-parsing — a prerequisite for
self-hosting — regresses with every modernized module until the
frontend becomes version-aware.

## Minimal MNCS reproductions

Lexical (`!`), proven against the pinned oracle through the probe's
`oracle` channel:

```text
mncs 0.13;
module t;
fn neg(a: bool) -> (result: bool) { return !a; }
```

Stage-0 lexical facts: a `not` token at the `!` span (64, 65), zero
diagnostics. `lexer.next_token` over the same bytes reports kind 7,
diagnostic 2 at that span. (Profile-less snippets keep the historical
`unknown` + MNL002 on both sides — see below.)

Parser rows (0.13 programs that elaborate cleanly on the pinned
Stage-0, verified with `mncs abi` during the re-pin survey):

```text
fn f() -> (result: i64) { return -5; }                  // negative atom
fn f() -> (result: [u64; 4]) { return [0; 4]; }         // repeat literal
record R { next: u64 }                                  // next field
fn f(x: u64) -> (result: u64) { return match x { 0 => 1, _ => 2 }; }
```

## Current behavior

`lexer.next_token` / `segment.next_token` take `(text, cursor)` with no
profile input, so they cannot reproduce version-gated tokenization.
`decl.parse_unit` takes raw bytes and encodes pre-0.13 refusals for the
parser rows. The existing differentials cannot catch any of this: they
drive the oracle with profile-less snippets, for which Stage-0
deliberately preserves the historical spelling (verified: bare `!` in a
snippet is `unknown` + MNL002 on both sides).

## Current workaround

None. New-syntax use in compiler sources is constrained to what the
pipeline can still process: `!` and scalar `match` appear only in
modules whose *own* parsing is not required (frontend modules are
executed, never re-parsed by MNCS code), and the decl-layer migration
is staged to land together with version-aware parsing.

## Why the workaround is insufficient

- Every modernized module widens the self-parsing gap; the constraint
  is invisible until someone runs the MNCS parser over a modern source.
- Deferring new syntax in compiler sources would forfeit the
  pressure-proving value of the migration (CP-0004/CP-0010/CP-0013
  resolutions would stay exercised only by toy probes, not real code).
- The differential harness must learn versioned oracle inputs, or the
  next profile will repeat this silent divergence.

## Desired behavior

Version-aware declaration parsing keyed off the already-parsed header
version: after `parse_unit` establishes `0.13+`, expression parsing
accepts `!`/negative atoms/repeat literals, declaration parsing accepts
`next` fields and integer match arms (with the oracle's MNE140
totality), while older versions keep byte-exact historical behavior.
For the lexer layer itself, either a profile parameter on the token
requests (with the header version threaded from the caller) or a
documented reinterpretation rule (kind-7 single-`!` spans become `Not`
at 0.13+ in the declaration layer, keeping the byte scanner agnostic).
The choice belongs to compiler architecture; what matters is that one
of them is implemented and differentially pinned, including a `not`
entry in the token-kind wire vocabulary if the lexer owns it.

## Likely ownership

compiler architecture first (version threading design), language for
any accompanying test-transport (versioned oracle inputs already exist
implicitly — full program texts).

## Impact

- Correctness: MNCS parse of modern sources diverges from Stage-0
  (wrong tokens, spurious failures, missing totality checks).
- Safety: none (rejection/diagnostics, not miscompilation).
- Runtime performance: none.
- Compiler performance: none directly; unblocks the decl-layer
  migration which reduces predicate/dispatch code size.
- Memory: none.
- Determinism: deterministic on both sides; the gap is a fixed
  version-skew, not nondeterminism.
- Implementation complexity: medium — one version value threaded from
  the header fact through expression/declaration parsing, five new
  accept-paths with oracle-span parity each.

## Evidence / reproduction

- `not`-token oracle proof: full 0.13 program text through the probe
  `oracle` channel yields kind `not` at the `!` span with zero
  diagnostics; `lexer.next_token` on the same bytes yields
  `(kind 7, diagnostic 2)`. Snippet-level control (no header): both
  sides agree on `unknown` + MNL002, which is why current suites pass.
- Parser rows: current Profile 0.18 Stage-0 emits no diagnostics for all five
  source forms; native `decl.parse_unit` returns `parse_ok=false` with the
  exact spans recorded in `evidence/profile-surface-results.json`.

## Upstream tracking

- `mncs-language` issue/PR: (none — compiler-architecture work first;
  upstream only if a test-transport or spec clarification is needed)
- Resolution revision:
- Follow-up evidence in this repository:

## Current reconciliation (2026-09-26, differential evidence at Stage-0 `b0f3e644`; current CLI/bootstrap pin `4f9e1224`)

The locked Rust Stage-0 accepts all five forms with no diagnostics. Native
`decl.parse_unit` and `decl.prove_unit` now accept the `next` field and
projection. They still reject `!` at [66,67], the negative atom at [58,59],
the repeat literal at [60,60], and integer `match` at [67,67]. Exact
machine evidence is in
[`evidence/campaign-20260925-profile-surface-results.json`](../evidence/campaign-20260925-profile-surface-results.json).

The `next` row was fixed in the native declaration parser by recognizing it
as a field token in declaration and projection contexts. The other four
forms remain compiler architecture work; no language change was made.
