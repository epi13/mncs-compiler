# Backend-neutrality audit and execution-cost campaign (2026-10-04)

Session: `ses_f4f800e6cefd6628` (consumer `spark-backend-vm-20261004`).
Checkouts: `mncs-compiler@c67b403`, `mncs-language@174c4b4`, `mncs-vm@6cfc5af`
(branch `spark/backend-vm-20261004`), pinned Stage-0 `a3ac17df`.

Goal: intentional backend choice — Cranelift where native matters, the
canonical VM (and the cheap retained backends beneath it) where it does
not — and structurally lower cost per unit of compiler work.

## Phase 1 — measured baseline (clean `.build`, Cranelift unless noted)

| suite | wall | peak RSS | `.build` growth | result |
|---|---|---|---|---|
| decl | 5:01 | 3.2 GB | +792 B / 2 files | PASS |
| decl, research-bytecode | 4:56 | 2.4 GB | +12 B | PASS |
| decl, portable-wasm | 1:58 | 1.4 GB | +0 | FAIL: host stack overflow on first execution |
| sem | 4:43 | 3.2 GB | +24 KB / 1 file | PASS |
| flow | 2:33 | 3.1 GB | +0 | FAIL: retained 0 (CP-0024 worker panic, contained) |
| flow retry | 2:38 | 3.1 GB | +0 | FAIL, same signature |
| flow, research-bytecode | 2:50 | 2.5 GB | +2 KB / 1 file | PASS |
| imported-nominal SSA chain | 12:50 | 4.0 GB | +4.3 MB / 7 files | PASS |
| project resolution, bytecode | 4:42 | 3.7 GB | +0 | FAIL: step budget exhausted at 8M |

Readiness split (decl closure): interpreter-only 64 s (frontend),
Cranelift 143 s, bytecode 136 s. Backend lowering is ~75 s; the
frontend is ~64 s. Every suite runs twice per invocation
(`first, second = suite(), suite()`), so one `test_decl.py` run pays
~280 s of readiness for ~20 s of requests: **readiness is ~93% of
wall time**.

Step semantics differ per executor: Cranelift reports 1 step per
request (decl total 23); bytecode counts real SSA steps (decl total
13,889,717). Step budgets are meaningless on native and binding on
interpreters; `compile_project` cannot fit the pin-maximum 8M
interpreted steps. This is workload/budget mismatch, not a backend
defect, and it keeps the project suite native-bound (see CP-0024).

Disk (main-checkout `.build`, 5.7 GB total, vs ~1.7 MB committed
evidence): `stage0/<hash>/target/debug` 4.4 GB (incremental 2.2 GB +
deps 2.1 GB + build 193 MB, no release), `elab/` 898 MB (7 modules ×
one reproducible `mncs compile` input+output pair each — proven by
fingerprint match between `semantic.json` and the request input),
compile-cost study ~408 MB, campaign facts 13 MB. Per-run suite
growth is kilobytes; the gigabytes are rebuild caches and ad-hoc
emission dumps, not evidence.

## Phase 2 — backend-neutrality audit

The stage0 probe is already backend-parameterized: `MNCS_PROBE_BACKEND`
passes straight into `request_for_program_with_backend`, and
`OwnedExecutionSession` retains research-bytecode, portable-WASM, and
Cranelift sessions behind one `reused()` flag. No second backend
architecture is needed.

Classification of current `cranelift` uses in compiler tests:

- **A. Truly native: none found in the suites.** No ABI, calling
  convention, relocation, or layout assertions exist. (The C11 backend
  canary is target-specific by construction and stays explicit.)
- **B. Needs an executor, not native code: everything else.**
  decl/sem/flow/frontend/segment/cp0001/profile-surface/project/
  imported-nominal all execute through the retained probe session.
  Proven portable so far: decl + flow on research-bytecode with
  identical semantic results; project is budget-bound to native
  (see above).
- **C. Backend-independent: oracle/parse transports** (`oracle`,
  `elaborate`, `ssa`, `project_oracle` probe paths) never touch a
  backend session, but they ride a probe process that still pays
  full readiness. True C-class work (bare `mncs abi`/`validate`
  invocations) needs no probe at all.
- **D. Cross-backend conformance: ad hoc.** Cranelift/reference twins
  exist as separate runs; the C11 canary compares against the
  oracle. No routine VM/Cranelift/WASM matrix.

Accidental Cranelift coupling removed or classified:

1. Backend defaults now resolve through `tools/backend_policy.py`
   (explicit `MNCS_PROBE_BACKEND` always wins). Migration proceeds
   per-suite as verified: flow flipped to bytecode (Cranelift is
   broken there, bytecode passes); decl verified identical on both
   but kept native as the canary; sem measured execution-dominated
   on bytecode (16:37 vs 4:43 native) and kept native. The rule:
   flip readiness-dominated suites, keep execution-dominated ones
   native until the interpreter is faster.
2. `'execution_mode': 'retained_cranelift' if retained else
   'reference_interpreter'` (7 sites) mislabeled every non-Cranelift
   run. Now reports the actual retained backend
   (`retained_<backend>`); `retained_cranelift` is unchanged for
   Cranelift runs.
3. `result_sha256` digests covered backend-coupled fields (step
   counts, artifact identities, backend names), making cross-backend
   comparison impossible by construction. Digests are now
   semantic-only (`[request, returned]`, plus status/failure where
   the suite asserts negatives); steps/identities remain recorded
   as telemetry. Historical digests change accordingly. Proven:
   `test_decl` on Cranelift and research-bytecode produce the
   identical digest `bbf91e03…` — the first checkable
   cross-backend equality in the compiler suite.
4. `reference_interpreter` is a pseudo-name implemented by *unsetting*
   the env var. Recorded; an explicit name is future work.
5. `assert retained_sessions == N` topology guards now fail on
   Cranelift (flow 2/2) while passing on bytecode — the guards
   measure reuse, and reuse currently favors the cheaper backend.

## Readiness cache (the wall-time prize)

`tools/stage0-probe` gains a content-addressed cache
(`MNCS_PROBE_CACHE_DIR`, wired by all 8 harnesses to
`.build/probe-cache`, unset = historical behavior). Key =
sha256(backend, module, closure names + bytes, raw seeds,
version tag). Entries are gzip-compressed `{program, artifact}`
(~25x: one decl entry ~468 MB raw → ~20 MB). A hit skips frontend
*and* lowering; invalidation is exact. Compression goes through a
temp file, never a stdin pipe (a pipe deadlocks past 64 KB).

Regression coverage: every suite already runs each workload twice
and asserts identical digests; within one cached invocation the
first run misses and the second hits, so the existing
determinism assertion *is* the cache-equivalence test. No new
harness is needed.

Measured effect: readiness 149 s → 23 s on hits (entry ~20 MB
compressed); `test_decl.py` 4:56 → 1:03 end to end (both probe
spawns hit, determinism assertion passes across the hit path).
A repeated unchanged run adds +7 bytes (success criterion 8).
Compression goes through a temp file: an earlier stdin-pipe design
deadlocked past 64 KB of kernel pipe buffer and was replaced
before any suite ran through it.

## Phase 3 — VM route (narrowest correct)

No new format. The existing chain already connects:

```text
mncs source --Stage-0 research-bytecode backend--> BackendArtifact
  --mncs-vm/src/migrate.rs--> mncs.vm.artifact/1 --admit--> Session::call
```

`mncs-vm/tests/compiler_source.rs` (new) drives it with
`mncs-compiler/src/compiler/source.mncs` read from the sibling
checkout: `page_count_for`, `span_valid_global`, `fnv_basis`
through VM execution, each asserted against both the `execute_ssa`
oracle and a concrete expected value. Generic helpers (`byte_at<N>`
etc.) are blocked on session calls carrying no type arguments —
recorded gap, not worked around.

Direct `mncs.vm.artifact/1` emission (P-VM-COMPILER-001) remains the
stated long-term direction; this campaign proves the migration route
with real compiler content instead of widening scope to a new
backend adapter. Status: `mncs-vm` suite 50/50 green including the
new differential (7.2 s). Required one narrow VM-side fix: generic
exports without a seeded SSA instance are recorded under the
artifact's existing `unsupported` list instead of refusing the whole
artifact (ambiguity still refuses; empty callables still fail
admission).

## Phase 8 — environment integration (contract, not code)

`mncs-environment` coordinates (sessions, claims, projections) but
does not execute MNCS or select backends anywhere in its scripts or
`mncs_env` package: there is no dispatcher to modify. The
integration delivered here is the selection contract itself —
`MNCS_PROBE_BACKEND` (explicit choice always wins),
`tools/backend_policy.py` (per-suite defaults with recorded
rationale), truthful `execution_mode`, and semantic-only digests —
which environment-orchestrated consumers (Doctor helpers,
projections, language-service calculations, agent-authored tooling)
can now adopt per intent: bytecode/VM where execution suffices,
Cranelift where native matters, explicit backends for
target-specific proof. Intent-driven selection inside environment
orchestration is future work once consumers exist.

## Pressures

- CP-0024 (backend, mncs-language; observed at pin): widened. The
  flow session now fails retention 2/2 with the same contained
  `mncs-cranelift-session` worker panic (`compiled_blob.rs:56`
  NegOverflow) previously seen only on project. Mechanism: JIT
  finalize panics on the worker thread (contained, retained 0);
  any one-shot execution attempt re-panics on the serving thread.
  Peak RSS does not discriminate (decl 3.2 GB retains, flow 3.1 GB
  panics): the discriminator is JIT image layout, not process size.
  Honest status: open, mitigated for B-class suites by the bytecode
  path, still blocking the native project suite. A fix (PIC/layout/
  dep upgrade) lives in language main and cannot reach the pin
  without a repin.
- CP-0025 (new, backend, mncs-language; observed at pin): portable-
  WASM lowering and retention of the decl module succeed, but the
  first execution request stack-overflows the probe host thread.
  WASM execution of compiler-scale workloads is blocked; lowering
  is proven.
- Step-budget calibration (test, mncs-compiler): native 1-step
  accounting vs interpreted real counting makes one budget cover
  two regimes. Project resolution needs native steps or a chunked
  interpreted topology (open design question, not implemented).

## Test taxonomy (Phase 10, encoded where practical)

- Semantic / compiler-kernel: no backend when execution is
  unnecessary (oracle transports; bare CLI).
- Executable semantic regression: cheapest retained backend that
  covers the operations (research-bytecode today; canonical VM as
  the artifact route matures). Default migration per-suite as
  verified.
- Native compiler/backend: Cranelift explicitly (project suite,
  backend canaries, backend-matrix points).
- Target-specific: requested backend explicitly (C11 canary, WASM
  proofs, Atlas-driven cases).
- Cross-backend conformance: deliberate matrices; semantic-only
  digests now make cross-backend equality checkable.
- Release/promotion: broader matrix at deliberate points, not per
  edit.

`execution_mode` is truthful, digests are semantic-only, and the
cache makes repeated runs reuse instead of recompiling. The
remaining default flips are mechanical follow-ups, one suite at a
time with re-recorded evidence.
