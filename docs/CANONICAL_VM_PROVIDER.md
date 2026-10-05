# Canonical VM artifact provider

`canonical-vm-artifact/1` emits standalone `mncs.vm.artifact/1` from verified SSA;
`compiler-producer/1` inspects the exact selected executable. Descriptors bind a
checkout-relative executable, never a PATH compiler. The producer is currently
**stage0-bootstrap-direct-emitter**: the pinned Stage-0 frontend/verifier
bootstraps the compiler-owned emitter. This is not a self-hosted next-generation
frontend or a transfer of language authority.

```sh
CARGO_TARGET_DIR="$PWD/.bootstrap/target" cargo build --release --offline --manifest-path tools/stage0-probe/Cargo.toml
python3 tools/vm_provider.py inspect
python3 tools/vm_provider.py emit --request request.json --cache /explicit/artifact/cache
```

`mncs.compiler-vm-request/1` names `source`, stable `logical_name`, explicit
`libraries`, optional generic `seeds`, `include_tests`, and independent reference
`calls`. The pinned compiler owns module compatibility, verification, SSA,
inventory and source spans. The filesystem adapter reads explicit roots, refuses
conflicting authorities and escaping symlinks, and bounds root inventories. No
ambient stdlib resolver is substituted.

The embedded local build receipt includes relative compiled input hashes,
compiled-in stdlib bundle, observed build revision, bootstrap pin, Rust toolchain
and configuration. Inspection checks current input bytes and the pin before
emission. This is a local observation, not independent attestation or a
reproducible-build certificate. HEAD and executable SHA alone cannot prove origin.

Products use `mncs.provider-build-receipt/1` vocabulary. Artifact, evidence,
producer/executable, inventory and source-map identities stay separate. Keys bind
exact selected roots and content, including the source module directory; adjacent
imports cannot change without invalidation. Equal immutable bytes share one
content-addressed file and retain their inode for admitted readers. Consumers
choose/share the cache; mutable execution state remains private. Cache pruning is
caller-owned, and a hot lookup does not require Store.

Artifacts seal producer/source-map references and optional compiler Test callable
bindings. Source maps retain semantic operations and add selected SSA operation
correspondence where root-module compiler spans exist; other imported/specialized
operations remain unmapped. Frozen bytes contain no compiler Program/session or
migration bytecode. `compiler-next-generation/1` remains the broader development
contract, while `mncs-language.lock.json` owns the bootstrap dependency.

Validation: `tools/test_vm_provider.py`, `tools/test_vm_emit.py`, backend-policy
checks, and bounded three-backend segment/decl probes. Family evidence/matrix:
`mncs-environment/docs/compiler-vm-coherence/`.
