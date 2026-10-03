# CP-0023 — native backend per-request value-arena ceiling

Status: workload relieved in mncs-compiler (acceptance met on all milestones); backend cap unchanged, configurability still open (`mncs-language`).
Found: 2026-10-03, perf campaign (CP-0021 follow-up). Stage-0 `a3ac17df`, profile `0.18`.

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

- The 16 MiB per-request cap binds before any MNCS semantic bound on
  real compiler modules. Options owned by the backend: raise or
  configure `NATIVE_ARENA_BYTES`, denser cell allocation, cell reuse
  for identical no-op updates, or an arena-usage query so drivers can
  split work before exhaustion.
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
- The backend ceiling itself (16 MiB, `NATIVE_ARENA_BYTES`) is
  unchanged and still unconfigurable: larger or denser inputs past
  today's milestones can still exhaust it, and drivers still cannot
  query usage to split work. That hardening remains open backend
  ownership (`mncs-language`), now decoupled from any failing
  compiler milestone.
