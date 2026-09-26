# Semantic-proof evidence (decl.prove_unit)

MNCS semantic vertical in `src/compiler/decl.mncs`: symbols to
semantic facts/types/contracts/effects (`check_sig`), bidirectional proof
with fused typed lowering (`prove_expr` explicit-stack machine), statement
proving (`prove_stmts` frame machine), whole-unit proof (`prove_unit`), and
a type-stack verifier for the produced typed IR (`verify_tops`). All
behavior executes in MNCS on Rust Stage-0 revision
`709ba00810099e6965bb47dec14ed19e9e1ae6f8`, source Profile 0.18. Compiler
modules declare 0.18; the semantic corpus retains Profile 0.10 source fixtures
to test supported legacy syntax. This is **not self-hosting** or backend
parity; Rust remains the current compiler.

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

## Oracle-pinned semantics (all verified against current Stage-0, 49 cases)

- Elaboration order is [all signature type resolutions] then [per function:
  remaining signature obligations, body]. Deduced from duplicated
  diagnostics (bad annotations surface twice) and cross-function ordering
  probes.
- Poison equals only poison. Every other expected/actual combination
  involving poison records its FAIL (literals, names, arguments, results,
  conditions included). Poisoned-result binary expressions preserve Rust's
  child and enclosing result diagnostics in both operand orders (CP-0017).
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

## Differential results vs current Stage-0 oracle

- Twin differential (`tools/test_sem.py`, two identical runs): 49
  semantic cases plus 5 intrinsic-proof verdicts, 54 requests per run,
  0 mismatches. Interpreter steps total 73,631,716, max per request
  3,511,846 (8,000,000 budget). The identical result digest is
  `bced8deff2157ebdfe2b151f4e29d34b7e20ca1a74cfa7c69c34689e7f1a5e6c`;
  elapsed time was 5,468.621 seconds over both runs. Stage-0 revision:
  `709ba00810099e6965bb47dec14ed19e9e1ae6f8`, source Profile 0.18.
  Promoted report: [`sem-results.json`](sem-results.json).
- Every case compares FAIL obligations against the oracle `elaborate`
  MNE diagnostics in order with exact spans, plus the proof `ok` verdict
  and function count. UNKNOWN obligations for overflow (13) and division by
  zero (14) are asserted present where expected and never surfaced as
  diagnostics.
- The five proof verdict requests reject the four sabotaged proof values and
  accept the sound sample. The CP-0017 minimal pair is retained in
  [`semantic-pressure-cp0017-after.json`](semantic-pressure-cp0017-after.json).
- The superseded profile-0.10 result is preserved as
  [`sem-results-pre-campaign.json`](sem-results-pre-campaign.json); it is not
  current-pin parity evidence.
- Oracle-pinned semantics (all verified against Stage-0, 49 cases):
  the case list in `tools/test_sem.py` `CASES` is the corpus.

## Scope limits and next target

- This semantic differential covers elaboration only. The compiler-owned
  `flow.lower_unit` pass now adds branch/jump/return/fail blocks and MNB038
  join diagnostics; see [`PARITY.md`](PARITY.md) and
  [`flow-results.json`](flow-results.json) for its separate current-run
  evidence.
- Call authority covers callee effects (MNE134); the capability dimension
  of the oracle check is a signature-level concern (MNE111) here.
- Cost pressure (CP-0007/CP-0009): this 256-byte-unit twin semantic corpus
  took 5,468.621 seconds and 73.6 million interpreted steps. The heaviest
  request used 3,511,846 of its 8,000,000-step budget. This is reference
  interpreter/test cost, not native compiler runtime, and does not establish
  peak memory or whole-project throughput. Current linked artifact sizes are
  measured separately in [`compile-results.json`](compile-results.json).
- Strongest next target after the current flow pass is a current-profile
  project-source/module-resolution path that feeds the parser, proof, and CFG
  stages. The flow representation still lacks Rust-equivalent SSA values,
  callable identities, and executable target output.
