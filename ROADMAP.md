# Roadmap

<!-- MNCS:generated:begin -->
## Evidence-bound roadmap

- **complete** — Declared ambient projection surfaces satisfy their contract (`mncs-compiler:projection-conformance`)
<!-- MNCS:generated:end -->

This roadmap separates compiler succession from compiler research. The project should earn each stage with evidence rather than treating self-hosting alone as completion.

## MNCS Native Application Readiness (2026-10-14)

This milestone is separate from compiler succession. `mncs-test` remains the
first consumer workload. The current selected application uses nominal records,
finite payloads, bounded sequences, branching and matches, generic
specializations, typed failures, and explicit host effects.

- **Gate A — passed for the selected workload.** Compiler commit
  `24dda63ae59e0505969c490604e14348dab1f7d7` emits
  `mncs.compiler.application-build/4`. The `mncs-test` readiness application
  compiles its `run` entry and two explicit generic specializations from a
  five-module closure; the canonical VM and Stage-0 project oracle agree on
  the report and typed failures. An invalid record projection returns
  `MNE162`. Evidence `evidence/native-app-readiness-gate-a.json` has SHA-256
  `b52f3a4baff2d38220900d3714a5748c9e86f47a8dc19d02f884edc6d95be3f3`.
  The artifact is `sha256:874730ef4022ccd9414127a70b4af495a9192d7770f494b0916c6fc3965ac0cd`.
  A direct `mncs.test.digest::build_digest` proof checks mixed pass/fail/skip
  aggregation, failure caps one and zero, omission counts, and all-pass
  results against Stage-0 and three canonical VM executions. Its `/4` artifact
  is `sha256:3bd4cc51f4e61ff91131f1d6d65be73615ebaf19ebc66deca75e2a0d7e3ee3df`;
  evidence `evidence/native-app-readiness-test-digest.json` has SHA-256
  `3ad5ba0758dfce1c7fed304bc3e91d37ff30b867ce244f144b98304ab025d694`.
  Cold build measured 0.628 s / 0.590 CPU / 65.0 MiB RSS; Stage-0 measured
  0.354 s; canonical VM calls measured 0.210–0.212 s.
  A second proof compiles and executes the effectful
  `mncs.test.digest::digest_app` from Test and Stdlib sources. Its verified
  interface accepts `ApplicationContext`, returns `ApplicationExit`, and
  invokes structured read/write through an admitted `test_artifact` host
  capability. `/4` dispatch rows bind both calls to the canonical capability
  identity and alias, with instruction and source-operation identities;
  the runner independently checks those rows against admitted SSA. Stage-0
  and VM results agree for all three cases. Missing capability is denied, and
  a rejected write returns typed exit 3 without publishing output. Artifact
  `sha256:da7f6609992c33975c1f02ad71f31bddafe55ca8e524e6cddedc9ac299643a3d`,
  interface `sha256:6f2f19db351c5d3c8e86c3c2ff34848fafd314cda62e97f758b364dfb5f8aee2`,
  and evidence `evidence/native-app-readiness-test-digest-app.json` (SHA-256
  `7b385f42d16bf1c4bf011fe307362b5bd6d48fccb95a98acb8ee770398679898`).
  Build measured 0.839 s / 0.620 CPU / 62.6 MiB RSS; VM cases took
  0.022–0.024 s / 0.010 CPU and 11.3–11.4 MiB RSS. Producer identities are
  Test `f9575e09ec3481c0ded52bb9e3ec72940725aff6`, Stdlib
  `6aa443cacf8c80a8ccb0fe6586b1f8c68575bf6c`, Language
  `3cfbc094f1a1770fc2409b990f3f7d8912f49bc7`, VM
  `1c19a94732c9d24029dd00e2d5af13710f481d02`, Stage-0
  `e8f8a5f8659c71b33cd32c964d4982bec1a9668e`, and release probe SHA-256
  `0c574df5c69330b2bdc753bfe744638304d6c4d7a61b80e899c78c6e6dae6827`.
  cgroup memory counters were unavailable. These application proofs do not
  establish compiler succession or full Test command parity.
- **Application VM SSA projection — verified.** Compiler commit
  `c70dc851717b623f091d844dab23eab0c810052b` serializes only the selected
  exports' verified transitive call closure, including bounded-iteration
  callees, and revalidates the projected module at the lowering boundary. The
  artifact retains the full selected-SSA identity and fingerprint as source
  provenance and adds an `application-ssa-closure/1` fingerprint for the
  executable payload. Capabilities and dispatch bindings are derived from that
  same closure. Missing or bodyless callees and attached proof relationships
  fail closed. The same `digest_app` source snapshot emits a 1,108,394-byte
  artifact versus 1,161,949 bytes in the prior artifact
  `sha256:95cf2d3e3361b7089336c82e0cd4870dfe74775547831eed90879555d494f919`
  (53,555 bytes / 4.6091% smaller). Stage-0 and VM outputs still agree across
  all three effectful cases; interface identity is unchanged. Build measured
  0.839 s / 0.620 CPU, so this establishes smaller artifacts rather than faster
  compilation. The closure fingerprint, artifact hash and size comparison are
  retained in `evidence/native-app-readiness-test-digest-app.json`. The
  self-consumption matrix is unchanged; Stage-1 and Stage-2 remain unproven.
- **Gate B — partial.** Current readiness-suite evidence
  `evidence/native-app-readiness-gate-b.json` has SHA-256
  `65106578dc52ac9ea89412cde06b7a0578e989d9101e2c4c79d0e67e9f098f03` and
  pins the same Compiler and probe identities. Cold build measured 0.461 s;
  an unchanged fresh-process request took 0.120 s after 0.120 s process
  startup. A source-only dependency edit reused the verified artifact after
  a fresh semantic check in 0.188 s. A changed root function (0.366 s) and a
  semantic dependency change (0.379 s) reran source checking, SSA verification,
  and artifact generation. Invalid source was not published; interruption
  recovery and unreferenced-module closure reuse passed. cgroup memory was
  unavailable. The real digest application repeated the six required cases:
  evidence `evidence/native-app-readiness-test-digest-incremental.json` has
  SHA-256 `bd85ae27d21b1ae6c1b99ac934d0a61273f52d04b8198727ada931744a59a161`.
  Cold/warm measured 0.633/0.246 s (2.57×); a changed root function and a
  changed `mncs.test.suite` dependency took 0.416/0.432 s and recomputed all
  three obligations. Invalid source retained `MNE162` plus two `MNE102`
  cascades. A SIGKILL at 28.5 ms left no partial cache publication; retry and
  fresh-process reuse succeeded. Fine-grained changed-source reuse remains
  unimplemented.
- **Gate C — partial.** `/4` includes the versioned
  `mncs.compiler.capability-dispatch/1` contract with verified mappings from
  canonical capability identities to the exact VM runtime keys and their
  function, instruction, operation, and source-operation identities. The
  Environment-facing provider summary preserves this contract. Its
  cold/warm/invalid, Store-input-artifact, schema, and corruption cases pass.
  A native VM runner consumed and checked the contract for `digest_app`.
  Production Test-service consumption, canonical Store RPC publication and
  readback, and Forge/Monitor/Automation receipts for this larger workload
  remain unproven.
- **Gate D — assessment recorded.**
  `docs/native-tool-conversion-readiness.md` recommends `mncs-test` as the
  first conversion target. The native suite and digest semantics execute,
  while manifest/OS handling, retained-session orchestration, and service
  persistence remain host-owned or integration work. No production host code
  is safe to retire yet.
- **Compiler Stage-0 module reuse — verified; Gate B still partial.**
  `evidence/native-app-readiness-stage0-module-cache-20261014.json` remains
  evidence for exact-input reuse of verified Stage-0 frontend Programs.
  Application builds also reuse a verified artifact across a source-only
  dependency edit after a fresh semantic check. Semantic root and dependency
  edits still rerun whole-application obligations; independently reusable
  declaration/function proofs are the next compiler-owned frontier.
- **Self-consumption is unchanged.**
  `evidence/SELF-HOST-MATRIX.json` remains authoritative. The module
  succession evidence does not advance; Stage-1 and Stage-2 remain unproven.

The earlier constant-return Forge proof remains a regression baseline. The
expanded digest application currently runs through the canonical VM harness,
not the larger Forge/Store/Monitor feedback loop.

Next work: use `/4` in the canonical `mncs-test` native runner, then prove its
retained-session and batch path. In parallel, implement exact-input verified
declaration/function reuse and repeat Gate B; only cross repository ownership
boundaries after obtaining their exact scopes. Run the representative native
application through the existing Forge, Monitor, Store, and Automation path
without creating replacement schedulers or persistence.

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

## Current evidence-backed campaign state (2026-10-04)

The lock pins Rust Stage-0 revision
`a3ac17df69e68f6373cbff336db0a572667d73da` at source Profile 0.18. All
compiler modules declare 0.18. The [parity matrix](evidence/PARITY.md) and
machine-readable [ledger](evidence/parity-ledger.json) are current at this
pin; the dated sections below are preserved as historical evidence with
their original pins and measurements.

- Backend choice is now intentional, and development got cheaper (see
  `evidence/BACKEND-NEUTRALITY-AUDIT.md`). Readiness (~93% of suite
  wall) is served by a content-addressed probe cache (149 s → 23 s
  per spawn; `test_decl` 5:01 → 0:52; repeats add bytes, not
  minutes). Backend defaults resolve through `tools/backend_policy.py`
  with explicit per-suite rationale: flow runs research-bytecode by
  default (Cranelift retention is broken there), decl/sem/project
  stay native as canaries or for execution cost, explicit
  `MNCS_PROBE_BACKEND` always wins. Evidence labels are truthful
  (`execution_mode` names the real backend) and digests semantic-only
  (identical `bbf91e03…` on Cranelift and bytecode). Real compiler
  source executes through canonical `mncs-vm` against the oracle.
  CP-0024 stays open and honest (flow joined project in scope);
  CP-0025 records the WASM execution overflow.

- Self-consumption has moved through qualified finite-match patterns,
  nested scalar-match composition, and the real source module's verifier.
  Native parsing now ingests nine compiler modules whole, including `decl`,
  `flow`, `lexer`, `ssa`, `kernel`, and `project`; `tools/test_decl.py` is
  twin-identical at 153 requests (76 POS, 58 NEG, 9 checks). Qualified pattern
  qualifiers remain through checking and proof. `source.mncs` is accepted by
  Stage-0, has a valid proof and CFG (20 functions, 119 blocks), and verifies
  all 20 functions in native Cranelift SSA (693 values, 198 instructions).
  The full module is native-only because the canonical VM reaches its 8M-step
  ceiling. The nested `lexer.punctuation` witness agrees across Stage-0
  reference, canonical VM, and Cranelift on all 192 input pairs.

  The imported enum-constructor fixture now reaches checker/proof, valid flow,
  and verified SSA for two functions; six invalid constructors match Stage-0
  diagnostic spans. The focused `tools/test_flow.py` differential also passes
  seven CFG/reachability/iteration cases on research-bytecode (39 requests,
  two identical native repetitions). These focused results do not prove the
  corresponding whole compiler modules beyond parse/signature depth.

  The current real `flow.mncs` closure is 894 pages at stride 1024. Its
  independent Stage-0 project oracle accepts 1,324 linked functions and emits
  reference SSA for 376. Native Cranelift requests at M=1024 and M=896 ended
  with child exit `-9` before a compiler response; the research-bytecode
  route later admitted both project and SSA sessions in 569 seconds, then
  produced no response for the real target during a ten-minute run at the
  standard 8M step budget before it was stopped. This is an unresolved
  execution boundary, not a native compiler rejection. A prior
  source+lexer project probe at Stage-0 `3e874f642b30` exhausted the 128 MiB
  Cranelift value arena (CP-0023); that arena cause has not been established
  for the current flow attempts. Stage-1 is not yet proven. The compact
  machine-readable self-host matrix is `evidence/SELF-HOST-MATRIX.json`.
- Capacity pressure escalates with this growth (+6.3% `decl` session
  image): the whole-compiler project session no longer survives
  Cranelift JIT finalization (single-session NegOverflow, CP-0024),
  and the `flow` session is marginal. Project-mode suites cannot run
  on Cranelift until the backend image/layout pressure is resolved;
  the reference interpreter exhausts step budgets on project modes.
  Downstream fixtures carry no generics, so proven pipeline behavior
  on them is unchanged; only session retention regressed.
- Verified value SSA covers finite enum matches, enum construction,
  scalar matches, record projection, nested matches (CP-0011, modulo
  the outer stack-temp / payload-binding residual), and compiler
  operations (CP-0019), with Stage-0 oracle agreement throughout.
  Full project closure: 43 requests in 1,362.503 s at the prior
  commit. See `evidence/campaign-20261003-project-results.json`.
- The native pipeline consumes logical paged sources past the old
  1,024-byte views (CP-0001/CP-0021): tier D proves stride-variant
  transport to 558,004 bytes with precise feature-gap spans, and the
  `synthetic-2049` anchor runs the whole pipeline green. See
  `evidence/cp0001-matrix.json` (10 rows, zero ceiling rows).
- Proof-bound backend admission lands for scalar integer arithmetic
  (CP-0018 resolved): the verified native-SSA C11 adapter admits
  `Integer` with operator/intent validation mirrored from the scalar
  backend, explicit withheld no-overflow promises, and a `u64`
  constant wire type (the old `i128` was undeserializable through the
  CLI). The two-module canary migrates off the unattested structural
  projection onto `mncs.native-scalar-ssa/1` with verifier PASS plus
  compiler/stage0 provenance; emitted execution matches the pinned
  Stage-0 oracle. See
  `evidence/campaign-20261004-native-backend-vertical.json` and
  `pressure/0018-verified-native-ssa-arithmetic.md`. Backend/runtime
  lowering is still unverified for enum construction, finite switch,
  scalar switch, payload extraction, sequence repeat, projection, and
  compiler operations; do not infer broad backend parity from the
  small scalar executable.
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
