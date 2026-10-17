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
closure reuse result are recorded below. The latest evidence files are
`evidence/native-app-readiness-gate-a.json` (SHA-256
`95c9bb93efc579aa832ebba55c6e50c1570c7332b204baa97e70b4af57eb81dc`) and
`evidence/native-app-readiness-gate-b.json` (SHA-256
`0cb8a4e7a1ff4129d71c59321bdf23e069dba6d6af6978b715ff33549a766ce4`).

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
and records the exact application harness hash. This does not prove
generic-specialization invalidation, nominal type invalidation, Forge routing,
or Language-owned fact reuse.

## Evidence boundary

This assessment uses the read-only `mncs-test` checkout at `f9575e09ec3481c0ded52bb9e3ec72940725aff6`, Stdlib `6aa443cacf8c80a8ccb0fe6586b1f8c68575bf6c`, Language `3cfbc094f1a1770fc2409b990f3f7d8912f49bc7`, VM `1c19a94732c9d24029dd00e2d5af13710f481d02`, and Compiler producer commit `24dda63ae59e0505969c490604e14348dab1f7d7`. The Stage-0 reference revision is `e8f8a5f8659c71b33cd32c964d4982bec1a9668e`, release probe SHA-256 `190d0bf1128f24962fbf56b6d5701ac60ffbe04bceda4c59064671f103714c58`; the VM binary SHA-256 is `2f7bd4a9f6b537b17e4c1dbf08bcc8cd55362a3f9f1bc28bd91a265a30e1e7f5`.

The readiness application compiles the `run` entry and two explicit generic specializations from a five-module Test source closure. Its typed report, nominal records, finite payloads, bounded sequences, matches, and typed failures agree between Stage-0 and the canonical VM. An invalid field projection reports `MNE162`. The initial Gate A evidence used artifact identity `sha256:cff9e5cc4cb47ea19096ec735384263fb87d211d3d7d835173b56bca3e2ea879`; the post-reboot rerun and current artifact identity are in the continuation above.

The real `mncs.test.digest::build_digest` function compiles under application-build/4 from its Digest, Assertions, Suite, and Stdlib application sources. Stage-0 and three canonical VM calls agree on pass/fail/skip aggregation, failure caps, omitted-failure counts, and all-pass results. Artifact identity is `sha256:3bd4cc51f4e61ff91131f1d6d65be73615ebaf19ebc66deca75e2a0d7e3ee3df`; callable interface is `sha256:3c08227564c44096355994ec3689e817d710ca5848581880dc278d5cd7e434f9`. Evidence is `evidence/native-app-readiness-test-digest.json` (SHA-256 `3ad5ba0758dfce1c7fed304bc3e91d37ff30b867ce244f144b98304ab025d694`). Cold build measured 0.628 s / 0.590 CPU / 65.0 MiB peak RSS; Stage-0 took 0.354 s; VM calls took 0.210–0.212 s at about 28 MiB. These are bounded application measurements, not full `mncs-test` performance.

A second application, `mncs.test.digest::digest_app`, exercises typed application input/output and structured read/write effects through the canonical VM. The compiler response includes `mncs.compiler.capability-dispatch/1`; both `HostCall` rows map canonical identity `mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact` to runtime key `test_artifact`, with operation and instruction provenance. The Rust runner only supplies the canonical VM host and the explicit test-artifact capability; it cross-checks the contract mapping against admitted SSA and contains no digest policy. Stage-0 and VM results agree for all three cases. Missing provider admission returns `capability_denied`; rejected output publication returns typed application exit 3 and leaves no output.

The original effectful-app proof used artifact `sha256:95cf2d3e3361b7089336c82e0cd4870dfe74775547831eed90879555d494f919` and evidence SHA-256 `ef625aca37e674d493dc64f6f45fe13c380d9770cc631c54eca34936be72fa89`; it is retained as the historical baseline. The projected artifact and current retained-session batch proof are recorded in the follow-up above.

Gate B remains partial. The latest readiness-suite evidence
`evidence/native-app-readiness-gate-b.json` (SHA-256
`0cb8a4e7a1ff4129d71c59321bdf23e069dba6d6af6978b715ff33549a766ce4`)
records a 0.528 s cold build and 0.125 s warm fresh-process hit (4.24× for
this workload). A comment-only imported-module edit reused the artifact after
fresh source checking in 0.222 s. A semantic edit to uncalled
`mncs.test.generative::shrink` changed the full semantic identity but preserved
the selected application closure; the verified artifact was reused after
closure verification in 0.220 s, and canonical VM output matched the independent
Stage-0 oracle. Changing that uncalled function's exported signature preserved
the selected closure and entry interface but changed the whole-program artifact
interface, so the build correctly missed and regenerated the artifact in
0.442 s. A changed reachable function, exported nominal record, and added
imported call also missed and rebuilt, with canonical VM results matching the
oracle. Invalid source kept its stable `MNE162` diagnostic and was not
published. A SIGKILL at 25 ms left no partial cache entry; retry and fresh-
process reuse passed. Probe peak RSS was about 57 MiB; the VM peak was about
27 MiB. cgroup memory counters were unavailable. Gate B is still partial
because every changed snapshot receives a fresh whole-program semantic check;
Language-owned verified-fact reuse and selective recomputation remain open.

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

## Conversion classification

| Area | Classification | Evidence and boundary |
|---|---|---|
| Assertion meanings, failure categories, finite results, suite summaries, bounded generation, replay inputs, snapshots, and digest aggregation | **Convertible now; native modules already own these semantics** | `native/mncs/test/assertions.mncs`, `suite.mncs`, `generative.mncs`, `snapshot.mncs`, and `digest.mncs` are compiled and executed. The Stage-0 oracle agrees with canonical VM output. The effectful digest application also proves a bounded `test_artifact` structured-read/write capability path. |
| Python manifest runner, external-result fallback, and legacy `discover`/`run` entrypoints | **Temporary bootstrap implementation awaiting a verified replacement** | `tools/mncs_test.py::run_manifest` carries native `TestResult` values through `mncs.test.suite::empty` and `observe`; host projections remain for external fixtures and failures. `AGENTS.md` designates `mncs test`, `bin/mncs-test-*`, and `bin/mncs-test-verify` as canonical entrypoints and `bin/mncs-test-compat` as compatibility-only. Caller population and the full parity corpus are still needed before removal. |
| TOML manifest parsing, controlled file discovery, source reads, and repository inventory | **Intentionally retained operating-system mechanism** | `validate_manifest`, `load_manifest`, `discover_manifests`, `read_source`, and repository inventory in `tools/mncs_test.py` use host filesystem and process state. A native replacement needs explicit bounded filesystem capabilities and stable inventory data. |
| Compiler admission, generic inventory resolution, retained sessions, and batch callable execution | **Requires compiler/backend execution work** | Current proof selects and executes a callable application artifact. It does not prove the canonical Test runner's compile-once retained-session path, generic inventory resolution across a full corpus, or batch receipts. |
| General JSON/TOML decoding and typed service argument/result schemas | **Requires a concrete Language/Stdlib/runtime capability** | The harness supplied a typed `ApplicationContext`; the native app itself did not parse JSON/TOML or discover files. The tested `test_artifact` operation is a bounded provider contract, not a general service transport layer. |
| Process start, argument/environment transport, timeout, stdout/stderr capture, and temporary request/result files | **Intentionally retained operating-system mechanism** | `tools/mncs_test_native.py::run_native_app` uses `subprocess.run`, temporary files, OS timeout, and schema validation. These remain approved host-provider responsibilities until a bounded process capability has measured parity. |
| Store persistence, Forge admission, Monitor correlation, and Automation eligibility | **Partially integrated** | The selected native `build_digest` workload now runs through Forge and canonical Store, with fresh-process readback of the app result and Forge receipt. Monitor correlation, Automation eligibility, and the full production `mncs test` service loop remain unproven. Reuse the existing service owners and infrastructure. |

## What can be retired now

No production host implementation is safe to remove from this evidence. The native modules own and execute the digest policy semantics, and one real digest application now runs through Forge and Store, but the complete command still relies on host manifest processing, compiler/session integration, process execution, external-result handling, and service persistence. Keep those bridges until the canonical command passes full parity and its remaining caller population is known.

## Prioritized next steps

1. Feed application-build/4 into the production `mncs-test` native runner and prove its retained-session and batch path with the existing provider receipt checks; the current integrated call invokes the selected application through Environment but does not yet prove that full command workflow.
2. Add Language-owned verified-fact reuse for the real multi-module application so unchanged declarations and bodies can avoid repeated whole-program semantic work. Preserve the source, compiler, backend, effect, and verification-policy identities already covered by the current artifact cache; rerun cold, warm, local edit, interface change, invalid source, and interruption cases.
3. Run the effectful digest application through the established Forge, Monitor, Store, and Automation path; preserve the current `build_digest` result and receipt as the integrated regression baseline.
4. Add JSON/TOML and filesystem capability work only for concrete Test consumer needs. Obtain the owning repository scope before any cross-repository implementation.
5. Retire host policy or compatibility code one behavior at a time only after full native-command parity proves it unnecessary.

Gate A passes for the selected multi-module applications. Gate B remains partial. Gate C passes for the selected native readiness and `mncs.test.digest::build_digest` workloads through Forge and canonical Store, while the full production Test command and Monitor/Automation path remain open. Gate D is a concrete conversion assessment rather than a claim that the complete tool is ready to replace.
