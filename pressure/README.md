# Language Pressure

This directory records concrete pressure discovered while implementing the compiler in `mncs-language`.

Compiler development runs should not modify `mncs-language` directly. Record pressure here, continue with the smallest reasonable workaround, and address upstream changes in a dedicated language run.

## Categories

- `language` — syntax, type system, ownership, effects, generics, concurrency, metaprogramming, or other language semantics.
- `stdlib-runtime` — collections, hashing, arenas, serialization, channels, atomics, task pools, process APIs, filesystem, memory pools, etc.
- `backend` — behavior or missing capability in WASM/WASI, PTX/CUDA, eBPF, RISC-V/native, or other targets.
- `tooling` — compiler driver, package/build integration, diagnostics, profiling, testing, or developer tooling.
- `compiler-architecture` — pain caused by a poor representation or compiler design rather than a missing language feature.

## Pressure lifecycle

```text
compiler workload
      |
      v
record pressure here
      |
      v
classify + reproduce + measure
      |
      v
dedicated mncs-language run if upstream change is justified
      |
      v
return to mncs-compiler and prove the original case is resolved
```

A finding is not complete merely because an upstream feature was added. Preserve the reproduction/evidence that demonstrates the compiler workload improved.

Use `0000-template.md` for new findings.
## First frontend pass

| Finding | Priority for the next language run |
| --- | --- |
| [CP-0001: bounded source storage](0001-bounded-source-storage.md) | Highest: whole modules cannot fit the current source API |
| [CP-0002: Unicode classification](0002-unicode-source-classification.md) | High: establish a faithful scalar/property path |
| [CP-0003: bounded scan cost](0003-scan-traversal-cost.md) | Measure early termination versus one-pass compiler design |
| [CP-0004: boolean comparisons](0004-boolean-comparison.md) | Low: small local workaround works |
| [CP-0005: test transport](0005-test-transport.md) | Standardize raw oracle/test interfaces when practical |
| [CP-0006: envelope inference](0006-envelope-profile-inference.md) | Align leading-trivia profile inference with syntax |
| [CP-0007: bootstrap evidence cost](0007-bootstrap-evidence-cost.md) | Measure IR size and checked-obligation propagation |

## Declaration-vertical pass

| Finding | Priority for the next language run |
| --- | --- |
| [CP-0008: finite payload visibility](0008-finite-payload-visibility.md) | Highest: blocks multi-module compiler architecture at lowering |
| [CP-0009: iteration fuel chaining](0009-iteration-fuel-chaining.md) | High: affects every pass; silent-exhaustion risk |
| [CP-0010: scalar match dispatch](0010-scalar-match-dispatch.md) | Medium: exhaustiveness checking for opcode tables |
| [CP-0011: acyclic calls force machines](0011-acyclic-calls-machines.md) | High: dominant complexity/cost driver; needs direction |
| [CP-0012: payload sequence ban](0012-payload-sequence-ban.md) | Low: cons-list workaround is exact |
| [CP-0013: keyword field next](0013-keyword-field-next.md) | Low: rename workaround is exact |

## Re-pin pass (Stage-0 `a7a8c05`, profiles 0.13–0.16 available)

| Finding | Priority for the next language run |
| --- | --- |
| [CP-0014: bool payload regression](0014-bool-payload-regression.md) | Blocking: decl/sem verticals cannot elaborate until fixed |
| [CP-0015: version-aware frontend](0015-version-aware-frontend.md) | High: self-parsing gap for 0.13 syntax; decl package staged post-CP-0014 |

The previous pass's closing note is superseded: recursive enums with
scalar/finite/record payloads elaborate, construct, match, and compile
(verified with 25 variants and 6-field payloads), and imported record
results remain supported. The missing capabilities found at compiler
scale are finite-payload visibility across modules (CP-0008), scalar
match dispatch (CP-0010), and direct sequence payloads (CP-0012).
