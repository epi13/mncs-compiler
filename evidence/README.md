# Evidence

This directory holds durable evidence for compiler claims.

Examples include:

- Rust vs MNCS differential results,
- self-host stage comparisons,
- canonical semantic/IR/artifact hashes,
- conformance and negative-test summaries,
- backend compile/execute/validate results,
- cold/warm/incremental benchmark reports,
- peak-memory and compiler-cost measurements,
- multi-session and scheduler stress results,
- reproducibility runs across machines/workers,
- resolved language-pressure before/after evidence.

## Rule

Do not describe a compiler capability as complete because source code for it exists. Link or store executable evidence showing that the behavior works with the pinned toolchain revision.

## Current compiler-parity campaign (2026-09-25)

The current Stage-0 pin is `709ba00810099e6965bb47dec14ed19e9e1ae6f8`, profile 0.18.
See the [refreshed parity ledger](PARITY.md), [current pressure sweep](pressure-current-results.json),
[current-profile parser differential](profile-surface-results.json),
[current semantic twin](sem-results.json), and [typed control-flow differential](flow-results.json).
The flow report covers four programs and 20 requests. It checks exact CFG
diagnostics/spans, current Rust SSA branch/return shape on both positive cases,
and preservation of all proof operations in two identical native runs per case.
It records 41,112,106 interpreted steps and 1,822.472 seconds; that is
Stage-0 interpreter/test cost, not native compiler cost. The [current ABI
identities](flow-abi-current.json) and [production call](flow-call-current.json)
capture the typed block schema and a 107-byte positive call (4,028,123 steps,
4 blocks, 4 typed operations). The exact 91-byte CP-0016 post-fix call remains
in [pre-extension evidence](flow-call-blocks-only-pre-extension.json), paired
with the original [pre-fix failure](flow-failure.json).

The semantic report covers 49 cases and five proof verdicts twice, with
73,631,716 steps over 5,468.621 seconds. Superseded parity and result files
are preserved as `*-pre-campaign.*`; they retain their recorded historical
Stage-0 revisions. The refreshed declaration twin covers 9 requests in 900.429
seconds; the frontend twin covers 5,887 requests in 190.018 seconds; the
segment twin covers 10,679 requests in 1,318.891 seconds. The linked kernel
compile took 15.191/15.657 seconds over two identical runs and emitted
byte-identical semantic, HIR, and SSA files; the report retains 213 unresolved
CMP301 obligations. See [current compile cost](compile-results.json).

The RAVEL impact request for `flow.lower_unit` returned `UNKNOWN`: native
`mncs impact` and `test-inventory` each timed out at 180 seconds, so no impact
plan or closure was produced. [`ravel-impact-flow.json`](ravel-impact-flow.json)
preserves the result. It is not treated as a zero-obligation PASS; the direct
current-pin suites above provide this campaign's verification closure.

## Succession evidence

As `mncs-compiler` approaches canonical status, maintain a succession matrix covering at minimum:

| Area | Rust Stage-0 | MNCS compiler | Evidence |
| --- | --- | --- | --- |
| Language conformance | reference | pending | |
| Negative tests | reference | pending | |
| Self-host | n/a | pending | |
| IR/backend correctness | reference | pending | |
| Reproducibility | reference | pending | |
| Diagnostics | reference | pending | |
| Cold compile cost | baseline | pending | |
| Incremental compile cost | baseline | pending | |
| Peak memory | baseline | pending | |
| Concurrent agent load | baseline | pending | |
| Bootstrap/recovery | canonical | pending | |

The exact matrix may evolve, but replacement of the Rust compiler must remain evidence-based.
## Current kernel evidence

See [bounded frontend contracts and reproduction](FRONTEND.md),
[repeat/differential execution results](frontend-results.json),
[current Stage-0 pressure sweep](pressure-current-results.json), and
[qualified Stage-0 compilation results](compile-results.json). These establish a bounded
ASCII lexer/header slice, not self-hosting or backend parity.

## Current declaration-vertical evidence

See [declaration/symbol/IR contracts and reproduction](DECL.md) and
[repeat/differential declaration results](decl-results.json). These establish
bounded declaration parsing with oracle span parity, function-name symbol
facts, resolve/span walking, and depth-verified stack-IR lowering for
256-byte units, not whole-module compilation or backend parity.
