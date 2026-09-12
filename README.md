# mncs-compiler

Self-hosted next-generation compiler for MNCS, built in `mncs-language` to pressure-test the language, advance incremental and machine-native compilation, and ultimately replace the Rust compiler.

## Status

`mncs-compiler` is experimental. The Rust compiler in `mncs-language` remains the canonical Stage-0/reference compiler until this project proves semantic parity, self-hosting, backend coverage, reproducibility, and acceptable cost.

The repository exists so active compiler architecture can move into MNCS without destabilizing `mncs-language`. Compiler development runs should work here, record language pressure here, and leave language changes for dedicated follow-up runs in `mncs-language`.

## Executable slices

The kernel implements **bounded ASCII source processing** in MNCS: lossless
lexical tokens, byte spans, 1-based line/column rendering, lexical
diagnostics/coverage evidence, and a parser for the language header and
qualified module declaration (64-byte inputs). The segment layer mirrors
the same lexical semantics over four-chunk ≤256-byte units with absolute
offsets, proven by its own twin differential. All six frontend modules
are profile-0.13 sources using native negation, total scalar `match`
dispatch, and reused iteration identities.

The declaration vertical (`src/compiler/segment.mncs`, `src/compiler/decl.mncs`)
extends this to **bounded declaration-scale compilation**: header, `use`,
`record`, payload `enum`, and `fn` declarations with bodies and expressions
(256-byte units); function-name symbol collection with duplicate detection; a
resolve/span walk over every body; and lowering of every expression to a
postfix stack IR checked by a stack-depth self-verifier. The semantic
vertical (`decl.prove_unit`) adds signature facts with contracts/effects,
bidirectional expression proof with fused typed lowering, statement
proving, whole-unit proofs with FAIL/UNKNOWN obligations, and a type-stack
verifier for the typed IR — with FAIL-obligation parity vs Stage-0
diagnostics over a 49-case twin differential. The declaration/semantic
verticals last ran green at the previous Stage-0 pin and are migrated to
0.13 (parse-checked); their elaboration proof awaits the upstream
bool-payload fix (CP-0014). This is not yet a whole-module compiler or
self-hosting implementation.

Run `tools/bootstrap.sh`, then `python3 tools/test_frontend.py`,
`python3 tools/test_segment.py`, `python3 tools/test_decl.py`,
`python3 tools/test_sem.py`, and `python3 tools/test_pressure.py`. See
[frontend evidence](evidence/FRONTEND.md), [segment evidence](evidence/SEGMENT.md),
[declaration evidence](evidence/DECL.md), [semantic evidence](evidence/SEM.md),
[parity matrix](evidence/PARITY.md),
and the [pressure index](pressure/README.md). All production behavior lives in
`src/compiler/`; the Rust/Python code under `tools/` is a temporary Stage-0 test
transport, not a compiler implementation. There is no standalone driver or
coordinator yet.

## Mission

Build a compiler appropriate for an ecosystem where humans, tools, and autonomous agents are first-class compiler clients.

The target architecture is a deterministic compiler kernel over immutable program snapshots and content-addressed facts, with optional coordination layers for incremental reuse, persistent service operation, heterogeneous execution, verification, diagnostics, observability, and eventually adaptive optimization.

## Core principles

1. **MNCS first.** Production implementation should be written in `mncs-language` wherever the language can express the requirement.
2. **Pressure is output.** Do not silently fix `mncs-language` from this repository. Record concrete pressure cases under `pressure/`.
3. **Behavior before replacement.** The Rust compiler remains a differential oracle and bootstrap path until succession criteria are met.
4. **Deterministic semantics.** Caches, scheduling, remote execution, history, and learned models must not change program meaning.
5. **Graph-shaped compilation.** Treat compilation as establishment of facts and obligations needed to produce a requested artifact, not only as a monolithic batch pipeline.
6. **Immutable snapshots.** Concurrent clients compile exact workspace snapshots. Shared state must be immutable, versioned, or safely content-addressed.
7. **Evidence over claims.** Verification, diagnostics, provenance, benchmark results, and pressure findings should be machine-readable where practical.
8. **Cost matters.** Measure compiler CPU, wall time, memory, cache I/O, backend work, and generated-code benefit. The goal is a strong compile-cost/code-quality ratio.
9. **Bootstrap stays small.** `mncs-store`, `mncs-index`, `mncs-fabric`, learning systems, and a persistent service may enhance the compiler but must not be required to bootstrap the language.
10. **Models propose; deterministic mechanisms decide.** Learned systems may rank, prewarm, predict, or propose. They are not part of the correctness authority.

## Architectural shape

```text
CLI / Harness / Atlas / agents / IDE
                 |
                 v
      Compiler coordinator (optional)
  sessions | scheduling | cache | telemetry
                 |
                 v
          Compiler kernel
 source -> facts -> IR -> verify -> lower -> artifact
                 |
       deterministic evidence
```

The compiler kernel must also remain directly usable in standalone mode.

## Initial repository layout

- `src/` — MNCS deterministic compiler kernel.
- `tests/` — compiler/self-host/differential tests as implementation grows.
- `rfcs/` — architectural contracts.
- `pressure/` — language, stdlib, runtime, backend, and architecture pressure findings.
- `evidence/` — reproducible build, parity, performance, and succession evidence.
- `ARCHITECTURE.md` — unified architecture and ownership boundaries.
- `ROADMAP.md` — staged migration from Rust Stage-0 to canonical MNCS compiler.
- `AGENTS.md` — operating contract for agent-driven work in this repository.

## Near-term implementation order

1. Establish a deterministic MNCS compiler kernel and stable internal contracts.
2. Port frontend behavior while comparing against the Rust compiler.
3. Port semantic/type/effect/ownership behavior and IR construction.
4. Port verification and backend interfaces.
5. Reach self-hosting while keeping Rust as Stage-0/reference.
6. Add fine-grained incremental identities and immutable snapshot reuse.
7. Add the optional persistent compiler coordinator and shared scheduling/cache layers.
8. Integrate persistent storage and Fabric only after local contracts are stable.
9. Introduce micro-verifiers and richer provenance where they improve correctness or fault isolation.
10. Introduce adaptive/micro-model optimization only after deterministic instrumentation and benchmark evidence exist.

## Succession rule

Self-hosting alone does not replace Rust. The MNCS compiler becomes canonical only after it demonstrates sufficient:

- language and negative-test parity,
- IR/backend correctness,
- self-host reliability,
- reproducibility,
- diagnostic quality,
- cold and incremental compilation cost,
- memory behavior,
- concurrent-agent behavior,
- recovery/bootstrap capability.

Rust then becomes a frozen Stage-0/reference implementation rather than an independently evolving compiler.

## Relationship to the MNCS ecosystem

- `mncs-language`: language specification, Stage-0 toolchain, stdlib, RFCs, and upstream pressure fixes.
- `mncs-harness`: differential, conformance, stress, and self-host validation.
- `mncs-store`: optional future persistent L3 compiler object/cache storage.
- `mncs-index`: reusable indexing ideas/infrastructure where bootstrap layering permits.
- `mncs-fabric`: optional execution placement for pure/capability-described compiler work.
- `mncs-ingest`: compiler telemetry/observation ingestion.
- `mncs-learn`: training of optional specialist optimization models.
- `mncs-memory`: adaptive historical knowledge, never semantic truth.

See `ARCHITECTURE.md` and the RFCs for the detailed design.