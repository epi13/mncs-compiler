# Roadmap

<!-- MNCS:generated:begin -->
## Evidence-bound roadmap

- **complete** — Declared ambient projection surfaces satisfy their contract (`mncs-compiler:projection-conformance`)
<!-- MNCS:generated:end -->

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

## Current evidence-backed campaign state (2026-10-03)

The lock pins Rust Stage-0 revision
`a3ac17df69e68f6373cbff336db0a572667d73da` at source Profile 0.18. All
compiler modules declare 0.18. The [parity matrix](evidence/PARITY.md) and
machine-readable [ledger](evidence/parity-ledger.json) are current at this
pin; the dated sections below are preserved as historical evidence with
their original pins and measurements.

- Verified value SSA covers finite enum matches, enum construction,
  scalar matches, record projection, nested matches (CP-0011, modulo
  the outer stack-temp / payload-binding residual), and compiler
  operations (CP-0019), with Stage-0 oracle agreement throughout.
  Full project closure: 43 requests in 1,362.503 s. See
  `evidence/campaign-20261003-project-results.json`.
- The native pipeline consumes logical paged sources past the old
  1,024-byte views (CP-0001/CP-0021): tier D proves stride-variant
  transport to 558,004 bytes with precise feature-gap spans, and the
  `synthetic-2049` anchor runs the whole pipeline green. See
  `evidence/cp0001-matrix.json` (10 rows, zero ceiling rows).
- Backend/runtime lowering is still unverified for enum construction,
  finite switch, scalar switch, payload extraction, sequence repeat,
  projection, and compiler operations (CP-0018). Native executable
  output remains a narrow test-only structural scalar C11 slice;
  replacing that projection with backend admission tied to compiler
  proof/provenance is open work. Do not infer broad backend parity
  from the small scalar executable.
- The 21-case Profile 0.18 surface, pressure reproductions,
  imported-nominal SSA, family slices, and CP-0001 ABCD evidence are
  re-recorded at this pin in
  `evidence/campaign-20261003-profile-surface-results.json`,
  `evidence/campaign-20261003-pressure-suite-results.json`,
  `evidence/campaign-20261003-imported-nominal-ssa.json`,
  `evidence/campaign-20261003-family-results.json`,
  `evidence/cp0001-results.json`, and
  `evidence/cp0001-matrix.json`.

## Scalar-match value SSA and family campaign (2026-10-01)

At the same pin and compiler head `3f678c3` plus the scalar-match slice:

- Integer scalar `TMatch` now lowers to verified value SSA: proof
  captures per-arm typed operations, `ssa.mncs` emits `ScalarSwitch`
  terminators with pattern/default/edge verification and source
  re-parse agreement, and the suite covers five functions plus four
  corruption rejections with Stage-0 oracle agreement. See
  `evidence/campaign-20261001-scalar-match-value-ssa.json`. The
  semantic suite grows to eight proof verdicts.
- The full project/value-SSA suite repeats byte-identically
  (`a3c5a13f…`) across two independent runs.
- A first real-project campaign compiled family slices natively: two
  full-slice successes (single-module 0.17 fixture, two-module aliased
  import pair), two exact differential agreements (bare-`use`
  rejection, MNE140 non-exhaustive with identical span), and three
  classified gaps: host-intrinsic callee model (new CP-0019), nested
  match parsing (CP-0011 instance with a real reproducer), and the
  1024-byte per-source ceiling (known CP-0001). See
  `evidence/campaign-20261001-family-slices.json`.
- Backend pressure is now precise: main's proof-bound C11 adapter
  admits only Constant/Call (new CP-0018, validated against the
  adapter's own passing unit tests), blocking migration off the
  unattested structural projection.

The highest-leverage next slice is `TProj` record-projection value SSA
(the empirically confirmed next boundary), the host-intrinsic proof
model pulled by real sources, and backend lowering for the verified
switch/construct operations. (Update: the TProj slice has since landed;
see the next section.)

## Record-projection value SSA and match-env fix (2026-10-01)

On top of the scalar-match head:

- `TProj` record projection now lowers to verified value SSA: proof
  carries the declaration field index, `ssa.mncs` emits kind-8
  instructions with declared-field, nominal-identity, and source
  re-parse verification, and the suite covers five functions (including
  projections in match arms and over let-bound records) plus six
  corruption rejections with Stage-0 oracle agreement. The semantic
  suite grows to nine proof verdicts.
- Landing it exposed a latent shared bug (new CP-0020, resolved in the
  same slice): match lowering reversed block-parameter environments,
  misaligning arm-to-join edge arguments for heterogeneous-type envs.
  The fix at the shared prepare step is covered by mixed-type
  regressions in the finite/enum and scalar-match sections.
- The full project/value-SSA suite repeats byte-identically
  (`da46e431…`, 40 requests) across the local runner and the
  Environment value-SSA obligation. Family campaign round 2 repeats
  the round-1 stage classifications on all 9 slices; remaining real-
  source blocks are the host-intrinsic callee model (CP-0019), nested
  match parsing (CP-0011 instance), and the 1024-byte ceiling (CP-0001).

The highest-leverage next slice is the CP-0019 host-intrinsic proof
model pulled by real sources (`structured_write`, `structured_read`,
`fs_entry_kind_at`), then backend lowering for the verified operations.
(Update: the CP-0019 slice has since landed; see the next section.)

## Compiler-operation proof and value SSA (2026-10-02)

On top of the projection head:

- Compiler operations now prove and lower to verified value SSA:
  parser reservation of the five operation names with parse-time
  arity, proof in expected → authority → operand order twinning
  MNE257/MNE258/MNE261/MNE262 and MNE287–MNE297 as obligation kinds
  53–67, exactly-one-effect plus declared-capability authorization,
  canonical `mncs:0.2:operation::<name>` identities on typed
  `TOp.TOp` nodes, kind-9 SSA instructions with slot/arity/identity/
  source/result/input verification, and the ungated exact→view borrow
  at name elaboration plus the let/return backstop. The semantic
  suite grows to 131 cases plus eleven proof verdicts; the project
  suite gains an operation section (5 verified functions, 7
  corruption rejections, unauthorized twin kind 53 / MNE257 at
  identical spans) with a focused `operations` runner mode.
- The full project/value-SSA suite establishes a new canonical digest
  (`2fb25256…`, 42 requests). Both pulling real sources now reach
  verified SSA unmodified: `structured-artifact.mncs` (632 bytes, 2
  functions) and `fs-metadata.mncs` (708 bytes, 3 functions). See
  `evidence/campaign-20261002-project-results.json` and
  `evidence/campaign-20261002-family-results.json`.
- Explicitly remaining: backend/runtime lowering for kind-9
  operations (no executable operation support claimed),
  introduced-profile gating below 0.12/0.18 (native accepts where
  Stage-0 reports MNP204/MNP217), and deferred view behaviors (widen,
  narrow, non-name borrow sites). See the CP-0019 pressure record.

The highest-leverage next slice is now nested-match parsing (CP-0011
instance, real reproducer `cre1-evidence-combine.mncs`) or the
per-source size ceiling (CP-0001, real 4364-byte `cli-outcome`
source), then backend lowering for the verified operations.

## Performance, scalability, and memory campaign (2026-10-03)

On top of the CP-0011/CP-0001 self-ingestion head (`0e63ec1`):

- CP-0021 resolved without raising any bound: `decl`, `flow`, `ssa`,
  and `project` consume logical pages with global positions
  (pages-outer fuel; flat pages plus `ProjectSource` descriptors with
  a `descriptors_cover` partition check). The full ABCD matrix proves
  stride-variant transport to 558,004 bytes with precise feature-gap
  spans and zero ceiling rows; `synthetic-2049` runs the whole native
  pipeline green. See `evidence/cp0001-matrix.json`.
- CP-0003 hot loops restructured with conditional chunk-chains (total
  fuel unchanged, existing `if` + `iterate` idiom only): 1-function
  reference steps 901811→352537, 10-function exhausted→returned. The
  same reduction relieved every CP-0023 backend-arena exhaustion on
  the milestones (workload half done; the 16 MiB backend cap itself
  is unchanged and still unconfigurable).
- CP-0022 decided: the version-neutral scanner stays — bare `!` is
  kind 7 + MNL002 with the `decl` 0.13-gated reinterpretation, no
  `not` kind. Parse/proof conformance stands via CP-0015.
- Full revalidation green: project closure 1,362.503 s (43 requests,
  23 native steps, prior pin 1,406.305 s), `test_decl.py` digest
  byte-identical, `test_sem.py` 170+11 twice-identical, staged
  imported-nominal chain verified through the new ABI, family
  campaign 9/9 with oracle agreement. Reference-backend merge
  section 10/10 green; the 8M-step enum-constructor exhaustion there
  reproduces identically on the base tree (pre-existing, not a
  regression).
- Measured resource contracts: ~4.5 GB per Cranelift project probe,
  ~3.4 GB per ssa probe (fixed images, unchanged by the refactor),
  ~15 GB peak for the 4-probe `match-ssa` overlap — the suite's
  first OOM victim under multi-agent contention. Vector/SIMD
  audited: semantic foundation sound, but every backend scalarizes
  and bulk load / find-first primitives are missing, so no
  compiler vectorization was profitable in this campaign. See
  `evidence/campaign-20261003-perf.json` and the CP-0023 record.

The highest-leverage next slice is the CP-0015 grammar gaps tier D
now names with precise spans (generics `<` first-failures), CP-0002
non-ASCII admission, probe-image/topology memory (backend
ownership), and the CP-0018 backend-lowering path.

## Historical evidence-backed campaign state (2026-09-25)

The lock pinned Rust Stage-0 revision
`709ba00810099e6965bb47dec14ed19e9e1ae6f8` at source Profile 0.18. All seven
compiler modules declared 0.18. The refreshed [parity matrix](evidence/PARITY.md)
and machine-readable [ledger](evidence/parity-ledger.json) superseded this
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
  the separate 103-case semantic proof twin plus seven verifier verdicts also
  passes twice at this pin. Native parse/proof now accepts the CP-0014
  bool-payload project; value SSA explicitly defers finite matches and enum
  construction with named failure kinds.
- CP-0017 is compiler semantic drift, not language pressure. Its two minimal
  poisoned-result cases now match Rust's ordered diagnostics and spans in
  repeated native runs; the full 103-case semantic twin plus seven verifier
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

The 2026-09-25 highest-leverage next slice was a current-profile
project-source path: version-aware syntax, module/import resolution, and a
source representation beyond the legacy 256-byte unit. That path has since
been built (`project.mncs`, one-parse fact reuse, imported identity into
verified SSA); the current next slice is stated above. The standing caution
remains: record-sequence acceptance in Stage-0 is not evidence that the
native compiler has a project collection or module model.

Architecture may already use fact/obligation boundaries even when evaluation is single-threaded and uncached.

`mncs-environment` is the preferred development, session, and provider-composition layer for this roadmap's verification work (canonical entry, selected-artifact caching, durable sessions, obligation execution, Store-backed artifact retention). It must never become a semantic bootstrap dependency of the standalone compiler kernel.

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

A compiler generation developed here is promoted into `mncs-language` as the
canonical compiler once it can reliably compile the MNCS family, self-host
through Stage-1→Stage-2, maintain semantic/backend correctness, reproduce
itself deterministically, and satisfy sufficient conformance, diagnostic, and
recovery evidence. Self-hosting alone is not sufficient for promotion, but
neither is full architecture maturity required: the Phase 4–8 capabilities
(incremental graph reuse, persistent coordinator, Store/Fabric integration,
verified specialization, adaptive optimization) may continue advancing across
later compiler generations. The Rust compiler then becomes frozen
Stage-0/reference rather than an independently evolving compiler.

The MNCS compiler does not need to win every microbenchmark; it must be the better canonical architecture without sacrificing trustworthiness.
