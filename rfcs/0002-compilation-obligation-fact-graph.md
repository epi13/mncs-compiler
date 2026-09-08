# RFC 0002 — Compilation Obligation and Fact Graph

Status: Foundational

## Summary

Compilation should converge on a graph of deterministic facts and obligations required to satisfy a requested artifact rather than relying internally on one mutable whole-program batch pipeline.

The user-facing experience may remain `mncs build`; the internal question becomes:

> What must be established, verified, or transformed to produce this artifact from this exact program snapshot?

## Terminology

### Fact

A deterministic compiler result with stable identity derived from its operation, deterministic inputs, compiler semantic identity, and only relevant target/capability context.

Examples:

- parsed module,
- resolved declaration,
- type/effect/ownership result,
- constant evaluation,
- IR region,
- analysis result,
- verified transform,
- lowered backend fragment,
- target artifact.

### Obligation

Work required to establish a fact or prove a condition before another result is valid.

Examples:

- establish imported symbol identity,
- prove an optimization preserves an invariant,
- confirm target capability,
- lower an IR region for PTX,
- validate an external toolchain artifact.

## Identity

A conceptual fact key includes:

```text
operation
input identities
compiler semantic/version identity
relevant target identity
relevant capability identity
optimization/build policy when semantically relevant
```

Facts must not depend on hidden ambient process state.

## Incremental units

The first implementation should prefer natural regions:

- module/source unit,
- top-level declaration,
- function/type semantic unit,
- generic instantiation,
- function or region IR,
- optimization region,
- backend compilation unit,
- package/artifact.

Do not cache finer units simply because they can be hashed. The total cost of hashing, dependency bookkeeping, lookup, serialization, and storage must be lower than useful recomputation.

## Invalidation

A changed source/dependency identity produces a new downstream identity. Reverse dependencies accelerate discovery of candidate recomputation, but semantic correctness should not rely on mutating a global cache perfectly.

The desired model is structural:

```text
unchanged inputs -> same fact identity -> safe reuse
changed input    -> new fact identity  -> recompute dependent work
```

## Verification

A transformation may emit both an output fact and verification evidence. Reusing verification is permitted only when the evidence identity captures all assumptions required for validity.

## Scheduling

The initial executor may be serial. Parallelism, persistent service scheduling, and Fabric delegation are execution strategies over the same graph rather than prerequisites for the graph design.

## Observability

Work nodes should eventually expose enough metadata to answer:

- why did this run,
- which dependency invalidated it,
- was it reused,
- how much did it cost,
- what capability did it require,
- what evidence did it produce.

## Non-goals

This RFC does not require:

- a database for every compiler object,
- every compiler stage to be independently persistent,
- every node to be a process/service,
- remote compilation,
- learned scheduling,
- exact final data structures before pressure testing reveals what MNCS can express efficiently.
