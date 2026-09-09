# Semantic-proof evidence (decl.prove_unit)

Self-hosted MNCS semantic vertical in `src/compiler/decl.mncs`: symbols to
semantic facts/types/contracts/effects (`check_sig`), bidirectional proof
with fused typed lowering (`prove_expr` explicit-stack machine), statement
proving (`prove_stmts` frame machine), whole-unit proof (`prove_unit`), and
a type-stack verifier for the produced typed IR (`verify_tops`). All
behavior executes in MNCS on the pinned Rust Stage-0 reference interpreter
unless noted. This is **not self-hosting** and not backend parity; Rust
remains the current compiler.

## Reproduce

From the repository root (after `tools/bootstrap.sh`):

```sh
MNCS_LIBRARY_PATH=src .bootstrap/target/debug/mncs abi src/compiler/decl.mncs
python3 tools/test_sem.py
```

`tools/test_sem.py` is the committed twin differential: every corpus case
runs twice (identical digests required); its report lands in
`.build/sem-results.json`. Obligation kinds map to Stage-0 codes per the
table in `decl.mncs` (`Obligation kinds` comment); only FAIL obligations
(status 1) correspond to oracle diagnostics, in order with exact spans.

## What is implemented

`decl.prove_unit` (4 bounded source chunks + total length, inputs <= 256
bytes): parse; duplicate function-name rejection (MNE104, aborts the
proof); a declaration-type pre-pass resolving every parameter/result
annotation in order (MNE105/MNE173 surface here, and again per body);
then per function in order: the result-count gate (anything other than
exactly one output ends the function after a single MNE101), remaining
signature obligations (capabilities MNE110, effects MNE111, contracts stay
UNKNOWN, then parameter scope MNE110), body re-resolution of annotations,
and statement proving with `let`/`if`-`else`/`fail`/`return` plus the
terminal tail. Every proved expression emits typed operations (`TOp`)
checked by `verify_tops` for shape only (depth, operand equality, no
poison); the requirement comparison itself stays a non-fatal match
obligation, and poison-involved lowerings skip verification (a FAIL
obligation already covers them; poisoned IR is never verified).

`decl.prove_expr`: Name/Integer/Boolean/Binary/Call/Project over an
explicit job/continuation stack (no recursion). Calls resolve against the
completed signature table (MNE131/MNE132), check arguments incrementally
left to right (MNE133 at the argument span, non-fatal), enforce effect
coverage after all arguments (MNE134, fatal), then the result requirement
(MNE135, non-fatal). Projections resolve the base record and field by
exact name bytes (MNE161 base / MNE162 field, fatal; MNE163 requirement,
non-fatal). A returned name resolves only (never MNE117); the output
comparison still applies.

Adversarial constructors (`sabotage_depth`, `sabotage_call_arity`,
`sabotage_bin_mismatch`, `sabotage_final_type`) must be rejected by the
verifiers; `sound_sample` must pass. Construction and verdict both execute
in MNCS through the probe; no host-constructed values cross the boundary.

## Oracle-pinned semantics (all verified against Stage-0, 49 cases)

- Elaboration order is [all signature type resolutions] then [per function:
  remaining signature obligations, body]. Deduced from duplicated
  diagnostics (bad annotations surface twice) and cross-function ordering
  probes.
- Poison equals only poison. Every other expected/actual combination
  involving poison records its FAIL (literals, names, arguments, results,
  conditions included).
- Fatal (drain the expression proof): unresolvable names, arity,
  callee, operand inequality, class violations, effect-cover failure,
  bad base/field. Non-fatal (record and continue): literal/argument/
  result/requirement mismatches, conditions, unreachable-after-terminal
  (first one reported, rest of the block skipped).
- An `if` rejoins unconditionally: statements after a both-return join
  elaborate normally at this level (lowering validation reports MNB038,
  which is out of scope here).
- A body with anything other than exactly one declared output is skipped
  after its signature (MNE101 only).

## Differential results vs Stage-0 oracle

- Twin differential (`tools/test_sem.py`, two identical runs): 49
  semantic cases plus 5 intrinsic-proof verdicts, 54 probe requests,
  0 fails. Interpreter steps total 77081331, max per request 3661553
  (step budget 8000000). Result digest
  `c078a3dd1d63b2b6…`, elapsed 5679 s over both runs, Stage-0 lock
  revision `6906d0b1eee7`. Report: `.build/sem-results.json`.
- Every case compares FAIL obligations against the oracle `elaborate`
  MNE diagnostics in order with exact spans, plus the proof `ok` verdict
  and function count. UNKNOWN obligations (overflow 13, div-zero 14,
  contracts 16) are asserted present where expected and never surfacing
  as diagnostics.
- Oracle-pinned semantics (all verified against Stage-0, 49 cases):
  the case list in `tools/test_sem.py` `CASES` is the corpus.

## Scope limits and next target

- MNB body-graph/lowering codes are not modeled (elaboration only).
- Call authority covers callee effects (MNE134); the capability dimension
  of the oracle check is a signature-level concern (MNE111) here.
- Cost pressure (CP-0007/CP-0009): proving a ≤256-byte unit costs up to
  3.66M interpreter steps of the 8M budget; the twin run took 5679 s
  wall-clock, dominated by probe boots re-elaborating the now 4107-line
  `decl.mncs` (1942 lines on main before this vertical). Proof cost per function is roughly 2× signature checks
  plus one body walk; obligation appends are linear each (quadratic
  accumulation), still well inside fuel for bounded inputs.
- Strongest next target: lower the verified typed IR to body blocks,
  which forces reachability/join validation (today's MNB038) into
  elaboration scope; after that, cross-module `use` resolution, which is
  the missing semantic piece before any backend lowering.
