# Pressure Finding CP-0011

ID: CP-0011

Status: open

Category: language | compiler-architecture

Severity: high

Frequency: pervasive

## Acyclic calls forbid recursive descent; parsers become explicit machines

Source Profiles 0.3+ reject every call cycle (MNE130), including direct
self-recursion and mutual recursion between two functions. A compiler
frontend is naturally recursive — expressions nest in calls nest in
expressions, statements nest in blocks nest in statements — so the entire
declaration core is written as explicit state machines:

- Expression parsing is an iterative shunting-yard loop with explicit
  operand/operator stacks (one action per fuel step), not precedence
  climbing, because `binary_expression(min_prec)` recurses.
- `if`/`else` nesting is a block machine over an explicit frame stack
  (`Frame { cond, then_list, parent, phase }`), not recursive statement
  functions, because branches contain statements that contain `if`.
- Even list reversal is a fuel loop over a two-field record, not a
  three-line recursive function.
- Lookahead that Stage-0 does with backtracking cursors (generic-argument
  detection, record-literal-vs-block disambiguation) is either
  reimplemented as fixed multi-token peeking or deferred with an explicit
  error.

Recursion *in values* is fully supported (recursive enums elaborate,
construct, match, and compile — the AST itself is a family of mutually
recursive enums), but recursion *in calls* is unavailable, so every
traversal of those values must also be iterative.

## Compiler workload

The whole of `src/compiler/decl.mncs`: ~1400 lines of expression,
statement, declaration, and unit parsing with zero call cycles.

## Minimal MNCS reproduction

Any direct or mutual call cycle, e.g. `pressure/repro/recursive-call.mncs`:

```text
mncs 0.10;
module probe.recursive_call;
enum E { Leaf, Node { left: E, right: E } }
fn depth(e: E) -> (result: u64) {
    return match e {
        Leaf => 0,
        Node { left: l, right: r } => depth(l) + depth(r) + 1
    };
}
```

Stage-0 reports MNE130 on the recursive call. (Recursive *types* in the
same file elaborate and compile; only the *call* is rejected.)

## Current behavior

The acyclicity rule is presumably load-bearing for termination analysis,
and this finding does not ask for general recursion. The pressure is the
*shape* it forces on compiler code: every naturally recursive algorithm
arrives as a hand-rolled machine with its own stack type, fuel argument,
progress invariant, and reversal pass.

## Current workaround

Three machine patterns, each documented at its definition site:

1. Shunting-yard with marker-carrying operator stack (expressions).
2. Frame-stack block machine with await-else states (statements).
3. Accumulator fuel loops over cons-lists (reversals, flattening).

## Why the workaround is insufficient

- Each machine is 100–250 lines where the recursive form would be
  20–50, inflating the exact code whose compile cost is already the
  dominant pressure (CP-0007).
- Progress and fuel-sufficiency arguments live in comments; Stage-0
  checks termination structurally (acyclicity) but cannot check that a
  machine's fuel covers its state space, so fuel exhaustion is a silent
  wrong-answer risk (see CP-0009).
- The `depth(l) + depth(r)` shape — tree recursion producing a value
  from children — has no iterative formulation that avoids materializing
  an explicit worklist plus a rebuild pass, roughly tripling the code at
  every future traversal (resolution, checking, lowering each need one).

## Desired behavior

Without prescribing general recursion: bounded-recursion combinators
with statically visible depth bounds (e.g. structural recursion over a
finite value with a declared measure), or first-class machine-building
support (a state-machine/block form whose fuel derives from its state
type), or documented confirmation that hand-rolled machines are the
intended compiler style — with guidance on fuel verification so the
silent-exhaustion risk is addressed by construction rather than review.

## Likely ownership

language | compiler architecture

## Impact

- Correctness: machines are harder to audit than the recursive forms;
  fuel exhaustion fails silently.
- Safety: none (termination is preserved; expressiveness is the cost).
- Runtime performance: worklist traffic replaces call stacks; measured
  per-request step maxima in `evidence/`.
- Compiler performance: 3–5x code size versus recursive forms,
  multiplying CP-0007 cost growth.
- Memory: explicit stacks live in values copied per step.
- Determinism: deterministic.
- Implementation complexity: the dominant complexity driver of this run.

## Evidence / reproduction

The declaration core itself is the evidence: grep for `Frame`,
`ExprState`, `BlockState`, and the eight-link fuel chains. Step counts
per request class are recorded in `evidence/decl-results.json`.

## Upstream tracking

- `mncs-language` issue/PR:
- Resolution revision:
- Follow-up evidence in this repository:
