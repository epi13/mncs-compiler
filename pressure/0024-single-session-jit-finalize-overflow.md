# Single-Session Cranelift JIT Finalize Overflow

ID: CP-0024

Status: open

Category: backend

Severity: blocking

Frequency: pervasive

## Title

The project execution session no longer survives Cranelift JIT
finalization: `finalize_definitions` panics with
`TryFromIntError(NegOverflow)` on an X86 PC-relative relocation, so
`retained_sessions` drops to 0 and every project-mode suite fails.

## Compiler workload

`tools/test_project.py` (all modes) and `tools/test_cp0001.py` tiers BCD
build one retained Cranelift session for `mncs.compiler.project.v1`,
which monomorphizes essentially the whole compiler (project + ssa +
flow + decl + leaf modules) for a single `(1024, 1024)` seed. The
2026-10-03 baseline retained this session at ~4.5 GB HWM; the generic
declaration-surface growth in this campaign (~7% more `decl` source,
plus one record field each in `Sig`/`SemState`/`StmtWalk`) deterministically
tips finalization over the 2 GB PC-relative relocation range.

## Minimal MNCS reproduction

No reduced MNCS input: the failure is capacity/layout, not source shape.
Reproducer is the session build itself:

```
MNCS_PROBE_MODULES=source,lexer,parser,segment,decl,flow,ssa,project \
MNCS_PROBE_EXECUTION_MODULES=mncs.compiler.project.v1 \
MNCS_PROBE_BACKEND=cranelift \
MNCS_PROBE_GENERIC_SEEDS='[{"module":"mncs.compiler.project.v1", \
  "function":"compile_project","type_arguments":[{"kind":"nat","value":1024}, \
  {"kind":"nat","value":1024}]}]' \
  .bootstrap/target/release/mncs-compiler-stage0-probe
{"execution_status": true}
```

Observed: `thread 'mncs-cranelift-session' panicked at
cranelift-jit-0.121.2/src/compiled_blob.rs:56:80:
i32::try_from((what as isize) - (at as isize))` with `NegOverflow`.

## Current behavior

- Pristine `main` (`8a7ced7`): project session retains (1/1).
- Campaign tree: project session panics deterministically (3/3 runs);
  `decl.v1`, `flow.v1`, and `ssa.v1` single sessions still retain.
- The pinned backend configures the JIT with `is_pic=false`
  (`cranelift_backend.rs` `host_isa`), so all code/data references use
  range-limited absolute and PC-relative relocations.
- `cranelift-jit 0.121.2` cannot serve PIC/GOT relocations (explicit
  panic arms), so flipping `is_pic` is not a fix at this pin.

## Current workaround

Run project-mode suites with `MNCS_PROBE_BACKEND=reference_interpreter`
(suite support exists via the backend-var pop in each `Probe`), and
verify `decl`/`flow`/`ssa` sessions on Cranelift. This keeps semantic
verification moving but surrenders executable-backend coverage for the
project pipeline and runs 5-10x slower under per-request step budgets
that large modes may exhaust.

## Why the workaround is insufficient

- The reference interpreter shares no code with the executable path;
  JIT-only defects (calling convention, value layout, relocation) go
  uncovered for the largest session.
- The 8M-step request cap already exhausts on some reference workloads,
  so full-closure verification may be unreachable, not merely slow.
- Any further compiler growth re-trips the threshold on more sessions;
  the `decl` and `ssa` sessions have unknown remaining margin.

## Desired behavior

A retained executable session for the whole-compiler reachable graph
survives finalization with margin for ordinary campaign growth —
whether via colocated/small-model blob layout, PIC/GOT-capable JIT
memory management, reachable-only codegen pruning, or measured image
reduction that keeps every current seed and assertion intact.

## Likely ownership

backend

## Impact

- Correctness: executable-backend project verification blocked; no
  wrong-code evidence (elaborated artifacts stay valid; reference
  execution agrees where it runs).
- Safety: none (build-time panic, no corrupt output).
- Runtime performance: n/a.
- Compiler performance: project suites forced onto the slower backend.
- Memory: per-probe HWM already 3.4-4.5 GB before this growth.
- Determinism: panic is deterministic (3/3), not a flake.
- Implementation complexity: backend-owned; compiler cannot shrink
  the reachable graph without dropping tested seeds.

## Evidence / reproduction

- `tools/test_project.py`: `Probe` docstring records the predecessor
  pressure `prs_4606a5e9b5d87684` (dual-session finalize NegOverflow)
  and the split-process workaround; this finding is its single-session
  escalation.
- Perf baseline: `evidence/campaign-20261003-perf.json` memory section
  (`cranelift_project` HWM 4554 MB, `cranelift_ssa` 3433 MB).
- A/B: pristine `main` retains 2/2; campaign tree panics 6/6 with
  identical seeds and probe binary (failed builds measure an
  identical 3655 MB VM each time — deterministic, not ASLR luck).
  The smaller `flow` session is marginal (1 retain, 1 panic in 2
  runs); `decl` and `ssa` sessions retain reliably.
- Session maps (pristine, post-build): ~4 GB total VM, largest regions
  heap/anon data (1.8 GB + 1.6 GB); no single huge executable mapping,
  consistent with scattered-blob PC-relative overflow rather than one
  oversized function.
- Growth is proportional, not pathological: the `decl.v1` single session
  (retains on both trees) measures 2144 MB pristine vs 2279 MB campaign
  (+135 MB, +6.3%) for ~7% more `decl` source. No single new construct
  dominates; the project session's layout simply had under 6% margin.

## Upstream tracking

- `mncs-language` issue/PR:
- Resolution revision:
- Follow-up evidence in this repository:

## 2026-10-04 backend campaign findings (still open)

- Scope widened: the `flow.v1` session now fails retention 2/2 with
  the identical contained `mncs-cranelift-session` worker panic
  (`compiled_blob.rs:56` NegOverflow); previously marginal 1/2.
  `decl`/`ssa` still retain. Growth keeps pushing sessions over.
- Mechanism, fully characterized: JIT finalize panics on the
  dedicated worker thread inside `prepare_stateful_session`
  (`JitSession::new` → `finalize_definitions`). The panic is
  contained — the probe survives and reports `retained_sessions:
  0` — but every one-shot execution attempt re-enters JIT finalize
  on the serving thread (`execute_backend` → `jit_scalar` →
  `JitSession::new`) and panics again. Retention failure is
  therefore total execution failure on Cranelift for affected
  modules, not a graceful fallback.
- Peak RSS does not discriminate: decl 3.2 GB retains, flow 3.1 GB
  panics. The discriminator is JIT image layout (PC-relative
  relocation distances), not process size.
- Mitigation (this campaign): B-class suites migrate to the
  research-bytecode retained backend (flow passes there 2:50 /
  2.5 GB; decl passes 4:56 / 2.4 GB with identical semantic
  results). This keeps verification green but does not fix the
  defect and is not claimed as a fix.
- The project suite cannot follow: interpreted step budgets
  (pin-maximum 8M) exhaust on `compile_project`, while native
  reports 1 step/request. Project execution needs a retained
  native session that currently cannot finalize — the defect
  remains load-bearing.
- A real fix (PIC-capable JIT memory management, blob layout
  colocation, cranelift dep upgrade, reachable-only pruning) lives
  in `mncs-language` main and cannot reach the execution pin
  without a repin. Not attempted in this campaign; tracked here.

## 2026-10-05 VM-architecture campaign findings (still open)

- `flow.v1` retention is layout-marginal, not deterministically
  failed: two cold `execution_status` probes (fresh cache dirs,
  `lower_unit` `(1024, 1024)` seed, `ready_s` 170/168) both
  retained 1/1 this run, vs 0/2 on 2026-10-04. Margin remains
  ~zero; any growth re-trips it.
- `decl.v1` still retains reliably (multiple 1/1, including the
  three-executor VM proof runs). Project session not re-run: the
  deterministic single-session panic stands unaddressed and nothing
  in this campaign touches JIT layout inputs.
- Cranelift native coverage is preserved and explicit: `decl` and
  `segment` VM drivers execute the policy backend (Cranelift) as
  the third executor with digest agreement; no suite was silently
  routed away from native.
