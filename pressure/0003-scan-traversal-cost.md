# CP-0003 — Cursor scans retain bounded no-op iterations

Status: open; language behavior still reproduced

> Historical description and reproductions below preserve the original
> finding. See the current reconciliation at the end of this file and the
> pressure index for live status.

compiler-architecture, tooling. Severity: medium in this slice,
potentially high at larger source sizes. Frequency: pervasive.
Upstream tracking: none; "loop regions" (Sep-10 language work) checked
and unrelated (WASM memory reclamation, not early termination).

## Workload and reproduction

`lexer.next_token` advances a cursor until a token boundary; nested comments carry
depth. `significant` skips trivia, and `parser.header` stops after a module name.
The implementation uses `iterate i over text` with a carried `done`/terminal
state. Remaining iterations still call a helper which returns unchanged state.
`repro/unbounded-scan.mncs` demonstrates MNP106 for an ordinary `while` spelling;
run `python3 tools/test_pressure.py`. This does not establish that an unbounded
loop is the right solution: bounded early termination or a one-pass state machine
may preserve stronger resource guarantees.

## Workaround and impact

Every scan is bounded by source length and guarded against EOF. Token scanning
cost is O(n) per requested token, whole-source summary O(n²) in the conservative
bound. Significant-token/header composition retains further bounded traversals;
it is not a scalable whole-module parser design. `evidence/frontend-results.json`
records interpreter steps, which include these no-op iterations; wall time includes
reference tooling and test transport, not just kernel execution.

- Correctness/safety: progress and span verifier plus explicit terminal guards;
  no exhausted-bound success for a partially consumed token (at most n advances).
- Runtime/compiler performance: extra loop dispatch/IR; no native cost claim.
- Memory: scalar/record carried state, no token collection allocation.
- Determinism: fixed bounds make work reproducible.
- Complexity: helper functions are needed to guard scans and preserve state.
- Desired capability: efficient source-length-bounded early termination, or a
  measured one-pass compiler representation. Investigate compiler optimization
  and available iteration forms before requesting language changes.
- Likely owners: compiler architecture first; language/tooling where bounded
  iteration cannot efficiently represent the measured workload.

## Re-evaluation (Stage-0 `a7a8c05`, 2026-09-12): still valid

Re-probed: `while` is still refused (MNP106) on profile 0.13, and no
break/early-exit form exists in any sealed profile. The 0.13 migration
(five frontend modules + `decl.mncs`) keeps the terminal-guard idiom
everywhere; the 0.13 `up_to 1024` ceiling changes fuel *arithmetic* but
not the no-op cost model. Measured signal: the scalar-`match` migration
cut frontend interpreter steps ~9% (1453237 → 1326192), proving dispatch
shape matters, but per-token scans still pay their full bound
(`execution_steps_max` scales with input length in every suite).
`repro/unbounded-scan.mncs` still yields MNP106. The desired capability
is unchanged: source-length-bounded early termination, or an explicit
decision that one-pass-with-no-ops is the intended compiler style.

## Current reconciliation (2026-09-25)

Current Stage-0 rejects the preserved while-loop reproduction with MNP106 (+ parser cascades). The compiler continues to use bounded traversal; no upstream change was made because the new flow workload did not require while.
