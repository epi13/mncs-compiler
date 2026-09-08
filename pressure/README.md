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

Record sequences and imported record results are supported in the inspected
pin; no missing-enum/arena/generic/concurrency claim is inferred from this pass.
Those larger compiler workloads have not yet been implemented or measured.
