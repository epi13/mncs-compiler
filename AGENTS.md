# Agent Contract — mncs-compiler

`mncs-compiler` is the experimental self-hosted compiler for MNCS and a deliberate pressure vessel for `mncs-language`.

## Non-negotiable goals

1. Prefer `mncs-language` for production compiler implementation, tests, fixtures, and tooling wherever the language can express the requirement.
2. Do not hide language gaps behind permanent Rust, Python, Go, C++, JavaScript, or other host-language scaffolding.
3. Do not modify `mncs-language` from compiler-development runs. Record pressure here first; fix upstream in a dedicated language run.
4. Preserve deterministic language semantics regardless of cache state, worker count, scheduling, remote execution, telemetry, or learned heuristics.
5. Treat the current Rust compiler as Stage-0/reference until explicit succession criteria are satisfied.
6. Do not claim self-hosting, parity, performance, or backend support without executable evidence.
7. Keep bootstrap dependencies minimal. Optional MNCS infrastructure must not become required for basic compilation.

## Required implementation posture

When adding a compiler unit or transformation, define where applicable:

- deterministic inputs,
- output/fact identity,
- dependency set,
- invalidation rule,
- semantic invariants,
- verification boundary,
- target/capability requirements,
- provenance/source relationship,
- incremental unit,
- concurrency safety,
- cancellation/failure behavior,
- cacheability and expected reuse value,
- measurable cost.

If these are intentionally deferred, document the reason.

## Pressure methodology

Pressure is a first-class deliverable.

A pressure finding should include:

- concise title and category,
- compiler workload that exposed it,
- smallest faithful MNCS reproduction available,
- current workaround,
- why the workaround is insufficient,
- correctness/safety/performance/ergonomics impact,
- frequency/severity,
- likely ownership (`language`, `stdlib/runtime`, `backend`, `tooling`, `compiler architecture`),
- desired behavior without prescribing an unnecessary feature,
- reproduction command/test when available,
- status and upstream issue/PR when later created.

Do not turn every awkward compiler pattern into a language feature. A finding may instead reveal a missing stdlib abstraction or a poor compiler representation.

## Evidence over claims

Preferred evidence includes:

- current `mncs-language` compile success,
- differential agreement with Rust Stage-0,
- canonical IR or semantic hashes,
- self-host stage comparison,
- negative/conformance tests,
- repeated deterministic builds,
- cold/warm/incremental timing,
- peak memory and work-reuse measurements,
- backend execution/validation evidence,
- multi-session stress evidence when coordinator work begins.

Store durable evidence under `evidence/` or link it from there.

## Micro-components

`micro-verifier`, `micro-debugger`, and `micro-model` describe narrow responsibility, not mandatory service/process boundaries.

- Deterministic correctness stays authoritative.
- Learned components may propose/rank/predict, never silently decide semantic truth.
- Do not add orchestration overhead merely to make an internal function look like a microservice.

## Cross-repository work

Respect these boundaries:

- `mncs-language`: language/stdlib/toolchain changes.
- `mncs-harness`: broad differential/conformance/stress orchestration.
- `mncs-store`: optional persistent compiler storage.
- `mncs-index`: reusable index/graph infrastructure where layering permits.
- `mncs-fabric`: placement/execution of capability-described work, not compiler semantics.
- `mncs-ingest` / `mncs-learn` / `mncs-memory`: observations and adaptation, not semantic truth.

When a sibling dependency would create a bootstrap cycle, preserve a small local interface and integrate the sibling implementation only as an optional adapter.

## Rust policy

Do not expand the Rust compiler with next-generation architecture unless required for correctness or bootstrap. Rust exists to keep the ecosystem productive, provide differential behavior, and bootstrap the MNCS compiler while succession is incomplete.
