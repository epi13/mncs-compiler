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

## Current compiler-parity campaign (2026-09-29)

The current Stage-0 pin is `a3ac17df69e68f6373cbff336db0a572667d73da`,
Profile 0.18. See the [current parity report](PARITY.md),
[parity ledger](parity-ledger.json),
[finite-match/enum-construction value-SSA record](campaign-20260929-cp0014-value-ssa.json),
[project/value-SSA differential](campaign-20260929-project-results.json),
[current-profile surface](campaign-20260929-profile-surface-results.json),
[pressure reproductions](campaign-20260929-pressure-suite-results.json),
[imported-nominal SSA](campaign-20260929-imported-nominal-ssa.json), and
[CP-0014 bool-payload acceptance](campaign-20260929-agent-native-cp0014.json).
Verified value SSA now covers finite enum matches and enum construction
(10 verified functions, 4 finite-match switches, 4 payload extractions,
7 enum constructions, 1 sequence repeat, 10 corruption rejections). The
first unsupported value-SSA operation is integer scalar `TMatch`, and
backend/runtime lowering for the finite/enum operations is unverified.
Native executable output remains a narrow test-only structural scalar C11
slice; see the [2026-09-28 backend vertical](campaign-20260928-agent-native-native-backend-vertical.json).
It is not full SSA parity and the projection is not proof-carrying.

The 2026-10-01 baseline re-measures all twelve verification obligations at
the same pin and compiler head (`3f678c3`); the `campaign-20261001-*`
files carry those results, including a byte-identical project/value-SSA
digest (`6edaa27b…`) to the 2026-09-29 run. The 2026-09-29 record above
remains the original finite/enum slice evidence.

The scalar-match slice lands on top of that baseline: integer scalar
`TMatch` lowers to verified SSA (5 functions, 5 switches, 4 corruption
rejections) with a byte-identical repeated digest (`a3c5a13f…`); see
the [scalar-match record](campaign-20261001-scalar-match-value-ssa.json).
The first [family campaign](campaign-20261001-family-slices.json)
compiled real sources natively with two full-slice successes, two exact
differential agreements, and three classified gaps (new CP-0018/CP-0019
plus a CP-0011 instance).

The record-projection slice lands next: `TProj` lowers to verified SSA
(5 functions, 7 project instructions, 6 corruption rejections) with a
byte-identical repeated digest (`da46e431…`) across the local and
Environment-obligation runners; see the updated
`campaign-20261001-project-results.json`. Landing it exposed and fixed a
latent shared bug (new CP-0020, resolved): match lowering reversed
block-parameter environments, misaligning arm-to-join edges for
heterogeneous-type envs. Finite/enum and scalar-match sections each gain
a mixed-type regression. Family campaign round 2
(`campaign-20261001-family-results.json`) repeats the round-1 stage
classifications on all 9 slices.

The compiler-operation slice (CP-0019) lands next: the five operations
prove in expected → authority → operand order (kinds 53–67 twinning
MNE257/MNE258/MNE261/MNE262 and MNE287–MNE297) and lower to verified
kind-9 SSA (5 functions, 5 operation instructions, 7 corruption
rejections, kind-53/MNE257 unauthorized twin), with the ungated
exact→view borrow at name elaboration plus the let/return backstop;
the semantic suite grows to 131 cases plus eleven proof verdicts and
the project suite establishes digest `2fb25256…` (42 requests). Family
campaign round 3 (`campaign-20261002-family-results.json`) moves both
pulling real sources to verified SSA unmodified
(`structured-artifact.mncs`, `fs-metadata.mncs`); remaining real-source
blocks are the CP-0011 nested-match instance and the CP-0001 size
ceiling. See `campaign-20261002-project-results.json`,
`campaign-20261002-semantic-results.json`, and the CP-0019 pressure
record for the explicitly remaining backend/gating/view items.

The 2026-09-26 and 2026-09-25 campaigns below are historical; their recorded
pins and timings are preserved as measured.

## Historical compiler-parity campaign (2026-09-26)

The 2026-09-26 Stage-0 pin was `843c5bcca7476bb6600218f6410a3da7ef5d96d5`,
Profile 0.18. See the [campaign execution summary](campaign-20260926-execution-summary.json),
[project/value-SSA differential](campaign-20260926-project-results.json),
[current-profile surface](campaign-20260926-profile-surface-results.json),
[front-end differential](campaign-20260926-frontend-results.json),
[segment differential](campaign-20260926-segment-results.json),
[semantic proof](campaign-20260926-semantic-results.json),
[typed CFG](campaign-20260926-flow-results.json), and
[identity-bound RAVEL evidence](campaign-20260926-current-evidence.json).
The new compiler slice carries imported callable identity into verified
value-carrying SSA across a two-module control-flow merge. It is not full SSA
parity and does not emit native executable code.

## Historical compiler-parity campaign (2026-09-25)

The 2026-09-25 Stage-0 pin was `709ba00810099e6965bb47dec14ed19e9e1ae6f8`, profile 0.18.
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
