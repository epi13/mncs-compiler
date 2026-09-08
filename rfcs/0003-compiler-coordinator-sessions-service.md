# RFC 0003 — Compiler Coordinator, Sessions, and Service Mode

Status: Architectural direction

## Summary

The preferred long-term MNCS compilation path is a persistent local compiler coordinator serving isolated logical sessions over a shared deterministic compiler kernel.

This coordinator is not required for bootstrap, CI, recovery, or standalone compilation.

## Motivation

Agent-driven development can otherwise launch several independent compiler processes that each duplicate startup, stdlib loading, semantic work, caches, backend initialization, and resource saturation.

A persistent coordinator can share safe immutable work and globally schedule scarce resources without sharing mutable client state.

## Compilation session

A session represents one logical request context, not one process. It includes:

- workspace snapshot,
- compiler/toolchain identity,
- requested target/artifact,
- target/capability policy,
- build/optimization policy,
- reproducibility policy,
- priority/cancellation,
- request-local scratch and diagnostics.

## Shareable state

Candidates for safe sharing when immutable/versioned/content-addressed include:

- stdlib parse/semantic state,
- unchanged parsed modules,
- semantic graph fragments,
- generic instantiations,
- verified IR,
- target descriptions,
- backend initialization,
- device/capability knowledge whose identity includes relevant environment versioning.

## Isolated state

Keep isolated or request-scoped:

- partially edited workspace state,
- inference scratch,
- speculative transforms,
- temporary arenas,
- cancellation state,
- request diagnostics,
- nondeterministic/unsafe external execution context.

## Scheduling

The coordinator should eventually reason globally about:

- CPU cores,
- RAM pressure,
- GPU devices,
- backend/toolchain capacity,
- Fabric workers and capabilities,
- current load,
- request priority,
- fairness,
- latency versus throughput,
- reproducibility constraints.

The scheduler may choose in-process threads, isolated workers, GPUs, or remote Fabric workers according to workload and trust requirements.

## Standalone path

User-facing behavior should retain an equivalent to:

```text
mncs build
  -> coordinator available: submit session
  -> coordinator unavailable: launch/local fallback or execute kernel standalone
```

Standalone mode must exercise the same kernel contracts.

## Storage

The coordinator may maintain:

- L1 session-local caches,
- L2 shared in-memory caches,
- optional L3 persistent storage.

`mncs-store` is a preferred long-term L3 implementation, but the compiler must preserve a minimal bootstrap storage adapter.

## Fault isolation

Immutable facts and explicit work identities should limit poisoned shared state. Risky operations such as external toolchains, compile-time user code, GPU execution, or unstable backends may run in isolated workers. Safe deterministic frontend work should not incur process isolation overhead by default.

## Fabric boundary

The compiler defines work meaning and required capabilities. `mncs-fabric` decides placement among eligible workers.

Initial distributed work should favor pure/idempotent tasks with content-addressed inputs so worker loss can be retried safely.

## Observability

The coordinator should eventually report active sessions, queued obligations, worker utilization, memory pressure, backend saturation, cache reuse, invalidation reasons, verifier bottlenecks, and language-pressure hotspots.

## Non-goals

- turning compiler stages into HTTP microservices,
- requiring network access for normal compilation,
- making service mode part of language correctness,
- allowing historical/learned state to alter semantic meaning.
