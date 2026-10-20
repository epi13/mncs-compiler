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

## Environment continuation and registered worktree acceptance (2026-10-17)

The [continuity acceptance record](environment-continuity-acceptance-20261017.json)
captures a fresh resume of the authenticated Compiler campaign in its nested
registered worktree and five passing Control regression cases. The cases cover
nested checkout recognition, selection by exact registered checkout across
multiple sessions and campaigns, refusal of duplicate sessions for one
checkout, and preservation of repository-wide claim conflicts. The nested
worktree fixture also rejects an unregistered lookalike and a symlink escape.
This validates the Environment and Control continuation path; it does not
provision Language write scope or a Forge application-execution binding.

## Project import-closure reachability vector (2026-10-19)

The [reachability-vector record](project-import-closure-reachability-vector-20261019.json)
replaces Project's linked-list membership checks with a fixed 64-entry vector
for target dependency closure discovery and filtering. The 64-source reversed
chain passed the Stage-0 oracle and native target Flow/SSA checks across the
25-case project-capacity harness. The warm 64-source request took 0.401
seconds; the cold probe admitted its 437.6 MB Cranelift artifact in 274.7
seconds and sampled 4.65 GB peak RSS. No same-revision before/after timing
exists, so the record does not claim a speedup. The test ran without a
worktree claim because the authenticated Environment MCP bridge rejected its
claim-acquisition arguments; no live Compiler claim was present at the
pre-run readback. A later isolated one-run baseline comparison against
`f07b0b4` passed the same harness on both revisions. The candidate's one
64-source request sample was 0.392 seconds versus 0.414 seconds at baseline,
but cold readiness was 250.4 seconds versus 249.2 seconds and backend
compilation was effectively unchanged. The runs were sequential, so this is
not a general speedup claim; details are in the
[baseline comparison](project-import-closure-baseline-comparison-20261019.json).
Language verified facts remain unreused, canonical-VM execution was not part
of this frontier, and Stage-1/Stage-2 remain unproven.

## Project import-closure worklist (2026-10-19)

The [worklist record](project-import-closure-worklist-20261019.json) keeps up
to four whole-link scans for shallow closures, then builds bounded source
adjacency and visits reachable sources once. The 27-request Project capacity
harness passes, including a reverse-ordered 64-module target, a cyclic
eight-module nominal-schema closure whose nested projections pass Flow/SSA,
and the existing import-limit and fresh-diagnostic regressions. Stage-0
confirms the 64-module function chain and an acyclic counterpart of the
nominal-schema fixture; its frontend rejects the cyclic graph by policy.

One sample per path measured 0.384 seconds for the 64-source target and 0.546
seconds for the 4,096-link input, versus 0.411 and 0.566 seconds on the
pre-worklist source. The 1,024-import and long-name samples were also lower.
These sequential observations do not establish a general speedup. CPU, RSS,
and cgroup samples were unavailable. This was a Cranelift/Stage-0 Project
frontier, not a canonical-VM run. Language verified facts remain unreused;
Forge execution, Stage-1, and Stage-2 remain unproven.

The focused [canonical-VM record](project-import-closure-worklist-canonical-vm-20261019.json)
builds a 491 KB selected application around `project_import_closure` and runs
the exact eight-link cyclic graph on both Stage-0 and the canonical VM. Both
return indices 0–7 reachable with the same completed result. Artifact
generation took 61.21 seconds at 1.78 GB peak RSS; Stage-0 execution took
74.67 seconds at 1.89 GB peak RSS; the VM call took 0.10 seconds at 23 MiB
peak RSS. Cgroup measurements were unavailable. This proves the selected
callable for this graph, not the whole Project module or a Forge route.

The [maximum-capacity VM record](project-import-closure-worklist-max-capacity-vm-20261019.json)
extends the same artifact to a second callable with 64 import links over all
64 source indices and a 62↔63 cycle. Both that case and the eight-link case
match Stage-0 exactly; all 64 indices are reachable in the maximum case. The
1.13 MB artifact ran both calls in one VM batch in 0.273 seconds at 27 MiB
peak RSS. Stage-0 took 78.38 seconds and peaked at 1.80 GB RSS. The repeat
application-build request returned the same artifact in 0.143 seconds. This
does not test every 4,096-link topology or complete a Forge route.

The [4,096-link VM record](project-import-closure-worklist-4096-vm-20261019.json)
generates 64 links for each of 64 source indices inside MNCS, counts all 4,096,
and passes the list through `project_import_closure`. Stage-0 and canonical VM
agree that all indices are reachable across the reverse-ordered cycle. The VM
used 1,741,056 of an 8,000,000 step allowance and 221,590 memory cells for
that call. One batch with all three graph cases took 89.84 seconds at 34.3 MiB
peak RSS; the Stage-0 oracle took 82.56 seconds at 1.89 GB peak RSS. Cgroup
measurements were unavailable. This proves one maximum-size topology, not all
possible 4,096-link graphs.

The [deterministic graph-corpus record](project-import-closure-graph-corpus-20261019.json)
adds six focused edge cases and sixteen generated 64-node graphs with 128 links
each. An independent Python breadth-first oracle, Stage-0, and the canonical VM
agree on all 25 results, including empty closures, self-cycles, diamonds,
duplicate edges, invalid and disconnected indices, and the four-round path.
The VM batch used 2,567,263 steps total; its largest call used 1,741,056 steps
and 221,590 memory cells. The batch took 91.92 seconds at 35.7 MiB peak RSS.
Stage-0 took 157.46 seconds at 1.81 GB peak RSS. Cgroup measurements were
unavailable. This is deterministic bounded graph coverage, not enumeration of
all possible import graphs or full Project-module proof.

## Current-source Flow-to-value-SSA counted-loop frontier (2026-10-17)

The [Flow-to-SSA report](self-host-flow-ssa-current-20261017.json) carries the
MNCS `flow.lower_unit` result directly into `lower_value_ssa` on one canonical
VM artifact. Both the fallthrough-join case and a conditional return inside a
counted loop match pinned Stage-0 SSA shapes. The registered Environment
verification capability passed both calls. The loop call used 7,772,803 of
its 8,000,000 step budget; the canonical batch took 43.89 seconds and the
whole frontier 48.91 seconds, with 1.19 GiB child peak RSS and 604 MiB VM peak
RSS. Cgroup memory measurements were unavailable. A more complex two-exit
loop exhausted the same per-call step ceiling and is excluded from this
frontier. The report records zero Language verified facts reused. This does
not prove the complete SSA module, imported-interface lowering, compiler
succession, or Language fact reuse.

## Imported nominal schemas and generic calls through Flow/SSA (2026-10-17)

The [current Environment report](self-host-imported-nominal-generic-flow-ssa-current-20261017.json)
and [cold run record](self-host-imported-nominal-generic-flow-ssa-cold-20261017.json)
exercise an imported `Box { value: u64 }` schema and the `read<4>` generic call
through declaration checking, Flow, and verified value SSA. The positive case
verifies both functions and retains the same generic callable identity as
Stage-0, while Stage-0 independently resolves its `<4>` specialization. Three
negative cases reject a mismatched nominal identity, a wrong field schema, and
missing import context. The registered Environment verifier passed all four
calls on a fresh canonical VM batch.

The cold application build took 140.83 seconds at 4.39 GiB peak child RSS;
the canonical VM batch took 42.14 seconds. A fresh Environment run then reused
the exact verified application artifact in 1.83 seconds, invoked no frontend,
and completed its VM batch in 41.87 seconds (49.96 seconds for the full
frontier, 1.51 GiB peak child RSS). This is executable artifact reuse after
semantic verification: `reused_semantic_facts` remains empty. Cgroup memory
measurements were unavailable. The first registered cold attempt hit the
bridge's effective 120-second invocation limit even though the capability
declares 600 seconds; the warm exact-cache run passed. That timeout mismatch
remains an Environment integration issue. This slice does not establish
project-wide module resolution, native generic specialization cloning, full
SSA-module proof, Language fact reuse, or compiler succession.

## Native `mncs-test` core batch isolation (2026-10-17)

The [batch isolation record](native-app-readiness-test-core-batch-isolation-20261017.json)
compiles the production assertions, suite, generative, and snapshot modules
into one selected-callable VM artifact. Seven valid MNCS calls match the
Stage-0 oracle. In the same admitted VM session, a one-step budget exhaustion
and an invalid typed argument are isolated to their calls; a later valid suite
call returns the same typed result as before. Fresh-process exact artifact
reuse, selected-callable isolation, invalid-source rejection, and unknown
callable rejection also pass. This is a Compiler/VM workflow slice; it records
zero Language verified-fact reuse and does not establish Forge, Store, or full
production CLI conversion.

The [direct parser-module frontier](self-host-parser-module-canonical-vm-frontier-20261017.json)
selects `parser.header` and `parser.header_global` as artifact roots instead
of reaching them through kernel wrappers. All 22 bounded and 88 paged cases
agree with the reference interpreter, Cranelift, and canonical VM; the
registered Environment verifier also passed at inventory revision 37. This
proves these selected parser callables, not full parser-module canonical VM
verification or compiler succession. The parser module's overall canonical VM
status remains `not-proven`.

The [current parser-module rerun](self-host-parser-module-canonical-vm-frontier-20261017-current.json)
tests all six parser callables at the selected 1024-byte specializations in 23
bounded cases, 92 paged cases, and five helper calls. It records the exact
Compiler revision `ac23237` and VM source revision `1c19a94`; the Environment
verification passed. The [terminal-state early-exit measurement](self-host-parser-terminal-early-exit-20261017.json)
adds a 1 KiB source with a 20-byte header. Bounded header parsing drops from
29,667 to 12,398 reference-interpreter steps (58.2%); global reference and VM
work also falls modestly because whole-source ASCII validation remains. All
semantic digests match across the reference interpreter, Cranelift, and
canonical VM. Whole-frontier wall times varied with warm toolchain caches, so
the record makes no end-to-end timing claim. Language verified-fact reuse
remains zero, and full parser-module canonical VM coverage and compiler
succession remain unproven.

The [current campaign-head parser rerun](self-host-parser-module-canonical-vm-frontier-20261017-4fbf53d.json)
revalidates the same source hash at Compiler revision `4fbf53d`. All six parser
functions again agree across the reference interpreter, Cranelift, and
canonical VM for 23 bounded cases, 92 paged cases, and five helper calls
(120 VM calls). The 1 KiB short-header case used 12,398 reference steps. The
frontier took 40.35 seconds with 686 MiB peak child RSS; the VM batch peaked at
72 MiB. Cgroup memory values were unavailable. This is selected current-source
evidence; the full parser-module canonical VM status remains `not-proven`.

## Parser diagnostic source locations (2026-10-18)

The [current direct parser-module frontier](self-host-parser-module-canonical-vm-frontier-20261018.json) adds `parser.header_with_locations_global`. It preserves the existing `Header` result and maps each diagnostic's byte start and end through `Source.line_col_span_global` in one scan. A byte-based oracle, the reference interpreter, Cranelift, and the canonical VM agree across 61 diagnostic cases, including a multiline failure and the zero-stride page path. The harness also exercises all seven current parser functions at selected specializations: 24 bounded inputs, 96 paged inputs, and 186 direct canonical-VM calls. The authenticated Environment verifier passed at inventory revision 44.

The full frontier took 25.41 seconds, with 24.75 seconds child CPU, 650,032 KiB peak child RSS, and 76,596 KiB canonical-VM peak RSS. Cgroup measurements were unavailable. Language verified facts remain unreused, and this selected specialization frontier does not establish full parser-module coverage, Stage-1, or Stage-2 succession.

## Extended global header fuel (2026-10-19)

The [current parser frontier](self-host-parser-global-header-fuel-256-e9ff30b.json)
raises the bounded `parser.header_global` token budget from 64 to 256. Valid
40- and 100-segment
module headers now parse; a 130-segment header still returns the explicit
fuel-exhaustion code. All seven selected parser functions agree across the
reference interpreter, Cranelift, and canonical VM for 196 calls, including 61
source-location cases. The authenticated Environment verifier passed at
inventory revision 44.

The frontier took 36.25 seconds (35.46 seconds child CPU), with 651,284 KiB
child peak RSS and 76,504 KiB canonical VM peak RSS. Cgroup measurements were
unavailable. This is one workload-specific run, not a general speedup claim.
Language verified facts remain unreused, and full parser-module coverage plus
Stage-1/Stage-2 succession remain unproven.

## Retained native Test workflow (2026-10-19)

The [retained workflow record](native-app-readiness-native-test-workflow-20261019T053813Z-2074631.json)
uses a four-module, 35-function application. A seven-call Test subset matches
the Stage-0 oracle; a single retained VM session then handles nine calls,
including a budget exhaustion and invalid typed input followed by successful
calls. Fresh-process executable-artifact reuse, comment-edit artifact reuse
after a full frontend pass, reachable dependency invalidation, and tampered
cache rejection are recorded. This revision reused no Language verification
facts and did not emit a Forge execution receipt.

## Local effectful digest application (2026-10-19)

The [digest application record](native-app-readiness-test-digest-app-20261019T023709.640317Z-2003347.json)
captures three Stage-0/canonical-VM result matches for the real
`mncs.test.digest::digest_app` source and its bounded `structured_read` and
`structured_write` effects. The artifact is 619,051 bytes and exercises
capability identity checks, invalid typed input, budget exhaustion, rejected
output writes, and later successful calls in one retained session. The local
Provider API test adapter handled the effects; this evidence is not a Forge
execution or Store receipt.

## Compiler-parity snapshot (2026-09-29)

This snapshot used Stage-0 pin `a3ac17df69e68f6373cbff336db0a572667d73da`,
Profile 0.18. See the [parity report](PARITY.md),
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

## Native Test digest suite specialization (2026-10-16)

The [digest suite specialization record](native-app-readiness-digest-suite-specialization-20261016.json)
extends the real `mncs.test.digest` multi-callable artifact with a harness-only
root that explicitly selects and executes
`mncs.test.suite::summarize<4>`. A mixed PASS/FAIL/SKIP/UNSUPPORTED suite result
matches the Stage-0 project oracle in both a six-call canonical VM batch and an
eight-call retained VM session, including recovery after exhausted fuel and a
rejected typed request. The artifact also preserves fresh-process cache reuse
and invalidation after a reachable imported function edit. The refreshed
[digest suite report](native-app-readiness-digest-suite-specialization-20261016.json)
was refreshed at Compiler revision `3c02556`. It records a 4.120-second cold
build (4.04 CPU seconds, 81,629,184-byte peak RSS), a 0.584-second
fresh-process hit (0.55 CPU seconds, 37,031,936-byte peak RSS), a six-call VM
batch at 0.216 seconds and 27,512 KiB peak RSS, and an eight-call retained
session at 0.236 seconds and 27,460 KiB peak RSS. Cgroup memory measurements
were unavailable. The cold build and changed-source check still run the full
frontend; the exact-snapshot hit avoids it. The run reused zero Language
verified facts. The Environment-bound verification passed without producing a
separate Store artifact; this does not establish Forge execution of
`digest_app` or full `mncs-test` conversion.

## Native Test digest workflow refresh (2026-10-17)

The [current workflow report](native-app-readiness-native-test-workflow-20261017.json)
re-runs the multi-callable `application-build/5` workflow on Compiler revision
`270fa87`. The pinned Stage-0 digest cases, schema callables, and generic Test
suite summaries agree across the reference oracle and canonical VM. The
bounded batch completes seven calls in 0.258 seconds at 27,664 KiB peak RSS;
one retained VM process completes nine calls in 0.269 seconds at 27,412 KiB
peak RSS, recovering after low fuel and rejecting invalid typed input without
poisoning later calls. The cold build takes 4.521 seconds (4.48 CPU seconds,
92,770,304-byte peak RSS); a fresh-process exact-snapshot hit takes 0.581
seconds with zero frontend invocations. The comment-edit path still runs the
full frontend and reuses the verified VM artifact closure; Language verified
facts reused remain zero. The registered Environment verifier passed on the
updated Compiler revision. This does not establish Forge/Environment/Store
application execution or full `mncs-test` conversion.

## Effectful digest application, incremental cache, and Environment artifacts (2026-10-17)

The [effectful digest application report](native-app-readiness-test-digest-app.json)
executes the real `mncs.test.digest::digest_app` artifact with the exact
`test_artifact` capability identity through the canonical VM Provider API.
Three successful structured read/write cases match the pinned Stage-0 outputs
in one retained process. Missing or mismatched capability admission, a
mismatched dispatch alias, incorrect artifact identity, invalid typed input,
rejected output publication, and exhausted execution fuel all fail with the
recorded typed outcomes; subsequent calls remain isolated and can complete.
This proof is the permanent Compiler obligation
`mncs-compiler.native-test-digest-effectful-app-canonical-vm`. The latest
Environment-bound run cold-built in 0.568 seconds (0.52 CPU seconds,
65,712,128-byte peak RSS); its seven-call retained session took 0.029 seconds
and peaked at 11,845,632 bytes. It still does not establish Forge invocation
or full Test conversion.

The same effectful application now has a permanent incremental artifact
obligation, `mncs-compiler.native-test-digest-effectful-app-incremental-cache`.
Its [report](native-app-readiness-test-digest-app.json) records a 0.575-second
cold build (0.52 CPU seconds, 65,699,840-byte peak RSS), a 0.123-second
fresh-process exact-snapshot hit with zero frontend invocations, and a
0.351-second comment-only imported-module edit with one frontend pass and the
identical executable artifact bytes and callable interface. The edit refreshes
the source snapshot and imported source hash. These observations reused zero
Language-owned verified facts; cgroup memory readings were unavailable.

The latest registered Environment verifier run cold-built in 0.567 seconds
(0.52 CPU seconds, 65,642,496-byte peak RSS); its seven-call retained session
took 0.029 seconds (0.01 CPU seconds, 11,796,480-byte peak RSS). Cgroup memory
measurements were unavailable. The verifier stored the report and canonical
VM artifact in a 1,136,512-byte Store bundle. Fresh authenticated Store reads
returned the 28,118-byte report and both ordered VM artifact chunks with exact
matching SHA-256 values. The two chunks total 1,108,394 bytes and reconstruct
the verifier's artifact file
(`efb6411d21c968f625e7b299b1994ec1be753a48ae23209b7a0afc33ecd1f047`) within
Environment's 1-MiB per-file read bound. Exact readback metadata is in the
[earlier Environment artifact readback evidence](native-app-readiness-environment-artifact-readback-20261017.json)
and [effectful digest Store readback evidence](native-app-readiness-effectful-digest-store-readback-20261017.json).
This is verifier-output publication, not a Forge application-execution receipt.

A further Environment-bound rerun at 23:43 UTC refreshed the report against
Compiler revision `a366a08`. The cold build took 0.529 seconds (0.48 CPU
seconds, 65,245,184-byte peak RSS); the fresh-process unchanged build took
0.123 seconds, and the comment-only imported-module edit took 0.317 seconds.
The retained seven-call VM batch took 0.030 seconds and peaked at 11,882,496
bytes. It again reused zero Language verification facts. Its 1,136,646-byte
Store bundle was read through the selected authenticated session; the report
and both ordered VM artifact chunks are recorded with their exact Store
reference and SHA-256 values in the [latest readback evidence](native-app-readiness-digest-app-store-readback-20261017.json).
This remains Compiler verifier output, not a Forge execution receipt.

### Pushed Compiler-source digest application rerun (2026-10-19)

The [current digest application report](native-app-readiness-test-digest-app-20261019T034010.166758Z-2034183.json) reruns the three-module-import `mncs.test.digest::digest_app` workload on pushed Compiler revision `fd04fb2`. All three canonical-VM cases matched the Stage-0 oracle, including bounded `structured_read` and `structured_write` effects. Seven isolated calls ran in one retained process (0.033 seconds, 15.5 MB peak RSS). Missing or mismatched capability identity, dispatch alias, and artifact identity, invalid typed input, rejected output, and step-budget interruption failed closed.

The cold application build took 0.889 seconds (0.70 CPU seconds, 42.6 MB peak RSS); a fresh-process exact-snapshot hit took 0.126 seconds with zero frontend invocations. A comment-only imported-module edit took 0.662 seconds, reran the frontend, and reused the executable artifact while reusing zero Language facts. The typed verifier result and VM artifact were read back through a separate authenticated Environment request from canonical Store with matching hashes. This remains canonical-VM verifier output, not a Forge receipt; the Forge route is still the next application-execution frontier.

### Environment-bound effectful digest rerun and Store readback (2026-10-20)

The [canonical VM report](native-app-readiness-test-digest-app-20261020T004551.957312Z-2407786.json) and [incremental-cache report](native-app-readiness-test-digest-app-20261020T004613.711987Z-2407918.json) compile and execute the real `mncs.test.digest::digest_app` with its three imported MNCS modules. All three successful canonical-VM cases match the pinned Stage-0 results. The verifier checks the two admitted `test_artifact` dispatch operations (`structured_read` and `structured_write`), denied or mismatched capability identities, mismatched dispatch alias, incorrect artifact identity, invalid typed input, rejected output, and step-budget interruption. Seven isolated calls complete in one retained VM process.

The incremental-cache run cold-built in 0.704 seconds (0.66 CPU seconds, 42.6 MB peak RSS), reused the artifact in a fresh process in 0.116 seconds with zero frontend invocations, and handled a comment-only imported-module edit in 0.517 seconds. That edit invoked the full frontend once and reused the same executable artifact. It reused zero Language verified facts. Cgroup memory readings were unavailable.

Environment published the verifier report and 619,051-byte VM artifact in a 648,719-byte canonical Store bundle. Fresh authenticated reads returned the exact 29,668-byte report (`dc2a1e477103ce808008ba2c81097a3a2de4f3474b8b9af16ca605ae9ce739ea`) and artifact (`d2e5d7485cc6b7f5ad459aebacc03b7b253cc8a4aa9b50b7d90a4bbc30e3df2b`); see the [Store readback record](native-app-readiness-digest-app-store-readback-20261020.json). This remains a canonical-VM verifier run through the bounded host adapter. The Store bundle contains verifier outputs, not a Forge application-execution receipt or a published application result.

## Current declaration parser frontier (2026-10-16)

The [current `decl.parse_unit` frontier](self-host-decl-parse-expanded-canonical-vm-current-8817cdb-20261016.json)
covers 37 selected hand-picked and production declaration cases: 19 valid and
18 invalid, including five missing-field-separator regressions. Stage-0
diagnostics and structure, the reference interpreter, Cranelift, and a fresh
canonical VM artifact agree on the same semantic digest. The run took 124.797
seconds wall time and 122.875 child CPU seconds; child peak RSS was 1,977,932
KiB and canonical VM peak RSS was 362,076 KiB. Cgroup readings were unavailable.
This is a function frontier, not full declaration-module or succession
evidence; it reused zero Language verified facts.

## Current declaration checker frontier (2026-10-16)

The [current `decl.check_unit` frontier](self-host-decl-check-unit-canonical-vm-20261016.json)
runs all nine focused declaration-check cases through the pinned reference
interpreter, Cranelift, and one fresh canonical VM artifact. The cases cover
successful structural IR checks, duplicate function names, unresolved calls,
generic signatures, and iterate/index/cast/slice bodies. All three executors
returned the same digest, `0abe0e3e…e59cc`. The canonical VM batch took 12.35
seconds and peaked at 362,656 KiB; the complete run took 136.04 seconds and
134.41 child CPU seconds. Host memory was sampled; cgroup readings were
unavailable.

This advances the native self-consumption frontier from parsing into
declaration resolution and structural IR verification. `check_unit` parses
each complete source unit and does not perform full type checking. It reused
zero Language-owned verified facts; full declaration-module verification and
Stage-1/Stage-2 succession remain unproven.

## Current declaration proof-unit frontier (2026-10-16)

The [current `decl.prove_unit` frontier](self-host-decl-prove-unit-canonical-vm-20261016.json) runs six selected current-source proof cases on one fresh canonical VM artifact. Stage-0 oracle/elaboration diagnostics match the VM FAIL obligations in both diagnostic cases, and UNKNOWN obligation kinds 13 and 14 remain explicit. The canonical VM digest is `ba9a750c…3ae538`; the batch took 24.57 seconds and peaked at 444,184 KiB. The whole frontier took 72.17 seconds and 69.53 child CPU seconds; host memory was sampled and cgroup readings were unavailable.

This is a selected `decl.prove_unit<1024,1024>` function frontier. It establishes no Cranelift differential or full declaration-module proof, reused zero Language-owned verified facts, and does not claim Stage-1 or Stage-2 succession.

## Current proof-to-Flow frontier (2026-10-17)

The [current Flow proof handoff](self-host-flow-proven-unit-current-20261017.json) runs ten selected fixtures through one fresh canonical VM artifact on the Compiler campaign worktree based at `bca39a8`; the tested Flow source SHA-256 is `ee84e1d0…8b762f38`. It includes multiple conditional returns in counted and sequence loops, and checks loop headers/backedges against Stage-0 metadata. Nine cases match Stage-0 diagnostics exactly. The `unreachable_loop_join` case now retains the declaration-verified `next` operations in one explicit elided-expression record, keeps the unreachable join as a dead sink, and emits no executable loop backedge. Stage-0 still reports two `MNB010`, two `MNB017`, and `MNB038`; native Flow reports `MNB038`, so that diagnostic difference remains open and is excluded from the parity claim. Evidence SHA-256: `6872c87fe55bf58e268c88fe64cec00c8b075ead1587e5d6989f99b60bff9a7e`.

The VM batch took 50.21 seconds and peaked at 462,972 KiB; the whole frontier took 97.14 seconds, 94.58 child CPU seconds, and 2,629,780 KiB child peak RSS. Host memory was sampled and cgroup readings were unavailable. The registered verifier timeout is 180 seconds after the earlier 120-second invocation timed out. This bounded Flow function frontier reused zero Language-owned verified facts and does not establish full module proof or compiler succession.

### Flow diagnostic pressure: unreachable loop join

The smallest reproduction is the `unreachable_loop_join` fixture in `tools/test_vm_flow_proven_frontier.py`. Run it with:

```sh
MNCS_FLOW_PROVEN_FRONTIER_EVIDENCE_PATH=evidence/self-host-flow-proven-unit-current-20261017.json python tools/test_vm_flow_proven_frontier.py
```

Native Flow now separates executable CFG expressions from verified but unreachable expression facts. Its result accounts for every declaration-verified operation in either a CFG block or the function's explicit elided-expression inventory, while retaining the dead join as a sink and omitting a loop backedge. This fixes the malformed generated next-state block without claiming exact Stage-0 diagnostic parity. The remaining pressure is diagnostic stability: Stage-0 adds two `MNB010` and two `MNB017` cascades to the join's `MNB038`. The likely ownership is shared between Language's Stage-0 diagnostic path and Compiler's Flow diagnostic contract. Status: observed and open; no upstream issue is recorded. Language-owned verified facts reused: zero.

## Current Flow-to-SSA frontier (2026-10-16)

The [current Flow-to-SSA handoff](self-host-flow-ssa-canonical-vm-20261016.json) runs a real MNCS source function through `flow.lower_unit`, derives local nominal identities from its proved unit, and passes that exact `flow.LoweredUnit` value into value SSA from one canonical VM call. The selected function is supported and verified; its one branch and two returns match the Stage-0 SSA oracle. Environment's registered verification capability passed with the exact-source artifact cache hit.

The Environment verification took 32.53 seconds end to end, including a 25.29-second canonical VM batch. Child peak RSS was 1,242,372 KiB and VM peak RSS was 614,972 KiB; the cgroup counters were unavailable. Language-owned verified facts reused: zero. This selected pipeline handoff does not prove the whole SSA module, imported-interface invalidation, incremental semantic reuse, or compiler succession. The imported nominal and generic path is now exercised below; Language-owned fact reuse remains the next semantic frontier.

## Imported generic/schema application cache invalidation (2026-10-17)

The [imported SSA cache report](self-host-imported-ssa-incremental-20261017.json)
continues the real Compiler `ssa.lower_flow_unit_value_ssa_with_imported_schemas<4,64>`
frontier through application-build/4. It compares a cold artifact, exact warm
hit, an added uncalled callable, and a body-only edit to that uncalled callable.
The exact warm hit reused the artifact with zero frontend invocations (1.36 s).
Adding the callable changed the module interface and correctly generated a new
artifact (142.77 s). Editing only its body then reused that artifact after the
full frontend ran (54.96 s). The cold build took 140.47 s. The retained probe
process peaked at 4.70 GB RSS; this is a process high-water mark, not a separate
per-request memory delta. Cgroup measurements were unavailable.

The body-edited artifact ran `demo.root::read<4>` in the canonical VM; both
imported functions were verified and the result was valid. A `bool` imported
field schema for the source's `u64` field returned an invalid summary. The two
VM checks took 28.76 s and 22.67 s respectively and peaked near 597 MiB RSS.
The whole frontier took 401.11 s. Changed snapshots still invoked the full
Language frontend and reused zero Language verified facts. Artifact reuse here
does not establish semantic-fact reuse or compiler succession.

The Compiler application cache can now use
`MNCS_PROBE_APPLICATION_CACHE_DIR` independently of
`MNCS_PROBE_CACHE_DIR`, so this test isolates application artifacts while
retaining the compiler program cache. The focused Rust cache-path unit test
passed.

## Imported nominal field application-cache invalidation (2026-10-17)

The [imported nominal cache report](self-host-imported-nominal-app-cache-invalidation-20261017.json)
uses a real two-module application. An unchanged request reuses its exact
artifact in 0.4 ms with no frontend invocation. Changing only the imported
`Box.padding` field from `bool` to `u8` changes the canonical record identity,
runs the frontend, and emits a different artifact. Both artifacts return the
same `u64` value from `demo.root::read` in the canonical VM. Changing the
projected `Box.value` field to `bool` is rejected with the same `MNE163` and
`MNE103` diagnostics as the independent Stage-0 oracle; the cache bypasses and
emits no artifact. The entire frontier took 0.50 seconds and peaked at about
12.6 MB in the retained probe process; cgroup measurements were unavailable.

These changed-source builds still rerun the full frontend and reuse zero
Language-owned verified facts. This proves imported nominal artifact
invalidation and fresh diagnostics, not semantic-fact reuse.

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

## Current source scanner evidence (2026-10-16)

The [eight-byte source ASCII frontier](self-host-source-ascii-global-eight-byte-20261016.json)
checks 39 inputs across the reference interpreter, Cranelift, and canonical VM,
including every tail length from 0 through 17 and every non-ASCII position in a
two-page input. The 256 KiB scan used 14.18% fewer reference interpreter steps
than the preceding four-byte frontier. This is selected callable evidence;
Language verified facts remain unreused and the full source module is not proven
on this candidate.

The [sixteen-byte source ASCII frontier](self-host-source-ascii-global-sixteen-byte-20261016.json)
extends that callable to 71 inputs, covering tail lengths 0 through 33 and each
non-ASCII position in a 33-byte source split across stride-9 pages. Reference
interpreter and canonical VM step counts fell about 7.9% on full 64 KiB and
256 KiB scans relative to the eight-byte version. Single-run reference and
Cranelift wall times for the large scans were about 2% to 3% higher, so this
record does not claim a wall-time speedup. The full source module remains
unproven; Language verified facts remain unreused.

The [first-invalid-page frontier](self-host-source-ascii-global-early-exit-20261017.json)
keeps the same 71 cases and returns from the page walk after the first page
reports a non-ASCII byte. For a 256 KiB input with a non-ASCII first byte,
reference interpreter work fell from 3,114 to 51 steps (98.36%). Full ASCII
and last-byte scans changed by 0.03% in steps; single-run wall times do not
establish a general speedup. The callable agreed across the reference
interpreter, Cranelift, and canonical VM. Language verified facts remain
unreused, and full Source module verification and compiler succession remain
unproven.

## Source content identity frontier (2026-10-17)

The [four-byte fingerprint frontier](self-host-source-fingerprint-global-four-byte-20261017.json)
changes `source.fingerprint_global` to fold four sequential FNV-1a bytes inline
per page-loop transition. It matches the previous fingerprint for 24 cases
covering empty input, all short tails, page boundaries, and 64/256 KiB inputs
across the reference interpreter, Cranelift, and a fresh canonical VM artifact.
On 256 KiB of source, reference steps fell 33.78% and observed wall time fell
45.86% (13.95 s to 7.55 s); the 64 KiB run fell 33.78% in steps and 50.39%
in wall time. These are single-run measurements. The canonical VM batch took
5.06 s at 243 MiB peak RSS. The CP-0001 Tier C source identity checks also
passed in 96 probe requests. This optimizes source identity only: Language
verified facts remain unreused, and it does not prove the full Source module
or compiler succession.

## Source location line/column frontier (2026-10-17)

The [line/column frontier](self-host-source-line-col-global-20261017.json)
checks `source.line_col_global` for empty input, newline and page-edge offsets,
clamped offsets, CRLF bytes, UTF-8 byte columns, and a 64 KiB source. The MNCS
implementation now scans only pages and bytes before the clamped offset. The
reference interpreter, Cranelift, and one fresh canonical VM artifact agree
over 85 requests per executor and strides 1, 7, 64, and 1024. Aggregate
reference-interpreter and canonical-VM steps fell 19.64% and 19.57% on the
same requests; two candidate canonical-VM batch samples took 9.23 and 9.32
seconds versus one 11.38-second baseline sample. Peak VM RSS was 103,056–
103,244 KiB versus 102,184 KiB; the maximum individual call stayed effectively
flat because the end-of-source cases scan the full input. CPU samples include
different compiler cache state, so no CPU speedup is claimed. Cgroup measurements
were unavailable.
Language verified facts remain unreused, and this selected callable does not
prove Unicode-scalar columns, the full Source module, or compiler succession.
The verifier descriptor allows 900 seconds, while the effective invocation
limit observed was 120 seconds; the bounded 85-request suite passed through the
same authenticated Environment verifier.

## Source location batch frontier (2026-10-17)

The [batch line/column frontier](self-host-source-line-col-batch-20261017.json)
adds `source.line_cols_global`, which maps up to 64 nondecreasing byte offsets
in one forward scan. Repeated offsets and offsets that clamp to the same source
end are accepted; a decreasing clamped sequence is rejected. A zero-stride page
uses the existing scalar lookup path. Reference interpreter, Cranelift, and a
fresh canonical VM artifact agree across 24 input layouts, including empty and
64 KiB sources, newline page edges, UTF-8 byte columns, and the zero-stride case.
The harness also checks 90 scalar requests, 26 spans, and 25 batches per
executor, including one rejected out-of-order batch.

For the 24 valid canonical-VM batches, measured transition counts fell by
5,495,544 (75.396%) compared with independent scalar lookups. The largest batch
used 1,712,779 transitions. The full canonical-VM run took 25.357 seconds at
135,320 KiB peak RSS; these are workload-specific single-run measurements.
Language verified facts remain unreused, and this selected Source frontier does
not establish full Source module proof or compiler succession.

## Current real-Flow resource profile (2026-10-09)

The [Flow resource profile](campaign-20261009-flow-resource-profile.json)
records the earlier 1,600 MiB bounded pair. The follow-up
[HIR prelude profile](campaign-20261009-flow-hir-prelude-profile.json) records
matched pristine, V1, and indexed-lookup Stage-0 identities at a 2,560 MiB
sampled child RSS stop. Reusing the validated HIR prelude and indexing
function identities reduced Stage-0 host HIR preparation from 91.290 to
63.052 seconds on the exact compiler input. The target request still stopped
during retained-session admission without a returned compiler result; cgroup
OOM deltas were zero. The
[phase-correlated RSS repeat](campaign-20261009-flow-phase-resource-samples.json)
adds time-series samples for the actual child and shows RSS rising by about
567 MiB between the host compiler-SSA timing point and the bounded stop. This
narrows the next investigation to the tail of the research-bytecode backend
compilation/admission path, without attributing it to a specific allocation.
The associated compiler finding is
[CP-0026](../pressure/0026-real-flow-hir-prelude-reconstruction.md).

## Current declaration-vertical evidence

See [declaration/symbol/IR contracts and reproduction](DECL.md) and
[repeat/differential declaration results](decl-results.json). These establish
bounded declaration parsing with oracle span parity, function-name symbol
facts, resolve/span walking, and depth-verified stack-IR lowering for
256-byte units, not whole-module compilation or backend parity.

## Declaration lexer dispatch repeat (2026-10-10)

The [dispatch repeat report](campaign-20261010-decl-significant-dispatch-repeat.json)
records four warm unprofiled runs per source identity and a paired runtime
profile for the 500,000-step `compile_project_target<1024,1024>` request.
The late interleaved pairs reduced target execution time from 75.418 to
72.277 seconds (4.16%) and actual child CPU from 76.24 to 73.07 seconds
(4.16%); peak RSS stayed near 1.68 GiB and the child held five file
descriptors. The earlier sequential block showed a larger timing difference,
so it is retained as context rather than used for the stable effect estimate.

The candidate removes 186 profiled `segment.significant_global` wrapper calls
(3.22 seconds exclusive in the baseline). `lexer.next_token_global` remains
the largest measured cost at about 33.2 seconds exclusive. Both bounded target
runs exhausted the same step budget while parsing `decl`, returned no project
result, and therefore leave semantic outcome UNKNOWN. There was no sampled RSS
cap or cgroup OOM event. This evidence does not promote any module stage or
claim Stage-1/Stage-2 succession.

A candidate-only 1,000,000-step follow-up timed out at 240.497 seconds and the
runner interrupted the still-active child. The child used 234.17 seconds of
CPU, peaked at 1,758,896 KiB RSS, and held five FDs; the RSS stop and cgroup
OOM counters remained clear. No response or step count was returned, so the
classification is TIMEOUT / INTERRUPTED with semantic outcome UNKNOWN. The
follow-up record is in the same report and is not a matched performance
comparison.

## Declaration name-token reuse (2026-10-10)

The [token-reuse report](campaign-20261010-decl-name-token-reuse.json) records a focused `parse_project_unit` fixture covering record fields, payload enum fields, and generic parameters. Reusing the token already read by `fields_step`, `variants_step`, and `generics_step` reduced mean request execution from 17.161 to 16.230 seconds (5.43%) and executor steps from 85,933 to 77,324 (10.02%) over two warm runs per source identity. The Stage-0 oracle accepted the positive fixture and matched the malformed-field diagnostic span. A larger `compile_project_target` follow-up timed out without a result and remains UNKNOWN. No module-stage, executable, Stage-1, or Stage-2 claim is made.

## Shared probe cache across linked worktrees (2026-10-10)

The [shared-cache report](campaign-20261010-shared-probe-cache.json) records two bounded preparation cases. In a linked worktree without a local `.bootstrap/revision`, the probe selected the main checkout's pinned Stage-0 toolchain and Git-common-directory cache. For source identity `bc522b7b…`, it decoded the expected cache key in 1.002 seconds; the decode phase took 612.9 ms. The external sampler observed one child sample at 125,936 KiB RSS, 0.43 seconds CPU, and five FDs. A child-internal report correlated by PID and start ticks recorded 319,963,136 bytes kernel RSS high-water and 0.73 seconds CPU. Both observations are retained because the short-lived child was externally sampled only once.

The report also includes a matched cold-fill/warm-hit pair for source identity `ef04cc83…`, Stage-0 `b05dfa2b…`, and cache key `2646f6ad…`. The cold miss took 155.708 seconds (154.25 seconds child CPU, 2,142,380 KiB sampled RSS, five peak FDs); the warm hit took 1.002 seconds (0.43 seconds sampled child CPU, 131,184 KiB sampled RSS, five peak FDs). Both were cache-preparation-only successes, with no cgroup OOM event. Neither case attempted the target compiler request, so these measurements establish reusable frontend preparation, not target execution or semantic success. Cache keys continue to bind exact toolchain and compiler source identities.

## Declaration variant-loop early exit (2026-10-10)

The [variant-loop report](campaign-20261010-variants-loop-early-stop.json)
records a source-owned optimization in `decl.parse_variants`: enums that reach
the terminal state within the first 64 calls skip the remaining no-op calls,
while the original 1,024-call ceiling remains. The focused Stage-0 differential
accepted 65 variants and matched the exact malformed-65th-variant span
`[428,429]`.

On the same 500,000-step reference-interpreter prefix of
`compile_project_target<1024,1024>`, two baseline runs averaged 87.873 seconds
wall and 84.715 seconds of child CPU; two candidate runs averaged 73.602
seconds wall and 70.675 seconds CPU. The runtime profile reduced
`variants_step` from 1,024 calls / 15.877 seconds exclusive to 64 calls / 1.015
seconds. Both target identities still exhausted the bounded step budget and
returned no project result, so the semantic result remains UNKNOWN. The sampled
RSS stop and cgroup OOM counters remained clear. `lexer.next_token_global` is
now the leading measured function cost.

The direct whole-module re-ingestion attempt for the edited `decl.mncs` source
and the broader rerun of all legacy POS/NEG fixtures were interrupted before
complete results and have no child-level resource telemetry. They do not
establish fresh whole-module parse evidence or a complete legacy-fixture sweep.
The dedicated boundary differential above completed. The historical matrix
rows and compiler-head identity remain unchanged; this report keeps the
candidate source digest and delivered revision separate.

## Native digest callable batch (2026-10-16)

The [digest callable batch report](native-app-readiness-digest-native-batch.json)
uses the exact `mncs-test` digest source snapshot covered by the Gate A
Stage-0 fixture. One `application-build/5` artifact contains `build_digest`,
`request_schema`, and `result_schema`; five calls then complete in one bounded
canonical VM batch. All three digest results match the pinned Stage-0 outputs,
and a fresh-process build reuses the same artifact with zero frontend
invocations. The batch used 1,977 VM steps and peaked at 27,076 KiB RSS; the
measured cold build took 9.16 seconds and the warm hit 4.32 seconds.

This establishes a pure native Test workflow slice. It does not establish
`digest_app` capability execution through Forge, Environment, or Store, full
`mncs-test` conversion, or Language verified-fact reuse. The
[cursor-carry candidate record](self-host-global-segment-canonical-vm-cursor-carry-candidate-20261016.json)
retains a correctness-passing lexer experiment that was reverted after its
matched 1,000,000-step profile showed more `next_token_global` work.

## Digest application interface and specialization invalidation (2026-10-16)

The [digest invalidation report](native-app-readiness-digest-interface-invalidation-20261016.json)
extends the real `mncs-test` application-build workload in two ways. Changing
the imported `suite.empty` signature reverified the frontend, regenerated the
backend artifact, and preserved all pinned digest VM results. Editing the
selected `assertions.equals_bytes<4>` body also reverified and regenerated its
specialization; original and edited canonical VM calls both matched Stage-0.
Fresh processes then reused each exact rebuilt artifact with zero frontend
invocations. These checks establish safe artifact invalidation and reuse;
they do not establish Language-owned verified-fact reuse, which remains zero.

## Native segment token batch frontier (2026-10-16)

The [segment batch report](self-host-segment-batch-canonical-vm-20261016.json)
extends the selected global cursor workload through `segment.lex_batch_from`.
Twelve calls cover three existing segment corpus inputs at page strides 1, 7,
64, and 256. Stage-0 expectations, the reference interpreter, Cranelift, and a
single canonical VM artifact agree on every token, span, diagnostic, count,
cursor, and EOF result. The canonical VM batch took 2.10 seconds and peaked at
56,000 KiB RSS; the full frontier took 20.98 seconds. This is a selected
executable batch frontier, not full module proof or Stage-1/Stage-2 succession.

## Current-source global segment frontier (2026-10-19)

The [authenticated rerun report](self-host-global-segment-canonical-vm-current-source-20261019T055838Z.json)
revalidates `segment.next_token_global` and `significant_global` at Compiler
revision `eacb5097e7bbfe0b134af222d251a11a75fbf222`. Stage-0, the reference
interpreter, Cranelift, and a fresh canonical VM agree across 1,224 cases with
the same semantic digest. The frontier took 91.83 seconds and 89.04 child CPU
seconds; child peak RSS was 463,588 KiB and canonical VM peak RSS was 155,600
KiB. Cgroup readings were unavailable. This remains a selected cursor
frontier, not full segment/lexer verification or Stage-1/Stage-2 succession.

## Probe producer identity cache cost (2026-10-16)

The [probe cache identity report](native-app-readiness-probe-cache-identity-20261016.json)
records a focused Stage-0 change that preserves exact executable SHA-256
identity while hashing in bounded 64 KiB chunks with an optimized SHA-256
dependency. On the same multi-callable digest snapshot, the fresh-process exact
cache hit fell from 4.346 to 0.581 seconds, identity generation fell from
4.130 to 0.420 seconds, and sampled peak RSS fell from 123 MiB to 48 MiB. The
application-build/5 contract and artifact identity checks remained intact. The
[provider regression record](native-app-readiness-provider-digest-cache-identity-20261016.json)
also retains the post-change invalid-input, Store-input validation, and local
VM adapter checks. These host-specific measurements do not represent Language
verified-fact reuse or the live Forge/Environment/Store effectful route.

## Raw application-cache response verification (2026-10-16)

The [cache response validation report](native-app-readiness-probe-cache-response-validation-20261016.json)
records the next warm-hit improvement. For the same digest snapshot, exact
cache lookup fell from 100.0 ms to 53.6 ms, warm-hit wall time fell from 0.581
to 0.550 seconds, and sampled peak RSS fell from 48 MiB to 34 MiB. The reader
hashes the exact stored response bytes and deserializes the response once,
without changing cache-entry/3. A tampered artifact identity with a stale
response digest was rejected and rebuilt through the full frontend. The
[provider regression record](native-app-readiness-provider-digest-raw-response-20261016.json)
passed invalid-source, corrupt Store-input, and local VM adapter checks. This
still reuses zero Language verified facts and does not establish live
Forge/Environment/Store execution.

## Unreachable imported body and digest artifact reuse (2026-10-16)

The [unreachable-body report](native-app-readiness-digest-unreachable-body-20261016.json)
edits the unused `mncs.test.assertions::equals_i64` body in the real digest
application's imported module. The changed source snapshot receives a complete
frontend verification (one invocation, all 30 program functions rechecked),
while its selected executable closure retains the same artifact identity and
reuses the VM artifact. A fresh process then reuses the exact edited snapshot
with zero frontend invocations. Stage-0, canonical VM, and the pinned digest
fixture agree on all five results. The cold, changed-snapshot, and fresh-process
builds took 3.81, 2.20, and 0.55 seconds respectively; peak RSS was 74, 63, and
35 MiB. This is executable artifact reuse after full semantic verification,
not Language-owned verified-fact reuse; the reused-fact count remains zero.

## Environment application-build contract revision (2026-10-16)

The [contract revision report](native-app-readiness-environment-build-contract-v5-20261016.json)
records the selected nested Compiler worktree resuming its existing authenticated
session after the manifest update. Environment now reports capability revision
5 with request `/2` and response `/5` as current while retaining request `/1`
and response `/4`. The focused manifest/provider agreement check passed. This
validates Environment's advertised contract metadata and compatibility pair;
it does not establish an invocation through the live Environment provider or
the effectful Forge/Environment/Store route.

## Application provider regression under contract revision 5 (2026-10-16)

The [provider regression report](native-app-readiness-provider-contract-v5-20261016.json)
reruns the local provider after the Environment descriptor update. Both request
and response pairs remain supported. The digest application compiles with
verified capability dispatch, reaches an exact fresh-process cache hit, and
reports zero Language verified facts. Invalid source returns `MNE162` without
publishing success; corrupt Store-input bytes are rejected. The selected
multi-callable artifact runs both exports in one canonical VM batch. Cold,
warm, digest cold, digest warm, and multi-callable provider timings were
0.61, 0.23, 0.68, 0.24, and 0.19 seconds; the VM batch took 7.5 ms. This uses
the local provider and VM adapter, not Forge, the live Environment provider,
or canonical Store execution.

## Declaration bound-prefix page fast path (2026-10-16)

The [declaration prefix report](self-host-decl-up-to-window-inline-fast-path-20261016.json)
records a same-page fast path in `decl.is_up_to_bound`, with the prior global
reads retained for page edges and malformed views. Thirty-one direct helper
cases cover every prefix start offset for strides 1 through 7; seven actual
sequence declarations exercise split prefixes and an exact-Nat identifier
lookalike. Stage-0 parser facts, the reference interpreter, Cranelift, and one
fresh canonical VM artifact agree across all 38 cases.

Reference steps fell from 442,559 to 442,381 across the selected cases. The
canonical VM batch stayed at about 10.5 seconds and 358 MiB peak RSS, and warm
Cranelift time stayed near 14.4 seconds. This is a small interpreter-step
reduction with no material end-to-end speedup; full declaration-module proof,
Language verified-fact reuse, Stage-1, and Stage-2 remain unproven.

## Concurrent application-cache publication regression (2026-10-17)

The [cache publication report](application-cache-concurrent-publication-20261017.json)
adds a regression around the existing `fsync` plus atomic-rename path for
application-cache entries and indexes. Two writers repeatedly publish the
same key while a reader verifies every visible cache response digest and
index. The targeted test passed, and the complete Stage-0 probe release unit
suite passed 23/23 tests. The check covers in-process threads; it is not a
multi-process stress run or a canonical Store concurrency test.

## Language fact reuse telemetry acceptance (2026-10-17)

The [semantic fact telemetry report](semantic-fact-telemetry-acceptance-20261017.json)
removes the Compiler incremental tests' fixed-zero assertion for
`reused_semantic_facts`. They still require the field to be an explicit list,
and record counts from that list rather than from cache-hit observations. Both
the multi-module incremental verifier and effectful digest incremental verifier
passed. The current runs returned zero facts, so Gate B remains partial. The
effectful digest verifier's refreshed report and both VM chunks were returned
by authenticated Environment `artifact_read`; this remains verifier output,
not a Forge execution receipt.

## Native Test provider folding in a selected-callable VM batch (2026-10-17)

The [provider fold report](native-test-provider-fold-batch-20261017.json)
compiles the production `mncs.test.provider::fold_selected` implementation
with its Test assertions/protocol and standard sequence/application imports.
Four selected roots exercise mixed returned, unsupported, budget-exhausted,
invalid-request, and assertion-failure cases. Their typed results match the
Stage-0 oracle in one canonical VM batch. A fresh process reuses the exact
artifact with zero frontend invocations. The cold build took 1.08 seconds,
the exact hit 0.14 seconds, and the VM batch 0.34 seconds at 32 MiB peak RSS.
This remains workload-specific evidence; Language verified-fact reuse is
still zero, and the report does not execute `provider_batch_app`, admit
`provider_digest`, invoke Forge, publish Store evidence, or establish full
provider/CLI conversion.

## Native Test provider application boundary (2026-10-17)

The [provider application boundary report](native-test-provider-app-boundary-20261017.json)
compiles the production `mncs.test.provider::provider_batch_app` with verified
dispatch for `provider_artifact` file operations and `provider_digest` hashing.
The VM runner now validates the selected canonical capability identity against
its own source alias while permitting other independently identified
capabilities in the same artifact. It rejects a mismatched identity and a
mismatched alias. The canonical VM then executes the application's native
invalid-argument branch with no capabilities admitted; its typed exit code 2
and message match the Stage-0 oracle, and no effects or output publication
occur. The cold application build took 1.03 seconds at 109 MiB peak RSS; the
VM call took 42 ms at 18 MiB peak RSS. Language verified facts remain unused.
This covers an application boundary and fail-closed dispatch only; successful
provider execution still needs the exact `structured_digest` provider and the
authorized Forge/Environment/Store route.

The check is registered as `mncs-compiler.native-test-provider-app-boundary`
in verification inventory revision 41, with the JSON proof exported as a
bounded Environment shareable artifact for fresh-process readback.

The follow-up [eight-case provider report](native-test-provider-app-boundary-eight-case-20261019.json)
executes all seven real `mncs-test` self-suite cases together with a case
compiled from a separate Test module in one canonical VM application call.
The provider returns seven passing assertions, including the typed skip, and
its result and check match Stage-0 exactly. Rebinding the cross-module row to
the other valid artifact identity returns typed `UNSUPPORTED` with failure
code 9001. The call took 81 ms at 34.3 MiB peak RSS. This still uses the local
bounded structured-file adapter; Forge invocation, canonical Store
publication, and fresh-process Store readback remain unproven.

The [retained-session follow-up](native-test-provider-app-retained-session-20261019.json)
uses one runner process, one artifact admission, and one retained VM session
for three isolated calls: valid, wrong artifact identity, then valid again.
All three match Stage-0, and the valid result identity is unchanged after the
typed rejection. The batch took 217 ms at 112.7 MiB peak RSS. This remains a
local VM runner proof; Forge application execution and application-result
Store publication are still unproven.

### Repeatable provider-boundary verification and Store readback (2026-10-20)

The provider-boundary harness previously stopped because its fixed repository
evidence path already existed. It now gives each checkout report a timestamped,
process-qualified immutable path while retaining the manifest’s exact
shareable filename inside each fresh Environment invocation artifact
directory. Two consecutive registered verifications passed without changing
or replacing the earlier report.

The [first run](native-test-provider-app-boundary-20261020T005912.107431Z-2414522.json)
and [second run](native-test-provider-app-boundary-20261020T005957.505338Z-2414811.json)
bind the same harness SHA-256
(`2eb9275cd9f410d9b2d396ed0347fe3df9293df0c119cd307c59ae7d94a1872e`) to
Compiler `25b46d1f0c233532e7d5187545688c1c4acadbe5`. The real seven-case
`mncs-test` self-suite matches Stage-0; its provider application call took
0.068 seconds at 33.5 MB peak RSS. An eight-case heterogeneous batch across two
valid Test artifacts matches Stage-0 in 0.073 seconds at 35.9 MB peak RSS. A
retained VM session admitted one artifact and completed valid,
mismatched-artifact, valid-again calls in 0.217 seconds at 117.4 MB peak RSS.

The second verifier bundle was read back through a fresh authenticated
Environment operation from canonical Store. Its 135,488-byte report matched
SHA-256 `481367ec39d6f4d85aa3a8c919d18b7c4dbf9331050629e3cd92f5123439684f`;
see the [Store readback record](native-test-provider-app-boundary-store-readback-20261020.json).
These executions use the bounded local canonical-VM adapter. They do not
establish Forge execution or application-result publication. Language verified
facts reused remain zero, and this does not advance compiler self-consumption.

## Counted-loop SSA instruction accounting (2026-10-18)

The [loop accounting report](self-host-flow-ssa-loop-accounting-delivery-20261018.json)
resolves the three-instruction delta in the selected counted-loop frontier.
Pinned Stage-0 emits six ordinary loop-bookkeeping instructions: bound and
zero initialization, the remaining-count guard, index materialization, and
the decrement constant and operation. MNCS carries the index directly and
verifies one dedicated init, guard, and advance instruction in the
preheader/header/backedge. Both representations have three non-loop
instructions, and the reported block and terminator counts match. This is an
accounted representation difference; it does not claim one-to-one instruction
parity or execution of the produced SSA function. The canonical VM executed
the source-to-Flow-to-SSA compiler application for both selected cases.

The project `match-ssa` regression also passed 24 requests, including rejection
of cross-loop guard reassociation and the existing malformed-SSA cases. The
report records a 68.03-second application build (cache hit), a 27.72-second
canonical VM batch, 2.72 GiB child peak RSS, and 378 MiB VM peak RSS. These
measurements are specific to this selected compiler workload. Language-owned
verified facts reused remains zero.

## Global parser header limit 1024 (historical, 2026-10-19)

The [focused global-header frontier](self-host-parser-header-global-fuel-1024-deaad92.json)
raises the MNCS global parser's fixed iteration limit from 256 to 1024. It accepts
130- and 400-segment module names, while a 600-segment name still returns the
explicit code-9 fuel-exhaustion result. Across 104 cases, Stage-0 checked results,
the reference interpreter, Cranelift, and canonical VM have identical semantic
digests. This focused run took 77.07 seconds overall, 76.16 seconds of child CPU,
683,672 KiB child peak RSS, and 76,660 KiB canonical-VM peak RSS; cgroup memory
measurements were unavailable. These figures describe this parser workload;
the current implementation is extended below.

The full [authenticated parser-module frontier](self-host-parser-module-canonical-vm-frontier-fuel-1024-b44ee57-20261019.json)
passed Environment verification on Compiler revision `b44ee579`. It directly
exercised all seven selected parser functions: 27 bounded cases, 108 global cases,
61 header-location cases, and 201 canonical VM calls. The run took 73.43 seconds
overall with 774,696 KiB child peak RSS and 76,508 KiB canonical-VM peak RSS;
cgroup measurements were unavailable. The fixed-size bounded parser test now
excludes inputs over its 1,024-byte argument capacity; the focused global-root
test covers the 600-segment rejection on valid page strides.

This advances the selected parser frontier. It does not prove full parser-module
succession, Stage-1/Stage-2 succession, or Language-owned verified-fact reuse.

## Global parser header fuel in bounded batches (2026-10-19)

The current [4096-token global-header frontier](self-host-parser-header-global-fuel-4096-20261019.json)
is tested at Compiler revision `3b2cbbee`. It processes four bounded batches of
1,024 significant-token steps, preserving explicit code-9 exhaustion beyond
that limit. The previously failing 600-segment header and a 900-segment header
now parse at strides 2, 7, 64, and 1,024. A 2,047-segment header remains a
deterministic code-9 failure at strides 7, 64, and 1,024. Long-header spans and
terminal state are checked against the generated source layout.

All 111 cases have identical semantic digests across the reference interpreter,
Cranelift, and canonical VM (`154d86c5…12d60894`). The frontier took 157.36
seconds overall, 155.78 child CPU seconds, and peaked at 563,404 KiB child RSS;
the canonical VM peaked at 76,856 KiB. Cgroup values were unavailable. This is a
selected parser helper frontier, not full parser-module verification or a general
performance claim. The authenticated [parser-module verifier](self-host-parser-module-canonical-vm-frontier-fuel-4096-3b2cbbe-20261019.json)
also passed on the same revision through the exact registered nested checkout:
27 bounded cases, 108 global cases, 61 header-location cases, and 201 canonical
VM calls. It took 74.03 seconds overall with 776,744 KiB child peak RSS and
76,764 KiB canonical-VM peak RSS; cgroup values were unavailable. Language
verified facts remain unreused, and Stage-1/Stage-2 succession remain unproven.

## Declaration proof harness integrity (2026-10-19)

The [harness integrity check](decl-proof-harness-integrity-20261019.json)
hardens the selected current-source declaration proof. Its source extractor
ignores line comments, nested block comments, and quoted literals; rejects
duplicate or incomplete definitions; and confirms the exact current
`source.span_valid_global` bytes. Evidence publication now writes and syncs a
temporary JSON file before atomically linking it into the immutable evidence
path. Existing paths are preserved, failed publication leaves no artifact,
and a two-writer race admits exactly one complete result.

Eight focused checks passed. The authenticated [declaration frontier rerun](self-host-decl-prove-unit-current-20261019T191720.375142Z-2291618.json)
also passed seven proof cases on a fresh canonical VM artifact, with the same
source function and semantic digest as the prior run. The hardened harness
does not change Compiler semantics or add Language verified-fact reuse; full
Source-module verification and compiler succession remain unproven.

## Source byte_at_global canonical VM frontier (2026-10-19)

The [current-source frontier](self-host-source-byte-at-global-canonical-vm-20261019T193345Z.json) executes the exact `source.byte_at_global<1024,1024>` body from Compiler revision `2b030b21` through a fresh canonical VM artifact. All 70 byte, EOF, zero-stride, empty-source, missing-page, and short-page cases match the Stage-0 reference and Cranelift. The semantic digest is `f606619d…fc2277da`.

The run took 2.95 seconds overall and 2.53 child CPU seconds; child peak RSS was 140,024 KiB and canonical-VM peak RSS was 44,312 KiB. Host available memory before the run was about 24.1 GB; cgroup readings were unavailable. This is a selected source helper frontier, not full Source-module proof or a general speed claim. Language-owned verified facts reused remains zero; Stage-1 and Stage-2 succession remain unproven.

The [current Compiler-head rerun](self-host-source-byte-at-global-canonical-vm-20261020T010428Z.json) revalidates the same selected source function at `d1ecdd1ad3a1022440a26538be54ca6c2f885849`. All 70 cases still match the Stage-0 reference and Cranelift with semantic digest `f606619d…fc2277da`. The frontier took 2.964 seconds overall, 2.539 child CPU seconds, and 139,764 KiB child peak RSS; canonical-VM peak RSS was 44,424 KiB. Cgroup readings were unavailable. The verifier published no Store artifact; the report is preserved in this commit. It reuses zero Language facts and does not prove the full Source module or compiler succession.

## Multi-input native Test inventory execution (2026-10-19)

The [multi-input inventory record](application-build-test-inventory-multi-input-20261019.json)
extends the Compiler's batch adapter to validate several typed `u64` inputs and
several bounded byte-sequence inputs whose lengths follow multiple Language
`Nat` parameters. The fixture specializes independent bounds 2 and 3. Wrong
argument count, wrong integer wire type, wrong specialization arity, and a
malformed byte sequence are rejected before VM dispatch. The selected calls,
including both repeated results, match Stage-0.

The same run rebuilt the real `mncs-test` self-suite from its pinned sources:
one frontend invocation emitted an artifact for seven inventory-selected tests;
one canonical-VM session ran two rounds (14 calls), with six passes and one
typed skip. Results match Stage-0 and the repeated round. The VM batch took
0.073 seconds at 22.2 MiB peak RSS; the build took 0.493 seconds at 0.47 CPU
seconds and 29.1 MiB peak RSS. The parameterized and generic fixture builds
used 0.184 and 0.219 seconds respectively, each with one frontend invocation.
Cgroup memory measurements were unavailable. These are workload-specific
samples, not general speed claims. The record reports zero Language verified
facts reused; this advances Compiler test-callable execution and adapter
validation, not Language incremental verification, Forge execution, or the
production Test runner migration.

## Current-head Source self-host frontier refresh (2026-10-19)

The [current self-host matrix](SELF-HOST-MATRIX.json) now records a rerun at
Compiler commit `8bde1f77`. The exact `source.byte_at_global<1024,1024>` body
again passed 70 reference-interpreter, Cranelift, and canonical-VM cases with
matching semantic digest `f606619d…fc2277da`. The [run record](self-host-source-byte-at-global-20261019T200453.513955Z-2313721.json)
took 2.95 seconds overall (2.53 child CPU seconds), with 136.4 MiB child peak
RSS and 43.3 MiB canonical-VM peak RSS. Available host memory was 22.7 GiB;
cgroup measurements were unavailable. All nine MNCS compiler-module source
hashes match the previous matrix revalidation; this refresh updates the
selected Source frontier and source identity, not full-module coverage.
Language verified facts reused remains zero, and Stage-1/Stage-2 succession
remain unproven.

## Signed integer native Test inputs (2026-10-19)

The [signed-input inventory report](application-build-test-inventory-signed-inputs-final-20261019.json)
extends typed Test calls to accept `i64` wire values with the exact signed
64-bit schema and range. Its three-input fixture binds two `u64` values and
`i64(-17)` from the Language-issued inventory signature, repeats the same
verified artifact twice in the canonical VM, and matches Stage-0. Wrong arity,
wrong `u64` and `i64` wire types, and an out-of-range signed value are rejected
before dispatch. The focused build used one frontend invocation and took
0.220 seconds at 15.9 MiB peak RSS; its two canonical-VM calls took 0.019
seconds at 20.6 MiB peak RSS. Cgroup measurements were unavailable.

The full inventory harness then passed every boolean proof check, including
cache isolation, fresh-process artifact reuse, generic two-`Nat` byte inputs,
failure isolation, interrupted-call recovery, and the real `mncs-test`
self-suite. One canonical-VM session ran seven inventory-selected tests twice
(14 calls); all results matched Stage-0, with six passes and one typed skip.
The real suite build took 0.586 seconds at 28.7 MiB peak RSS; its canonical-VM
batch took 0.075 seconds at 22.4 MiB peak RSS. Cgroup measurements were
unavailable. Language verified facts reused remains zero. The harness now
refuses to publish evidence when any boolean proof check is false.

## Boolean native Test input validation (2026-10-19)

The [Boolean-input inventory report](application-build-test-inventory-bool-signed-20261019.json)
extends the same Language-issued Test signature to accept an exact typed `bool`
wire value alongside two `u64` inputs and one signed `i64`. The four-input Test
runs twice on one verified canonical-VM artifact and matches Stage-0. A Boolean
whose wire value is integer `1` is rejected before VM dispatch. The build used
one frontend invocation and took 0.228 seconds at 16.0 MiB peak RSS; the two VM
calls took 0.017 seconds at 20.9 MiB peak RSS. Cgroup measurements were
unavailable.

All boolean proof checks in the full guarded inventory harness passed. Its
real `mncs-test` self-suite again ran seven selected tests twice (14 calls) with
Stage-0 agreement; the VM batch took 0.073 seconds at 22.7 MiB peak RSS. The
record reports zero Language verified facts reused, and the production Test
runner remains on its existing host orchestration.

## Standalone byte native Test input validation (2026-10-19)

The [primitive-input inventory report](application-build-test-inventory-primitive-wire-20261019.json)
adds exact `byte` wire validation with the range 0..255. Its five-input Test
fixture binds two `u64` values, `i64(-17)`, `true`, and `byte(255)` from the
Language inventory and returns the same typed result twice on one verified
artifact; both calls match Stage-0. Malformed Boolean data and out-of-range
byte, signed integer, and unsigned integer values are rejected before dispatch.
The build used one frontend invocation and took 0.232 seconds at 16.7 MiB peak
RSS; the two VM calls took 0.019 seconds at 21.0 MiB peak RSS. Cgroup memory
measurements were unavailable.

Every boolean check in the guarded full inventory harness passed. The real
seven-test `mncs-test` self-suite again completed 14 canonical-VM calls with
Stage-0 agreement, taking 0.074 seconds at 22.6 MiB peak RSS. Language verified
facts reused remains zero; production runner migration and Forge execution are
still separate frontiers.

## Project source-count closure fast path (2026-10-19)

The [current self-host matrix](SELF-HOST-MATRIX.json) records Compiler revision
**06bfbc9349096001874fe2408e2a149ad8f05e9c**. The Project import-closure
function now checks the selected source count and can return after a complete
first scan for small projects. The
[canonical-VM run record](project-import-closure-source-count-fast-path-20261019.json)
passed 29 cases against the Python oracle and locked Stage-0 behavior.

On the same four-source dense graph, the source-count path used 470 VM steps
versus 655 for a control that takes the second scan (28.24% fewer). The shared
25-case corpus increased from 1,887,794 to 1,888,230 steps (+436, 0.023%);
the 4096-link case increased by 22 steps, from 1,175,474 to 1,175,496. These
measurements show a targeted first-scan saving, not a general speedup. The
build took 0.153 seconds at 17.8 MiB peak RSS; the canonical-VM batch took
61.71 seconds at 37.0 MiB. Cgroup memory data was unavailable.

The Project target-dependency integration also passed: it selected one target
module among 20 unrelated record modules, matched Stage-0 for a transitive
nominal case, and rejected a fresh missing field with matching diagnostics.
Language verified facts reused remains zero; this improves a narrow Compiler
resolver path and does not establish Forge execution, production Test migration,
full Project-module proof, or succession.

## Source-count-bounded Project worklist seeding (2026-10-19)

Compiler revision 7cec1c1095a5307c1896c7dc9c858d40d9d271f8 now bounds worklist seeding to the selected
project source count. The [canonical-VM record](project-import-closure-worklist-seed-bound-20261019.json)
contains a paired deep eight-module graph: both paths reach the same eight
modules, while the bounded path uses 3,003 steps versus 4,138 for the 64-slot
control (27.4% fewer). The full 31-case corpus matched the Python oracle and
Stage-0.

The target-dependency integration also passed on this revision: with 20
unrelated record modules present, it lowered only the selected target module;
the transitive nominal case matched Stage-0, and a fresh missing-field case
retained the matching diagnostic span. The target probe took about 7.0 minutes;
periodic sampling observed at most 6.2 GiB RSS while at least 15.9 GiB remained
available. Cgroup measurements were unavailable.

The run's canonical-VM batch took 59.95 seconds at 37.3 MiB peak RSS. Stage-0
oracle work took 193.6 seconds and peaked at 1.9 GiB RSS. Language verified
facts reused remains zero; Forge-routed execution and Stage-1/Stage-2 succession
remain unproven.

## Project import-closure source domain (2026-10-19)

The [source-domain boundary record](project-import-closure-source-domain-boundary-20261019.json)
closes a correctness gap between Project's 64-slot storage capacity and the
selected project's actual `source_count`. The closure scan, adjacency builder,
and worklist expansion now reject edges whose source or target is outside that
project. The 33-case independent-oracle/Stage-0/canonical-VM corpus passes;
its paired four-source case excludes index 4 while the same edges in a
64-source control include it. The 21-source target integration lowered only
the target module, matched Stage-0 for transitive nominal verification, and
rejected the missing-field case with fresh span [108, 114]. This change makes
no general performance claim. Language verified facts remain unreused; Forge
execution and Stage-1/Stage-2 succession remain unproven.

## Project verification provenance (2026-10-20)

The Project verifier now emits the
mncs.compiler.project-verification-provenance/1 schema with the Compiler
revision and branch, hashes for all nine src/compiler/*.mncs files, the
Language lock and Stage-0 probe hashes, and the harness hash. The bound
resolution verifier passed 18 requests in retained Cranelift sessions; its
native callable identities matched the locked Stage-0 identities, and missing
imports, duplicate modules, missing imported members, and parse failures were
rejected or returned as expected. The full Project differential also passed at
the same Compiler source revision in 407.733 seconds.

The [provenance record](project-verification-provenance-20261020.json) binds
the verifier output to its exact inputs. The [full Project result](project-full-closure-current-20261020.json)
preserves the complete suite report. These checks exercise Project resolution
and SSA fixtures; they do not run the canonical VM, reuse Language verified
facts, or establish compiler succession.

## Project resolver resource observations (2026-10-20)

The [focused resolution report](project-resolution-resource-observation-20261020.json)
now retains the opt-in per-child process telemetry already collected by the
Project harness. The 18-request retained-Cranelift run matched the prior
deterministic result digest and locked Stage-0 callable identities. It took
67.087 seconds wall time and 66.230 seconds of Stage-0 child CPU. At 500 ms
sampling, the child reached 6,238,470,144 bytes sampled RSS and a kernel
VmHWM of 6,239,125,504 bytes; it peaked at four file descriptors and three
threads. Cgroup memory metrics were unavailable and remain unknown. The report
identifies the Compiler, nine MNCS source files, Language lock, Stage-0 binary,
and modified harness by revision or hash. It reports no Language verified
facts reused.

## Current Source byte_at_global frontier (2026-10-20)

The [current-head report](self-host-source-byte-at-global-20261020T013651.595702Z-2426923.json) binds the selected `source.byte_at_global<1024,1024>` execution to Compiler revision `4cf9d354c4349db0e486e7f7a4382a98cea88528`. All 70 bounded and malformed-layout cases agree across the reference interpreter, Cranelift, and a fresh canonical VM artifact. The frontier took 2.985 seconds, used 2.554 child CPU seconds and 140020 KiB child peak RSS; the canonical VM batch took 0.741 seconds at 44380 KiB peak RSS. Cgroup measurements were unavailable. Language verified facts remain unreused. The matrix is still partial: only this callable was rerun at the current head, and the Project source hash changed since the preceding partial revalidation. This does not prove the full Source module, Stage-1, or Stage-2 succession. Evidence SHA-256: `3d19a51b567a1b68d82de659215a0b04b88676d16f8e270a4eb5d32fe2471c48`.

## Multi-callable native Test provider build (2026-10-20)

The [provider boundary run](native-test-provider-app-boundary-20261020T014308.157199Z-2429143.json)
now selects `provider_batch_app` and `run_selected_batch` in one
`application-build-request/2`. Compiler verified both callable interfaces,
their capability identities, and one combined VM artifact in one frontend
invocation. The real seven-case `mncs-test` provider batch and the eight-case
heterogeneous-artifact batch both matched Stage-0; the three-call retained VM
session also reproduced valid results around a typed artifact-identity
rejection.

The [build comparison](native-test-provider-multicallable-build-comparison-20261020.json)
compares that 1.161-second build with two earlier runs that requested the
application and protocol interfaces separately. Their mean combined build
time was 1.622 seconds, so this workload measured a 28.41% wall-time reduction
and a 28.66% CPU-time reduction. The source snapshot and Stage-0/VM binaries
matched; host load and Compiler Git revisions differed. Current build peak RSS
was 61.1 MB and cgroup memory was unavailable. The real provider VM batch took
0.067 seconds at 32.1 MiB peak RSS. This improves native Test artifact
orchestration, but Language verified facts reused remains zero. Execution
still uses the local bounded provider adapter rather than Forge and canonical
Store, and compiler self-consumption did not advance.

## Current Source location and span frontier (2026-10-20)

The [current-head Source report](self-host-source-line-col-scalar-batch-current-head-20261020.json)
binds `line_col_global`, `line_col_span_global`, and `line_cols_global` to
Compiler revision `1c4f002629a73eadf692bcd828ba194a81c535d3` and Source hash
`927209afd171de7f2aac97d7b5a90c511f417d9837c845032a28de34dbdc3e22`. A
fresh run exposed that the older Compiler frontier harness still expected
byte-counted UTF-8 columns. `mncs-syntax::SourceSpan::at` uses byte offsets
with Unicode-scalar columns, which matches the MNCS Source implementation;
the harness oracle and wording now follow that contract.

The corrected frontier passed 178 location queries, 31 span queries, and 30
batches in each of the reference interpreter, Cranelift, and canonical VM. On
the VM, batching saved 27,099,821 steps (90.1%) over independent lookups, and
span mapping saved 1,640,735 steps (13.0%) over two lookups. The canonical VM
run took 55.323 seconds at 241,908 KiB peak RSS; reference and Cranelift child
CPU totaled 228.550 seconds. The Python harness peaked at 834,208 KiB RSS;
cgroup measurements were unavailable. This covers selected Source callables,
not the full Source module. Language verified facts reused remains zero, and
Stage-1/Stage-2 succession remains unproven. Evidence SHA-256:
`7794a02d386885bfd34f32a2e86b147d5e2683c3d78e9cf234448e835fde8880`.

## Fresh-process native Test provider cache reuse (2026-10-20)

The [provider cache run](native-test-provider-app-boundary-20261020T020218.167569Z-2435417.json)
reopens the Compiler probe after its cold multi-callable `/2` build and reuses
the same bounded application cache. The fresh process returned a cache hit
with zero frontend invocations, the same artifact identity, both original
callable interface identities, unchanged capability dispatch, and the same
source snapshot identity. The cold build measured 1.203 seconds wall, 1.15
seconds CPU, and 60.9 MB peak RSS; the warm process measured 0.127 seconds,
0.10 seconds CPU, and 25.4 MB peak RSS. Cgroup memory was unavailable. These
are workload-specific cache measurements, not Language verified-fact reuse.
The real seven-case `mncs-test` provider run again matched Stage-0. Execution
still uses the local provider adapter; Forge execution, canonical Store
publication/readback, and Compiler self-consumption remain separate frontiers.

## Language-owned verification fact v3 reuse (2026-10-20)

Language v3 facts now include a separate callable-implementation dependency in
addition to the callable interface. Editing a callee body invalidates its
direct callers while unrelated facts remain reusable. The [full Parser
dependency run](self-host-parser-module-fact-reuse-20261020T101638Z.json)
produced 108 facts, reused all 108 after a comment-only edit, and on a reachable
`finish` edit reverified `finish`, its generic specialization, and the two
dependent `header` bodies while reusing 104 facts. All four canonical VM
results matched Stage-0.

The [full Source, Lexer, and Segment run](self-host-full-module-fact-reuse-20261020T101606Z.json)
produced 103 facts, reused all 103 after a comment edit, and reverified the
changed UTF-8 helper plus its three direct callers while reusing 99 independent
facts. An imported interface edit reverified six dependent facts and reused
97. Canonical VM behavior matched Stage-0. The [Source slice
run](self-host-source-module-fact-reuse-20261020T101643Z.json) produced five
facts; its comment edit reused all five, and its isolated body edit reverified
one while reusing four.

Parser cold/comment/reachable-edit builds measured 7.124/5.077/6.936 seconds
wall time and up to 273 MB RSS. The complete Source/Lexer/Segment builds
measured 3.554/2.806/2.837 seconds and up to 156 MB RSS. The Source slice
measured 0.272/0.247/0.242 seconds and about 55 MB RSS. Cgroup memory readings
were unavailable. These are workload-specific measurements. The exact
evidence identities and reuse counts are recorded under
`current_campaign_language_verification_fact_v3_reuse_20261020` in
[`SELF-HOST-MATRIX.json`](SELF-HOST-MATRIX.json). This verifies actual
Language-owned body fact reuse; it does not establish full Compiler
self-hosting or Stage-1/Stage-2 succession.

## Native `mncs-test` incremental application with Language fact v3 (2026-10-20)

The [current incremental application record](native-app-readiness-gate-b-20261020T102934.660273Z-2764455.json)
revalidates the five-module, 24-function `tests.native_readiness::run`
application with an explicit harness assertion that Compiler consumed
`mncs.language-verification-fact/3`. The cold build produced 24 verified body
facts in 0.718 seconds (0.7 CPU seconds, 54.6 MB peak RSS); a fresh-process
warm build took 0.105 seconds with no frontend invocation. A fresh-process
comment edit reused all 24 facts. A dependency edit reused 21 and recomputed
the three affected facts; an unreachable body edit reused 23 and recomputed
one. Nominal, generic-specialization, effect, and capability edits reverified
their affected units while preserving independent facts. Invalid source
returned a fresh diagnostic, interrupted verification published no partial
artifact, and canonical VM behavior matched Stage-0. Cgroup memory was
unavailable. These are workload-specific measurements, not a general speedup
claim; full Compiler self-consumption and Stage-1/Stage-2 succession remain
unproven. The harness and evidence identities are recorded under
`current_campaign_native_application_fact_v3_incremental_revalidation_20261020`
in [`SELF-HOST-MATRIX.json`](SELF-HOST-MATRIX.json).

## Effectful `mncs.test.digest::digest_app` with Language fact v3 (2026-10-20)

The [current digest application record](native-app-readiness-test-digest-app-fact-v3-20261020.json)
rebuilds the real effectful Test application under Language revision
`f03bdc1` and checks the exact `test_artifact` capability identity and runtime
alias against the admitted VM artifact. Cold compilation produced 30 Language
v3 body facts in 0.865 seconds (0.83 CPU seconds, 54.6 MB peak RSS). The
comment-only imported Test edit reran the frontend, reused all 30 facts, and
retained the artifact; a fresh-process warm build used no frontend. Three
typed success cases matched Stage-0. Seven calls ran in one retained VM
session, including missing capability, invalid input, write failure, wrong
artifact and budget exhaustion cases. The canonical VM used the bounded local
file adapter; Forge execution and its receipt remain unproven. The current
campaign also lacks a Control-approved `mncs-forge` scope grant, so no Forge
claim or execution request was attempted. Exact identities and limitations
are recorded under
`current_campaign_effectful_digest_app_language_fact_v3_20261020` in
[`SELF-HOST-MATRIX.json`](SELF-HOST-MATRIX.json).
