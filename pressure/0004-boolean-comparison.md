# CP-0004 — Boolean equality is rejected in scanner state updates

Status: resolved (2026-09-12). Category: language, tooling. Severity:
low. Frequency: common. Upstream tracking: profile 0.13
(`BooleanNot`/`BooleanCompare`, MNE119/MNE121 refinement).

`repro/bool-equality.mncs` is the minimal reproduction: `a == b` for two booleans
emits MNE121, “comparison operands must have an integer type”. Run
`python3 tools/test_pressure.py`. Encountered while expressing `done = advance ==
false` in `lexer.scan_step`.

Workaround: `select(advance, false, true)`; logical predicates otherwise use
`&&`/`||`. This is correct and sufficient to continue, but makes commonplace
state predicates less direct. No measured runtime or memory penalty is claimed.
Compiler cost is not separately measured. Safety and determinism are unchanged;
implementation complexity is a small recurring readability cost.

Desired behavior: a documented, consistent boolean equality/negation idiom;
possibly stdlib helpers rather than a language change. Likely ownership:
language semantics and documentation/tooling. Do not prioritize this above
whole-source storage or Unicode parity.

## Update (declaration-vertical pass)

The family is larger than equality: there is no logical-negation operator
at all. `!x` lexes as MNL002 (unsupported character); only `!=` exists.
Every predicate in the declaration core negates through a local
`fn not(value: bool) -> (result: bool) { return select(value, false, true); }`
(duplicated per module since leaf helpers are not shared). Related:
`match` accepts no integer patterns (see CP-0010), so scalar dispatch and
negation both route around the same missing boolean/scalar operator
surface. Workaround remains sufficient; severity stays low.

## Re-evaluation (Stage-0 `a7a8c05`, 2026-09-12): resolved

Probed on the current pin: `a == b` and `!a` over bools elaborate at
profile 0.13 (exit 0, zero diagnostics); the 0.10 control still yields
MNE121, so old-profile behavior is preserved. Mixed-type operands keep
MNE119 and bool ordering keeps MNE121 — the refinement is exactly
scoped. Proven in real compiler code: the 0.13 migration uses native
`!` in `lexer.mncs` (`done: !advance`), `segment.mncs` (same),
`kernel.mncs` (`!source.span_valid(...)`), and 134 sites in `decl.mncs`
with the `not()` helper deleted. The three frontend modules are
differentially proven (`tools/test_frontend.py` green, 3435 requests);
`decl.mncs` is parse-checked with elaboration proof staged after
CP-0014. History preserved above: the original MNE121 limitation, the
`select` workaround, and the missing-negation family note.
