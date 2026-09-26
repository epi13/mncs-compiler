# Compiler pressure

These findings originate in `mncs-compiler` workloads. A finding is not
automatically language pressure: classify compiler architecture, runtime,
backend, tooling, and language separately, then reproduce it at the authority
that owns the behavior. Preserve each original reproducer and history; use the
current reconciliation below for live status.

Current locked Stage-0 CLI/bootstrap reference: `4f9e1224e7f3cdb67fa4687d8f496717da1354aa`,
profile 0.18. The compiler semantic pressure/differential suites were run at
`b0f3e6447dbdefb5cd9fceeb43da2ab1909a7e70`; the Rust compiler/model/syntax/codegen
libraries are unchanged between those pins. Their result files retain the exact
revision. The campaign also preserves the earlier 709ba008 results as historical evidence. Current pressure probes are in
[`../evidence/campaign-20260925-pressure-reconciliation.json`](../evidence/campaign-20260925-pressure-reconciliation.json),
and current native syntax results are in
[`../evidence/campaign-20260925-profile-surface-results.json`](../evidence/campaign-20260925-profile-surface-results.json).

## Current reconciliation (2026-09-26)

| Finding | Current status | Current result and owner |
| --- | --- | --- |
| [CP-0001 bounded source storage](0001-bounded-source-storage.md) | Partial | Current Rust accepts the preserved 65-byte case. Native project input now carries exact per-file byte sequences beyond 256 bytes (tested at 1,024 bytes), but Profile 0.18 caps the current generic source bound at 1,024 and the project snapshot at 64 modules. |
| [CP-0002 Unicode classification](0002-unicode-source-classification.md) | Open | Current Rust accepts the Unicode module probe; the native frontend remains ASCII-only. Compiler/frontend gap, not a missing generic language feature. |
| [CP-0003 bounded scan cost](0003-scan-traversal-cost.md) | Open | Current Rust still rejects the `while` reproducer with MNP106. Bounded traversal remains the compiler workaround; no upstream change was required for the CFG workload. |
| [CP-0004 boolean comparisons](0004-boolean-comparison.md) | Resolved | Current Stage-0 accepts boolean equality and negation. Native parser support for current syntax is tracked by CP-0015. |
| [CP-0005 test transport](0005-test-transport.md) | Open, reduced | The cached retained-session probe avoids rebuilding sessions, but Python/Rust still transports test requests and oracle facts. Tooling boundary. |
| [CP-0006 envelope inference](0006-envelope-profile-inference.md) | Open | Leading-comment profile probe still produces MNE002. Stage-0 envelope/tooling behavior. |
| [CP-0007 bootstrap evidence cost](0007-bootstrap-evidence-cost.md) | Re-measured; unresolved obligations remain | Release-mode linked kernel compile repeats byte-identically in 1.099/1.128 seconds; semantic/HIR/SSA files total 6,799,549 bytes, with 213 CMP301 obligations. An earlier same-pin timing of 10.792/10.848 seconds is retained, with executable mode unknown. Neither measurement establishes native cost or peak memory. |
| [CP-0008 finite payload visibility](0008-finite-payload-visibility.md) | Resolved | Current producer and imported consumer fixtures both elaborate. The original backend/source-level authority is Stage-0; it does not imply a native compiler module resolver. |
| [CP-0009 iteration fuel chaining](0009-iteration-fuel-chaining.md) | Resolved for tested cases | Current profile accepts a counted bound of 256 and repeated iteration identities. The compiler still uses its explicit bounded scans. |
| [CP-0010 scalar match dispatch](0010-scalar-match-dispatch.md) | Resolved | Current Stage-0 accepts the total integer-match fixture. Native compiler parsing of that Profile 0.18 form remains blocked by CP-0015. |
| [CP-0011 acyclic-call machines](0011-acyclic-calls-machines.md) | Partial; current scope narrowed | The structural recursive AST/function fixture passes. Numeric self-recursion and mutual recursion still fail with MNE130; explicit current-profile probes are in `pressure-current-results.json`. |
| [CP-0012 payload sequence ban](0012-payload-sequence-ban.md) | Resolved | Current Stage-0 accepts the finite sequence payload fixture. |
| [CP-0013 keyword field `next`](0013-keyword-field-next.md) | Resolved | Current Stage-0 and native `decl.parse_unit`/`decl.prove_unit` accept the field and projection under Profile 0.18. |
| [CP-0014 bool payload regression](0014-bool-payload-regression.md) | Resolved upstream | Current Stage-0 accepts the bool-payload fixture; regression evidence and language fix are linked in the finding. |
| [CP-0015 version-aware frontend](0015-version-aware-frontend.md) | Partially resolved | Rust accepts all five tested Profile 0.18 syntax examples. Native parsing and proof now accept `next` fields/projections; `!`, negative atoms, repeat literals, and integer `match` still fail. Compiler architecture. |
| [CP-0016 linked record call validation](0016-linked-record-call-validation.md) | Resolved upstream | The exact compiler-origin call now returns after a generic language runtime fix for nested nominal payload validation. |
| [CP-0017 poisoned-result semantic recovery](0017-semantic-poison-recovery.md) | Resolved in compiler; full semantic twin passes | Both operand orders and the 49-case semantic suite match current Rust diagnostics, including ordered codes/spans, with identical repeated native results on the retained backend. |

## Operating sequence

```text
current compiler workload
        ↓
smallest faithful reproduction + current Rust comparison
        ↓
classify owner and reconcile Commons evidence
        ↓
change only the owning layer
        ↓
return to the original compiler workload and prove resolution
```

The language change for CP-0016 is committed independently in
`mncs-language` revision `b0f3e6447dbdefb5cd9fceeb43da2ab1909a7e70`; its
focused generic model tests and the originating compiler call are covered in
the campaign evidence. The remaining current blockers are native compiler
architecture limits. No new MNCS language feature is being proposed by this
reconciliation.
