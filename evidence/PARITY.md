# Current Rust-vs-MNCS compiler parity

Reference: `mncs-language` Stage-0 revision
`709ba00810099e6965bb47dec14ed19e9e1ae6f8`, source profile 0.18, as pinned
in [`mncs-language.lock.json`](../mncs-language.lock.json). The detailed,
machine-readable ledger is [`parity-ledger.json`](parity-ledger.json).

This ledger replaces the September 12 matrix as the current parity picture.
Historical evidence remains in the older result files and pressure finding
histories, but claims based only on earlier Stage-0 revisions are not current
parity evidence. A positive current claim names executable evidence. Repeated
native results establish determinism; Rust Stage-0 differential or an explicit
invariant is still required for correctness.

## Status meanings

- **Parity**: executable comparison covers the stated behavior and agrees.
- **Parity with explicit exceptions**: the compared domain agrees; listed
  exceptions are reproduced and bounded.
- **Partial**: a measured slice agrees, with material behavior outside scope.
- **Absent**: no native compiler implementation exists for the behavior.
- **Blocked by reproduced pressure**: an executable counterexample prevents
  the corresponding parity claim.
- **Intentionally deferred**: the capability is outside this campaign slice.

## Current slice

The native compiler adds control-flow lowering after declaration and semantic
proof. It emits branch, jump, return, and failure blocks, retains each proved
typed postfix operation under its source expression and block, verifies block
targets, and diagnoses joins with no reachable predecessor. Four Profile 0.18
programs are compared with current Rust diagnostics/spans; two positive cases
also match Rust SSA branch/return shape. Native requests are repeated and
compared byte-for-byte, and the test asserts that every proof operation is
attached once in source order. See [`flow-results.json`](flow-results.json),
the current-profile production call [`flow-call-current.json`](flow-call-current.json),
and the replayable [`tools/test_flow.py`](../tools/test_flow.py).

This is a partial compiler vertical, not a native executable backend. The
blocks still lack Rust-equivalent SSA values, value merges, resolved callable
identities, and target code. Parsing and proof remain bounded to four 64-byte
source chunks.

## Parity matrix

| Area | Current MNCS compiler state | Status | Current evidence / boundary |
| --- | --- | --- | --- |
| Stage-0/profile pin | Locked to Rust revision `709ba00`, profile 0.18; all seven compiler modules declare 0.18 | Parity with explicit exceptions | [`mncs-language.lock.json`](../mncs-language.lock.json), [`flow-abi-current.json`](flow-abi-current.json); CP-0015 syntax exceptions remain |
| Bounded byte equality | `kernel.same_source` agrees on empty, equal, unequal, and prefix-related fixtures | Parity | [`frontend-results.json`](frontend-results.json), [`tools/test_frontend.py`](../tools/test_frontend.py) |
| Source loading | Four `[byte; up_to 64]` chunks feed declaration and flow; no project snapshot/source-unit graph | Partial | [`flow-abi-current.json`](flow-abi-current.json), [`decl-results.json`](decl-results.json); the 256-byte compiler interface is not a language ceiling |
| Lexing, spans, line/column | Bounded ASCII byte/token/span path agrees over the current frontend corpus | Parity with explicit exceptions | [`frontend-results.json`](frontend-results.json); Unicode remains rejected by the native frontend (CP-0002), and line/column is byte-oriented |
| Header/module syntax | Existing header parser on its supported subset | Partial | Current-profile syntax probes in `pressure-current-results.json`; leading-comment envelope remains MNE002 (CP-0006) |
| Declaration/expression parsing | Five sampled Profile 0.18 forms are rejected by native `decl.parse_unit` | Blocked by reproduced pressure | [`profile-surface-results.json`](profile-surface-results.json), CP-0015: `!`, negative atoms, repeat literals, `next` fields, and integer match |
| Module resolution/imports | The MNCS compiler has no source-project resolver; the Rust probe loads linked compiler modules | Absent | `tools/stage0-probe/src/main.rs` is test transport, not native module-resolution evidence |
| Symbol collection/name resolution | Bounded single-unit function symbols, calls, locals, records, and projections pass the tested checker cases | Partial | [`decl-results.json`](decl-results.json); imports and whole-project name/type resolution remain absent |
| Types, effects, capabilities, obligations | `decl.prove_unit` runs bounded type/effect/capability proof and typed-operation verification | Partial | [`sem-results.json`](sem-results.json): 49 cases plus five verifier verdicts pass twice; contracts and supported grammar remain narrower than Rust |
| Typed stack IR | Typed operations carry type/span facts and pass the depth/type-stack verifier | Partial | [`sem-results.json`](sem-results.json); CP-0017 recovery now matches Rust; no equivalence to Rust body/SSA IR is claimed |
| Control-flow blocks and reachability | Native branch/jump/return/fail graph, target checks, typed-op attachment, and unreachable-join MNB038 | Partial | [`flow-results.json`](flow-results.json): four cases, 20 requests; exact diagnostic code/spans and positive Rust SSA branch/return shape |
| Rust body/SSA contract | No native value graph, merge values, resolved callable identities, or SSA lowering | Absent | Current flow differential compares block shape only; its typed postfix operations are not SSA values |
| Imported project resolution | Native `use` syntax is parsed, but source-module discovery and imported name/type resolution are absent | Absent | CP-0008's Rust producer/consumer acceptance in [`pressure-current-results.json`](pressure-current-results.json) does not establish a native resolver |
| Process lifecycle and resource semantics | Native parity for Profile 0.18 `process_start/observe/cancel/reap` operations and resource contracts was not exercised | Intentionally deferred | Outside the current compiler workload and flow slice; current Rust authority is in `mncs-language` Profile 0.18 runtime/compiler sources |
| Generic collection/query library | Native compilation of the current generic sequence/query library surface was not exercised | Intentionally deferred | The tested CFG workload does not require these operations; this row makes no native parity claim |
| Structured artifact contracts | The tested native compiler path does not project its output through typed `structured_read/write` or `structured_digest` contracts | Intentionally deferred | No current workload requires them; Stage-0 callable ABI identity is captured in [`flow-abi-current.json`](flow-abi-current.json) |
| Backend/lowering to executable | No MNCS-owned target backend or emitted executable | Absent | Stage-0 execution/artifacts are not native compiler output |
| Artifacts and callable identity | Stage-0 produces artifacts for executing the MNCS compiler; native compiler output identity is not implemented | Absent | CP-0016's old Stage-0 artifact is recorded only as transport/runtime evidence |
| Unicode frontend | Current Rust accepts the Unicode module probe; native source admission is ASCII-only | Blocked by reproduced pressure | CP-0002 re-run in [`pressure-current-results.json`](pressure-current-results.json) |
| Diagnostics | Current CFG MNB038 code/span agrees; semantic FAIL codes/spans match on the proof corpus; full native messages/recovery are not modeled | Partial | [`flow-results.json`](flow-results.json), [`sem-results.json`](sem-results.json); CP-0006 remains an envelope exception |
| Determinism | Native semantic and flow requests repeat identically; Rust differential/invariants establish correctness only on the tested domains | Parity with explicit exceptions | [`sem-results.json`](sem-results.json), [`flow-results.json`](flow-results.json); repetition alone is not correctness evidence |
| Reference compile cost | Current linked kernel semantic/HIR/SSA output repeats byte-identically in two Stage-0 runs; 213 CMP301 obligations remain | Partial | [`compile-results.json`](compile-results.json); this is reference compile/artifact cost, not native cost, proof closure, or peak memory |
| Self-hosting | No Stage-1 → Stage-2 build or semantic comparison exists | Intentionally deferred | Stage-0 still loads and executes the MNCS implementation; this campaign does not claim self-hosting |
| Incrementality, project snapshots, cache invalidation | Not implemented in the native compiler slice | Intentionally deferred | Architecture documents remain targets, not current behavior |

## Reproduced pressure summary

- CP-0014 is resolved upstream: current Stage-0 accepts the bool-payload
  reproducer. The same nested nominal collision then found in compiler work was
  fixed generically in MNCS runtime record validation (CP-0016).
- CP-0015 is current compiler architecture pressure: Stage-0 accepts the five
  Profile 0.18 examples, while native `decl.parse_unit` rejects all five with
  spans recorded in `profile-surface-results.json`.
- CP-0001's historical 64-byte language limit is stale; current Stage-0 accepts
  the preserved 65-byte probe. The four-chunk 256-byte compiler unit remains an
  architecture limit and is not evidence for a larger fixed language bound.
- CP-0003 (unbounded `while`) and CP-0006 (leading-comment envelope MNE002)
  still reproduce. This campaign keeps bounded scans and records the envelope
  limitation; neither was required to implement the CFG slice.
- CP-0008 through CP-0013 are rechecked against current Stage-0 in
  `pressure-current-results.json`; profile support is distinct from native
  compiler parser support, which remains blocked by CP-0015.

## Current-reference capabilities outside this compiler workload

The 0.18 Rust reference now also defines retained process lifecycle and
resource contracts, type-directed structured artifact read/write and digest
projection, and generic bounded-sequence operations in its linked libraries.
The current compiler campaign did not exercise native compilation of these
surfaces, so the ledger marks them intentionally deferred for this slice. No
language pressure was filed: the compiler workload did not require them and they did not block
the control-flow parity step. If a real compiler input requires one, reduce it
to a compiler reproducer and classify it before changing the owning layer.

The single highest-leverage next parity obstacle is a compiler-owned project
source model and resolver that can feed current-profile modules larger than the
legacy 256-byte unit into parsing, semantic checking, typed IR, and the new CFG
pass. CP-0015's version-aware grammar is the immediate parser blocker inside
that source pipeline; neither pressure is a language feature request.
