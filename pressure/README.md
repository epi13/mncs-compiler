# Compiler pressure

These findings originate in `mncs-compiler` workloads. A finding is not
automatically language pressure: classify compiler architecture, runtime,
backend, tooling, and language separately, then reproduce it at the authority
that owns the behavior. Preserve each original reproducer and history; use the
current reconciliation below for live status.

Current locked Stage-0 CLI/bootstrap reference: `a3ac17df69e68f6373cbff336db0a572667d73da`,
profile 0.18. Earlier pins named below (`4f9e122`, `b0f3e64`, `709ba008`,
`843c5bc`) are preserved as historical evidence with their original
measurements. Current pressure probes are in
[`../evidence/campaign-20261001-pressure-suite-results.json`](../evidence/campaign-20261001-pressure-suite-results.json),
and current native syntax results are in
[`../evidence/campaign-20261001-profile-surface-results.json`](../evidence/campaign-20261001-profile-surface-results.json).

## Current reconciliation (2026-10-01)

| Finding | Current status | Current result and owner |
| --- | --- | --- |
| [CP-0001 bounded source storage](0001-bounded-source-storage.md) | Partial | Current Rust accepts the preserved 65-byte case. Native project input now carries exact per-file byte sequences beyond 256 bytes (tested at 1,024 bytes), but Profile 0.18 caps the current generic source bound at 1,024 and the project snapshot at 64 modules. |
| [CP-0002 Unicode classification](0002-unicode-source-classification.md) | Open | Current Rust accepts the Unicode module probe; the native frontend remains ASCII-only. Compiler/frontend gap, not a missing generic language feature. |
| [CP-0003 bounded scan cost](0003-scan-traversal-cost.md) | Open | Current Rust still rejects the `while` reproducer with MNP106. Bounded traversal remains the compiler workaround; no upstream change was required for the CFG workload. |
| [CP-0004 boolean comparisons](0004-boolean-comparison.md) | Resolved | Current Stage-0 accepts boolean equality and negation. Native parser support for current syntax is tracked by CP-0015. |
| [CP-0005 test transport](0005-test-transport.md) | Open, reduced | The cached retained-session probe avoids rebuilding sessions, but Python/Rust still transports test requests and oracle facts. Tooling boundary. |
| [CP-0006 envelope inference](0006-envelope-profile-inference.md) | Open | Leading-comment profile probe still produces MNE002. Stage-0 envelope/tooling behavior. |
| [CP-0007 bootstrap evidence cost](0007-bootstrap-evidence-cost.md) | Re-measured; unresolved obligations remain | Release-mode linked kernel compile repeats byte-identically in 1.090/1.088 seconds at the current pin, with 221 CMP301 obligations. Earlier same-shape timings are retained as historical. No measurement establishes native cost or peak memory. |
| [CP-0008 finite payload visibility](0008-finite-payload-visibility.md) | Resolved | Current producer and imported consumer fixtures both elaborate. The original backend/source-level authority is Stage-0; it does not imply a native compiler module resolver. |
| [CP-0009 iteration fuel chaining](0009-iteration-fuel-chaining.md) | Resolved for tested cases | Current profile accepts a counted bound of 256 and repeated iteration identities. The compiler still uses its explicit bounded scans. |
| [CP-0010 scalar match dispatch](0010-scalar-match-dispatch.md) | Resolved | Current Stage-0 accepts the total integer-match fixture. Native parse/proof/verified-SSA now cover integer scalar match (6 verified functions, 4 corruption rejections); see the project/value-SSA evidence. |
| [CP-0011 acyclic-call machines](0011-acyclic-calls-machines.md) | Partial; current scope narrowed | The structural recursive AST/function fixture passes. Numeric self-recursion and mutual recursion still fail with MNE130; explicit current-profile probes are in `pressure-current-results.json`. |
| [CP-0012 payload sequence ban](0012-payload-sequence-ban.md) | Resolved | Current Stage-0 accepts the finite sequence payload fixture. |
| [CP-0013 keyword field `next`](0013-keyword-field-next.md) | Resolved | Current Stage-0 and native `decl.parse_unit`/`decl.prove_unit` accept the field and projection under Profile 0.18. |
| [CP-0014 bool payload regression](0014-bool-payload-regression.md) | Resolved upstream and natively covered | Current Stage-0 accepts the bool-payload fixture; native parse/proof/verified-SSA cover finite matches and enum construction (10 verified functions, 10 corruption rejections). Backend lowering remains open. |
| [CP-0015 version-aware frontend](0015-version-aware-frontend.md) | Resolved for tested forms | All 21 Profile 0.18 differential cases conform: native parsing and proof accept `!`, negative atoms, repeat literals, `next`, and integer `match` with matching Stage-0 diagnostics. Broader grammar remains bounded. |
| [CP-0016 linked record call validation](0016-linked-record-call-validation.md) | Resolved upstream | The exact compiler-origin call now returns after a generic language runtime fix for nested nominal payload validation. |
| [CP-0017 poisoned-result semantic recovery](0017-semantic-poison-recovery.md) | Resolved in compiler; full semantic twin passes | Both operand orders and the 103-case semantic suite plus nine proof verdicts match current Rust diagnostics, including ordered codes/spans, with identical repeated native results on the retained backend. |
| [CP-0018 verified native-SSA arithmetic](0018-verified-native-ssa-arithmetic.md) | Open | Main's proof-bound C11 adapter (`mncs.native-scalar-ssa/1`) admits only Constant/Call; no integer arithmetic. Backend ownership. Blocks migration off the unattested structural projection. |
| [CP-0019 host-intrinsic callee model](0019-host-intrinsic-callee-model.md) | Open | Native proof reports kind 32 for effect-authorized host-intrinsic calls that Stage-0 accepts. Compiler-architecture ownership. Blocks real family modules at proof. |
| [CP-0020 heterogeneous match env order](0020-heterogeneous-match-env-order.md) | Resolved in compiler | Match lowering reversed block-parameter environments, misaligning arm-to-join edges for mixed-type envs. Fixed at the shared prepare step; scalar/finite/projection sections cover it. |

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
