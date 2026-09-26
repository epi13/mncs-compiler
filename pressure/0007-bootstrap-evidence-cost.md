# CP-0007 — Small frontend emits substantial Stage-0 IR/evidence

Status: current linked artifact remeasurement complete; cost remains measurable

> Historical description and reproductions below preserve the original
> finding. See the current reconciliation at the end of this file and the
> pressure index for live status.

compiler-architecture. Severity: medium. Frequency: each bootstrap
compilation. Upstream tracking: none; the earlier remeasurement was deferred
while CP-0014 blocked loading the declaration core. The current measurement
is recorded below.

## Historical report (Stage-0 `6906d0b`)

The earlier four-module frontend was 342 MNCS lines. Compiling its linked
kernel with `--emit semantic,hir,ssa` completed with 242 CMP301 unresolved
obligations. Serialized artifacts were 1,932,169 semantic bytes, 10,279,372
HIR bytes, and 8,613,578 SSA bytes. Those hashes and sizes are preserved in
[`evidence/compile-results-pre-campaign.json`](../evidence/compile-results-pre-campaign.json);
they are not current-pin measurements.
Reproduction is its recorded compile command; this whole linked request is the
smallest measured workload at that time, not a minimized attribution experiment.

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

## Current reconciliation (2026-09-25)

The old serialized-size evidence is pinned to 6906d0b. Under current
Stage-0 `b0f3e644`, the kernel compile was repeated twice in 10.792 and
10.848 seconds, with byte-identical outputs. `semantic.json` is 1,990,931
bytes, `hir.json` is 2,459,625 bytes, and `ssa.json` is 2,348,993 bytes;
the outer `result.json` is 7,240,844 bytes. Both reports retain 213 CMP301
unresolved obligations and status `completed_with_unresolved_obligations`.
This preserves the earlier same-pin measurement; its executable mode was
not recorded, so the timing is not treated as a controlled comparison with
the release build below. It measures linked artifact sizes and reference
compile time, not peak memory or native compiler cost.

### Release-mode remeasurement (2026-09-25, Stage-0 `b0f3e644`)

The compiler campaign rebuilt the locked Stage-0 CLI in release mode and
repeated the exact kernel compile twice. Wall times were 1.099 and 1.128
seconds, with byte-identical outputs and the same 213 CMP301 unresolved
obligations. Artifact sizes and hashes are recorded in
[`evidence/campaign-20260925-compile-cost-results.json`](../evidence/campaign-20260925-compile-cost-results.json).
This is a current release-mode reference measurement; it does not establish
peak memory or native compiler cost.
