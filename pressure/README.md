# Compiler pressure

These findings originate in `mncs-compiler` workloads. A finding is not
automatically language pressure: classify compiler architecture, runtime,
backend, tooling, and language separately, then reproduce it at the authority
that owns the behavior. Preserve each original reproducer and history; use the
current reconciliation below for live status.

Tested reference: Stage-0 revision `709ba00810099e6965bb47dec14ed19e9e1ae6f8`,
profile 0.18. `mncs-language` origin/main later advanced only through an
automated badge-metadata commit; the compiler source used for these results is
unchanged. Machine-readable current results are in
[`../evidence/pressure-current-results.json`](../evidence/pressure-current-results.json)
and the native parser differential is in
[`../evidence/profile-surface-results.json`](../evidence/profile-surface-results.json).

## Current reconciliation (2026-09-25)

| Finding | Current status | Current result and owner |
| --- | --- | --- |
| [CP-0001 bounded source storage](0001-bounded-source-storage.md) | Partial | Old Stage-0 64-byte ceiling is stale: current Rust accepts the preserved 65-byte case. Native compiler input remains four 64-byte chunks (256 bytes), a compiler architecture limit. |
| [CP-0002 Unicode classification](0002-unicode-source-classification.md) | Open | Current Rust accepts the Unicode module probe; the native frontend remains ASCII-only. Compiler/frontend gap, not a missing generic language feature. |
| [CP-0003 bounded scan cost](0003-scan-traversal-cost.md) | Open | Current Rust still rejects the `while` reproducer with MNP106. Bounded traversal remains the compiler workaround; no upstream change was required for the CFG workload. |
| [CP-0004 boolean comparisons](0004-boolean-comparison.md) | Resolved | Current Stage-0 accepts boolean equality and negation. Native parser support for current syntax is tracked by CP-0015. |
| [CP-0005 test transport](0005-test-transport.md) | Open, reduced | The cached retained-session probe avoids rebuilding sessions, but Python/Rust still transports test requests and oracle facts. Tooling boundary. |
| [CP-0006 envelope inference](0006-envelope-profile-inference.md) | Open | Leading-comment profile probe still produces MNE002. Stage-0 envelope/tooling behavior. |
| [CP-0007 bootstrap evidence cost](0007-bootstrap-evidence-cost.md) | Re-measured; unresolved obligations remain | Current Profile 0.18 linked kernel compile repeats byte-identically in 15.191/15.657 seconds; semantic/HIR/SSA files total 21,996,645 bytes, with 213 CMP301 obligations. This is Stage-0 cost, not native cost or peak memory. |
| [CP-0008 finite payload visibility](0008-finite-payload-visibility.md) | Resolved | Current producer and imported consumer fixtures both elaborate. The original backend/source-level authority is Stage-0; it does not imply a native compiler module resolver. |
| [CP-0009 iteration fuel chaining](0009-iteration-fuel-chaining.md) | Resolved for tested cases | Current profile accepts a counted bound of 256 and repeated iteration identities. The compiler still uses its explicit bounded scans. |
| [CP-0010 scalar match dispatch](0010-scalar-match-dispatch.md) | Resolved | Current Stage-0 accepts the total integer-match fixture. Native compiler parsing of that Profile 0.18 form remains blocked by CP-0015. |
| [CP-0011 acyclic-call machines](0011-acyclic-calls-machines.md) | Partial; current scope narrowed | The structural recursive AST/function fixture passes. Numeric self-recursion and mutual recursion still fail with MNE130; explicit current-profile probes are in `pressure-current-results.json`. |
| [CP-0012 payload sequence ban](0012-payload-sequence-ban.md) | Resolved | Current Stage-0 accepts the finite sequence payload fixture. |
| [CP-0013 keyword field `next`](0013-keyword-field-next.md) | Resolved upstream; native parser gap remains | Current Stage-0 accepts the field fixture. Native `decl.parse_unit` rejects it under Profile 0.18 (CP-0015). |
| [CP-0014 bool payload regression](0014-bool-payload-regression.md) | Resolved upstream | Current Stage-0 accepts the bool-payload fixture; regression evidence and language fix are linked in the finding. |
| [CP-0015 version-aware frontend](0015-version-aware-frontend.md) | Open, reproduced | Rust accepts all five tested Profile 0.18 syntax examples; native `decl.parse_unit` rejects all five. Compiler architecture. |
| [CP-0016 linked record call validation](0016-linked-record-call-validation.md) | Resolved upstream | The exact compiler-origin call now returns after a generic language runtime fix for nested nominal payload validation. |
| [CP-0017 poisoned-result semantic recovery](0017-semantic-poison-recovery.md) | Resolved in compiler; full semantic twin passes | Both operand orders and the restored 49-case semantic suite match the current Rust diagnostics, including ordered codes/spans, with identical repeated native results. |

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
`mncs-language` revision `709ba00810099e6965bb47dec14ed19e9e1ae6f8`; its
focused generic model tests and the originating compiler call are covered in
the campaign evidence. The remaining current blockers are native compiler
architecture limits. No new MNCS language feature is being proposed by this
reconciliation.
