# Native application build contract

`mncs.compiler.application-build-request/1` accepts an exact root source, a
bounded map of imported module sources, and optional explicit generic
specialization requests. Each `generic_seeds` row follows
`mncs.compiler.application-generic-seed/1`:

```json
{
  "module": "mncs.test.suite",
  "function": "summarize",
  "type_arguments": [{"kind": "nat", "value": 4}]
}
```

At most 64 imported modules, 4 MiB of combined source, and 256 specialization
requests are accepted. The compiler resolves and checks that exact snapshot
with the pinned Stage-0 compiler, then asks the compiler-owned canonical VM
emitter for the existing `mncs.vm.artifact/1` format. Specialization identities
are normalized, sorted, and included in the persistent application cache key.

The versioned multi-callable request
`mncs.compiler.application-build-request/2` selects between 1 and 256
non-generic callables with a `callable_roots` array of exact `{module,
function}` identities. Duplicate identities and empty selections are rejected.
The response is `mncs.compiler.application-build/5`; it reports a typed
interface and verification status for every selected callable in canonical
identity order. Its artifact exports those callables and their transitive
verified call closure, plus any explicit generic specializations. The
top-level `entry` field names the first canonical callable for clients that
need one primary target. A selected callable without an admitted executable
body makes the full request unsupported.

`mncs.compiler.application-build-request/3` keeps the `/2` callable selection
and returns `mncs.compiler.application-build/6`. Alongside the artifact, `/6`
carries `source_test_inventory`, the Language-owned `mncs.test-inventory/1`
for first-class `test` declarations in the root source module. The inventory
comes from the same frontend invocation as callable verification and artifact
emission; it retains declaration, test, function, and subject identities plus
current source spans. Its scope remains `source_module` and does not imply a
repository-wide inventory. A valid source with no tests returns an empty
inventory; rejected or unavailable builds return `null`. On an exact cache hit
the source snapshot is identical. When a changed root source is reverified,
the response uses its fresh inventory and spans even if the selected
executable artifact remains reusable. This gives the production Test runner a
single compiler result from which to obtain both an artifact and the root
module's test inventory; consuming that response in `mncs-test` is a separate
integration step.

An exact `/5` or `/6` source-snapshot hit skips the frontend. A changed snapshot always
runs the full Language frontend; if the selected executable closure and
callable interfaces still match a verified artifact, the Compiler reuses that
VM artifact, otherwise it rebuilds the selected SSA closure and artifact. This
does not reuse Language-owned verified facts. The
`tools/test_vm_native_digest_unreachable_body_reuse.py` workload verifies that
an uncalled imported body edit takes the full-frontend path while preserving
the digest executable artifact. The Environment provider accepts request `/1`
with response `/4`, request `/2` with response `/5`, and request `/3` with
response `/6`; its bounded stdout summary preserves each selected callable identity when it fits the 12 KiB
bound. For large selections it reports the root count and points to the
shareable result artifact, which retains the complete typed interfaces. The
focused direct compiler proof is
`python3 tools/test_vm_native_application_batch.py`; the provider regression
also sends `/2` through the adapter and calls `snapshot_witness` from its
seven-callable Test-core artifact in one canonical VM batch.

The Environment descriptor advertises request `/3` and response `/6` as
current while retaining all three request-response pairs.
`python3 tools/test_application_build_test_inventory.py` checks that the
response carries the root module's Language-issued inventory, fresh-process
reuse preserves it, comment edits refresh spans, test-body edits refresh only
the affected test identity, invalid source publishes neither inventory nor
artifact, and the provider adapter publishes the `/6` response artifact.

`mncs.compiler.application-build/4` records framed source-snapshot identity,
source hashes, resolved dependency identity, Stage-0 and producer identities,
artifact and module interface identities, required capabilities, unresolved
obligations, structured source diagnostics, target compatibility, and explicit
unknown resource estimates. The selected entry has its own `verified`,
`unsupported`, or `unknown` status and a `mncs.compiler.callable-interface/1`
value. That callable interface carries the semantic function identity, typed
inputs and outputs, contracts, effects, capabilities, failure mode, and the
referenced record and finite type definitions. Consumers can construct
structured calls from these identities without inferring the ABI from source
text. Type closure is `verified` only when every referenced nominal type
resolves to exactly one declaration; unresolved or ambiguous references mark
the interface and entry `unknown`.

The artifact callable scope is the selected entry plus generic specializations
explicitly requested by generic_seeds. The artifact exports only that exact
set, while internal functions remain in the selected SSA module for calls from
the entry. Unselected declarations with no SSA instance are listed separately
as unselected_unresolved_exports; they do not block a different, closed entry
artifact. A requested entry or specialization without a verified SSA instance
remains in unresolved_obligations and makes the result unsupported. The
response names the callable scope explicitly, and the sealed VM artifact remains
the executable authority. Before serialization, the application emitter now
projects the full verified SSA module to the selected exports' transitive call
closure, including bounded-iteration callees. The projected module is validated
again at the backend boundary and receives an `application-ssa-closure/1:<sha256>`
reference in artifact source provenance. The original selected-SSA identity and
fingerprint remain recorded as provenance for the full compiler output; the
closure reference identifies the exact SSA payload embedded for execution.
Capability requirements and dispatch mappings are derived from this projected
closure. Missing call targets, declaration-only targets, invalid identities,
failed projected-SSA validation, or attached proof relationships reject the
application artifact instead of dropping information silently. The regular
non-application VM emitter continues to serialize its full verified module.

This is a bounded bootstrap adapter with a content-addressed cache for complete
verified application artifacts. An exact snapshot hit reuses its artifact
directly. A second candidate index can reuse a prior artifact when the root
source and build assumptions match and every module in the prior resolved
dependency graph has the same source hash. The response still carries a newly
computed identity and file list for the current full snapshot, so adding or
editing an unreferenced module does not leave stale input provenance. A changed
root or imported module rejects that candidate and recomputes the complete
application closure. This does not yet reuse verified declarations or
functions within a changed closure, and it makes no claim of Stage-1 or
Stage-2 succession. The real consumer exercise is
`python3 tools/test_vm_native_application.py`,
which reads the selected `mncs-test` checkout without modifying it, requests
`equals_bytes<4>` and `summarize<4>`, accepts a typed `ReadinessRequest`, returns
a `ReadinessOutcome`, and compares both successful and typed rejection results
from the Stage-0 oracle with canonical VM execution. The error cases exercise
finite payload extraction and retain the exact artifact receipt. The test also
constructs its argument using the callable interface emitted by the compiler.
`python3 tools/test_native_application_incremental.py`
measures cold and warm builds, reuses the artifact across added and edited
unreferenced modules, checks invalidation for a changed root and imported
module, and verifies that rejected or interrupted requests do not publish
successful results. Its cache observation is now
`mncs.compiler.application-build-cache-observation/4`; the nested
`mncs.compiler.application-build-verification-work/1` reports whether the
full Language frontend ran, counts resolved modules and verified program
functions when available, and separates dependency-closure, SSA, and backend
artifact work. A cache hit skips these stages; it does not claim that separate
Language fact objects were reused. Changed dependency snapshots currently
report one full frontend invocation and an empty `reused_semantic_facts` list,
even when a compatible executable artifact is reused after verification.
This is the measured baseline for integrating a Language-owned fact cache.

The real `mncs-test` readiness application also exercises an edit to the body
of the selected `mncs.test.suite::summarize<N=4>` specialization. A semantic
change to the generic body produced a different executable artifact, and the
Stage-0 result matched canonical VM execution. The changed source still ran
the full frontend; fresh-process reuse of that exact result then skipped it.
The regression is
`tools/test_vm_native_generic_specialization_invalidation.py`, with exact
provenance and measurements in
`evidence/native-app-readiness-test-generic-specialization-invalidation-20261017.json`.
It proves Compiler artifact invalidation for this specialization, not
Language-owned verified-fact reuse.

## Language incremental-fact handoff

The first Language-owned slice should let the Compiler submit a fresh source
snapshot and the previous verified-fact-set identity to the Language
frontend. The response must still contain the complete current program and
diagnostics, while identifying which function-body facts were reused,
reverified, or invalidated. A reusable function fact must bind a stable
function identity, body-content identity, callable signature identity,
Language verifier and policy identity, resolved call and nominal-type
dependencies, generic specializations, and effect/capability assumptions.
Source spans remain fresh snapshot metadata and are not part of semantic fact
identity. Each invalidation must name the changed dependency or assumption;
source names, timestamps, and a matching body fingerprint alone never prove
that verification occurred. Invalid input returns fresh diagnostics and
cannot publish a successful fact set. Fact publication is atomic, and
interrupted verification leaves the prior set usable but publishes no partial
success. This keeps parsing/resolution policy in Language while giving
application builds exact revalidation counts. The existing
`verification_work.reused_semantic_facts` field is reserved for these
Language-issued identities; it intentionally remains empty until that
interface is implemented and consumed.

## Environment capability

The Compiler manifest declares revision 6 of the contract as
`mncs-compiler:native-application-build`, advertises request `/3` and response
`/6` as current, and retains the `/1` to `/4` and `/2` to `/5` pairs. The provider accepts a request from the selected
Compiler checkout with `--request-file PATH`, or from a canonical Store input
artifact with `--request-artifact NAME`. It writes the complete response as
the shareable `native-application-build.json` invocation artifact and keeps
stdout bounded; consumers read the full response through the exact Environment
artifact binding and path. The selected real `mncs-test` digest
workload has already passed the integrated Compiler to Forge to canonical VM
to Environment to Store route with fresh-process artifact readback; its exact
identities and limits are recorded in
`evidence/native-app-readiness-gate-c.json`. That proof used the pure
`build_digest` entry. The effectful `digest_app` capability path remains a
separate, unproven integration milestone.

The contract keeps build and execution obligations distinct. `verified` on an
entry means the selected entry has a verified canonical VM artifact; it does
not imply that every export in the snapshot is supported or that the artifact
has already executed. `unsupported`, `unavailable`, `unknown`, and `rejected`
are preserved as structured outcomes. Provider transport errors remain
invocation errors and do not produce a result artifact.

The provider regression is `python3 tools/test_application_build_provider.py`.
It checks the manifest revision and all supported request/response pairs, a
cold build, a fresh-process warm hit, Store-input byte/hash
validation, the exact canonical artifact identity, precise invalid-source
diagnostics, suppression of successful cache publication for rejected input,
and local VM execution of the two selected callables. This local adapter test
is not evidence of live Environment or canonical Store execution. The
`mncs-test` consumer fixture remains the source and Stage-0 differential oracle
for the application itself.

The probe keeps exact executable SHA-256 as its producer identity. It now hashes
the executable in 64 KiB streaming chunks, and optimizes `sha2` in the debug
profile used by the Stage-0 development probe. This avoids loading the full
binary into memory and prevents an unoptimized digest from dominating fresh
process cache hits; it does not change application-build/5 or /6 cache identity
semantics. The workload-specific measurement is recorded in
`evidence/native-app-readiness-probe-cache-identity-20261016.json`.

Cache entry validation hashes the exact stored response bytes before decoding
that response into the current result. It no longer clones and re-serializes a
large response to verify `application-build-cache-entry/3`. A mismatched
response digest remains a cache miss and triggers source verification; the
digest batch includes a tampered-entry recovery case.
