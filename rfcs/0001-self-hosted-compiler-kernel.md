# RFC 0001 — Self-Hosted Compiler Kernel

Status: Foundational

## Summary

`mncs-compiler` will implement the canonical future compiler as a deterministic compiler kernel written in `mncs-language`. Persistent service operation, shared caches, Fabric execution, telemetry, and learned heuristics are optional orchestration layers around this kernel rather than alternate compiler implementations.

## Motivation

The current Rust compiler is expensive to continue evolving architecturally if its future is to be rewritten in MNCS. At the same time, replacing it immediately would destabilize the language and remove a valuable behavioral oracle.

This RFC establishes a staged relationship:

```text
Rust compiler: Stage-0 + reference + recovery
MNCS compiler: active next-generation implementation
```

## Decisions

### 1. MNCS is the implementation target

New compiler architecture belongs here in MNCS. Rust receives only changes required for correctness, compatibility, bootstrap, or critical backend maintenance.

### 2. Rust remains an oracle during transition

Differential testing should compare the two implementations at useful observable boundaries: diagnostics, semantic decisions, IR, backend output, and execution behavior.

### 3. Faithful means semantic fidelity, not structural copying

The MNCS compiler should preserve language behavior while being free to introduce better internal architecture. A line-for-line port of Rust is explicitly not required.

### 4. Kernel and coordinator are separate

The kernel must support standalone invocation. The coordinator may schedule and reuse kernel work but must not contain semantics that cannot be exercised independently.

### 5. Bootstrap subset is allowed

A bootstrap compiler may implement language features that its own source does not yet use. This permits:

```text
Rust Stage-0 -> conservative MNCS compiler -> current MNCS compiler -> self-host
```

without requiring Stage-0 Rust to understand every newly added syntax feature forever.

## Trusted correctness base

The trusted compiler base should remain deterministic and as small as practical:

- parsing/syntax rules,
- semantic/name/type/effect/ownership rules,
- IR invariants,
- deterministic transformation legality,
- ABI/layout rules,
- backend legality and artifact validity checks.

Caches, scheduling, remote execution, historical observations, and models are outside semantic authority.

## Succession criteria

The MNCS compiler becomes canonical only after evidence demonstrates sufficient:

- conformance and negative-test parity,
- self-host stability,
- backend coverage,
- reproducibility,
- diagnostics,
- compile-time and memory behavior,
- recovery/bootstrap path.

Incremental and concurrent-agent performance are expected to become major advantages but do not excuse correctness gaps.

## Consequences

- Compiler work immediately pressures `mncs-language` in realistic ways.
- The repository can experiment with machine-native architecture without destabilizing the current canonical compiler.
- Rust work is deliberately bounded rather than duplicated indefinitely.
- The eventual persistent compiler service can evolve without making service mode mandatory for CI/bootstrap/recovery.
