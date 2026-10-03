# CP-0001 — Whole compiler sources exceed bounded sequence capacity

Status: resolved in mncs-compiler (representation + pipeline scale; revalidated by the committed suite)

> Historical description and reproductions below preserve the original
> finding. See the current reconciliation at the end of this file and the
> pressure index for live status.

stdlib-runtime, tooling. Severity: high for whole-module compilation
(was blocking at 64 bytes). Frequency: pervasive. Upstream tracking:
profile 0.13 raised ceilings (see re-evaluation); no dedicated storage
API yet.

## Workload and reproduction

An immutable compiler source snapshot must retain arbitrary module bytes and
support stable byte indexing. The first lexer uses `[byte; up_to 64]`.
`repro/source-65.mncs` changes only the capacity to 65. Run
`python3 tools/test_pressure.py` after bootstrap: Stage-0 emits MNE105 (a generic
supported-types message, not a direct capacity diagnostic). The executable
frontend tests also pass 65 bytes to the 64-byte API and require
`invalid_request`, proving that oversized input is not silently truncated.
The pinned `mncs-model/src/body.rs` sets `MAX_SEQUENCE_BOUND` to 64.

## Workaround and desired capability

The kernel accepts exact, immutable, at-most-64-byte sources. The caller iterates
`next_token` results; no host language implements storage or scanning for a
production compiler. This cannot read even this compiler's own source modules.
Chunking is possible but requires token continuation across arbitrary boundaries,
shared backing storage, and bounded-resource policies; it is deliberately not
pretended to be equivalent to a whole-module source API.

The desired capability is explicit owned/shared source storage with byte views,
deterministic indexing, and resource limits independent of one tiny static
sequence ceiling. Investigate existing nested sequences and runtime facilities
before prescribing a new language feature. Record sequences *do* elaborate:
`tests/fixtures/token-sequence.mncs` is a positive control, not a missing-feature
report. Growable token/AST arenas, allocation, hashing, and stable persistent
identity remain unimplemented and unmeasured; this report does not establish
that all of them are absent.

## Impact and ownership

- Correctness/safety: oversized input is rejected at the ABI; no truncation.
- Runtime/memory: current working source is bounded; larger-source cost unknown.
- Compiler performance: no large-source benchmark is possible through this API.
- Determinism: exact byte values are reproducible, but are not content hashes.
- Complexity: chunked ownership, continuation, and indexing would spread through
  lexer, parser, diagnostics, and snapshots.
- Likely owners: language/resource model and stdlib/runtime storage; tooling for
  a precise capacity diagnostic. Compiler architecture must choose storage units.

## Re-evaluation (Stage-0 `a7a8c05`, 2026-09-12): partially resolved

Probed on the current pin (`mncs abi`, profile 0.13): `[byte; up_to 65]`
and `[byte; up_to 1024]` elaborate; `[byte; up_to 1025]` is refused
(MNE105, then MNE161 cascades). Counted_iteration bounds rose the same
way (1..=1024, MNE142 past it). The ceiling is now 16x higher, and the
0.10-profile `repro/source-65.mncs` still yields MNE105
(`tools/test_pressure.py` green), so old-profile behavior is preserved.

What remains: 1024 bytes still cannot hold real modules
(`decl.mncs` is ~267KB); there is still no owned/shared source storage
with views, no growable arenas, no stable content identity. The
four-chunk laboratory interface (256B units) is unchanged. Severity
drops from blocking to high: chunked compilation can now span 4KB per
unit-shape change, but whole-module compilation still needs the storage
API this pressure originally asked for.

## Current reconciliation (2026-09-26)

Current Stage-0 (`b0f3e644`) accepts the preserved 65-byte source. The old
64-byte language limit is stale. The native `compile_project` API now receives
exact per-file byte sequences separately from source metadata; its project
probe crosses the old 256-byte boundary and exercises one 1,024-byte source.
The representation has no four-chunk compatibility path. Current Profile 0.18
caps the generic source length at 1,024 and the snapshot at 64 modules, so this
does not yet ingest this repository's 268 KB declaration module. The remaining
gap is compiler source representation/project scale; it is not a request to
raise a generic language bound. See
[`evidence/campaign-20260926-project-results.json`](../evidence/campaign-20260926-project-results.json).

The refreshed two-module project run contains 1,103 bytes across two sources
and uses the current 1,024-byte per-source generic ceiling. The current
`src/compiler/decl.mncs` is 275,918 bytes, so full compiler-source ingestion
remains unimplemented. The Profile 0.10 65-byte reproducer still reports MNE105
as the old-profile control; that does not contradict the larger current-profile
project representation.

## Current reconciliation (2026-10-03)

Resolved as MNCS-native source representation; full-pipeline scale remains
open under CP-0021. This is not a bigger hardcoded limit: the 1,024-byte
single-view ceiling is unchanged, and no Store-backed kernel semantics or
host-side tokenizing/parsing was introduced.

Stage-0 (`a3ac17df`, profile 0.18) still caps one bounded view at 1,024
bytes, and that ceiling is no longer the compiler's source limit. The
compiler now owns a standalone logical immutable source
(`src/compiler/source.mncs`): fixed-stride page compositions of bounded
views (stride 1..1024, at most 1,024 pages, 1 MiB per source) with
canonical-form validation (codes 0-6), O(1) global byte access by
quotient/remainder, stride-stable spans, FNV-1a content identity, global
line/column rendering, and static resource accounting — all executing in
MNCS, with the host transporting page bytes only. The lexer, segment
cursor surface, header parser, and kernel shape/evidence entry points
consume the logical abstraction (`next_token_global`,
`significant_global`, the `lex_tokens_from` batch traversal,
`header_global`, `token_shape_global`, `lex_step_global`) with
cross-boundary token continuation, exact EOF, an 8,192-step per-call scan
budget (trivia prefixes, diagnostic 6, continue; overlong significant
tokens refuse as diagnostic 5), and header fuel accounting (code 9 past 64
steps). Single-view entry points are untouched.

Proof (`tools/test_cp0001.py`, tiers A-D; evidence
`.build/cp0001-results.json` / `.build/cp0001-matrix.json`, reproduced by
one command — see below):

- Tier A: 738 strided small-input cases (fixed samples plus seeded fuzz)
  with full-token oracle equality at strides 1-256, single/batch/
  significant-walk equivalence, cross-stride fingerprint stability, and
  single-view triple equivalence including host-driven `lex_step_global`
  accumulation equal to whole-source `lex_summary`.
- Tier B: boundary grid (lengths 0-4,097; exact page fills, off-by-ones,
  straddling tokens, EOF-on-boundary) with 114 batch/single differential
  cases, plus two-page splits at every position of two samples (42
  canonical halves lexed identically, 40 non-canonical halves rejected
  with precise codes).
- Tier C: every validation code (strides, over-length/short/empty pages,
  count and terminal mismatches), order/duplication transport faults
  detected by identity, oversized indexes, empty sources, trivia-prefix
  and overlong budget edges, header facts including the fuel code,
  line/column rendering, and accounting checks.
- Tier D self-host-distance matrix (actual working-tree bytes; the
  solution itself grew parser/kernel/lexer, which the matrix measures):

| milestone | bytes | transport/lex/parse/proof/CFG/SSA | first failure | pressure |
| --- | --- | --- | --- | --- |
| parser | 5,950 | 1/1/0/0/0/0 | parse [1024,1025] | CP-0021 |
| kernel | 6,297 | 1/0/0/0/0/0 | lex [1262,1263] | CP-0022 |
| cli-outcome | 4,364 | 1/0/0/0/0/0 | lex [35,36] admission | CP-0002 |
| lexer | 39,629 | 1/0/0/0/0/0 | lex [14102,14103] | CP-0022 |
| flow | 20,980 | 1/0/0/0/0/0 | lex [5504,5505] | CP-0022 |
| project | 63,283 | 1/0/0/0/0/0 | lex [5115,5116] | CP-0022 |
| ssa | 196,528 | 1/0/0/0/0/0 | lex [12536,12537] | CP-0022 |
| decl | 490,607 | 1/0/0/0/0/0 | lex [21434,21435] | CP-0022 |
| synthetic-1024 | 1,024 | 1/1/1/1/1/1 | none | none |

Every milestone lexes stride-invariant (strides min/256/1024) with exact
global spans modulo the classified CP-0022 `not` pairs (2-267 per file;
the comparator raises on any second divergence), exact EOF and coverage,
cross-stride identity, and header facts on all ASCII milestones
(`outcome.mncs` is byte-exact too — `oracle_exact` at every stride — but
not admitted: one em dash, header code 8, CP-0002). Oracle maximum
significant token is 45 bytes and maximum trivia 88 bytes across all
milestones, against the 8,192-step budget. The synthetic two-module
project at the 1,024-byte ceiling runs the full native pipeline green
with oracle agreement (valid, SSA present). No milestone shows a
non-resolution oracle diagnostic: Stage-0 parses every file (the lone
MNE173 per file is the single-file-elaborate resolver artifact, split
out in the matrix script, not a file defect).

First size-free gaps, classified as progress: bare `!` diverges lexically
by deliberate version-neutral-scanner design (native `(7, MNL002)` vs
oracle `not`; CP-0022); `outcome.mncs` carries one non-ASCII em dash the
ASCII-only frontend rejects at admission (CP-0002). Parse/proof/CFG/
verified-SSA past 1,024 bytes need declaration-stage logical-source fuel
(CP-0021); the matrix records first-failure span [1024, 1025] there.

Observed digests (cranelift retained sessions): tiers ABC `2789f7e3…bda619`
(136,301 requests, 95,895 tokens); tier D `8081aec1…d6f6f` (890 requests,
738,393 tokens, 1,107 s). One cranelift session-thread panic
(`TryFromIntError(NegOverflow)`) appeared on stderr during tier D; every
asserted call returned normally and the run exited 0 — unattributed,
pending a clean rerun.

Reproduce: `python3 tools/test_cp0001.py` (tiers ABCD; `MNCS_CP0001_TIERS`
selects; `MNCS_CP0001_SMOKE=1` for a fast cut).

## Resolution (2026-10-03, performance campaign)

CP-0021 landed: `decl`/`flow`/`ssa`/`project` consume logical pages
with global positions (pages-outer fuel), so the pipeline scale the
matrix left open is now proven. Full ABCD on the campaign tree
(`9ff21e6f…9501e`, 137,299 requests, 944,444 tokens, 1,197 s): all 8
real milestones transport as stride-variant logical pages; the 558,004-byte
`decl.mncs` and 217,526-byte `ssa.mncs` reach precise feature-gap spans
(CP-0015 `<`) instead of ceiling blocks; `synthetic-1024` and
`synthetic-2049` run the whole native pipeline green with oracle
agreement and stride invariance. The matrix records zero CP-0021 and
zero backend-arena rows. Single-view spellings survive only as the
canonical compositions in unit suites, not as pipeline inputs. The
earlier unattributed session-thread panic did not reappear on the
campaign's reruns.
