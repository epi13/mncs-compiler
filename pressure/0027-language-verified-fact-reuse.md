# Pressure Finding

ID: CP-0027

Status: partially resolved

Category: language

Severity: high

Frequency: common

## Title

Changed snapshots still repeat frontend work after verified units are reusable.

## Compiler workload

`mncs-compiler` builds real multi-module MNCS applications and reuses their
verified VM artifact when the executable closure is unchanged. Language-owned
function-body and declaration facts are now also reused across changed source
snapshots. The eight-module Compiler Project workload verifies selective reuse
over 2,031 cold body and declaration facts. A current-pinned four-module
Source/Lexer/Segment application also verifies imported callable-interface
invalidation. Source parsing and frontend work still run for changed snapshots;
nominal, generic-specialization, and effect/capability invalidation remain to
be exercised in application builds.

Recent consumers include the real `mncs-test/tests/self_suite.mncs` workload
(41 functions across seven resolved modules) and the 18-case Automation
evaluation application. The Test workload compiles once and runs all seven
inventory-selected tests twice in one canonical VM batch; all 14 results
match Stage-0, but this does not avoid frontend work on later changed
snapshots.

## Minimal MNCS reproduction

Use the Compiler Project fact-reuse fixture and make a comment-only, uncalled
body, or reachable body edit. Reproduction:

```text
python tools/test_vm_compiler_project_fact_reuse.py
```

The Automation counterpart is `python tools/test_vm_native_automation_application.py`.

## Current behavior

Each changed snapshot still invokes the frontend in the current Project path,
but Language reports actual function-body and declaration fact reuse. A
comment edit reused all 2,031 body facts and all 2,031 declaration facts. An
uncalled body edit recomputed only that body and reused 2,030 body facts. A
reachable body edit recomputed its 15-function dependency closure and reused
2,016 body facts. Invalid source returned fresh diagnostics and published no
artifact. Every snapshot ran in a fresh process.

The current-pinned Source/Lexer/Segment self-host frontier adds application-
level imported interface invalidation: renaming the exported `byte_at_global`
input recomputed 21 changed/dependent facts and reused 82 independent facts.
The edited application still compiled and its canonical VM calls matched
Stage-0. The bad-body case returned fresh `MNE103` diagnostics without
publishing facts or an artifact.

The 2026-10-23 `mncs.test.digest::digest_app` run extends this proof to the
real Test application. A root comment edit reused 30 verified facts, recomputed
none, reused its current syntax tree from unchanged significant tokens, and
preserved canonical VM output. An imported assertion-body edit recomputed the
single changed function fact and reused 29 independent facts; invalid source
still returned a fresh rejection, and tampered cache evidence forced cold
verification. A fresh process reused the same Language facts and VM artifact.
The frontend invocation count remains one for each changed snapshot, so the
remaining pressure is parsing/linking and other whole-snapshot work rather
than a lack of body-fact reuse.

The same harness now changes imported interfaces in two isolated application
caches. Adding `extension_identity: u64` to the imported nominal
`mncs.test.assertions::TestResult` invalidated the assertion and suite module
entries, recomputed 11 body facts, and reused 19 independent facts. Renaming
the imported `mncs.test.suite::observe` result binding invalidated the suite
entry and recomputed the four affected body facts while reusing 26. Both
edited applications matched Stage-0 canonical VM output. Their Compiler build
times were approximately 0.800 s and 0.733 s, respectively, with peak process
RSS near 55.4 MB; these short workload measurements establish selective work,
not an end-to-end speedup. Exact observations are in
[`language-module-interface-application-invalidation-compiler-20261023.json`](../evidence/language-module-interface-application-invalidation-compiler-20261023.json).

## Current workaround

Selective Language fact reuse now avoids redundant verification, while the
Compiler preserves full frontend validation and canonical artifact checks.
This still repeats source-level frontend work and has not yet shown an overall
wall-time reduction for the large Project workload.

## Why the workaround is insufficient

The current frontend still spends most of the measured Project build time in
parsing, module linking, and remaining whole-snapshot work. Fact-count reuse
is proven, but it has not yet reduced this workload's end-to-end wall time.
Artifact-cache hits must not be reported as Language fact reuse.

## Desired behavior

Language's versioned `mncs.language-verification-fact/3` contract now supports
function-body and declaration reuse with verifier, policy, profile, interface,
dependency, and assumption identities. The Compiler consumes those facts and
reports actual reused and recomputed subjects. Imported callable and nominal
interface edits plus one seeded generic specialization are covered by real
application builds. Continue by covering module export-set edits and
effect/capability changes; reduce repeated
source-level work only where measurement shows it is safe and useful.

## Likely ownership

`mncs-language` owns fact identity and verification. `mncs-compiler` owns
application-build orchestration, fact-cache transport, and executable artifact
generation. The implementation is in Language main and the Compiler consumes
it without duplicating semantic identity.

## Impact

- Correctness: preserve full fresh diagnostics and invalidate facts on every changed assumption.
- Safety: preserve verifier/policy identity and atomic publication; never infer verification from a matching body fingerprint alone.
- Runtime performance: no direct change expected.
- Compiler performance: high impact on changed snapshots; avoid repeating verification for unchanged units.
- Memory: potential reduction in repeated frontend working state; measure rather than assume.
- Determinism: fact selection and invalidation must be content- and policy-deterministic.
- Implementation complexity: requires Language-owned dependency evidence and Compiler integration.

## Evidence / reproduction

- `evidence/application-build-test-inventory-repeat-20261018.json`: 41 functions, seven modules, one frontend invocation, 14 canonical VM calls over two rounds, Stage-0 agreement, and zero facts reused. Comment and unselected-body edits each rechecked the full three-function fixture while reusing its executable artifact.
- `evidence/native-app-readiness-automation-evaluation-inplace-20261018.json`: multi-module Automation application; changed snapshots receive fresh whole-frontend verification before artifact reuse. The measured comment edit took 0.608 s and the uncalled-body edit 0.578 s in this workload; cgroup memory was unavailable.
- `evidence/project-language-fact-reuse-20261021T152950Z.json` (SHA-256 `d9d085e896dfb779d6ae2ed416c035448c7bd589df6d862be3f9c2ae48675b79`): eight-module Project workload, 2,031 cold body and declaration facts, full reuse after a comment edit, one body recomputed after an uncalled edit, 15 bodies recomputed after a reachable edit, fresh `MNE135`/`MNE103` diagnostics, and Stage-0/canonical VM agreement. Each changed-snapshot build took 73.5–80.7 s, so end-to-end speedup is not established; peak RSS was 1.80–2.09 GB and cgroup memory was unavailable.
- `evidence/self-host-full-module-fact-reuse-20261021T163947Z.json` (SHA-256 `69a55b1beaf9d633cc7ac4adb1ba4b37b8741b056a5af16df06e3a4a277c36a2`): current Language main `f31b098`, three full Compiler modules plus the root test module, 103 verified body facts, exact imported interface invalidation (21 recomputed, 82 reused), fresh invalid-source diagnostics, and six canonical VM calls matching Stage-0. Cold, comment, body-edit, and interface-edit builds measured 2.88 s, 2.11 s, 2.42 s, and 2.02 s; peak RSS was 144–154 MB and cgroup memory was unavailable.
- `evidence/language-module-elaboration-cache-20261023-language-d74402a.json` (SHA-256 `f03623e51a856ebd64d8a8bc7ff24be18ba0e0682e5491125a5172b0e176820f`): current Compiler main `1bbd5a5` with Language `d74402a`, real digest application, comment edit (30 facts reused, zero recomputed), imported body edit (29 reused, one recomputed), fresh-process reuse, fresh invalid-source rejection, tamper-triggered full verification, and canonical VM equivalence.
- `evidence/project-language-fact-reuse-20261021T155938Z.json` (SHA-256 `271f04c1e3b1ace856b5f48684bd3ce7616600aafe46b6d9eea93070c7d043e6`): exploratory borrowed-module-linking revision. It preserved fact counts and VM results, reduced measured peak RSS to 1.47–1.93 GB, but took 374.1 s total versus 349.1 s in the earlier run. The measurements were sequential rather than a controlled benchmark; this candidate is not pinned by Compiler main.
- `docs/native-application-build-contract.md`: defines the reserved Language-fact result field and the provenance/invalidation contract.

## Upstream tracking

- `mncs-language` issue/PR: not opened; the dedicated Language campaign delivered the fact implementation on main.
- Verified-fact implementation first landed in Language `a235081db27db4965612f926c69bb6a5769b0d67`. The current Compiler pin is Language `9505d3f557701f072b5803a5bad046389b84cb3b`; application-build integration and digest-app reuse evidence are in Compiler main `9ddf7da5d010ec25d5325985583b63f796312233`.
- Follow-up: exercise module export-set and effect/capability invalidation through real application builds, then reduce remaining changed-snapshot frontend, parsing, and linking work where measurement supports it.
