# MNCS Native Application Readiness: Test conversion assessment

**Assessment date:** 2026-10-16
**Candidate tool:** `mncs-test`
**Current recommendation:** Start with the native-first digest and verify path. Keep the production host orchestration until the native command proves full parity.

## Post-reboot continuation (2026-10-14)

The host journal records an orderly reboot initiated through `systemd-logind` at
17:13:40 AKDT. The request's caller and reason are unknown; no OOM, panic,
watchdog reset, package transaction, or hardware fault was recorded. Current
host resources are healthy. The existing native-readiness campaign resumed in
the same session and worktree, and Control transferred its worktree claim from
version 3 to version 4 after boot-aware owner recovery. Full observations and
limits are recorded in `evidence/post-reboot-recovery-20261014.json`.

After recovery, Gate A's multi-module Stage-0/VM check and Gate B's incremental
cache suite passed. The initial warm exact-artifact path measured 0.123 s versus
0.472 s cold (3.84×). The current Gate B status and finer-grained executable
closure reuse result are recorded below.
The evidence identities captured at that post-reboot checkpoint were
`evidence/native-app-readiness-gate-a.json` (SHA-256
`95c9bb93efc579aa832ebba55c6e50c1570c7332b204baa97e70b4af57eb81dc`) and
`evidence/native-app-readiness-gate-b.json` (SHA-256
`0cb8a4e7a1ff4129d71c59321bdf23e069dba6d6af6978b715ff33549a766ce4`). Later
incremental-build revalidations are recorded below.

The Gate B workload was rerun after the integration work. Its exact-source
fresh-process build measured 0.475 s cold and 0.122 s warm (3.88× for this
workload). A new cache-isolation case copied the same Stage-0 compiler and
appended bytes so its executable identity changed. The altered producer used a
distinct cache key, missed, recomputed source checking, SSA verification, and
the VM artifact, then matched the independent Stage-0 result after canonical
VM execution. The variant build measured 0.498 s / 0.45 CPU s. Compiler probe
identity changed from `sha256:a19061ac544f56530700501f22b144b535dbd68e9fe5f84d958bd9a38bf05e6a`
to `sha256:d538a2e000b045d80ff05b81fd98470b215ee25a747832dc4fd98b4c5a4b197f`;
the cache key changed from `sha256:e69ce62e4d62f66aa92d12422fc21110496164c305e1e7e8c1d3ca5da751c7f9`
to `sha256:589f2568e377ef4b48099406953b2f7e8fa689d8605a785312d01541fa9ab931`.
The measured VM executable was built from the pinned `mncs-vm` checkout at
`1c19a94732c9d24029dd00e2d5af13710f481d02` (debug profile, SHA-256
`1f3f52ac3db092b7f342eff6222c5cd60850419fbe68aff00912b080c5307583`).

Gate C now passes for the selected native application workloads. The real
`mncs.test.digest::build_digest` entry was built from its four-module source
closure, admitted by Forge, executed in the canonical VM, and published with
its Forge receipt through Environment to canonical Store. A fresh process read
both artifacts back through the normal authenticated `artifact_read` surface.
The mixed PASS/FAIL/SKIP input produced the expected structured `FAIL` digest,
including a one-row failure cap and the omitted-failure count. A separate
readiness application call returned both a passing summary and a typed
`InvalidTestCount` rejection. Exact source, compiler, VM, Store, and resource
identities are in `evidence/native-app-readiness-gate-c.json` and
`evidence/provider-input-artifact-capacity-20261014.json`.

The 1,071,468-byte compiler artifact exposed matching 1 MiB limits in the
Environment transfer path and Forge provider. Both were raised to a bounded
2 MiB input limit (the Environment aggregate limit remains 4 MiB), and Control's
bounded response projection now allows the base64 form of a 1 MiB Store result.
Focused Environment, Forge, and Control tests passed. The observed Forge
envelope for this workload peaked at 20,045,824 cgroup bytes, used one
concurrent task, and reported no resource-limit events; that measurement is
specific to this invocation. The current live capability projection reports a
120-second provider timeout for both Compiler build and Forge application
execution bindings, matching their manifests. Forge's measured-work envelope
for this call is tighter at 45 seconds. The previously reported null timeout
is not present in the current bindings.

This proves the selected native application invocation contract. The effectful
`digest_app` now has a local retained-session batch proof, but the complete
production `mncs test` command, Forge-routed `digest_app` execution,
Monitor correlation, and Automation eligibility remain unproven. The selected
Environment session exposes Compiler capabilities only; it has no bound Forge
application-execution capability. Language-owned verified-fact work also
remains outside this Compiler campaign's write authority.

The effectful `digest_app` proof and canonical VM artifact were subsequently
published as one Environment verification invocation and read back through the
authenticated Store artifact surface. The proof covers three successful
structured read/write calls with Stage-0 agreement, seven calls in one retained
VM process, and the recorded capability, artifact, typed-input, publication,
and fuel failures. The exact report and VM artifact chunk hashes are in
[`evidence/native-app-readiness-effectful-digest-store-readback-20261017.json`](../evidence/native-app-readiness-effectful-digest-store-readback-20261017.json).
This remains verification-output publication through the bounded test file
provider; it is not a Forge application-execution receipt.

The same bound Compiler verifier passed again on commit
`8d3669c96428027056983e7b666c6588a771eb54`. Campaign continuation selected the
single active Compiler session, and authenticated `artifact_read` returned all
three files from canonical Store with byte counts and SHA-256 values matching
the invocation manifest. The rerun measured a 0.581 s cold build, 0.125 s
fresh-process warm hit, and 0.362 s comment-edit build; the comment edit still
ran the full frontend and reused zero Language facts. See
[`evidence/native-app-readiness-effectful-digest-store-readback-rerun-20261017.json`](../evidence/native-app-readiness-effectful-digest-store-readback-rerun-20261017.json)
for the exact invocation and readback identities. This remains a Compiler
canonical VM test-provider run and does not exercise Forge.

A fresh attempt to publish the `/4` application-build result through the
selected Compiler campaign was refused by Control with
`ENVIRONMENT_AUTHORITY_DENIED`. Control currently requires the separate
`mncs-environment` project grant for this build invocation, while that project
cannot resolve the Compiler campaign identity. The selected session's
authenticated writable set contains only `mncs-compiler`, and the refused
operation left no invocation artifact. Forge-routed `digest_app` execution
therefore remains unproven until the authorized Environment/Control route can
bind this campaign without widening its worktree scope.

## Retained-session batch follow-up (2026-10-16)

Compiler commit `a6151ca832c29feeba110d7b3068464d31760bb2` extends the
Compiler-owned `digest_app` harness with the versioned
`mncs.compiler.native-application-batch-request/1` contract. It admits one
verified artifact and opens one canonical VM `Session`, then executes seven
calls with unique request, input, and output paths. Three successful results
match the Stage-0 oracle exactly. Missing capability admission, invalid typed
input, rejected output publication, and a one-step budget interruption are
interleaved; subsequent calls still succeed. Separate runs reject a mismatched
capability identity, mismatched dispatch alias, and tampered artifact identity.
Each call has an independent VM execution record and resource budget under the
same artifact and runtime identity.

Compiler commit `8d44d0b57aea1807ef5131094c8dfcd364dae449` makes sibling-root
overrides use the standard `MNCS_TEST_ROOT`, `MNCS_STDLIB_ROOT`,
`MNCS_LANGUAGE_ROOT`, and `MNCS_VM_ROOT` names.

The batch took 0.031 s / 0.010 CPU / 11.7 MiB peak RSS in one process. The
Compiler build measured 3.02 s / 2.97 CPU; its separate Stage-0 oracle took
0.350 s / 0.340 CPU. The probe process peaked at 1.06 GiB RSS. These are
measurements for this selected workload; cgroup memory counters were
unavailable. The test harness now locates sibling repositories
from the selected nested Compiler worktree or explicit `MNCS_*_ROOT` overrides,
and seeds its isolated offline Cargo build from the pinned VM lockfile.

Current evidence is `evidence/native-app-readiness-test-digest-app.json`,
schema `mncs.compiler.native-application-proof/2`, SHA-256
`28338c927e9f3666118dcb7fb69700f404f13e23257aad639b249b1e0892ee36`. It pins
Compiler `8d44d0b57aea1807ef5131094c8dfcd364dae449`, Test
`57802fba4dea04ad50004d3e1e6768b507b889bb`, Stdlib
`6aa443cacf8c80a8ccb0fe6586b1f8c68575bf6c`, Language
`3cfbc094f1a1770fc2409b990f3f7d8912f49bc7`, and VM
`1c19a94732c9d24029dd00e2d5af13710f481d02`. Stage-0 is pinned at
`e8f8a5f8659c71b33cd32c964d4982bec1a9668e`. The host adapter remains a
bounded test fixture, not a production provider. This work did not reuse
Language verification facts or advance the compiler self-consumption matrix.
The authenticated Forge/Environment/Store route for `digest_app` remains the
next integration target.

## Multi-callable executable-closure reuse (2026-10-16)

Compiler commit `f36bf6f` enables the existing verified executable-closure
cache for application-build/5 multi-callable requests. The cache index locates
a previously verified candidate; every changed source snapshot still runs the
full Language frontend. The artifact is reused only when the selected
executable closure, each selected callable interface, and the
Language-owned whole-program artifact interface all match. Reused responses
carry the current source snapshot, graph, diagnostics, and all selected
callable interfaces.

On the real four-module `mncs.test.digest` callable set, the 9.22 s cold build
produced a 1,160,570-byte artifact with 123.6 MiB peak RSS. Adding a comment to imported
`mncs.test.assertions` changed the source snapshot; the full frontend still
checked all 30 functions across four resolved modules, then reused the same
artifact and refreshed all three callable interface rows in 6.74 s / 6.69 CPU s
with 123.3 MiB peak RSS. A
semantics-preserving edit to reachable `mncs.test.suite::empty` changed
`total: 0` to `total: 0 +% 0`; the executable closure changed, so the Compiler
regenerated the artifact in 9.36 s / 9.31 CPU s with 123.3 MiB peak RSS. Its
five canonical VM calls matched the
pinned Stage-0 results. A fresh process then reused that new exact snapshot
without a frontend invocation in 4.40 s. The VM batch took 0.192 s, peaked at
27,080 KiB RSS, and used 1,983 total steps.

These measurements are specific to this workload. The changed-snapshot path
still performs complete semantic verification, and
`language_verified_facts_reused` remains zero. This advances multi-callable
artifact maintenance; it does not complete Language-owned fact reuse or the
Forge/Environment/Store `digest_app` route. Evidence is
`evidence/native-app-readiness-digest-multi-callable-reuse-20261016.json`,
SHA-256 `05cd710cae196905fca285c3843ee73f67df1556697e27510e3386db56d69b37`.

## Structured digest provider and Test provider frontier (2026-10-17)

Compiler commit `cd2fc1bd760fb020c75fd2da97ad58b6a356408a` extends the
canonical VM runner's bounded test provider to bind multiple verified
capability identities in one invocation. It validates each identity against
the artifact and admitted SSA, keeps the source dispatch aliases distinct,
and derives the caller-scope capability key needed to enter a nested function.
The test provider can read and write typed fixtures and calculate the
language's type-directed external `structured_digest` projection from the
admitted SSA record declarations. Unsupported shapes fail closed.

`tools/test_vm_native_app_runner_structured_digest.py` builds and executes a
typed record digest application. Its canonical VM result exactly matches both
the Stage-0 oracle and an independent compact-JSON/SHA-256 calculation,
including a fixed 32-byte hex field and a UTF-8 bounded byte view. The VM call
took 0.002 s and peaked at 4.5 MiB RSS. This is a bounded Compiler test
provider, not Forge production-provider evidence.

The same runner compiled the real `mncs.test.provider::provider_batch_app`
and admitted its `provider_artifact` and `provider_digest` identities. The
canonical VM completed `structured_read` and the first `structured_digest`,
then stopped at `instruction sequence_copy` while the MNCS function copied
the digest into its fixed-size identity. No result or check artifact was
published. This records a VM execution gap; it does not complete provider-batch
execution. Missing capability admission still returns `capability_denied`,
and incorrect capability identities and dispatch aliases remain rejected.

The existing `mncs.test.digest::digest_app` regression still passes all three
canonical VM cases and the four interleaved retained-session batch cases.
Its build measured 0.534 s / 0.490 CPU / 62.6 MiB peak RSS; the retained
batch measured 0.036 s / 0.020 CPU / 15.2 MiB peak RSS in one process.
These are workload-specific measurements; cgroup memory counters were
unavailable.

Evidence files are `evidence/native-app-readiness-structured-digest-provider.json`
(SHA-256 `f300f8d7f5110b0e20737ec5966ca5b15d57d8b83bf3ef6396ac93257c328038`),
`evidence/native-test-provider-app-boundary-20261017.json` (SHA-256
`4b2a8a51ba60756bdda907207df7d8d7878bb07ab9a9269d03c4b5921461aeb2`), and
the refreshed digest-app evidence at
`evidence/native-app-readiness-test-digest-app.json` (SHA-256
`1a3d0a60b1cef17f62b43777163099cab10a89549aa8ad370187455d347dca45`).
All three pin Compiler `cd2fc1bd760fb020c75fd2da97ad58b6a356408a`. No
Language verified facts were reused and the self-consumption frontier did not
advance. The local provider now passes an eight-case heterogeneous batch that
combines the seven-case `mncs-test` self-suite with a separately compiled Test
module. See `evidence/native-test-provider-app-boundary-eight-case-20261019.json`
for its Stage-0-matched result and artifact-identity rejection evidence. The
retained-session follow-up at
`evidence/native-test-provider-app-retained-session-20261019.json` passes the
same valid batch, a wrong-identity batch, and the valid batch again through one
admitted VM artifact. Forge/Environment/Store routing and canonical Store
readback remain outstanding.

The active campaign's selected Environment definition lists only
`mncs-compiler` as a writable repository and protects `mncs-language`.
Neither a Language nor VM write claim is present in this session. Compiler's
repository instructions require a dedicated Language run, while the VM
repository owns `sequence_copy` execution. The Compiler-owned work above did
not modify either repository.

## Application artifact and Test inventory co-result (2026-10-17)

Application-build request `/3` and response `/6` return the Language-owned
`mncs.test-inventory/1` for first-class tests in the root source module next to
the selected callable artifact. The inventory comes from the same frontend
result, retaining its source and semantic identities, test identities, and
current source spans. This removes the need for an additional frontend pass
when a consumer needs both products. The scope is deliberately
`source_module`; repository-wide inventory and consumer migration remain
separate work.

`tools/test_application_build_test_inventory.py` passed the cold build,
fresh-process exact hit, comment-edit location refresh, unselected test-body
identity update, and invalid-source checks. The cold build reported one
frontend invocation; exact reuse reported zero. The unselected test-body edit
preserved the selected executable closure while changing only that test's
body-sensitive identity. A comment edit and a fresh-process unselected body
edit each ran the full frontend to refresh current inventory spans and
diagnostics, then reused the existing verified VM artifact and skipped
SSA/backend emission.
Language verification facts reused: zero. This is
Compiler API progress, not completion of the production `mncs-test` runner
integration. Evidence is
`evidence/application-build-test-inventory-20261017.json`.

The follow-up now selects `selected_entry`, `first_case`, and `second_case` in
one application-build/6 request. Its single frontend result supplies both the
verified artifact and the Test inventory; the inventory's two stable case
identities drive one `mncs-vm batch` invocation over that same artifact. Both
typed results (`1` and `2`) match Stage-0. This selected zero-argument slice
measured 0.114 s for the build and 0.006 s for the VM batch, with a 20,292 KiB
VM peak RSS; cgroup memory was unavailable. It does not integrate the
production mncs-test consumer or resolve generic test specializations.

The follow-up also compiles the real mncs-test/tests/closure_suite.mncs
source together with its mncs.test.assertions and mncs.test.suite
dependencies. One application-build/6 frontend result returned both
compiler-issued Test identities and the selected artifact for
independent_arithmetic and independent_boolean; one canonical VM batch
returned their typed passing TestResult records, matching Stage-0. This run
measured 0.410 s for the build, 0.094 s for the batch, and 23,744 KiB VM peak
RSS; cgroup memory was unavailable. The source came from mncs-test revision
57802fba4dea04ad50004d3e1e6768b507b889bb. Language verification facts reused:
zero. This exercises the Compiler/VM contract on real Test sources; production
runner migration, Forge integration, and generic selection remain open.
Evidence is evidence/application-build-test-inventory-20261017.json.

## Test-case identity selection (2026-10-18)

Application-build request /4 and response /7 add Language-issued
test_case_identity selection to the Compiler contract. A request can select
all Test declarations in the root source inventory or a bounded, explicit set
of body-sensitive case identities. The Compiler resolves those identities
against the current frontend inventory, merges the resulting functions with
explicit callable roots, and returns the exact rows and callable mapping.
Stale identities and invalid source produce no artifact. Generic Test
declarations require matching explicit specialization seeds.

The real mncs-test provider-fold workload selected four cases by their exact
identities, then ran four typed scenario calls and all four Test callables in
one canonical VM batch. All eight results matched Stage-0, and the fresh
process exact hit reported zero frontend invocations. A comment edit refreshed
selected source spans, a changed Test body invalidated its old case identity,
and invalid source returned no inventory or artifact. The Compiler provider
adapter also published a /7 result. Language verified facts reused: zero;
this advances native Test selection and execution coverage without changing
the production Test runner. The identity-selected build measured 0.998 s wall,
0.96 s CPU, and 45.0 MiB peak RSS. Its fresh-process hit measured 0.112 s and
6.95 MiB peak RSS. The VM batch measured 0.073 s and 22,932 KiB peak RSS.
Cgroup memory was unavailable. Evidence is
evidence/native-test-provider-fold-batch-test-case-selection-edges-20261018.json.
The Environment provider adapter test also published the /7 response for the
two-case source inventory fixture; its measurements and identities are in
evidence/application-build-test-inventory-v7-20261018.json.

The actual seven-case mncs-test self-suite now runs through the
application-build/7 all-source selection and canonical VM batch contract. It compiles
tests/self_suite.mncs with five Test library modules and task from the
Compiler-pinned Language revision e8f8a5f8659c71b33cd32c964d4982bec1a9668e.
One frontend resolves seven modules and 41 functions, selects all seven
compiler-issued Test identities from the Language inventory without caller
callable mapping, and emits the selected artifact. One VM batch runs that
artifact for two rounds. Six cases return passing typed TestResult values and
skipped returns the expected typed skip; all 14 results match Stage-0. The
build measured 0.590 s wall, 0.55 s CPU, and 29.2 MiB peak RSS. The VM batch
measured 0.074 s and 22,204 KiB peak RSS. Cgroup memory was unavailable.
Language verification facts reused: zero. The production host runner and
Forge execution remain separate integration work. Exact Test and Language
source identities are recorded in
evidence/application-build-test-inventory-v7-20261018.json.

## Identity-selected self-suite execution (2026-10-19)

The shared Test batch adapter now accepts the exact `cases` projection from
application-build/7 selection. It requires every selected row to match the
current Language-issued inventory entry, rejects duplicate or stale rows, and
rejects argument bindings for cases outside the selected set. The real
seven-case `mncs-test` self-suite selected `arithmetic` and `skipped` by their
exact `test_case_identity` values. One frontend emitted the selected artifact
while retaining all seven inventory rows; one canonical VM batch invoked only
the two selected cases twice. All four typed results matched Stage-0, including
the pass and skip outcomes. The selected build measured 0.424 s wall, 0.38 s
CPU, and 26.1 MiB peak RSS; Stage-0 took 0.392 s; the VM batch measured
0.017 s and 21,420 KiB peak RSS. Cgroup memory was unavailable. Language
verified facts reused: zero. This strengthens exact case selection and batch
dispatch in the Compiler harness; it does not migrate the production Test
consumer or add Forge execution. Evidence is
[`evidence/application-build-test-inventory-identity-selected-batch-20261019.json`](../evidence/application-build-test-inventory-identity-selected-batch-20261019.json)
(SHA-256 `423943d8096e804cdc7584e2ce9685effb154e128f5e3406048135731673514c`).

## Heterogeneous Test artifact batch (2026-10-19)

The provider boundary now accepts the per-row `artifact_identity` already
defined by `ProviderTestExecution`. The previous harness restriction requiring
every selected test to come from one artifact prevented it from exercising the
protocol's actual identity contract. The run compiled
`tests.provider_cross_module::separate_module_identity` from the real
`mncs-test` source into a distinct application-build/7 artifact, executed it in
the canonical VM, and combined its compiler-issued test, declaration,
callable, and signature identities with the real self-suite `arithmetic` case.
The two source tests came from distinct artifact identities. Each individual
TestResult matched Stage-0; the native provider app then checked the row-level
bindings and published a two-case passing summary and check record exactly
matching the independent Stage-0 provider fold.

The second Test artifact build measured 0.207 s wall, 0.18 s CPU, and 15.3 MiB
peak RSS. Its VM call measured 0.0116 s and 20,684 KiB peak RSS. The provider
app's heterogeneous fold measured 0.0374 s and 22.4 MiB VM peak RSS; Stage-0
took 0.689 s wall, 0.66 s CPU, and 65.4 MiB peak RSS. Cgroup memory was unavailable.
This closes the local sequence-copy execution gap recorded in the earlier
provider boundary report and proves the compiler/VM/provider contract for a
small multi-artifact batch. It still runs through the local bounded structured
file adapter; Forge, Environment, Store publication, and fresh-process Store
readback remain open. A negative mixed-batch case changes the cross-module
Test row's artifact identity to the other row's valid artifact. The provider
returns typed `UNSUPPORTED` with code 9001 rather than accepting the neighboring
binding, matching Stage-0 exactly. This rejection took 0.0379 s and 22.6 MiB VM
peak RSS. Language verified facts reused: zero. The positive and negative
results are recorded in
[`evidence/native-test-provider-heterogeneous-identity-20261019.json`](../evidence/native-test-provider-heterogeneous-identity-20261019.json)
(SHA-256 `7131bf342b339727136a94975932d5e94f5efcf94fb279817c5539bd5d8cd8cf`).

The real five-module mncs-test readiness application also now verifies a
comment-only root edit from a fresh process. The full frontend ran once across
five resolved modules and 24 functions; the selected executable closure,
callable interfaces, semantic identity, and artifact identity remained stable,
so SSA/backend artifact work was reused. Canonical VM output matched the
independent Stage-0 oracle. This run measured 0.279 s for the changed-root
request. Language verification facts reused: zero. The full cold and warm
workload measurements and verification telemetry are in
`evidence/application-cache-index-root-edit-20261017.json`.

## Native Automation evaluation workload and alias identity fix (2026-10-17)

The Compiler now includes both requested import identities and resolved
declaration identities in its application semantic identity, each tied to the
frontend's module-resolution row and semantic fingerprint. This covers the
supported unversioned import to versioned declaration pattern used by
Automation (`mncs.automation.schedule` to
`mncs.automation.schedule.v1`). Previously, the requested identity appeared
in `Program.dependencies` but not in the identity set, so valid applications
received no semantic identity and their verified executable-closure candidate
was never considered.

The real Automation schedule, trigger, and evaluation modules plus Test's
Assertions and Suite modules compiled into one application-build/5 artifact.
Seventeen deterministic `evaluate_tick` and `evaluate_resource_headroom`
inputs matched the independent Stage-0 oracle in one canonical VM batch. A
fresh process reused the exact-source response without a frontend invocation.
A comment-only Assertions edit ran a fresh full frontend check, reused the
same artifact through `verified_application_executable_closure`, and retained
the same artifact identity. Cold build measured 1.589 s / 1.530 CPU s / 128.1
MiB peak RSS; exact-snapshot reuse measured 0.149 s / 0.130 CPU s / 46.0 MiB;
the comment edit measured 0.752 s / 0.720 CPU s / 101.2 MiB. The VM batch took
0.456 s / 0.430 CPU s and peaked at 35,952 KiB. cgroup memory counters were
unavailable. These results are workload-specific.

This is executable-artifact reuse after complete source verification; no
Language verified facts were reused, and the changed snapshot still performed
whole-frontend semantic checking. Forge/Environment/Store execution and
replacement of Automation host orchestration remain unproven. The focused
Compiler unit test uses the supported unversioned-to-`.v1` import, and the
real workload harness is `tools/test_vm_native_automation_application.py`.
Evidence is `evidence/native-app-readiness-automation-evaluation-executable-reuse-verified-20261017.json`
(SHA-256 `0ea6816e3fb3af1bf80b7e634dc95e264961cbc635beaa7b38d8acd35f2fb000`).

## Automation executable-closure invalidation checks (2026-10-17)

The Automation harness now tests three changed-source paths on the same
application. A comment-only Assertions edit and an uncalled schedule test-body
edit each ran the full frontend and reused artifact
`sha256:8412ca93ef536d7a583eccd3a4a679628009538634436a919e88fa9347b4332a`.
Changing the reachable `reason_cond_false` body changed the
`watch-cond-false` result, regenerated the artifact as
`sha256:cf7c3eca8408651bd65ff68f36dd3f07c5e38c68f95dd925c1dc957776f67219`,
and matched the fresh Stage-0 oracle in the canonical VM. Replacing that body
with an unresolved function produced a fresh `MNE131` rejection with no
artifact; the cache reported `bypass`.

The 18-case baseline matched Stage-0 and the canonical VM. The cold build took
1.600 s / 1.540 CPU s / 127.9 MiB peak RSS; exact-source fresh-process reuse
took 0.148 s / 0.130 CPU s / 46.1 MiB; the comment and uncalled-body edits
took 0.784 s / 0.730 CPU s / 101.4 MiB and 0.740 s / 0.730 CPU s / 105.4
MiB. The reachable edit build took 1.470 s / 1.440 CPU s / 128.5 MiB, its
Stage-0 oracle took 2.238 s, and its VM batch took 0.453 s / 0.440 CPU s with
36,316 KiB peak RSS. cgroup memory counters were unavailable.

Each changed valid snapshot still received a full frontend invocation and
`reused_semantic_facts` stayed empty. This verifies Compiler executable
closure invalidation and fresh diagnostics; it does not implement Language-
owned verified-fact reuse or the Forge/Store application route. Evidence is
`evidence/native-app-readiness-automation-incremental-invalidation-20261017.json`
(SHA-256 `fe0a450f22f2a3840a776287f6d67858755ffe921b8e5e52bf300c6e24f26a2e`).

## Imported interface invalidation (2026-10-17)

The Automation application harness now changes the imported
`mncs.automation.schedule::next_after` signature. With callers unchanged,
application-build/5 ran the frontend, returned fresh `MNE172` diagnostics, and
emitted no artifact. Updating the imported module's six internal calls and the
three application callers produced a new artifact
`sha256:908dd61ac6648153eda442759f7d52c9e19c487085e19002d280532336f5d8ef`;
all 18 results matched Stage-0 in the canonical VM. The stale-consumer check
took 0.073 s; the updated build took 1.454 s, followed by a 2.209 s Stage-0
oracle and 0.445 s VM batch. Peak RSS was 105.0 MiB for the stale edit and
128.5 MiB for the updated build; cgroup memory was unavailable.

This establishes imported function-interface invalidation and fresh
diagnostics on the real Automation/Test source closure. The Compiler still
rechecked the full frontend, and Language facts reused remained zero. Evidence
[`evidence/native-app-readiness-automation-interface-invalidation-20261017.json`](../evidence/native-app-readiness-automation-interface-invalidation-20261017.json)
has SHA-256 `b25d5d47d9ff658aab578898fc37bf22f62bb633228d787e990834f27bacf2b6`
and records the exact application harness hash. This does not prove nominal
type invalidation, Forge routing, or Language-owned fact reuse.

A combined follow-up also renamed the imported `NextOccurrence.at_ms` field.
The stale constructor and projection set was freshly rejected with `MNE172`
and no artifact. After updating the nine constructors and dependent projections,
the Compiler generated artifact
`sha256:c1c55a325e0352756b038c9afb42b5f730f0a9e80f65d69f8cf1299acfab6d3f`; all
18 Stage-0 results matched the canonical VM. The stale build took 0.069 s, the
updated build 1.475 s, the oracle 2.200 s, and the VM batch 0.449 s / 0.430 CPU
s with 35.8 MiB peak RSS. Peak RSS for both compiler builds was 129.0 MiB;
cgroup memory was unavailable. The combined evidence also repeats the imported
function-signature case and records the exact harness hash:
[`evidence/native-app-readiness-automation-dependency-invalidation-20261017.json`](../evidence/native-app-readiness-automation-dependency-invalidation-20261017.json)
(SHA-256 `0538e424a111d64ca820d8c64989d2c74b153b619f485375f9c2704623bd908b`).
The frontend still rechecks every changed snapshot and Language facts reused
are zero.

## Test generic-specialization invalidation (2026-10-17)

The native Test readiness application now has a focused generic-body
invalidation regression. The harness changes only the imported
`mncs.test.suite::summarize<N=4>` body so that its returned total increments by
one. The Compiler rebuilt the specialized artifact from
`sha256:55c18fc3ffe204e9c6326e4675e233d524d67d93facb3ccbe76562541f5b68e7` to
`sha256:1a3f36febec108e0c3218728e5c2cea71ebd9ae902a8ae10d69bbb5e1f73e2ac`.
The Stage-0 oracle and canonical VM both returned the changed total of 5
(baseline 4). The changed source used one full frontend invocation; a fresh
process then reused the exact artifact with zero frontend invocations. Build
times were 0.526 s cold, 0.441 s after the generic edit, and 0.125 s for the
fresh-process hit. Peak RSS was 56.8 MiB, 58.1 MiB, and 23.6 MiB respectively;
cgroup memory counters were unavailable.

This verifies generic-specialization artifact invalidation and current
execution behavior, while Language facts reused remained zero. A Compiler unit
test also asserts that a semantically changed selected generic body changes the
executable-closure identity. The `mncs-test` checkout remained read-only. Evidence
[`evidence/native-app-readiness-test-generic-specialization-invalidation-20261017.json`](../evidence/native-app-readiness-test-generic-specialization-invalidation-20261017.json)
has SHA-256
`cdcde90d6cb328e3c40aa670fbe2029cdcc5510ad1d0fb64f0bf230d90c554f0` and records
the exact Compiler, Stage-0, VM, Test, and harness identities.

## Evidence boundary

This assessment uses the read-only `mncs-test` checkout at `f9575e09ec3481c0ded52bb9e3ec72940725aff6`, Stdlib `6aa443cacf8c80a8ccb0fe6586b1f8c68575bf6c`, Language `3cfbc094f1a1770fc2409b990f3f7d8912f49bc7`, VM `1c19a94732c9d24029dd00e2d5af13710f481d02`, and Compiler producer commit `24dda63ae59e0505969c490604e14348dab1f7d7`. The Stage-0 reference revision is `e8f8a5f8659c71b33cd32c964d4982bec1a9668e`, release probe SHA-256 `190d0bf1128f24962fbf56b6d5701ac60ffbe04bceda4c59064671f103714c58`; the VM binary SHA-256 is `2f7bd4a9f6b537b17e4c1dbf08bcc8cd55362a3f9f1bc28bd91a265a30e1e7f5`.

The readiness application compiles the `run` entry and two explicit generic specializations from a five-module Test source closure. Its typed report, nominal records, finite payloads, bounded sequences, matches, and typed failures agree between Stage-0 and the canonical VM. An invalid field projection reports `MNE162`. The initial Gate A evidence used artifact identity `sha256:cff9e5cc4cb47ea19096ec735384263fb87d211d3d7d835173b56bca3e2ea879`; the post-reboot rerun and current artifact identity are in the continuation above.

The real `mncs.test.digest::build_digest` function compiles under application-build/4 from its Digest, Assertions, Suite, and Stdlib application sources. Stage-0 and three canonical VM calls agree on pass/fail/skip aggregation, failure caps, omitted-failure counts, and all-pass results. Artifact identity is `sha256:3bd4cc51f4e61ff91131f1d6d65be73615ebaf19ebc66deca75e2a0d7e3ee3df`; callable interface is `sha256:3c08227564c44096355994ec3689e817d710ca5848581880dc278d5cd7e434f9`. Evidence is `evidence/native-app-readiness-test-digest.json` (SHA-256 `3ad5ba0758dfce1c7fed304bc3e91d37ff30b867ce244f144b98304ab025d694`). Cold build measured 0.628 s / 0.590 CPU / 65.0 MiB peak RSS; Stage-0 took 0.354 s; VM calls took 0.210–0.212 s at about 28 MiB. These are bounded application measurements, not full `mncs-test` performance.

A second application, `mncs.test.digest::digest_app`, exercises typed application input/output and structured read/write effects through the canonical VM. The compiler response includes `mncs.compiler.capability-dispatch/1`; both `HostCall` rows map canonical identity `mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact` to runtime key `test_artifact`, with operation and instruction provenance. The Rust runner only supplies the canonical VM host and the explicit test-artifact capability; it cross-checks the contract mapping against admitted SSA and contains no digest policy. Stage-0 and VM results agree for all three cases. Missing provider admission returns `capability_denied`; rejected output publication returns typed application exit 3 and leaves no output.

The original effectful-app proof used artifact `sha256:95cf2d3e3361b7089336c82e0cd4870dfe74775547831eed90879555d494f919` and evidence SHA-256 `ef625aca37e674d493dc64f6f45fe13c380d9770cc631c54eca34936be72fa89`; it is retained as the historical baseline. The projected artifact and current retained-session batch proof are recorded in the follow-up above.

Gate B remains partial. The latest readiness-suite revalidation is
`evidence/native-app-readiness-gate-b-20261020T012132.473487Z-2422075.json`
(SHA-256
`f35aa4f45af4d001e34c8666949a68fa8b9f3cf1f43cff1a5909aacd1e21fd09`) at
Compiler `428b0d12ed0d4ec7bbf69bb57c9b6dd533d2373d`. The cold build measured
0.672 s / 0.64 CPU s / 39.4 MiB peak RSS. A fresh-process warm hit measured
0.108 s / 0.09 CPU s / 11.3 MiB (6.21× for this workload). A root comment
edit took 0.819 s and reused the executable artifact after one full frontend
run; an uncalled function edit took 0.411 s with the same full-frontend and
artifact-hit behavior. Reachable dependency, exported nominal interface, and
generic specialization edits rebuilt, and their canonical VM results matched
Stage-0. Invalid source returned fresh diagnostics and published no successful
artifact. Interrupted compilation published no partial cache entry; retry and
fresh-process reuse passed. cgroup memory counters were unavailable. At this
2026-10-20 checkpoint, changed snapshots reused zero Language verification
facts. The follow-up below supersedes that fact-reuse status; the earlier
artifact invalidation and interruption results remain historical evidence.

The `/4` producer exposes source snapshot and dependency graph identities,
selected callable interface, artifact provenance, build assumptions, structured
diagnostics and obligations, target compatibility, resource intent, and verified
capability-dispatch bindings. The Environment-facing provider returns the same
dispatch contract in its invocation summary. The integrated `build_digest`
call now exercises the actual Store-backed Compiler → Forge → canonical VM
route, with fresh-process result and receipt readback. A pre-fix oversized
artifact was explicitly rejected as a failed invocation; it was not represented
as a successful result. Existing Environment coverage also verifies that a
latest failed invocation cannot fall back to an earlier successful artifact.
`tools/test_application_build_provider.py` continues to cover cold,
fresh-process warm, invalid-source, unsupported-schema, Store-input-artifact
reuse, and corrupt-input cases. Production `mncs test` retained-session/batch
consumption, Monitor correlation, and Automation eligibility remain unproven.

## Language-owned verification fact reuse in Compiler Project (2026-10-21)

The Compiler application-build now consumes Language's
`mncs.language-verification-fact/3` and declaration facts. The real eight-module
Compiler Project workload contains 2,031 cold body facts and 2,031 declaration
facts. A comment-only edit reused all 2,031 of each. Editing an uncalled helper
recomputed that body and reused 2,030 body facts. Editing a reachable function
recomputed its 15-function dependency closure and reused 2,016 body facts. The
fact observations identify recomputation as `subject_changed` or
`dependencies_changed`, rather than inferring reuse from artifact-cache hits.

Each changed snapshot ran in a fresh process. Invalid input returned fresh
`MNE135` and `MNE103` diagnostics and published no artifact. The five canonical
VM outputs matched the Stage-0 oracle: `[true, false, true, false, false]`.
The cold build took 78.8 s; comment, uncalled-body, and reachable-body edits
took 73.5 s, 73.9 s, and 80.7 s. Peak RSS ranged from 1.80 to 2.09 GB;
cgroup memory measurements were unavailable. The work avoided thousands of
semantic verification units, but these measurements do not establish an
end-to-end speedup because source parsing and other frontend work remain.

The evidence is
`evidence/project-language-fact-reuse-20261021T152950Z.json` (SHA-256
`d9d085e896dfb779d6ae2ed416c035448c7bd589df6d862be3f9c2ae48675b79`), measured
at Compiler `37a0a3b` and Language `a235081`. A follow-up experiment with
borrowed imported-program references preserved fact counts and VM results,
reduced peak RSS to 1.47–1.93 GB, and increased total measured time from
349.1 s to 374.1 s. These were sequential workload runs, not a controlled
benchmark; the optimization candidate is preserved as evidence but is not
pinned by Compiler main. The Compiler lock now follows Language main at
`f31b098` (a badge-only update over the measured fact implementation).

Gate B has moved from “fact reuse not implemented” to partial: selective
function and declaration reuse is independently verified in Compiler
application-build, with fresh diagnostics and canonical execution parity.
An imported callable interface edit is also covered in the current Source,
Lexer, and Segment application frontier. Remaining proof includes nominal
type, generic-specialization, and effect/capability invalidation in
application builds, plus reducing changed-snapshot parse and frontend cost.

That current-pinned self-host frontier uses Compiler `f53b10d` and Language
`f31b098`. A comment edit reused all 103 body facts; a reachable body edit
reused 79; renaming the imported `byte_at_global` input recomputed 21
changed/dependent facts and reused 82 independent facts. Six canonical VM calls
matched Stage-0. Invalid source returned fresh diagnostics and published no
facts or artifact. Cold, comment, body-edit, and interface-edit builds took
2.88 s, 2.11 s, 2.42 s, and 2.02 s respectively, with 144–154 MB peak RSS;
cgroup memory measurements were unavailable. Evidence is
`evidence/self-host-full-module-fact-reuse-20261021T163947Z.json` (SHA-256
`69a55b1beaf9d633cc7ac4adb1ba4b37b8741b056a5af16df06e3a4a277c36a2`). This
advances the Source/Lexer/Segment frontier only; the full Compiler and
Stage-1/Stage-2 succession remain unproven.

## Conversion classification

| Area | Classification | Evidence and boundary |
|---|---|---|
| Assertion meanings, failure categories, finite results, suite summaries, bounded generation, replay inputs, snapshots, and digest aggregation | **Convertible now; native modules already own these semantics** | `native/mncs/test/assertions.mncs`, `suite.mncs`, `generative.mncs`, `snapshot.mncs`, and `digest.mncs` are compiled and executed. The Stage-0 oracle agrees with canonical VM output. The effectful digest application also proves a bounded `test_artifact` structured-read/write capability path. |
| Python manifest runner, external-result fallback, and legacy `discover`/`run` entrypoints | **Temporary bootstrap implementation awaiting a verified replacement** | `tools/mncs_test.py::run_manifest` carries native `TestResult` values through `mncs.test.suite::empty` and `observe`; host projections remain for external fixtures and failures. `AGENTS.md` designates `mncs test`, `bin/mncs-test-*`, and `bin/mncs-test-verify` as canonical entrypoints and `bin/mncs-test-compat` as compatibility-only. Caller population and the full parity corpus are still needed before removal. |
| TOML manifest parsing, controlled file discovery, source reads, and repository inventory | **Intentionally retained operating-system mechanism** | `validate_manifest`, `load_manifest`, `discover_manifests`, `read_source`, and repository inventory in `tools/mncs_test.py` use host filesystem and process state. A native replacement needs explicit bounded filesystem capabilities and stable inventory data. |
| Compiler admission, generic inventory resolution, retained sessions, and batch callable execution | **Partially available** | The Compiler can now return a selected application artifact and its root module's first-class Test inventory from one frontend invocation, and the canonical VM has a retained-session batch contract. The production Test consumer still performs separate selection/preparation and does not yet prove the complete compile-once retained-session path, generic inventory resolution across a corpus, or batch receipts. |
| General JSON/TOML decoding and typed service argument/result schemas | **Requires a concrete Language/Stdlib/runtime capability** | The harness supplied a typed `ApplicationContext`; the native app itself did not parse JSON/TOML or discover files. The tested `test_artifact` operation is a bounded provider contract, not a general service transport layer. |
| Process start, argument/environment transport, timeout, stdout/stderr capture, and temporary request/result files | **Intentionally retained operating-system mechanism** | `tools/mncs_test_native.py::run_native_app` uses `subprocess.run`, temporary files, OS timeout, and schema validation. These remain approved host-provider responsibilities until a bounded process capability has measured parity. |
| Store persistence, Forge admission, Monitor correlation, and Automation eligibility | **Partially integrated** | The selected native `build_digest` workload now runs through Forge and canonical Store, with fresh-process readback of the app result and Forge receipt. Monitor correlation, Automation eligibility, and the full production `mncs test` service loop remain unproven. Reuse the existing service owners and infrastructure. |

## What can be retired now

No production host implementation is safe to remove from this evidence. The native modules own and execute the digest policy semantics, and one real digest application now runs through Forge and Store, but the complete command still relies on host manifest processing, compiler/session integration, process execution, external-result handling, and service persistence. Keep those bridges until the canonical command passes full parity and its remaining caller population is known.

## Prioritized next steps

1. Extend the demonstrated Language fact reuse through real application-level imported/exported interface, nominal type, generic specialization, and effect/capability invalidation. Then reduce repeated changed-snapshot parsing and frontend work while preserving fresh diagnostics, policy identity, and interruption safety.
2. Feed application-build/7 into the production mncs-test native runner and prove its compile-once retained-session and batch path with the existing provider receipt checks; the Compiler can now select and execute source tests by Language-issued case identity, including across distinct artifacts.
3. Complete the effectful digest and Test provider applications through the established Forge, Monitor, Store, and Automation path; preserve the current `build_digest` result and receipt as the integrated regression baseline.
4. Add JSON/TOML and filesystem capability work only for concrete Test consumer needs. Obtain the owning repository scope before any cross-repository implementation.
5. Retire host policy or compatibility code one behavior at a time only after full native-command parity proves it unnecessary.

Gate A passes for the selected multi-module applications. Gate B remains partial. Gate C passes for the selected native readiness and `mncs.test.digest::build_digest` workloads through Forge and canonical Store, while the full production Test command and Monitor/Automation path remain open. Gate D is a concrete conversion assessment rather than a claim that the complete tool is ready to replace.
