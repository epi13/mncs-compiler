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

`mncs.compiler.application-build/2` records framed source-snapshot identity,
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

Requested generic instances are emitted as concrete callable exports. The
module-wide response continues to report unspecialized generic templates as
unsupported when they have no standalone SSA instance; requesting one concrete
instance does not promote its generic template. An emitted artifact remains
the executable authority.

This is a bounded bootstrap adapter with a content-addressed cache for complete
verified application artifacts. It does not yet reuse verified modules or
functions across changed snapshots, and it makes no claim of Stage-1 or
Stage-2 succession. The real consumer exercise is
`python3 tools/test_vm_native_application.py`,
which reads the selected `mncs-test` checkout without modifying it, requests
`equals_bytes<4>` and `summarize<4>`, accepts a typed `ReadinessRequest`, returns
a `ReadinessOutcome`, and compares both successful and typed rejection results
from the Stage-0 oracle with canonical VM execution. The error cases exercise
finite payload extraction and retain the exact artifact receipt. The test also
constructs its argument using the callable interface emitted by the compiler.
`python3 tools/test_native_application_incremental.py`
also changes one specialization request and checks that the cache key and
artifact identity change.

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
