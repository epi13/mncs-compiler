# MNCS Native Application Readiness: Test conversion assessment

**Assessment date:** 2026-10-14
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
cache suite passed. The warm exact-artifact path measured 0.123 s versus 0.472 s
cold (3.84×). Gate B remains partial because semantic edits still rebuild the
verified application closure. The latest evidence files are
`evidence/native-app-readiness-gate-a.json` (SHA-256
`95c9bb93efc579aa832ebba55c6e50c1570c7332b204baa97e70b4af57eb81dc`) and
`evidence/native-app-readiness-gate-b.json` (SHA-256
`084bf6c39d6d6bdac12907ed3317c06168db6f9d62eedd004033590f8e643fb5`).

Gate C remains partial. The version-4 provider regression passes, but this run
did not publish and read back a native application result through canonical
Store or Forge. The Language-owned verified-facts API needed for finer-grained
semantic reuse is also outside this campaign's write authority.

## Evidence boundary

This assessment uses the read-only `mncs-test` checkout at `f9575e09ec3481c0ded52bb9e3ec72940725aff6`, Stdlib `6aa443cacf8c80a8ccb0fe6586b1f8c68575bf6c`, Language `3cfbc094f1a1770fc2409b990f3f7d8912f49bc7`, VM `1c19a94732c9d24029dd00e2d5af13710f481d02`, and Compiler producer commit `24dda63ae59e0505969c490604e14348dab1f7d7`. The Stage-0 reference revision is `e8f8a5f8659c71b33cd32c964d4982bec1a9668e`, release probe SHA-256 `190d0bf1128f24962fbf56b6d5701ac60ffbe04bceda4c59064671f103714c58`; the VM binary SHA-256 is `2f7bd4a9f6b537b17e4c1dbf08bcc8cd55362a3f9f1bc28bd91a265a30e1e7f5`.

The readiness application compiles the `run` entry and two explicit generic specializations from a five-module Test source closure. Its typed report, nominal records, finite payloads, bounded sequences, matches, and typed failures agree between Stage-0 and the canonical VM. An invalid field projection reports `MNE162`. The initial Gate A evidence used artifact identity `sha256:cff9e5cc4cb47ea19096ec735384263fb87d211d3d7d835173b56bca3e2ea879`; the post-reboot rerun and current artifact identity are in the continuation above.

The real `mncs.test.digest::build_digest` function compiles under application-build/4 from its Digest, Assertions, Suite, and Stdlib application sources. Stage-0 and three canonical VM calls agree on pass/fail/skip aggregation, failure caps, omitted-failure counts, and all-pass results. Artifact identity is `sha256:3bd4cc51f4e61ff91131f1d6d65be73615ebaf19ebc66deca75e2a0d7e3ee3df`; callable interface is `sha256:3c08227564c44096355994ec3689e817d710ca5848581880dc278d5cd7e434f9`. Evidence is `evidence/native-app-readiness-test-digest.json` (SHA-256 `3ad5ba0758dfce1c7fed304bc3e91d37ff30b867ce244f144b98304ab025d694`). Cold build measured 0.628 s / 0.590 CPU / 65.0 MiB peak RSS; Stage-0 took 0.354 s; VM calls took 0.210–0.212 s at about 28 MiB. These are bounded application measurements, not full `mncs-test` performance.

A second application, `mncs.test.digest::digest_app`, exercises typed application input/output and structured read/write effects through the canonical VM. The compiler response includes `mncs.compiler.capability-dispatch/1`; both `HostCall` rows map canonical identity `mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact` to runtime key `test_artifact`, with operation and instruction provenance. The Rust runner only supplies the canonical VM host and the explicit test-artifact capability; it cross-checks the contract mapping against admitted SSA and contains no digest policy. Stage-0 and VM results agree for all three cases. Missing provider admission returns `capability_denied`; rejected output publication returns typed application exit 3 and leaves no output.

The effectful artifact is `sha256:95cf2d3e3361b7089336c82e0cd4870dfe74775547831eed90879555d494f919`; interface is `sha256:6f2f19db351c5d3c8e86c3c2ff34848fafd314cda62e97f758b364dfb5f8aee2`. Evidence is `evidence/native-app-readiness-test-digest-app.json` (SHA-256 `ef625aca37e674d493dc64f6f45fe13c380d9770cc631c54eca34936be72fa89`). The build took 0.628 s / 0.590 CPU / 64.9 MiB peak RSS; the Stage-0 project oracle took 0.354 s; canonical VM calls took 0.024–0.028 s / 0.010 CPU / 12.2–12.5 MiB. cgroup memory counters were unavailable.

Gate B remains partial. Current readiness-suite evidence `evidence/native-app-readiness-gate-b.json` (SHA-256 `65106578dc52ac9ea89412cde06b7a0578e989d9101e2c4c79d0e67e9f098f03`) records cold 0.461 s, unchanged warm 0.120 s after 0.120 s process startup, and source-only dependency reuse in 0.188 s after fresh semantic checking. A changed root function took 0.366 s and a semantic dependency change 0.379 s; both recomputed source checking, SSA verification, and artifact generation. The digest-specific rerun, `evidence/native-app-readiness-test-digest-incremental.json` (SHA-256 `bd85ae27d21b1ae6c1b99ac934d0a61273f52d04b8198727ada931744a59a161`), measured cold/warm 0.633/0.246 s (2.57×), but a root semantic edit and Suite dependency edit took 0.416/0.432 s and rebuilt all three obligations. Invalid source diagnostics were stable and not published. A SIGKILL at 28.5 ms left no partial publication; retry and fresh-process reuse passed. cgroup memory remained unavailable.

Gate C is partial. The `/4` producer now exposes source snapshot and dependency graph identities, selected callable interface, artifact provenance, build assumptions, structured diagnostics and obligations, target compatibility, resource intent, and verified capability-dispatch bindings. The Environment-facing provider returns the same dispatch contract in its invocation summary. `tools/test_application_build_provider.py` passes cold, fresh-process warm, invalid-source, unsupported-schema, Store-input-artifact reuse, and corrupt-input cases. This is a compiler-provider contract test; it does not yet prove production Test-service consumption, canonical Store RPC publication/readback, or Forge/Monitor/Automation receipts for the larger application.

## Conversion classification

| Area | Classification | Evidence and boundary |
|---|---|---|
| Assertion meanings, failure categories, finite results, suite summaries, bounded generation, replay inputs, snapshots, and digest aggregation | **Convertible now; native modules already own these semantics** | `native/mncs/test/assertions.mncs`, `suite.mncs`, `generative.mncs`, `snapshot.mncs`, and `digest.mncs` are compiled and executed. The Stage-0 oracle agrees with canonical VM output. The effectful digest application also proves a bounded `test_artifact` structured-read/write capability path. |
| Python manifest runner, external-result fallback, and legacy `discover`/`run` entrypoints | **Temporary bootstrap implementation awaiting a verified replacement** | `tools/mncs_test.py::run_manifest` carries native `TestResult` values through `mncs.test.suite::empty` and `observe`; host projections remain for external fixtures and failures. `AGENTS.md` designates `mncs test`, `bin/mncs-test-*`, and `bin/mncs-test-verify` as canonical entrypoints and `bin/mncs-test-compat` as compatibility-only. Caller population and the full parity corpus are still needed before removal. |
| TOML manifest parsing, controlled file discovery, source reads, and repository inventory | **Intentionally retained operating-system mechanism** | `validate_manifest`, `load_manifest`, `discover_manifests`, `read_source`, and repository inventory in `tools/mncs_test.py` use host filesystem and process state. A native replacement needs explicit bounded filesystem capabilities and stable inventory data. |
| Compiler admission, generic inventory resolution, retained sessions, and batch callable execution | **Requires compiler/backend execution work** | Current proof selects and executes a callable application artifact. It does not prove the canonical Test runner's compile-once retained-session path, generic inventory resolution across a full corpus, or batch receipts. |
| General JSON/TOML decoding and typed service argument/result schemas | **Requires a concrete Language/Stdlib/runtime capability** | The harness supplied a typed `ApplicationContext`; the native app itself did not parse JSON/TOML or discover files. The tested `test_artifact` operation is a bounded provider contract, not a general service transport layer. |
| Process start, argument/environment transport, timeout, stdout/stderr capture, and temporary request/result files | **Intentionally retained operating-system mechanism** | `tools/mncs_test_native.py::run_native_app` uses `subprocess.run`, temporary files, OS timeout, and schema validation. These remain approved host-provider responsibilities until a bounded process capability has measured parity. |
| Store persistence, Forge admission, Monitor correlation, and Automation eligibility | **Requires compiler/backend and service integration work** | The compiler contract has source, artifact, capability, and resource evidence, but this workload has not gone through the canonical Store RPC and Forge/Monitor/Automation loop. Reuse the existing service owners and infrastructure. |

## What can be retired now

No production host implementation is safe to remove from this evidence. The native modules own and execute the policy semantics, but the complete command still relies on host manifest processing, compiler/session integration, process execution, external-result handling, and service persistence. Keep those bridges until the canonical command passes full parity and its remaining caller population is known.

## Prioritized next steps

1. Feed application-build/4 directly into the canonical `mncs-test` native runner and prove its retained-session and batch path with the existing provider receipt checks.
2. Implement exact-input verified declaration/function reuse for the real multi-module application. Bind reuse to source/module identities, compiler and backend versions, effect assumptions, and verification policy; rerun cold, warm, edits, invalid source, and interruption cases.
3. Run the effectful digest application through the established Forge, Monitor, Store, and Automation path; preserve the constant-return integration proof as the regression baseline.
4. Add JSON/TOML and filesystem capability work only for concrete Test consumer needs. Obtain the owning repository scope before any cross-repository implementation.
5. Retire host policy or compatibility code one behavior at a time only after full native-command parity proves it unnecessary.

Gate A passes for the selected multi-module applications. Gate B remains partial, Gate C has a versioned and tested producer contract without production service integration, and Gate D is a concrete conversion assessment rather than a claim that the complete tool is ready to replace.
