# CP-0004 — Boolean equality is rejected in scanner state updates

Status: open. Category: language, tooling. Severity: low. Frequency: common.
Upstream tracking: none.

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
