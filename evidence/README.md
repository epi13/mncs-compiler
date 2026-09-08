# Evidence

This directory holds durable evidence for compiler claims.

Examples include:

- Rust vs MNCS differential results,
- self-host stage comparisons,
- canonical semantic/IR/artifact hashes,
- conformance and negative-test summaries,
- backend compile/execute/validate results,
- cold/warm/incremental benchmark reports,
- peak-memory and compiler-cost measurements,
- multi-session and scheduler stress results,
- reproducibility runs across machines/workers,
- resolved language-pressure before/after evidence.

## Rule

Do not describe a compiler capability as complete because source code for it exists. Link or store executable evidence showing that the behavior works with the pinned toolchain revision.

## Succession evidence

As `mncs-compiler` approaches canonical status, maintain a succession matrix covering at minimum:

| Area | Rust Stage-0 | MNCS compiler | Evidence |
| --- | --- | --- | --- |
| Language conformance | reference | pending | |
| Negative tests | reference | pending | |
| Self-host | n/a | pending | |
| IR/backend correctness | reference | pending | |
| Reproducibility | reference | pending | |
| Diagnostics | reference | pending | |
| Cold compile cost | baseline | pending | |
| Incremental compile cost | baseline | pending | |
| Peak memory | baseline | pending | |
| Concurrent agent load | baseline | pending | |
| Bootstrap/recovery | canonical | pending | |

The exact matrix may evolve, but replacement of the Rust compiler must remain evidence-based.