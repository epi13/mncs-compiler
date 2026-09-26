# Roadmap

This roadmap separates compiler succession from compiler research. The project should earn each stage with evidence rather than treating self-hosting alone as completion.

## Phase 0 — Repository foundation

- establish architecture, RFC process, pressure methodology, evidence conventions, and MNCS-first agent contract,
- pin a known `mncs-language` revision for reproducible development,
- add a minimal MNCS source/test scaffold,
- define the Rust Stage-0 relationship and succession criteria.

## Phase 1 — Deterministic compiler kernel

Goal: establish compiler behavior in MNCS without requiring service mode or distributed infrastructure.

Priorities:

- source/module representation,
- syntax/frontend behavior,
- semantic facts,
- type/effect/ownership behavior,
- target-independent IR construction,
- deterministic verifier interfaces,
- backend contract surfaces,
- structured diagnostics/evidence,
- differential fixtures against the Rust compiler.

## Current evidence-backed campaign state (2026-09-25)

The lock pins Rust Stage-0 revision
`709ba00810099e6965bb47dec14ed19e9e1ae6f8` at source Profile 0.18. All seven
compiler modules declare 0.18. The refreshed [parity matrix](evidence/PARITY.md)
and machine-readable [ledger](evidence/parity-ledger.json) supersede this
roadmap's earlier pin-era descriptions.

- The frontend and declaration implementations are still bounded: the
  declaration ABI is four 64-byte chunks (256 bytes total). The old Stage-0
  64-byte source ceiling is stale; current Rust accepts the preserved 65-byte
  source probe. The 256-byte boundary is a compiler input/representation limit,
  not a language capacity claim.
- The five tested Profile 0.18 forms in CP-0015 are accepted by current Rust
  and rejected by native `decl.parse_unit`; current evidence is in
  `evidence/profile-surface-results.json`. Unicode and full project-source
  resolution are also absent from the native frontend.
- CP-0014's bool-payload regression and compiler-origin CP-0016's nested
  imported-record runtime rejection are resolved in `mncs-language` revision
  `709ba008`. The current declaration parse/check differential passes twice;
  the separate 49-case semantic proof twin plus five verifier verdicts also
  passes twice at this pin.
- CP-0017 is compiler semantic drift, not language pressure. Its two minimal
  poisoned-result cases now match Rust's ordered diagnostics and spans in
  repeated native runs; the full 49-case semantic twin plus five verifier
  verdicts also passes twice at the current pin. See
  `evidence/semantic-pressure-cp0017-after.json` and
  `evidence/sem-results.json`.
- `flow.mncs` adds a compiler-owned pass from proved declarations and typed
  stack operations to explicit branch/jump/return/failure blocks with target
  and reachable-join checks. Four current-profile CFG cases pass repeated
  native runs and Rust differential checks, including positive branch/return
  shape and complete typed-operation preservation. No Rust-equivalent value
  SSA, native project resolver, or compiler-produced executable artifact is
  claimed.
- Historical pressure reproductions are re-run against current Rust in
  `evidence/pressure-current-results.json`; their old histories remain in
  `pressure/`. Current statuses distinguish language behavior from compiler,
  runtime, and tooling ownership.
- Current-profile declaration, semantic, frontend, and segment twins now have
  current-pin durable results, and linked artifact compile cost has been
  remeasured at the current pin. The
  native parser still rejects the five CP-0015 Profile 0.18 forms, and the
  four-chunk 256-byte unit plus absent project resolver remain compiler
  architecture limits.
- The RAVEL impact query for `flow.lower_unit` returned UNKNOWN after its
  180-second impact and test-inventory commands timed out. No zero-obligation
  closure is inferred; the direct current-pin compiler suites are recorded in
  `evidence/README.md` and the exact RAVEL result in
  `evidence/ravel-impact-flow.json`.

The highest-leverage next slice is a current-profile project-source path:
version-aware syntax, module/import resolution, and a source representation
beyond the legacy 256-byte unit. It should feed the existing declaration/proof
and CFG passes before attempting Rust's value-carrying body/SSA and target
lowering.
Record-sequence acceptance in Stage-0 is not evidence that the native compiler
has a project collection or module model.

Architecture may already use fact/obligation boundaries even when evaluation is single-threaded and uncached.

## Phase 2 — Self-host capable

Goal: a compiler built by Rust Stage-0 can compile the MNCS compiler source itself.

Evidence:

- stage-1 compiler produced by Rust,
- stage-2 compiler produced by stage-1,
- semantic/IR/artifact comparison policy,
- deterministic repeated self-host runs,
- explicit bootstrap subset constraints.

Rust remains canonical at this stage.

## Phase 3 — Production parity

Goal: normal MNCS development can safely use `mncs-compiler` by default while retaining Rust as a bootstrap/reference path.

Required evidence should cover:

- language conformance and negative tests,
- diagnostics,
- supported backend behavior,
- ABI/layout expectations,
- reproducible builds,
- crash/failure behavior,
- cold compile cost,
- memory usage.

## Phase 4 — Incremental compiler graph

Goal: make reuse a fundamental property of compiler identities.

Add:

- immutable/versioned workspace snapshots,
- stable fact identities,
- reverse dependency discovery,
- natural-granularity invalidation,
- L1/L2 caching,
- warm and single-edit benchmarks,
- reuse telemetry.

Granularity changes must be measurement-driven.

## Phase 5 — Persistent compiler coordinator

Goal: efficiently serve concurrent agents/tools without multiplying compiler processes.

Add:

- lightweight compilation sessions,
- shared compiler-host cache,
- global CPU/RAM/backend scheduling,
- request priority/cancellation,
- fault containment,
- service observability,
- automatic local service discovery/startup,
- standalone fallback using the same kernel.

## Phase 6 — Persistent storage and heterogeneous execution

Goal: extend reuse and execution placement without making bootstrap depend on the wider ecosystem.

Explore:

- `mncs-store` L3 adapter,
- reusable `mncs-index` structures where appropriate,
- capability-described Fabric work units,
- remote verifier/backend tasks,
- GPU/backend contention scheduling,
- deterministic remote retry semantics.

## Phase 7 — Verified specialization and deeper provenance

Goal: improve correctness boundaries and debugging as the compiler becomes more aggressive.

Explore:

- reusable verification evidence,
- transformation-specific micro-verifiers,
- provenance through lowering,
- isolated risky toolchain/runtime validation,
- structured agent-remediation diagnostics.

## Phase 8 — Adaptive compilation

Goal: optimize valid choices using measured history while keeping correctness deterministic.

Only after instrumentation is mature, explore:

- target-specific optimization ranking,
- cache prewarming,
- specialization prediction,
- worker-placement heuristics,
- compiler observations through `mncs-ingest`,
- model training through `mncs-learn`,
- historical knowledge through `mncs-memory`.

Every learned path must retain version identity, reproducible fallback behavior, deterministic validation, and rollback.

## Succession milestone

The Rust compiler becomes frozen Stage-0/reference only when `mncs-compiler` demonstrates a compelling overall result across correctness, backend coverage, reproducibility, self-hosting, diagnostics, compile cost, memory, incremental reuse, concurrent-agent workload, and recovery/bootstrap behavior.

The MNCS compiler does not need to win every microbenchmark; it must be the better canonical architecture without sacrificing trustworthiness.
