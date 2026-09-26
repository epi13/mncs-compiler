# Rust-vs-MNCS Parity Matrix

Reference: Rust Stage-0 at pin `a7a8c05` (`mncs-language.lock.json`).
MNCS side: `src/compiler/*.mncs` executed on the pinned reference
interpreter unless noted. Status scale: absent / scaffolded / partial /
substantial / parity / parity-with-exceptions. Every non-absent claim
names its evidence; blocked rows name the blocker instead of guessing.

## How to read this

- "Oracle" = pinned Rust behavior observed through the probe's `oracle`
  (parse) and `elaborate` channels and `mncs abi` diagnostics.
- Twin-run digest equality is required by every suite; it proves
  determinism, not correctness. Correctness comes from oracle comparison.
- Rows marked **blocked (CP-0014)** last ran green at the previous pin
  (`6906d0b`); their code is migrated to 0.13 and parse-checked, but
  elaboration proof awaits the upstream bool-payload fix.

## Frontend

| Area | Rust (reference) | MNCS | Evidence |
| --- | --- | --- | --- |
| Bounded source loading (bytes, sentinel, ASCII gate, spans) | full | **substantial**: ≤64B kernel + 4-chunk ≤256B segments; ASCII-only; no whole-module API | `tools/test_frontend.py`, `tools/test_segment.py` |
| ASCII lexical analysis (tokens, keywords, operators, comments, diagnostics) | full | **parity with known exceptions**: full kind/span/diagnostic parity on 196-sample corpus; exceptions: profile-gated `!`→`not` (CP-0015), Unicode policy (CP-0002) | frontend + segment suites; `evidence/frontend-results-pre-campaign.json` |
| Unicode lexing | full (`char` classes, UTF-8 widths) | **absent**: whole-source ASCII gate, span-level rejection | CP-0002 (decoder half resolved upstream, tables missing) |
| Header/module-declaration parsing | full | **parity with known exceptions**: span parity incl. explicit-span envelope cases; profile/envelope obligations deferred; MNP008 expectation updated at re-pin | frontend suite (18 header cases) |
| Declaration/expression parsing | full | **partial, blocked (CP-0014)**: 22-pos/16-neg structural + first-error-span differential at old pin; 0.13 migration parse-checked; 0.13-syntax gaps open (CP-0015) | `evidence/decl-results-pre-campaign.json` (old pin); oracle parse-check |
| AST construction fidelity | full | **partial, blocked**: structural parity with projection canonicalization; deferred shapes fail explicitly | DECL.md corpus notes |
| Byte source spans | full | **parity**: exact spans on every compared token/decl/error | all suites |
| Line/column rendering | full (line/col in every span) | **parity with known exceptions**: `source.line_col` agrees byte-exactly on ASCII (oracle-anchored per token + strided exhaustive); non-ASCII columns out of scope (CP-0002) | frontend suite |
| Lexical/parse diagnostics (codes + spans) | full | **substantial**: MNL/MNP codes + spans compared; full message text not compared; recovery not modeled | frontend + segment suites |

## Semantics

| Area | Rust (reference) | MNCS | Evidence |
| --- | --- | --- | --- |
| Module resolution / `use` imports | full (file/library/bundle resolver) | **scaffolded**: probe-side `Sources` map only; no MNCS implementation | probe `main.rs` |
| Symbol tables (fn names, duplicates) | full | **partial, blocked (CP-0014)**: collection + duplicate detection at old pin | decl `check_unit` stage-1 verdicts (old pin) |
| Name resolution (resolve/span walk) | full | **partial, blocked**: two-stack walk at old pin | `check_unit` stage-2 verdicts (old pin) |
| Types / signatures / contracts / effects | full (MNE105/MNE110/MNE111/...) | **partial, blocked**: signature facts + per-fn gates at old pin | `evidence/sem-results-pre-campaign.json` (old pin, 49 cases) |
| Expression proof + typed lowering | full elaboration | **partial, blocked**: bidirectional proof, fused TOp lowering, type-stack verifier; FAIL parity at old pin | SEM.md; adversarial constructor verdicts |
| Obligation model (FAIL vs UNKNOWN) | full | **partial, blocked**: FAIL↔diagnostic parity; UNKNOWN classes pinned present-not-diagnostic | sem suite unknowns assertions |
| Control-flow validation (reachability, joins) | full (MNB038 etc.) | **absent**: both-return joins elaborate; lowering codes not modeled | SEM.md scope limits |
| Call authority / capabilities | full (MNE134 + capability dimension) | **partial, blocked**: effect cover at calls; capability dimension signature-level only | SEM.md scope limits |
| Generics / constants | full (inference, specialization) | **absent** in compiler code (language has 0.13 inference) | — |
| Ownership / borrow checks | full | **absent** | — |

## Lowering, IR, backends

| Area | Rust (reference) | MNCS | Evidence |
| --- | --- | --- | --- |
| Stack IR + depth verifier | n/a (Rust lowers directly) | **partial, blocked**: FlatOp postfix IR + self-check at old pin | decl suite `ir_ok` verdicts |
| Typed IR + verifier | n/a | **partial, blocked**: TOp + shape verifier at old pin | sem suite intrinsic verdicts |
| HIR / SSA / optimization | full | **absent** | — |
| Backend dispatch (C11/LLVM/Cranelift/WASM/native) | full (5 executable backends) | **absent** | — |
| Interpreter/JIT interfaces | full (reference + SSA executors) | **absent** (consumer only, via probe) | — |
| Executable emission / artifacts | full | **absent** | — |
| Package / project handling | full (workspace, bundles) | **absent** | — |
| Incremental / snapshots / caching | full identities (RFC 0002 target) | **absent** (fact-key concepts documented only) | ARCHITECTURE.md |

## Cross-cutting

| Area | Rust (reference) | MNCS | Evidence |
| --- | --- | --- | --- |
| Deterministic compilation | full | **substantial**: twin-run digest equality in every suite; no cross-machine runs yet | all `*-results.json` |
| Error recovery | full recovery + cascades | **partial**: first-error spans only; no recovery model | NEG cases (first-span equality) |
| Malformed-input behavior | full (fuzz corpora) | **substantial** (frontend/segment fuzz-ish corpora) / **partial, blocked** (decl/sem corpora at old pin) | 196 + 41 samples; DECL.md/SEM.md corpora |
| CLI behavior / exit codes | full research multitool | **absent**: no MNCS driver; Python transport is temporary (CP-0005) | — |
| Diagnostic formatting (human text) | full | **absent**: structured facts only, no rendering | ARCHITECTURE.md §8 (target) |
| Compile-cost evidence | baseline | **scaffolded**: interpreter step counts + artifact sizes recorded; no memory/native timing | evidence `*_results.json`, CP-0007 |
| Conformance corpora | full (`mncs-harness` scale) | **partial**: per-suite corpora; no harness integration | tools/test_*.py |
| Self-hosting | n/a | **absent**: not self-hosting; Stage-0 executes all MNCS code | README status |

## Biggest gaps in order

1. CP-0014 (upstream): unblocks all decl/sem rows at once.
2. CP-0015 (compiler architecture): version-aware parsing for 0.13
   syntax; without it the compiler cannot parse modern sources.
3. Whole-module sources (CP-0001 remainder): 256B units cap every
   downstream stage; capacity growth is the next interface change.
4. Backend lowering (any): no MNCS codegen exists; typed IR → body
   blocks (with reachability) is the staged next IR step per SEM.md.
5. Module resolution + multi-module trees in MNCS (enabled by CP-0008's
   fix; staged post-CP-0014): prerequisite for splitting `decl.mncs`.
6. Driver/CLI in MNCS (blocked on file/process APIs + probe parity).
