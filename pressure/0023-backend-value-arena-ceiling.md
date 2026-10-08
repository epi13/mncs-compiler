# CP-0023 — native backend per-request value-arena ceiling

Status: open; the full project boundary remains unresolved. The recorded `source.mncs` + `lexer.mncs` workload exhausted the bounded 128 MiB Cranelift arena at the 2026-10-06 pin. Current `decl.mncs` parse/signature ingestion passes, while the latest real `flow.mncs` project target ended before a compiler result. Backend ownership remains `mncs-language`.
Found: 2026-10-03, perf campaign (CP-0021 follow-up). Stage-0 `a3ac17df`, profile `0.18`.

## Initial compiler re-probe at 64 MiB (2026-10-06)

The historical workload relief below still stands for its recorded compiler
tree. Compiler growth reopened this backend pressure: with Stage-0
`a12ce8e55287ce74a7ff51fee30ab8dcd58e88cc`, the Cranelift `decl.parse_unit`
request for `src/compiler/decl.mncs` (693,292 bytes) returns
`budget_exhausted` before producing a parser verdict:

```text
MNCS_RSRC_EXHAUSTED cranelift JIT canonical arena exhausted: requested 72 byte(s), 67108864 of 67108864 byte(s) used; bounded loops over large aggregate values allocate one fresh cell per functional update
```

The independent Stage-0 oracle accepts this source with no diagnostics, and
the focused sequence-literal POS/NEG differential passes. The observed
failure is at the Cranelift canonical-value arena boundary, not a grammar
diagnostic or an MNCS semantic rejection. This is the original 64 MiB failure;
the later 128 MiB result below supersedes its `decl.mncs` ingestion status.

## Current compiler progression (2026-10-06)

The independently reproduced `carry_huge` loop crossed the old cap. Language
commit `f1a96a0` (included in pushed `mncs-language` main `3e874f642b30`) raises
the explicitly bounded per-request Cranelift arena from 64 MiB to 128 MiB.
The `pressure_loop_region` integration tests pass on reclamation, C11, LLVM,
and Cranelift: `carry_huge` returns 1024 on Cranelift/reclamation backends and
C11/LLVM report structured exhaustion at their unchanged 16 MiB limits.
The advertised-cap unit test passes at 134,217,728 bytes.

With that selected Stage-0 revision, Cranelift parses the 679,907-byte current
`src/compiler/decl.mncs` whole module and agrees with Stage-0 on all 1,002
function names and generic parameter names. It is now in the maintained
self-ingestion corpus at parse/signature depth; check, proof, flow, SSA, and VM
remain unproven. This resolves the current compiler workload, not the general
arena pressure: larger per-request values can still exhaust the bounded
128 MiB arena, so CP-0023 remains open.

## Reopened by compiler project ingestion (2026-10-06)

The real `src/compiler/lexer.mncs` module imports
`mncs.compiler.source.v1`. The isolated `flow.lower_unit` adapter therefore
reports Stage-0 MNE173 at the import; the Stage-0 project oracle is the
authoritative route for this workload. With `source.mncs` and `lexer.mncs`
submitted together, the Stage-0 project oracle accepts the project, resolves
one import, links 42 functions, and emits reference SSA for 15 functions.

The selected native Cranelift project route uses two retained sessions
(`project` and `ssa`) but its `mncs.compiler.project.v1::compile_project`
request fails before execution (`steps=0`) at the fixed arena cap:

```text
MNCS_RSRC_EXHAUSTED cranelift JIT canonical arena exhausted: requested 24 byte(s), 134217728 of 134217728 byte(s) used; bounded loops over large aggregate values allocate one fresh cell per functional update
```

This is a backend arena failure, not a project diagnostic, import failure, or
step-budget exhaustion. CP-0023 is load-bearing again for the compiler's
project-level self-consumption path and is being addressed at the Language
backend owner. The exact sources and selected Stage-0 revision are recorded
in the Environment session artifact; the two-module project result is in the
ignored `.build/project-source-lexer-reprobe-20261006.json` campaign report.

## Current compiler probe (2026-10-08, Stage-0 `1513bdf`)

The current `tools/test_decl.py` run passes twin-identically on one retained
Cranelift session (153 requests: 76 POS, 58 NEG, 9 checker cases). Its
self-ingestion corpus parses all nine current compiler modules and matches
Stage-0 function names and generic parameter names. In particular,
`src/compiler/decl.mncs` now parses at 820,430 bytes with 1,200 functions.
This closes the old `decl.mncs` parser admission failure at this pin; it does
not close the general arena pressure or prove whole-project compilation.

The focused imported-enum project witness also admits the project and SSA
sessions at M=896 and verifies two valid constructors in SSA while matching
six negative diagnostics to Stage-0. It is a small source fixture, not the
whole compiler project.

The current real `flow.mncs` closure is 894 pages at stride 1024. The
independent Stage-0 project oracle accepts it with 1,324 linked functions and
376 SSA functions. Native Cranelift attempts at M=1024 and M=896 ended when
the probe child exited `-9` before returning a compiler result. A reference
interpreter attempt ran without output for about 900 seconds before it was
stopped; a research-bytecode flow-target admission attempt was stopped after
120 seconds without a result. These observations do not establish that the
128 MiB arena caused the child termination, and they do not establish a
compiler rejection. The latest compact matrix and exact probe summaries are
in `evidence/SELF-HOST-MATRIX.json` and
`evidence/campaign-20261008-imported-enum-constructor-frontier.json`.

Current next frontier: obtain a complete native compiler result for the real
flow closure through a target/input route that returns within a trustworthy
execution envelope. CP-0023 remains open because the earlier exact arena
failure and the current whole-project gap are unresolved.

## Symptom

One native `compile_project` (or `parse_unit`) call on a real compiler
module exhausts the Cranelift JIT canonical arena and returns
`budget_exhausted` instead of a verdict:

```text
MNCS_RSRC_EXHAUSTED cranelift JIT canonical arena exhausted:
requested 80 byte(s), 16777216 of 16777216 byte(s) used;
bounded loops over large aggregate values allocate one fresh cell
per functional update
```

The arena is installed fresh per call (`cranelift_backend.rs`:
`JIT_ARENA`, cap `support::NATIVE_ARENA_BYTES` = 16 MiB) and is NOT
cumulative across retained-session requests. Exhaustion is therefore a
per-request step/allocation budget, distinct from the reference
interpreter's step budget.

## Reproducer (committed)

`tools/test_cp0001.py` tier D runs the native pipeline per milestone and
records `backend_arena_exhausted` with unknown pipeline stages instead
of crashing (`pipeline_run` returns `None` on `budget_exhausted`).

Observed 2026-10-03 (CP-0021 tree, Cranelift):

| input | bytes | native pipeline |
|---|---|---|
| `synthetic-1024` / `synthetic-2049` | 1024 / 2049 | full green, stride-invariant |
| `cliff` + 220 KiB padding, fail-fast | 225280 | returned, ParseFailed `[37, 38]` |
| `parser` / `kernel` / `cli-outcome` / `lexer` / `flow` / `project` | 4364–72480 | returned, feature-gap spans |
| `ssa` (incl. every 50–200 KiB prefix) | 51200–217526 | `budget_exhausted` |
| `decl` | 540339 | `budget_exhausted` |

Size alone does not predict it (220 KiB synthetic passes; 50 KiB `ssa`
prefix fails). Content does: `ssa` packs many (nested) match
expressions into the prefix, and every expression parse runs the full
`parse_expr` fuel (1024+1024+32 steps, see CP-0003) plus its nested
token-scan steps. Each step allocates a fresh state cell, so tail
waste — not input bytes — dominates the arena.

## Workload half (this repo)

- Per-expression `parse_expr` fuel was representable but wasteful:
  ~2080 steps ran for ~10–50 useful ones. Conditional chunk-chains
  (`if !done { iterate ... }`, already expressible — see
  `decl.header`, `lexer.next_token`) landed in the perf campaign
  (CP-0003 endgame) across `parse_expr`, `parse_fields`,
  `parse_clauses`, `parse_stmts`, `parse_unit`, and `TypeParse`.
  Measured interpreter steps (reference backend): 1-expr
  1137645→588260, 1-fn 901811→352537, 10-fn exhausted→5206166
  returned. The `ssa`/`decl` arena verdicts above predate this
  change; tier-D re-measurement on the chunk-chained tree is pending.
- Page slicing allocates per slice: `project.module_pages` results
  are hoisted out of every step/item loop (resolved once per
  comparison/walk, threaded as triples). Verified necessary but not
  sufficient for `ssa`/`decl`.
- Until both halves land, tier D records `CP-0023` with
  `backend_arena_exhausted: true` and unknown pipeline stages for
  affected milestones. Lex/transport stages still stand.

## Backend half (`mncs-language`)

- The original 16 MiB shared cap and subsequent 64 MiB Cranelift cap are
  historical. Cranelift now advertises an explicit 128 MiB per-request
  bound; denser cell allocation, cell reuse for identical no-op updates,
  reachable-only codegen, and arena-usage queries remain possible later
  remedies if compiler growth makes the new bound load-bearing.
- Reference/interpreter equivalence is unaffected (step budgets, not
  arenas); any relief must keep deterministic failure (`budget_exhausted`
  with attributed bytes, never silent truncation).

## Operational impact (2026-10-03)

`tools/test_project.py::_sec_enum` keeps its native requests on a
fresh probe rather than the fixture's shared probe ("the selected
native runtime has a bounded canonical arena, and each request is
independently installed"), so the shared probe stays alive for oracle
cross-checks and later sections while a second full split probe
(`enum_probe`, project + ssa sessions) serves the enum/match native
requests. Measured consequence (Cranelift): 4 concurrent probes ×
~3.4–4.5 GB RSS ≈ 15 GB for one `match-ssa` mode — the suite's peak
working set, and the first thing the OOM killer reaps under
multi-agent contention. The exact per-session accumulation mechanism
is unproven (the arena installs fresh per call, yet a fresh probe is
required in practice); resolving that question, or any backend relief
(configurable cap, denser cells, usage query), directly reduces
verification-suite memory by letting sections share probes.

## Acceptance

- `ssa.mncs` and `decl.mncs` milestones reach a pipeline verdict
  (feature-gap span or better) instead of `backend_arena_exhausted`
  — MET 2026-10-03 via the workload half (CP-0003 chunk-chains): the
  full ABCD matrix records zero `backend_arena_exhausted` rows; ssa
  (217,526 B) and decl (558,004 B) reach CP-0015 parse spans.
  No fuel bound was shrunk: supported inputs did not narrow.
- The original 16 MiB native cap and 2026-10-03 milestone verdict are
  historical. Cranelift's bounded 128 MiB per-request arena admits the
  current whole `decl.mncs` parse, verified against the selected Stage-0
  oracle. CP-0023 remains open because the backend still has a fixed ceiling.
