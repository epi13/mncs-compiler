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

`mncs.compiler.application-build-request/4` returns
`mncs.compiler.application-build/7`. It keeps the bounded explicit callable
roots and adds a required `test_selection`: either `{"kind":"all_source_cases"}`
or `{"kind":"case_identities","test_case_identities":[...]}`. The
selection is resolved only against the Language inventory produced by the
current verified frontend invocation. Test declarations become callable roots
by their Language-issued function identities; callers do not map test names
back to functions. Explicit callable roots can be combined with selected
tests, and the effective root count remains bounded at 256.

`/7` returns `test_case_selection` with the canonical request selection, exact
selected inventory rows, and resolved test callable roots. Case identity
selection rejects stale or duplicate IDs. `all_source_cases` uses the current
root module inventory and rejects a source inventory beyond the same 256-case
bound. Generic Test declarations require an explicit matching
`generic_seeds` specialization. The selection participates in both exact
artifact and cross-snapshot candidate cache keys. A changed source still runs
the full Language frontend; if executable closure reuse is admitted, `/7`
refreshes selected Test rows and their source locations from that frontend.
Invalid source or unresolved case identity produces no artifact.

An exact `/5`, `/6`, or `/7` source-snapshot hit skips the frontend. A changed snapshot always
runs the full Language frontend; if the selected executable closure and
callable interfaces still match a verified artifact, the Compiler reuses that
VM artifact, otherwise it rebuilds the selected SSA closure and artifact. This
does not reuse Language-owned verified facts. The
`tools/test_vm_native_digest_unreachable_body_reuse.py` workload verifies that
an uncalled imported body edit takes the full-frontend path while preserving
the digest executable artifact. The Environment provider accepts request `/1`
with response `/4`, request `/2` with response `/5`, request `/3` with
response `/6`, and request `/4` with response `/7`; its bounded stdout summary preserves each selected callable identity when it fits the 12 KiB
bound. For large selections it reports the root count and points to the
shareable result artifact, which retains the complete typed interfaces. The
focused direct compiler proof is
`python3 tools/test_vm_native_application_batch.py`; the provider regression
also sends `/2` through the adapter and calls `snapshot_witness` from its
seven-callable Test-core artifact in one canonical VM batch.

The Environment descriptor advertises request `/4` and response `/7` as
current while retaining all four request-response pairs.
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
directly. A second candidate index is stable for the root source ID and build
assumptions, so it can locate a candidate after root bytes change. Before
frontend execution, reuse still requires the exact root hash and every module
in the prior resolved dependency graph to match. If the root or a resolved
dependency changed, the full Language frontend runs and the candidate is
admitted only when the freshly verified executable closure, selected callable
interfaces, and Language-owned artifact interface still match. The response
then receives the current snapshot identity and file list, including current
inventory spans. Thus an edit to an unselected root test can reuse the VM
artifact and avoid SSA/backend emission, but it does not skip semantic
verification or reuse Language verification facts. Adding or editing an
unreferenced module also receives current input provenance. This does not yet
reuse verified declarations or functions within a changed closure, and it
makes no claim of Stage-1 or Stage-2 succession. The real consumer exercise is
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
module, and verifies that a fresh process can reuse the artifact after a
comment-only root edit while still running the current parse and resolution
path, reusing guarded Language body facts, and matching the Stage-0 execution
oracle. It also verifies that rejected or interrupted
requests do not publish successful results. Its cache observation is now
`mncs.compiler.application-build-cache-observation/4`; the nested
`mncs.compiler.application-build-verification-work/2` reports whether the
frontend ran, counts resolved modules and program functions, lists Language
body facts actually reused and reverified, and separates dependency-closure,
SSA, and backend artifact work. A cache hit that skips the frontend reports
no Language fact reuse; identities appear only when Language checks their
current guards and reuses them.

The real `mncs-test` readiness application also exercises an edit to the body
of the selected `mncs.test.suite::summarize<N=4>` specialization. A semantic
change to the generic body produced a different executable artifact, and the
Stage-0 result matched canonical VM execution. The changed source runs the
current frontend; unchanged function-body facts are reused while the changed
generic body is reverified. Fresh-process reuse of that exact result then
skips the frontend.
The regression is
`tools/test_vm_native_generic_specialization_invalidation.py`, with exact
provenance and measurements in
`evidence/native-app-readiness-test-generic-specialization-invalidation-20261020T040303.901183Z.json`.
It proves Compiler artifact invalidation and selective Language body-fact
reuse for this specialization workload.

## Language verified-fact reuse

The Language model exposes `mncs.language-verification-fact/1` for successful
function-body checks. Each fact binds the stable body subject and fingerprint,
verifier and policy identities, declaring source profile, callable interface,
actual called-function, nominal-type, assumption, and generic-specialization
dependencies, and effect/capability assumptions. A witness identity binds that
evidence, and the fact lists the changes that invalidate it. Source-location
identities refresh separately and do not affect reuse.

`Program::validate_with_verified_facts` rechecks whole-program declarations,
matches every supplied guard, skips only valid unchanged body checks, and
returns the actual reused and recomputed subjects with miss reasons. Invalid
bodies receive fresh diagnostics and produce no success fact.
`ReferenceCompiler::front_end_with_verified_facts` supplies the exact profile
and source artifact for each linked module while retaining the normal parse,
resolution, and complete semantic-program path.

The Stage-0 application cache stores Language facts in a separate checksummed
field under cache-entry `/4`; existing `/3` entries are not reinterpreted.
Changed snapshots pass those facts back to Language, and
`verification_work.reused_semantic_facts` contains only facts Language actually
matched and reused. Tests cover body, callable-interface, nominal-type,
profile, verifier, and policy changes, fresh diagnostics, and serialization
reuse across a new process.

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

Cache entry validation hashes the exact stored response and Language fact bytes
before decoding them into the current result. It does not reinterpret
`application-build-cache-entry/3`; version `/4` binds the two payloads with
separate digests. A mismatched digest remains a cache miss and triggers source
verification; the digest batch includes a tampered-entry recovery case.
