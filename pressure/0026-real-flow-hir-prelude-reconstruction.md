# Real-Flow compiler HIR preparation and backend admission

ID: CP-0026

Status: Open; measured HIR reconstruction bottlenecks reduced; retained-session admission remains incomplete

Category: compiler architecture / model-pipeline cost

Severity: high

Frequency: recurring on a cold compiler-sized backend build

## Title

The bounded real-Flow compiler request reaches Stage-0 HIR and SSA preparation,
then is interrupted during backend session admission without returning a
compiler result.

## Compiler workload

The target operation is
`mncs.compiler.project.v1::compile_project_target<1024,1024>`, loaded from the
selected `source`, `lexer`, `parser`, `segment`, `decl`, and `flow` compiler
modules. The matched probe uses Stage-0 Profile 0.18, `research-bytecode`, a
frontend Program cache prepared for each exact Stage-0 identity, an
8,000,000-step request budget, a 600-second timeout, and a 2,560 MiB sampled
child RSS stop.

The six target-input modules account for 892 pages at width 1024 in this
probe. The existing 894-page accepted project-closure evidence, including
1,324 linked functions and 376 Stage-0 SSA functions, remains a separate
historical scope in `SELF-HOST-MATRIX.json` and is unchanged.

The `compiler-hir` and `compiler-ssa` timings below are Stage-0 host
preparation of the MNCS compiler Program. They do not establish that the MNCS
compiler accepted `flow.mncs`, produced verified SSA for that target, executed
target VM steps, or emitted an executable artifact.

## Matched measurement

The exact source, input, backend, resource, phase, and raw evidence identities
are in [`campaign-20261009-flow-hir-prelude-profile.json`](../evidence/campaign-20261009-flow-hir-prelude-profile.json).
The compiler input source SHA-256 is
`7a89b18b4bd00e871faa92ddf7923cd8e8b40a9999c8057c33a5a654efd505d6` and the
probe candidate head is `76ccb18af4e1da9a849f421960ecb9634ca2409d`. Stage-0
identities are explicitly distinguished from that compiler revision.

| Stage-0 source | HIR prelude | Semantic graph | Host compiler HIR | Host compiler SSA | Wall to RSS stop | Child CPU | Peak RSS |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| pristine `b05dfa2b` | 31.196 s | not separately timed | 91.290 s | 103.475 s | 122.489 s | 121.69 s | 2,627,448 KiB |
| validated HIR reuse, patch `eefce44c` | 22.093 s | 18.862 s | 81.244 s | 93.805 s | 113.476 s | 112.70 s | 2,628,032 KiB |
| indexed graph and HIR, patch `357a3141` | 4.060 s | 0.775 s | 63.052 s | 75.411 s | 95.170 s | 94.20 s | 2,649,160 KiB |

Across the baseline-to-V2 pair, host HIR preparation fell by 28.238 seconds
(30.9%), host HIR-plus-SSA preparation by 28.064 seconds (27.1%), and wall and
child CPU to the resource stop by about 22%. Peak RSS remained near 2.5 GiB
and was 21,712 KiB higher in V2 than baseline; peak FDs remained five.

The indexed semantic graph fell from 18.862 seconds in V1 to 0.775 seconds in
V2. The compiler and model now reuse a single canonical form and validated
Program for HIR construction, then use function-identity maps instead of
repeated full function-table scans in graph construction and HIR call lowering.
Differential tests preserve the public identities, graph, evidence manifest,
HIR, fingerprints, invalid reports, and exact callee identity behavior.

All three target attempts were classified `RESOURCE_EXHAUSTED` by the bounded
sampled child RSS runner and ended `INTERRUPTED` with semantic result
`UNKNOWN_NO_RETURNED_PROJECT_RESULT`. V2 reached the observed
`backend_compilation` begin event, but its transport then stopped at
`retained_session_admission`; backend admission was not observed and target VM
steps remain UNKNOWN. The cgroup memory maximum was unlimited and event deltas,
including `oom` and `oom_kill`, were zero. This is not evidence of a kernel OOM
or a semantic rejection.

## Phase-correlated RSS repeat

The matched V2 request was repeated with probe measurement schema 3, which
retains the actual compiler child’s RSS, HWM, CPU, FD, and I/O samples beside
its phase trace. The 377 samples span 94.5 seconds at 250 ms intervals. The
nearest sample to each mapped compiler event was within 123 ms.

| Host Stage-0 milestone | Probe elapsed | Sampled child RSS |
| --- | ---: | ---: |
| `backend_compilation` begins | 2.271 s | 1,323,596 KiB |
| `compiler-hir` timing event | about 65.270 s | 1,627,860 KiB |
| `compiler-ssa` timing event | about 77.495 s | 2,049,960 KiB |
| 2,560 MiB runner stop | 94.512 s | 2,630,320 KiB |

The child used 94.0 CPU seconds, peaked at five FDs, and performed no
kernel-accounted storage reads. From the sample nearest the host
`compiler-ssa` event to the RSS stop, sampled RSS rose by 580,360 KiB
(566.8 MiB). This localizes additional growth to the remainder of the
`research-bytecode` backend-compilation/admission path, but it does not name an
allocator or data structure. It is not a Cranelift observation. The request
again stopped at retained-session admission without a compiler result; cgroup
OOM and `oom_kill` event deltas remained zero.

A separate run that paired the V2 binary with a different worktree’s pristine
identity metadata missed the candidate cache and re-elaborated the frontend.
It is excluded from matched comparisons. The exact matched trace, the new
collector source hash, and raw measurement hashes are retained in
[`campaign-20261009-flow-phase-resource-samples.json`](../evidence/campaign-20261009-flow-phase-resource-samples.json).

## Earlier bounded observation

The earlier 1,600 MiB paired emission experiment remains recorded in
[`campaign-20261009-flow-resource-profile.json`](../evidence/campaign-20261009-flow-resource-profile.json).
It stopped before HIR timing began and showed no material emission-selection
gain. The new 2,560 MiB measurement uses a later phase-instrumented path and
reaches host HIR/SSA preparation; neither observation is rewritten as a
successful target compilation.

## Smallest faithful reproduction

The real Flow compiler project target is still the smallest faithful
reproduction. The frontend cache preparation is an admission-only control: it
runs Stage-0 parsing/elaboration but does not enter backend compilation or run
the compiler function.

## Remaining boundary and desired behavior

The repeated semantic identity and callee lookup scans measured in the HIR
path have been reduced. The remaining boundary is backend compilation and
retained-session admission under the bounded RSS envelope. Complete a matched
run that admits the session and returns the `compile_project_target` result,
then establish the target's exact parse/check/proof/CFG/verified-SSA facts
before making executable or Stage-1 claims. Preserve exact diagnostics,
resource classifications, and Stage-0 differential authority.

No language syntax or semantic change was needed. This is an owner-local
`mncs-language` compiler/model implementation and `mncs-compiler` measurement
question.

## Impact

- Compiler succession: no new target semantic result, verified target SSA,
  executable artifact, Stage-1, or Stage-2 succession is established.
- Execution efficiency: Stage-0 host HIR/SSA preparation is materially faster
  on the exact measured source, while peak RSS and retained-session admission
  remain limiting.
- Resource attribution: child CPU/RSS/FD/I/O and cgroup event deltas are
  recorded; hardware instructions, target VM steps, and per-function CPU remain
  UNKNOWN.
- Correctness: Stage-0 project acceptance remains independently authoritative
  for its supported subset. This finding adds neither a semantic pass nor a
  semantic failure for the real target.

## Evidence and upstream tracking

- [`evidence/campaign-20261009-flow-hir-prelude-profile.json`](../evidence/campaign-20261009-flow-hir-prelude-profile.json)
- [`evidence/campaign-20261009-flow-resource-profile.json`](../evidence/campaign-20261009-flow-resource-profile.json)
- `mncs-language` source change commit: `02aee19774b1bc88aa3ffd329cd0a90dd938a82f`
- Delivered `mncs-language` main: `3b9690c4c872b84cfdb11da45a8d532c34f1ee93`
- Tests: `cargo test -p mncs-model` (215 passed) and `cargo test -p mncs-compiler -- --test-threads=4` (120 passed across targets)
- Follow-up: instrument the measured backend compilation/admission boundary
  and reduce its memory/work cost without raising the budget to hide the
  bottleneck.

## Follow-up on delivered compiler main (2026-10-10)

A separate bounded observation used delivered compiler main `aca7f5b` and
source-content identity `d1f29a87b7889342f74e6e9455967884faefe52ee1a31c115adc14af3cd55318`
for the six selected target modules (900 pages at width 1024). The Stage-0
lock remained `b05dfa2b`; the tested compiler head and source-content identity
are recorded separately in
[`campaign-20261010-flow-execution-and-phase-stop.json`](../evidence/campaign-20261010-flow-execution-and-phase-stop.json).

The warm reference-interpreter request exhausted its unchanged 200,000-step
budget after 29.726 seconds of target execution. It returned no project result,
so semantic acceptance remains UNKNOWN. The actual probe child used 30.62 CPU
seconds, peaked at 1,759,260 KiB RSS and five FDs, and did not reach the
2,300 MiB runner stop.

The research-bytecode request used the same compiler source identity and a
200,000-step budget, but its frontend Program cache was cold. Stage-0
frontend elaboration took 154.230 seconds and produced a 3,135-function
Program. The trace then entered backend compilation. The process reached the
2,300 MiB sampled RSS stop 104.541 seconds after that phase began, at
2,435,696 KiB RSS and 259.34 CPU seconds. The 0.5-second sampler overshot the
stop threshold by 80,496 KiB. The runner sent SIGINT to the revalidated
isolated child; no cgroup OOM or `oom_kill` event occurred. Backend session
admission and target execution were not reached, and the semantic result is
UNKNOWN. This pair does not isolate an allocator or backend data structure.

`measure_compiler_probe.py` now emits schema 4 with an
`incomplete_phases_at_stop` field. It matches phase-begin and phase-completion
events, then reports any active phase alongside the last sampled child time.
The raw research-bytecode measurement remains schema 3; its unmatched
`backend_compilation` phase was reconstructed from the hash-verified trace
using the new helper. A subsequent normal bounded reference-interpreter run
emitted schema 4 with an empty incomplete-phase list.

A same-operation eight-byte lexer-window experiment passed the focused
scanner/Stage-0 token and span differential, but two warm target measurements
were slower than the restored baseline. The source change was reverted; it
does not promote the lexer or flow module evidence.

## Follow-up: 500k reference profile and declaration dispatch

At the same six-module closure and reference-interpreter backend, a 500,000-step
baseline reached the fixed step budget after 79.528 seconds in the target
request. It used 78.77 CPU seconds, peaked at 1,759,140 KiB RSS, and held five
FDs. The sampled 2,300 MiB stop did not trigger; cgroup memory event deltas,
including `oom` and `oom_kill`, were zero. The request returned no project
result, so its semantic outcome remains UNKNOWN.

The function profile located the largest measured cost in
`lexer.next_token_global`: 515 calls, 112,116 VM steps, and 35.557 seconds
exclusive time (48.862 seconds inclusive). `lexer.significant_global` ran 186
times for 5.268 seconds exclusive. `segment.significant_global` forwarded the
same 186 calls and accounted for 3.478 seconds exclusive. `scan_chunk_window`
ran 2,159 times for 4.143 seconds exclusive; `source.byte_window4_page` ran
2,674 times for 1.258 seconds. This profile identifies token scanning and
interpreter dispatch as measured costs; it does not establish a particular
allocator or native backend cause.

A candidate changed the 89 declaration-parser calls from
`segment.significant_global` to `lexer.significant_global`, keeping the
`segment` import for remaining byte access. The paired profile target time was
78.040 seconds, 3.3% below the profiled baseline; the segment forwarding entry
disappeared, while lexer token-call count and steps stayed unchanged. Two warm
unprofiled candidate target times were 79.619 and 77.529 seconds, against one
79.528-second unprofiled baseline. The 78.574-second candidate mean is only
1.2% lower and is not repeatable evidence with these sample counts. The direct
call source was restored.

On the same candidate source identity, the focused
`test_decl_provider_body_retention.py` contract passed against pinned Stage-0
`b05dfa2b`: full and signature-only parse agreed on function signature and
span, and malformed provider-body rejection matched the Stage-0 diagnostic
span. The bounded real-flow request still exhausted its 500,000-step budget
without returning a project result. No module-stage, executable, Stage-1, or
Stage-2 claim is added. Compact metrics and raw measurement/trace hashes are in
[`campaign-20261010-decl-lexer-dispatch-profile.json`](../evidence/campaign-20261010-decl-lexer-dispatch-profile.json).
