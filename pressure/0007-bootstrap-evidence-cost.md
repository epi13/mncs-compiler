# CP-0007 — Small frontend emits substantial Stage-0 IR/evidence

Status: open. Category: tooling, compiler-architecture. Severity: medium.
Frequency: each bootstrap compilation. Upstream tracking: none.

The implemented four-module frontend is 342 MNCS lines. Compiling the linked
kernel with the pinned CLI and `--emit semantic,hir,ssa` completes with 242
CMP301 unresolved-obligation diagnostics and conservative fallbacks. Serialized
artifacts are 1,932,169 semantic bytes, 10,279,372 HIR bytes, and 8,613,578 SSA bytes.
`evidence/compile-results.json` records sizes, hashes and two identical runs.
Reproduction is its recorded compile command; this whole linked request is the
smallest currently measured workload, not a minimized attribution experiment.

The code uses checked arithmetic for source offsets/depth/counts. Its normal
entry paths keep values within the 64-byte resource envelope; Stage-0 does not
fully discharge the resulting obligations. No checks were removed or switched
to wrapping arithmetic merely to make compilation appear fully verified.

Workaround: preserve fallback obligations, test execution and record hashes/size
summaries rather than committing ~20 MB of generated JSON. Desired behavior:
measure linked IR duplication, serialization overhead and repeated bounds facts
before deciding whether compiler proofs, factoring, or representation changes
are warranted. These sizes are **not peak memory** and are not proof of an
optimizer bug or native performance problem.

Correctness/safety: conservative checks remain; compilation is explicitly
qualified as `completed_with_unresolved_obligations`. Determinism: complete
compile reports and emitted bytes agree across repeats. Runtime and peak-memory
impact: unmeasured. Tooling cost: substantial serialization/storage relative to
the source slice. Complexity: evidence must distinguish successful compilation
from fully discharged verification. Likely owners: Stage-0 tooling and compiler
architecture; no language feature is justified by these measurements alone.
