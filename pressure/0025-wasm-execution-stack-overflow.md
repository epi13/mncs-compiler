# Portable-WASM execution stack overflow on compiler-scale workloads

ID: CP-0025

Status: open

Category: backend

Severity: high

Frequency: always (for affected workloads)

## Title

Portable-WASM lowering and session retention succeed for the `decl.v1`
compiler module, but the first execution request overflows the host
thread stack and aborts the probe process.

## Compiler workload

`tools/test_decl.py` with `MNCS_PROBE_BACKEND=portable-wasm`: the probe
reports `backend=portable-wasm modules=1 retained_sessions=1` with a
valid `wasm_module` artifact, then `thread 'main' has overflowed its
stack / fatal runtime error: stack overflow, aborting` on the first
`parse_unit` request (a small fixture, not the self-ingestion case).

## Minimal reproduction

```
MNCS_PROBE_BACKEND=portable-wasm python3 tools/test_decl.py
```

Observed: valid retention line, then host stack overflow (SIGABRT,
exit -6) 1:58 into the run at 1.4 GB peak RSS.

## Current behavior

- WASM lowering of a real compiler module: works (retained 1/1).
- WASM execution of any `decl.v1` request: host stack overflow,
  process abort. The failing request is small, so this is about the
  module's code shape (depth/width of the compiled unit), not input
  size.

## Current workaround

None for WASM execution of compiler workloads. B-class suites use
research-bytecode; native suites use Cranelift where it retains.

## Why the workaround is insufficient

WASM is the materially-different-realization leg of backend
conformance (and the Atlas consumer path). Comparing only
interpreter/native paths gives a false sense of portability.

## Desired behavior

The in-process WASM engine executes retained compiler-module
sessions without host stack exhaustion — via explicit value/call
stacks, segmented execution, or a documented supported-depth
contract with fail-closed admission when exceeded.

## Likely ownership

backend (`mncs-language` portable-WASM engine; observed at the
`a3ac17df` pin, so a fix needs a language-main change plus eventual
repin to reach compiler execution).

## Impact

- Correctness: cross-backend conformance blocked for WASM on real
  workloads; no wrong-result evidence (lowering agrees, execution
  aborts instead of miscomputing).
- Safety: process abort (SIGABRT), no corrupt output.
- Runtime performance: n/a.
- Compiler performance: WASM cannot serve as a cheap executor yet.
- Determinism: deterministic abort.

## Evidence / reproduction

- Backend-neutrality audit: `evidence/BACKEND-NEUTRALITY-AUDIT.md`
  (decl-wasm row: 1:58 wall, 1.4 GB peak, abort on first request).
- Probe stderr: `mncs-stage0-probe backend=portable-wasm modules=1
  retained_sessions=1 artifacts=[("mncs.compiler.decl.v1",
  "mncs-portable-wasm-mvp", "wasm_module", true)]` followed by the
  overflow.

## Upstream tracking

- `mncs-language` issue/PR:
- Resolution revision:
- Follow-up evidence in this repository:
