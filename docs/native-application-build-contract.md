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

`mncs.compiler.application-build-request/5` returns
`mncs.compiler.application-build/8`. It preserves the `/4` callable and Test
selection fields and enables reuse of Language-owned function declaration
facts from an earlier verified application snapshot. The Compiler supplies
those facts to Language only after validating the cache entry and its digest;
Language checks each declaration fingerprint, verifier and policy identity,
profile, assumption dependencies, and source-location refresh independently.
The `/8` verification-work observation reports the declaration subjects that
Language actually reused or recomputed, with miss reasons. Requests `/5` and
`/6` retain function declaration-fact reuse; `/6` also carries the Language
source parse cache, and `/7` adds separately verified finite-type facts.

An exact source-snapshot hit skips the frontend for each contract. A changed
`/5` through `/8` snapshot runs the current Language frontend and supplies the
fact sets supported by that request: function declarations for `/5` and `/6`,
finite types for `/7`, and record types for `/8`. Language selectively reuses
facts whose verification guards still match; parsing and dependency resolution
remain part of the changed-snapshot frontend. Independently, if the selected
executable closure and callable interfaces still match a
verified artifact, the Compiler reuses that VM artifact; otherwise it rebuilds
the selected SSA closure and artifact. The
`tools/test_vm_native_digest_unreachable_body_reuse.py` workload verifies that
an uncalled imported body edit takes the full-frontend path while preserving
the digest executable artifact. The Environment provider accepts request `/1`
with response `/4`, request `/2` with response `/5`, request `/3` with
response `/6`, request `/4` with response `/7`, request `/5` with
response `/8`, request `/6` with response `/9`, request `/7` with response
`/10`, and request `/8` with response `/11`; its bounded stdout summary
preserves each selected callable identity when it fits the 12 KiB
bound. For large selections it reports the root count and points to the
shareable result artifact, which retains the complete typed interfaces. The
focused direct compiler proof is
`python3 tools/test_vm_native_application_batch.py`; the provider regression
also sends `/2` through the adapter and calls `snapshot_witness` from its
seven-callable Test-core artifact in one canonical VM batch.

The Environment descriptor advertises request `/8` and response `/11` as
current while retaining all eight request-response pairs.
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
verification or reuse Language verification facts on request `/1` through
`/4`. Request `/5` still reparses and resolves the current source snapshot, then
reuses only Language facts whose verifier, policy, subject, and dependency
guards match that snapshot. Adding or editing an unreferenced module also
receives current input provenance. Neither path makes a claim of Stage-1 or
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
module, and verifies that a fresh process can reuse the artifact after a
comment-only root edit while still running the current parse and resolution
path, reusing guarded Language body facts, and matching the Stage-0 execution
oracle. It also verifies that rejected or interrupted
requests do not publish successful results. This request uses cache
observation `/4` and verification-work `/2` to report whether the
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

The Stage-0 application cache stores Language facts in separate checksummed
body-fact and declaration-fact fields under cache-entry `/5`; cache-entry `/6`
adds the serialized Language parse cache with its own digest, and `/7` adds a
separately checksummed finite-type declaration-fact field. The reader accepts
`/4` as body-only evidence, `/5` as body-plus-declaration evidence, `/6` with
the parse cache, and `/7` with the parse cache and finite-type facts. It never
invents missing facts. Changed snapshots pass their available verified facts
back to Language, and
`verification_work.reused_semantic_facts` contains only facts Language actually
matched and reused. Tests cover body, callable-interface, nominal-type,
profile, verifier, and policy changes, fresh diagnostics, and serialization
reuse across a new process.

The separate Language-owned `mncs.language-declaration-verification-fact/1`
binds a successful function declaration to its subject and semantic
fingerprint, verifier and policy identities, language profile, referenced
assumptions, warning paths, and witness identity. Its invalidators cover
declaration, dependency, verifier, policy, and profile changes. Current source
locations refresh independently of the reusable witness. For request `/5`,
the frontend receives verified declaration facts from a checksummed cache
entry, asks Language to compare their exact guards, and reports actual reused
and recomputed declarations in verification-work `/3`. A frontend-skipping
artifact cache hit reports no Language fact reuse because Language did not
evaluate the guards.

`python3 tools/test_vm_language_declaration_fact_reuse.py` exercises the real
multi-module `mncs-test` readiness application through request `/7` and a fresh
probe process for each build. It checks cold fact production, honest exact
artifact hits, declaration reuse after a comment edit with refreshed source
locations, body-only invalidation for an uncalled imported function, an
affected reachable call chain, imported function signature invalidation, and
finite-type declaration invalidation. Each changed snapshot is compared with
Stage-0 oracle output and canonical VM execution; invalid source must produce a
fresh diagnostic and no cache publication. The workload writes its process and
per-stage timings to a new evidence file.

Request `/7` returns application-build `/10`, preserving request `/6`'s
callable, Test selection, declaration-fact, and Language parse-cache behavior.
It also reuses Language-owned
`mncs.language-declaration-verification-fact/1` facts for finite-type
declarations. The fact's subject fingerprint, witness, verifier
`mncs-language-finite-type-validator/1`, policy
`mncs-language-finite-type-policy/1`, language profile, and dependency guards
must match. The finite-type facts are stored in their own digest-bound field in
cache-entry `/7`; earlier `/6` receipts keep their existing meaning and do not
masquerade as finite-type evidence. Language refreshes the current source
locations independently of the reusable fact. A source edit still parses and
resolves through the current frontend; this slice skips only finite-type
declaration validation when Language establishes that its verified fact
remains valid. Verification-work `/5` reports the finite-type facts actually
reused, recomputed, produced, and their invalidation reasons. An exact artifact
hit reports `not_run`, because Language did not check the fact guards. A failed
frontend that returns a diagnostic without a complete fact observation reports
`not_reported`; it does not imply fact reuse or successful verification.

The workload-specific measurement in
`evidence/language-declaration-fact-reuse-20261020T203906.970341Z.json` records
9 finite-type facts reused on a comment edit, one recomputed and eight reused
after an imported enum change, and a fresh rejection for an invalid duplicate
variant. Parsing and dependency resolution still dominate changed-snapshot
wall time, so these results establish avoided semantic work rather than a
general build-time speedup.

Request `/8` returns application-build `/11` and adds nominal record
declaration facts as a separate checksummed cache field under cache-entry `/8`.
The facts carry the Language record verifier and policy identities and remain
separate from function and finite-type declarations. Verification-work `/6`
reports record facts Language actually reused or recomputed. In the
multi-module `mncs-test` workload, a comment edit reused all 9 record facts and
refreshed current source locations; changing `ReadinessRequest.expected_tests`
recomputed that record and its dependent `ReadinessReport`, while reusing 7
unrelated record facts. The changed application matched both Stage-0 oracle
results and canonical VM execution. The implementation still runs the current
frontend for changed snapshots; measured wall time was 1.206 seconds cold and
1.102 seconds for the comment edit. These single-workload measurements do not
establish a general speedup; the demonstrated gain is reduced semantic
validation work. Full evidence is in
`evidence/language-declaration-fact-reuse-20261020T212245.533651Z.json`.

## Environment capability

The Compiler manifest declares revision 11 of the contract as
`mncs-compiler:native-application-build`, advertises request `/8` and response
`/11` as current, and retains all eight request-response pairs. The provider accepts a request from the selected
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
`application-build-cache-entry/3`; `/4` binds the response and body-fact
payloads, `/5` adds a separate digest for function declaration facts, `/6`
adds the serialized parse cache, and `/7` adds a separate digest for finite-type
declaration facts. A mismatched digest remains a cache miss and triggers source
verification; `/8` adds nominal record declaration facts under their own
digest-bound fields. The digest batch includes a tampered-entry recovery case.

`mncs.compiler.application-build-request/6` returns
`mncs.compiler.application-build/9`. It retains the `/5` callable, Test
selection, and declaration-fact behavior, and enables the Language-owned
`mncs.language-source-parse-cache/1`. The Compiler persists this bounded cache
inside its checksummed application-cache entry. Language admits a parse result
only when its exact sealed source envelope, parser policy, syntax schemas, and
source-profile registry still match. `/9` reports parsed and reused source
identities under `verification_work.source_parse_cache`; these counts come
from Language's parse adapter. Exact application artifact hits report parsing
as not run. The provider descriptor retains request `/1` through `/8`,
advertising request `/8` and response `/11` as current. The focused
fresh-process proof is
`python3 tools/test_vm_language_source_parse_cache.py`: it checks cold parsing,
comment-only root edits with imported parse reuse, changed-import invalidation,
fresh diagnostics, and Stage-0/canonical-VM result equivalence.
