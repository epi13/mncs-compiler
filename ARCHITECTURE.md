# Architecture

This document defines the initial architecture for `mncs-compiler`. It is intentionally split between a deterministic compiler kernel that can bootstrap and run standalone, and optional infrastructure that can later make compilation persistent, incremental, distributed, and adaptive.

## 1. Architectural boundary

The compiler is two layers, not two implementations.

```text
Compiler kernel
  syntax
  semantics
  type/effect/ownership
  IR
  deterministic verification
  optimization legality
  backend contracts
  artifact production

Compiler coordinator
  sessions
  immutable workspace snapshots
  dependency scheduling
  shared caches
  resource accounting
  persistent service mode
  Fabric placement
  observability
  history/adaptation
```

The kernel must be exercisable without the coordinator. Service mode orchestrates the kernel; it does not own language semantics.

## 2. Compilation model

The user may still request `mncs build`, but internally the compiler should converge on:

```text
requested artifact
       |
       v
required facts / obligations
       |
       v
establish dependencies
       |
       v
compute or reuse deterministic results
       |
       v
verify invariants
       |
       v
artifact + evidence + diagnostics + measured cost
```

A compiler fact should have stable identity derived from the operation, deterministic inputs, compiler semantic version, and only the target/capability inputs that can affect the result.

Do not make every AST node independently persistent by default. Start with natural incremental units such as modules, declarations, functions, generic instantiations, IR regions, and backend compilation units. Finer granularity must justify hashing, indexing, serialization, and lookup overhead with measurement.

## 3. Workspace snapshots

Concurrent clients must compile exact snapshots rather than mutating one global semantic world.

A snapshot identifies source/module state and configuration. A new edit creates a new snapshot. Unchanged structures may be shared structurally between snapshots.

Session-local scratch state may be mutable, but reusable semantic state should trend immutable or versioned. A crashed request must not poison facts visible to another session.

## 4. Compilation session

A logical compilation session is expected to include:

- workspace snapshot identity,
- compiler semantic/toolchain identity,
- requested artifacts,
- target and target policy,
- capability snapshot,
- build/optimization policy,
- reproducibility policy,
- request priority/cancellation state,
- request-local diagnostics and scratch state.

A session is not a dedicated compiler process. Multiple sessions may safely share immutable parsed modules, stdlib facts, verified IR, target descriptions, backend initialization, and other content-addressed results.

## 5. Cache and storage layers

### L1 — session local

Cheap scratch data, temporary inference structures, arenas, stage-local memoization, and speculative transformations.

### L2 — compiler host

Shared parsed modules, semantic facts, generic instantiations, verified IR, target metadata, reusable backend state, and other high-value immutable results.

### L3 — persistent

Optional content-addressed storage surviving compiler restarts and possibly machines. `mncs-store` is the preferred long-term candidate, but bootstrap must support a minimal filesystem/memory implementation without depending on it.

Cache correctness comes from identity and deterministic validation, not mutable global invalidation alone. Reverse dependency information is still useful for scheduling recomputation.

## 6. Capabilities and target execution

The compiler must distinguish:

1. whether it can produce an artifact,
2. whether the current machine can execute it,
3. whether an environment exists that can validate it.

A compilation result may therefore succeed while carrying unresolved execution obligations.

Representative execution strategies include `native`, `jit`, `emulated`, `compile-only`, and `unavailable`.

Backend work should consume explicit capabilities rather than ambient assumptions. Fabric may locate an appropriate worker, but the compiler defines the semantic and legality requirements of the work.

## 7. Verification

Correctness-sensitive decisions remain deterministic.

The preferred pattern for uncertain or optimized transformations is:

```text
proposal -> deterministic verifier -> accepted/rejected artifact
```

Micro-verifiers are narrow contracts, not necessarily separate processes. A verifier may be an ordinary in-process function when that is cheapest. Process isolation is reserved for trust boundaries, unstable external tools, compile-time user code, GPU execution, or other risky operations.

Verification evidence should be reusable when its identity is stable and verification is cheaper to reuse than recompute.

## 8. Diagnostics and provenance

Diagnostics should originate as structured compiler data, not formatted terminal strings.

A diagnostic may reference:

- failed obligation,
- violated invariant/rule,
- semantic objects,
- dependency/provenance path,
- source ranges,
- evidence,
- invalidated facts,
- candidate remediation,
- confidence and whether automatic repair is safe.

Human text, agent responses, Harness output, and Atlas views should be renderings of the same diagnostic object.

Lightweight provenance should connect source semantics through IR and backend lowering sufficiently to explain failures without retaining every lowered object forever.

## 9. Cost and observability

Compiler work should record enough telemetry to answer what ran, why it ran, what was reused, and what it cost.

Important measures include:

- wall time,
- CPU time,
- peak/transient memory,
- hashing and cache I/O,
- verifier cost,
- backend/toolchain cost,
- remote-transfer cost,
- GPU resource use,
- code size/runtime benefit where measurable.

Optimization policies (`fast`, `balanced`, `runtime`, `size`, `debug`, `verification-heavy`) should eventually choose among valid work based on measured cost/benefit rather than requiring separate compiler architectures.

## 10. Persistent compiler coordinator

Persistent service mode is a long-term normal path, but not a bootstrap dependency.

The coordinator may provide:

- lightweight isolated sessions,
- shared L2 caches,
- global CPU/RAM/GPU/backend scheduling,
- fairness and latency priorities,
- automatic local daemon startup,
- fault containment,
- compiler telemetry,
- safe delegation of pure work to Fabric.

Architectural boundaries are not automatically process boundaries. Avoid HTTP-style microservices between compiler stages.

## 11. Fabric integration

Compiler work eligible for remote execution should become explicit deterministic work items with content-addressed inputs, capability requirements, compiler/toolchain identity, and reproducibility policy.

Fabric decides where permissible work runs. It does not define parsing, semantic, IR, verification, or backend correctness.

Pure/idempotent work is the preferred first distributed unit because it is easy to retry after worker failure.

## 12. Learning and adaptive systems

Compiler semantic truth and compiler observations are different data classes.

### Facts

Types, effects, ownership, layout, ABI, legal transformations, verified invariants, target requirements.

### Observations

Common rebuilds, measured transform wins, worker performance, cache popularity, optimization cost, hardware-specific performance.

Observations may feed `mncs-ingest`, `mncs-learn`, and `mncs-memory`. Learned components may rank optimization candidates, prewarm caches, predict useful specialization, or aid scheduling. They must have deterministic fallback behavior and versioned provenance.

## 13. Security

A persistent compiler increases the value of explicit capabilities.

Operations should declare required permissions/capabilities where relevant: filesystem access, process execution, network, macro/compile-time execution, dynamic libraries, GPU execution, eBPF/kernel access, or external toolchains.

Prefer narrow capability grants around operations over one unrestricted long-lived compiler process.

## 14. Bootstrap layering

The minimum bootstrap path must remain:

```text
Rust Stage-0 compiler
        |
        v
MNCS bootstrap-capable compiler
        |
        v
current MNCS compiler
```

The compiler implementation may understand newer language features before its own source uses them. This permits a conservative bootstrap subset while the language continues to evolve.

`mncs-store`, `mncs-index`, `mncs-fabric`, `mncs-memory`, `mncs-ingest`, and `mncs-learn` are optional enhancement layers, never required to establish the first canonical compiler.

## 15. Development rule

When compiler implementation exposes a language limitation, first record the pressure here. Classify it as language, stdlib/runtime, backend, tooling, or compiler-architecture pressure. Fix upstream only in a dedicated `mncs-language` run, then return here and prove the pressure case is resolved.
