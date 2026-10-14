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

## Environment capability

The Compiler manifest declares the contract as
`mncs-compiler:native-application-build`. The provider accepts a request from
the selected Compiler checkout with `--request-file PATH`, or from a canonical
Store input artifact with `--request-artifact NAME`. It writes the complete
response as the shareable `native-application-build.json` invocation artifact
and keeps stdout bounded; consumers read the full response through the exact
Environment artifact binding and path. The current Control operation allowlist
does not expose this capability yet, so canonical Store publication and
readback remain pending that Control contract change.

The contract keeps build and execution obligations distinct. `verified` on an
entry means the selected entry has a verified canonical VM artifact; it does
not imply that every export in the snapshot is supported or that the artifact
has already executed. `unsupported`, `unavailable`, `unknown`, and `rejected`
are preserved as structured outcomes. Provider transport errors remain
invocation errors and do not produce a result artifact.

The manifest-bound regression is
`python3 tools/test_application_build_provider.py`. It checks a cold build, a
fresh-process warm hit, Store-input byte/hash validation, the exact canonical
artifact identity, precise invalid-source diagnostics, and suppression of
successful cache publication for rejected input. The `mncs-test` consumer
fixture remains the source and Stage-0 differential oracle for the application
itself.
