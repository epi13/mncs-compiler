# Declaration-vertical evidence (segment + decl + check)

Self-hosted MNCS implementation in `src/compiler/`: bounded source
segments (`segment.mncs`), declaration/expression parsing (`decl.mncs`
through `parse_unit`), symbol collection, resolve/span walking, and a tiny
stack IR with a depth self-check (`decl.check_unit`). All behavior executes
in MNCS on the pinned Rust Stage-0 reference interpreter unless noted.
This is **not self-hosting** and not backend parity; Rust remains the
current compiler.

## Reproduce

From the repository root (after `tools/bootstrap.sh`):

```sh
MNCS_LIBRARY_PATH=src .bootstrap/target/debug/mncs abi src/compiler/decl.mncs
python3 tools/test_decl.py
python3 tools/test_frontend.py
python3 tools/test_pressure.py
```

`tools/test_decl.py` is the committed differential suite (twin run for
determinism); its report lands in `.build/decl-results.json` (promoted
copy: `evidence/decl-results.json`). The broader scratch corpora that fed
this pass (`/tmp/unitcheck.py`, `/tmp/declcorpus.py`) are documented below
with their exact case lists and results so a later run can re-derive them.

## What is implemented

`decl.parse_unit` (4 bounded source chunks + total length, inputs <= 256
bytes): header, `use` (aliased/plain), `record`, payload `enum`, and `fn`
declarations with contracts/effects/capabilities; bodies with
`let`/`if`-`else`/`fail`/terminal-`return`; expressions over names,
integers, booleans, all binary operators, direct calls, and field
projection. Deferred shapes fail with an explicit local span, never
silently accepted (see the module doc comment for the list).

`decl.check_unit` (same bounded inputs): parse, then duplicate
function-name detection (exact byte comparison), then a two-stack
resolve/span walk over every body (calls must name a declared function;
every statement/expression span must lie within the source), then lowering
of every walked expression to a postfix stack IR (`FlatOp`) verified by a
stack-depth self-check that must end at depth exactly one. Stage tags:
0 parse, 1 duplicate symbols, 2 resolve/span walk, 3 IR depth,
4 fully verified. Type-namespace resolution (record/enum/value types, use
aliases) is deferred and stated in the module comment.

Representation note: our `Expr.Project` node covers all `base.field`
shapes, while the Stage-0 AST splits them three ways (`FiniteVariant` for
`v.x`, `QualifiedPath` for `v.x.y`, `FieldProject` for `g(v).x`) with
identical segment/span facts. The differential canonicalizes name-based
chains to `(base, base-span, [(field, span)], total-span)` tuples on both
sides and compares those; the node tag is our IR choice, the facts are
shared. Non-name bases compare structurally and directly.

## Differential results vs Stage-0 oracle

- Unit differential (structural + first-error spans, 22 positive / 16
  negative): 38 requests, 0 fails, max interpreter steps 2995773
  (step budget 8000000). Fixed by this pass: sequence-type spans now
  include the opening bracket; empty `record`/`enum` bodies report at the
  token after `}` (MNP128/MNP075 parity); version gate matches Stage-0
  (`0.8`/`0.9`/`0.11`/`1.0` all elaborate; only the version *token shape*
  is gated, profile support stays a later obligation).
- Declaration corpus differential (operators `||`/`!=`/`>=`, precedence
  mixes, parens, nested calls, multi-`use`, multi-decl combos, projections
  (`v.x`, `v.x.y`, `g(v).x`), shifts: 16 positive cases x parse twin +
  check_unit): 48 requests, 0 fails, max steps 3334792. Projection
  chains compare through the representation canonicalization above.
- check_unit verdict probes: clean unit reaches stage 4 with `ir_ok`;
  duplicate `fn` reports stage 1 at the second name; call to undeclared
  `g` reports stage 2 at the callee; empty record reports stage 0 at the
  oracle span. During development the depth self-check caught a real
  lowering-order bug (postorder emitted children swapped); fixed and
  re-verified.

## Cost

- `decl.mncs` (1942 lines incl. ~500 lines of symbol/semantic/IR code)
  compiles under Stage-0 in ~120 s wall with status
  `completed_with_unresolved_obligations`: 687 CMP301 (493
  integer-overflow, 165 iteration-exact-resource-cost, 28 machine-intent,
  1 sequence-index-bounds). Emission sizes: semantic 18.1 MB, HIR
  59.3 MB, SSA 50.4 MB (`.build/compiled/` is ignored build output).
- Reference-interpreter steps per request are recorded in every report
  (`execution_steps_max`); the heaviest observed unit parse costs
  2995773 steps against the 8000000 probe budget. check_unit on small
  units costs 380k-1400k steps.
- No peak-memory or native-performance claim is made (CP-0007 stands).

## Coverage and limits

- Every claim above is bounded-corpus evidence, not all-input equivalence.
  Inputs are capped at 256 bytes by the four-segment laboratory interface
  (CP-0001); the 64-byte kernel boundary is superseded for declaration
  work but whole-module sources remain out of reach.
- The depth self-check is a verifier-completeness tripwire (a rejection is
  always a lowering bug), not a source-soundness proof. No type checking,
  exhaustiveness, effect, or borrow reasoning exists yet.
- Host Python only transports bytes and compares against the oracle; it
  never lexes, parses, or hashes source into facts (CP-0005).
- No new language pressures were found at this layer beyond CP-0008
  through CP-0012; the version-gate and empty-body divergences turned out
  to be implementation bugs (fixed) and a wrong harness expectation
  (corrected), not language gaps.
