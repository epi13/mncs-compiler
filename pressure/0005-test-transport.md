# CP-0005 — Bootstrap differential tests need a host test transport

Status: open. Category: tooling, compiler-architecture. Severity: medium.
Frequency: every evidence run. Upstream tracking: none.

The compiler workload is comparing many MNCS token/parse requests with pinned
Rust `mncs-syntax` results. The pinned CLI exposes one-request `execute`, but
`compare-execution` compares execution subjects rather than exposing raw Rust
lexer results for successful source inputs; successful `source-study` reports
compilation study output. Repeated per-token CLI elaboration is unnecessarily
expensive. The initial per-request reference `execute` harness also rebuilt a
`BodyExecutionSession` each time; using the existing session API avoids this
avoidable test cost. This is primarily a tooling boundary, not a proved MNCS
process API deficiency.

Temporary workaround: `tools/stage0-probe` only loads repository modules through
Stage-0, calls Stage-0 `parse`, and executes MNCS requests through cached reference
execution sessions. `tools/test_frontend.py` supplies fixtures, compares values,
and hashes test evidence. Neither implements production compiler semantics.
Run `tools/bootstrap.sh && python3 tools/test_frontend.py`.

This transport is explicitly not the standalone compiler driver. It requires
Rust/Python during bootstrap tests; it must be replaced by MNCS-native test
transport or a standard Harness/Stage-0 raw-fact interface when available.
No hard dependency on Harness or any other MNCS ecosystem repository is added.

Correctness: independent lexical expectations come from Rust, not a duplicate
Python lexer; MNCS and Rust still share the Stage-0 executor/toolchain trust base.
Safety: local test files and stdio only. Performance/memory: the test process
holds compiled reference programs and sessions; reported wall time is not pure
kernel timing. Determinism: compare normalized values and step counts twice;
wall time is excluded. Complexity: a small removable adapter and JSON transport.
Desired capability: reusable raw syntax-fact and MNCS test execution interfaces.
Likely ownership: tooling and compiler test architecture.
