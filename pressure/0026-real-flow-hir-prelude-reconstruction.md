# Real-Flow HIR prelude reconstruction cost

ID: CP-0026

Status: Open; measured boundary narrowed

Category: compiler architecture / model-pipeline cost

Severity: high

Frequency: recurring on a cold compiler-sized backend build

## Title

The compiler-sized `Program::lower_to_ir` prelude has not completed within the
bounded resource envelope for the real Flow target, before HIR timing begins.

## Compiler workload

The real target is
`mncs.compiler.project.v1::compile_project_target<1024,1024>`, loaded from
the selected `source`, `lexer`, `parser`, `segment`, `decl`, and `flow`
compiler modules. The matched probe uses Stage-0 `b05dfa2b`, Profile 0.18,
research-bytecode, a cached frontend Program, and an 8,000,000-step request
budget. The target request is sent, but the MNCS function body entry and its
step count remain unknown because backend preparation did not return.

The six target-input modules account for 892 pages at width 1024 in this
probe. The existing 894-page accepted project-closure evidence, including
1,324 linked functions and 376 Stage-0 SSA functions, remains a separate
historical scope in `SELF-HOST-MATRIX.json` and is unchanged.

## Reproduction

From the compiler repository, the bounded matched run is represented by:

```sh
python3 tools/measure_compiler_probe.py \
  --backend research-bytecode \
  --modules source lexer parser segment decl flow \
  --target-last --step-budget 8000000 \
  --max-probe-rss-mib 1600 --timeout-seconds 120 \
  --label flow-backend-artifact-only-pinned-20261009
```

The exact source, Stage-0, cache, binary, and phase identities are recorded in
[`campaign-20261009-flow-resource-profile.json`](../evidence/campaign-20261009-flow-resource-profile.json).
The matching baseline differs only in requested probe artifact emissions.

## Observed behavior

- Both measured runs loaded the same content-addressed frontend Program
  (`4ecbeacb…`) and began backend request construction at about 2.26 seconds.
- Baseline requested Semantic, HIR, SSA, TargetLoweringPlan, and BackendArtifact
  emissions. Candidate requested BackendArtifact only.
- Both runs emitted `compiler-semantic` and `compiler-validation` timing events,
  then reached the 1,600 MiB sampled RSS stop at about 31 seconds. Peak sampled
  RSS was 1,708,220 KiB and 1,708,208 KiB; child CPU was 30.74 and 30.67
  seconds; peak FDs were five in each run.
- The only measured deltas were −0.252 seconds wall time, −0.07 seconds CPU,
  −12 KiB peak RSS, no FD change, and +205 bytes of logical reads. Emission
  selection therefore did not materially reduce work.
- No `ir-prelude`, `compiler-hir`, `compiler-ssa`, `compiler-backend`, or final
  timing event was observed before the stop. No semantic result was returned.
- The runner revalidated the isolated Rust child identity before sending
  SIGINT. The sampled cap is an orderly probe boundary, not a process or cgroup
  memory limit. Cgroup memory was unlimited and per-run OOM event deltas were
  zero.

Source inspection shows `ReferenceCompiler::compile_inner` validates the
Program, then calls `Program::lower_to_ir`. That method validates again and
builds the semantic graph, evidence manifest, and obligations before emitting
`ir-prelude`. This makes the prelude the next profiling boundary; it does not
yet establish which operation dominates or how much memory each contributes.

## Smallest faithful reproduction

The real Flow project target is currently the smallest faithful reproduction.
No reduced MNCS fixture has isolated this compiler-model cost. Warm frontend
cache preparation is a useful bounded control, but it does not enter backend
compilation or exercise the prelude.

## Current workaround and limitation

The backend-independent frontend Program cache removes repeated cold
elaboration for an exact compiler/source/seed identity: the cold preparation
took 150.553 seconds of specialization and peaked at 2,129,132 KiB; the warm
cache-only preparation took 1.004 seconds and peaked at 301,256 KiB. The warm
operation is admission-only and does not run the target. Once backend
preparation begins, the resource profile still reaches the same cap.

## Desired behavior

Complete the HIR prelude for the real target within a measured finite envelope
while preserving semantic graph identities, evidence-manifest fingerprints,
obligations, diagnostics, and downstream HIR/SSA results. Reuse a validated
Program result and shared semantic identities if profiling confirms those
reconstructions are material; add phase timings before choosing a larger
resource bound.

## Likely ownership

`mncs-language` compiler/model pipeline. This is a compiler implementation and
reuse question, not evidence for a missing language feature.

## Impact

- Compiler succession: blocks a complete native check/proof/CFG/verified-SSA
  result for the real Flow module closure; no semantic rejection has been
  demonstrated.
- Execution: backend artifact preparation does not complete within the
  measured cap, so no executable artifact or canonical VM conformance is
  established.
- Resource attribution: observed child RSS/CPU/FD/I/O and cgroup events are
  available; exact internal prelude phase attribution remains unknown.
- Correctness: Stage-0 project acceptance remains independently authoritative
  for its supported subset; this finding adds no semantic verdict.

## Evidence and upstream tracking

- [`evidence/campaign-20261009-flow-resource-profile.json`](../evidence/campaign-20261009-flow-resource-profile.json)
- `mncs-language` issue/PR: not opened yet
- Resolution revision: pending
- Follow-up evidence: pending an instrumented `mncs-language` change and a
  same-input real Flow remeasurement
