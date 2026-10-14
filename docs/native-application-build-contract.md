# Native application build contract

`mncs.compiler.application-build-request/1` accepts an exact root source and a
bounded map of imported module sources. It resolves and checks that snapshot
with the pinned Stage-0 compiler, then asks the compiler-owned canonical VM
emitter for the existing `mncs.vm.artifact/1` format.

The response records framed source-snapshot identity, source hashes, resolved
dependency identity, Stage-0 and producer identities, artifact and interface
identities, required capabilities, unresolved obligations, structured source
diagnostics, target compatibility, and explicit unknown resource estimates.
The selected entry has its own `verified`, `unsupported`, or `unknown` status;
the overall response continues to report unsupported exports elsewhere in the
module closure. An emitted artifact remains the executable authority.

This is a bounded bootstrap adapter for application snapshots, not an
incremental cache and not a claim of Stage-1 or Stage-2 succession. It accepts
at most 64 imported modules and 4 MiB of source. The initial real consumer
exercise is `python3 tools/test_vm_native_application.py`, which reads the
selected `mncs-test` checkout without modifying it and compares Stage-0 oracle
results with canonical VM execution.
