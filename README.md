# mncs-compiler

<!-- MNCS:generated:begin -->
## Project entry

Experimental MNCS compiler implementation, written in mncs-language to pressure-test the language and move compiler semantics into MNCS while Rust remains the Stage-0/bootstrap/reference implementation.

```bash
tools/bootstrap.sh
```

Declared capabilities (declarations do not establish execution health):

- `canonical-vm-artifact/1` — compiler-artifact-provider (experimental)
- `compiler-build/1` — build-provider (experimental)
- `compiler-next-generation/1` — compiler-research-surface (experimental)
- `compiler-producer/1` — compiler-artifact-provider (experimental)

Semantic sources and ownership: `.mncs/projections.json`.
<!-- MNCS:generated:end -->

Experimental MNCS compiler implementation, written in `mncs-language` to
pressure-test the language and move compiler semantics into MNCS while Rust
remains the Stage-0/bootstrap/reference implementation.

## Status

`mncs-compiler` is experimental. The Rust compiler in `mncs-language` remains the canonical Stage-0/reference compiler. The lock file pins the exact code-bearing Stage-0 revision exercised by this campaign, `a3ac17df69e68f6373cbff336db0a572667d73da`, at source profile 0.18. All compiler source modules now declare profile 0.18. Older pins named in historical campaign evidence (for example `709ba008` on 2026-09-25 and `843c5bc` on 2026-09-26) are preserved as measured and are not relabelled.

The repository keeps compiler architecture in MNCS and records compiler-origin pressure here. Generic language/runtime changes are made in a separate `mncs-language` change.

## Executable slices

The existing frontend implements bounded ASCII source processing in MNCS: lexical tokens, byte spans, line/column rendering, diagnostics, and header/module parsing. The native compiler unit ABI still accepts four 64-byte chunks (256 bytes total). Unicode and newer current-profile syntax remain reproduced native frontend gaps; see the [current parity ledger](evidence/PARITY.md) and [current pressure results](evidence/pressure-current-results.json).

The declaration vertical extends this to bounded declaration-scale compilation: header, `use`, `record`, payload `enum`, and `fn` declarations with bodies and expressions (256-byte units); function-name symbol collection with duplicate detection; a resolve/span walk; stack IR and semantic proof with FAIL/UNKNOWN obligations and a typed-stack verifier. The semantic twin now passes 131 cases plus eleven proof verdicts twice at the current pin. `flow.mncs` consumes the proof and attaches each typed postfix operation to a source expression in branch, jump, return, and failure blocks, then checks targets and unreachable joins. Four current-profile programs match Rust diagnostics/spans; both positive cases also match Rust SSA branch/return shape. A bounded verified value-SSA slice now covers finite enum matches, enum construction, integer scalar matches, record field projection, and compiler operations (finite/scalar switches, payload extraction, enum construction, sequence repetition, nested nominal payloads, projections, arm result joins, kind-9 operation instructions); see the [current parity ledger](evidence/PARITY.md) and the [project record](evidence/campaign-20261002-project-results.json). Native executable output remains a narrow test-only structural scalar C11 slice with no proof-carrying backend adapter. Current real-family blockers are nested-match parsing (CP-0011 instance) and the per-source size ceiling (CP-0001); the host-intrinsic callee model (CP-0019) now reaches verified SSA.

Run `tools/bootstrap.sh`, then the focused and canonical suites documented in [`evidence/README.md`](evidence/README.md). See [frontend evidence](evidence/FRONTEND.md), [segment evidence](evidence/SEGMENT.md), [declaration evidence](evidence/DECL.md), [semantic evidence](evidence/SEM.md), the [current parity ledger](evidence/PARITY.md), and the [pressure index](pressure/README.md). Production compiler behavior lives in `src/compiler/`; Rust/Python code under `tools/` is a temporary Stage-0 test transport, not a compiler implementation. There is no standalone native driver or coordinator yet.

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

Self-hosting alone does not replace Rust. A compiler generation developed here is promoted into `mncs-language` as the canonical compiler only after it demonstrates convincing evidence for whole-family compilation, Stage-1→Stage-2 self-hosting, semantics/backend correctness, deterministic reproducibility, conformance/negative behavior, and adequate diagnostics/recovery.

Generation promotion is distinct from long-term compiler architecture maturity. Distributed Fabric execution, adaptive/learned optimization, persistent compiler service mode, sophisticated SIMD work, Store-backed reuse, and other advanced capabilities may keep improving across later compiler generations; a generation does not need them before it can be promoted. See `ROADMAP.md` for the phased maturity model.

Rust then becomes a frozen Stage-0/reference implementation rather than an independently evolving compiler.

## Development environment

`mncs-environment` is the preferred development, session, and provider-composition layer for compiler work: canonical entry, selected-artifact caching, durable sessions, verification-obligation execution, and Store-backed artifact retention all run through it. This does not change the bootstrap rule below: Environment, Store, Fabric, Index, Ingest, Learn, Memory, and any persistent service must never become semantic prerequisites of the standalone compiler kernel.

## Relationship to the MNCS ecosystem

- `mncs-language`: language specification, Stage-0 toolchain, stdlib, RFCs, and upstream pressure fixes.
- `mncs-environment`: preferred development/session/provider-composition layer; never a semantic bootstrap dependency.
- `mncs-harness`: differential, conformance, stress, and self-host validation.
- `mncs-store`: optional future persistent L3 compiler object/cache storage.
- `mncs-index`: reusable indexing ideas/infrastructure where bootstrap layering permits.
- `mncs-fabric`: optional execution placement for pure/capability-described compiler work.
- `mncs-ingest`: compiler telemetry/observation ingestion.
- `mncs-learn`: training of optional specialist optimization models.
- `mncs-memory`: adaptive historical knowledge, never semantic truth.

See `ARCHITECTURE.md` and the RFCs for the detailed design.

## Generated evidence retention

Commit compact evidence summaries and manifests with source, fixture, compiler, and Stage-0 identities. Keep full resolved-fact wires, SSA dumps, and intermediate payloads in the Environment session artifact store or ignored `.build/campaign-artifacts` directory. The manifest at `evidence/retained-generated-artifacts.json` records the four historical 2026-09-27 payloads, their hashes, and commands for retrieving the exact original blobs from their provenance commit. Their removal from the current tree does not rewrite Git history or remove the audit path.
