# Pressure Finding CP-0021

ID: CP-0021

Status: resolved in mncs-compiler (revalidated by the committed suite + full ABCD matrix)

Category: compiler-architecture

Severity: high

Frequency: pervasive for whole-module compilation past 1,024 bytes

## Declaration, proof, CFG, and SSA stages consume single bounded views

The CP-0001 logical immutable source (`source.mncs` page composition with
`validate_pages`, `byte_at_global`, `fingerprint_global`, `line_col_global`)
plus the logical lexer/segment/parser/kernel surface proves transport,
cross-boundary lexing, header facts, and content identity to the 1 MiB
composition ceiling (see `evidence/cp0001-results.json`,
`evidence/cp0001-matrix.json`, `tools/test_cp0001.py`). Every milestone
source lexes stride-invariant with exact global spans, and headers parse
over global offsets.

The declaration stage does not consume that representation yet.
`decl.parse_unit`, `decl.prove_unit`, `flow`, `ssa`, and
`project.compile_project` take `[byte; up_to N]` (N <= 1024) and use
`iterate i over input` whole-view fuel throughout. A module past 1,024
bytes is not representable as their input, so the tier-D matrix records
parse/proof/CFG/verified-SSA as blocked at span [1024, 1025] for every
milestone past the ceiling, including `mncs-cli` `outcome.mncs` (4,364
bytes) and the compiler's own modules.

This is not a request to raise a generic language bound. The proven
pattern for the refactor is pages-outer fuel: replace whole-view fuel
loops with an outer `iterate p over pages` (trip count <= 1,024) whose
step runs one page of byte fuel through the existing state machines, and
thread `(pages, stride, total)` with global offsets where byte access
occurs. Precedents that already elaborate under Profile 0.18:

- `project.compile_project` nests module loops over per-module byte
  loops through `decl.parse_unit` (nesting through calls is accepted);
- the CP-0001 `ascii_global`, `fingerprint_global`, and
  `line_col_global` nest page loops over byte loops at depth 2 with a
  work product under the 1,048,576 ceiling;
- `lexer.lex_tokens_from` nests a 1,024-token batch loop over the
  token-scan loops at the same accepted depth.

Cost forecast from tier-D evidence: per-call step budgets rise with the
work product (batch lexing at 128M steps for the largest milestone); the
refactor must keep per-loop trip counts at most 1,024 and total work
within backend step budgets, with the host driving any remainder the way
it already pages `lex_tokens_from` and `lex_step_global`.

## Reproduction

Run `python3 tools/test_cp0001.py` (tiers ABCD): tier D lexes each
milestone through the logical representation and records parse/proof/CFG
as blocked with pressure CP-0021 and first-failure span [1024, 1025].
The `synthetic-1024` matrix row anchors the full pipeline at the ceiling
(two modules, one padded to exactly 1,024 bytes, native-vs-oracle
compared).

## Ownership

Compiler architecture (declaration/proof/lowering fuel domains); no
language change was implicated, and none was needed.

## Resolution (2026-10-03, performance campaign)

The pages-outer refactor landed without raising any bound:
`decl.parse_unit`/`prove_unit`, `flow.lower_proven_unit`,
`ssa.lower_value_ssa`, and `project.compile_project` take
`(pages, stride, total)` with global positions; `project` transports
one flat page array plus per-module `ProjectSource` descriptors with a
`descriptors_cover` partition check; `segment.ascii_global` covers the
paged admission path. Whole-module native compilation is no longer
capped at one bounded view per module.

Evidence: full ABCD matrix (`evidence/cp0001-matrix.json`, 10 rows,
zero CP-0021 rows); full project closure green in 1,362 s (baseline
1,406 s); `test_decl.py` digest byte-identical (`820ac042…`);
`test_sem.py` 170+11 twice-identical; staged imported-nominal chain
verified through the new ABI on Cranelift.

Known remaining ceilings (follow-ups, not regressions): the
`spans_equal_between` 1024-byte counted comparison refuses
prefix-equal verdicts past fuel (fail closed), and per-module page
arrays bind at M=1024. Both need measured follow-up once real
modules press them.
