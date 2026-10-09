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
