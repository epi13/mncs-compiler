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