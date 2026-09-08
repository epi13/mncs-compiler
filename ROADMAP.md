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

Current evidence-backed slice (see [frontend evidence](evidence/FRONTEND.md)):

- implemented: bounded immutable byte inputs, guarded reads, span validation,
  ASCII lexer, nested comments, lexical errors, coverage evidence and source equality;
- partially implemented: syntax/parser (header and qualified module declaration
  only), structured diagnostics and pure source-unit fact requests;
- blocked for whole-module scale: 64-byte source capacity; Unicode classification
  remains a parity gap. See CP-0001 and CP-0002 before extending storage;
- not started: declarations/expressions, imports, symbols, semantic/type/effect/
  ownership checks, compiler IR and backend lowering;
- intentionally deferred: persisted fact IDs/cache, service/coordinator, distributed
  or learned features, Rust succession and self-hosting.

Next: resolve or deliberately redesign the measured storage boundary in a
separate language run, then extend source-backed syntax and dependency facts.
Record sequences already elaborate; do not assume every collection problem
requires a new language feature. Source Profile 0.10 is supported by the unchanged
pinned Stage-0 and now replaces the original scaffold's 0.8 metadata.

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
