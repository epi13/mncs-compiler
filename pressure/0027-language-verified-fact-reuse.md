# Pressure Finding

ID: CP-0027

Status: open

Category: language

Severity: high

Frequency: common

## Title

Language rechecks unchanged verified units after source-local edits.

## Compiler workload

`mncs-compiler` builds real multi-module MNCS applications and reuses their
verified VM artifact when the executable closure is unchanged. For edits
inside the resolved source closure, however, the current Language frontend
repeats parsing, declaration resolution, signature checking, and function-body
verification for the complete resolved program. The application-build
telemetry reports an empty `reused_semantic_facts` list.

Recent consumers include the real `mncs-test/tests/self_suite.mncs` workload
(41 functions across seven resolved modules) and the 18-case Automation
evaluation application. The Test workload compiles once and runs all seven
inventory-selected tests twice in one canonical VM batch; all 14 results
match Stage-0, but this does not avoid frontend work on later changed
snapshots.

## Minimal MNCS reproduction

Use the existing application-build/6 inventory fixture and make a comment-only
edit or edit an unselected test body. The executable closure remains equal,
but source parsing and all semantic checking still run. Reproduction:

```text
python tools/test_application_build_test_inventory.py
```

The Automation counterpart is `python tools/test_vm_native_automation_application.py`.

## Current behavior

An exact fresh-process hit for the same source snapshot skips the frontend.
For a changed snapshot inside the resolved semantic closure, the Compiler
invokes the full frontend, then may reuse the verified VM artifact after
comparing the current executable closure and interfaces. In the current `/6`
evidence, both a comment edit and an unselected test-body edit reported one
frontend invocation, full semantic recomputation, and zero reused Language
facts. A changed Automation snapshot has the same whole-frontend behavior.

## Current workaround

Reuse the executable artifact only after the full frontend has verified the
new snapshot. This preserves correctness and can avoid SSA/backend emission,
but does not reuse semantic verification work.

## Why the workaround is insufficient

Small source edits repeat work over unchanged declarations and function
bodies. This limits incremental application builds and increases CPU time,
latency, and temporary memory as module closures grow. Artifact-cache hits
must not be reported as Language fact reuse.

## Desired behavior

Language should issue versioned verified facts for independently reusable
semantic units. Each fact must bind its subject and semantic fingerprint,
Language verifier and policy identity, relevant declaration interfaces,
actual semantic and specialization dependencies, effect/capability
assumptions, verification result and witness identity. A reuse decision must
validate those dependencies against the new snapshot. Source locations must
refresh separately. Invalid input must produce fresh diagnostics, and
interrupted verification must not publish partial facts.

Start with function-body reuse under unchanged verified declaration and
dependency interfaces, then extend to declarations, module interfaces,
nominal types, generic specializations, and effect/capability assumptions.
The Compiler should continue to orchestrate application builds and artifact
generation; Language owns the semantic verification facts.

## Likely ownership

`mncs-language`, in a dedicated Language development run. The Compiler
campaign must not duplicate Language semantic identity or verification logic.

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
- `docs/native-application-build-contract.md`: defines the reserved Language-fact result field and the provenance/invalidation contract.

## Upstream tracking

- `mncs-language` issue/PR: not opened; authenticated Language writable scope is not present in the active Compiler session.
- Resolution revision: pending.
- Follow-up evidence in this repository: application-build verification telemetry must report non-empty Language-issued fact identities and independently verified reuse.
