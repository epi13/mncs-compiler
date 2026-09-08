# CP-0001 — Whole compiler sources exceed bounded sequence capacity

Status: open. Category: language, stdlib-runtime, tooling. Severity: blocking
for whole-module compilation. Frequency: pervasive. Upstream tracking: none;
no upstream changes made.

## Workload and reproduction

An immutable compiler source snapshot must retain arbitrary module bytes and
support stable byte indexing. The first lexer uses `[byte; up_to 64]`.
`repro/source-65.mncs` changes only the capacity to 65. Run
`python3 tools/test_pressure.py` after bootstrap: Stage-0 emits MNE105 (a generic
supported-types message, not a direct capacity diagnostic). The executable
frontend tests also pass 65 bytes to the 64-byte API and require
`invalid_request`, proving that oversized input is not silently truncated.
The pinned `mncs-model/src/body.rs` sets `MAX_SEQUENCE_BOUND` to 64.

## Workaround and desired capability

The kernel accepts exact, immutable, at-most-64-byte sources. The caller iterates
`next_token` results; no host language implements storage or scanning for a
production compiler. This cannot read even this compiler's own source modules.
Chunking is possible but requires token continuation across arbitrary boundaries,
shared backing storage, and bounded-resource policies; it is deliberately not
pretended to be equivalent to a whole-module source API.

The desired capability is explicit owned/shared source storage with byte views,
deterministic indexing, and resource limits independent of one tiny static
sequence ceiling. Investigate existing nested sequences and runtime facilities
before prescribing a new language feature. Record sequences *do* elaborate:
`tests/fixtures/token-sequence.mncs` is a positive control, not a missing-feature
report. Growable token/AST arenas, allocation, hashing, and stable persistent
identity remain unimplemented and unmeasured; this report does not establish
that all of them are absent.

## Impact and ownership

- Correctness/safety: oversized input is rejected at the ABI; no truncation.
- Runtime/memory: current working source is bounded; larger-source cost unknown.
- Compiler performance: no large-source benchmark is possible through this API.
- Determinism: exact byte values are reproducible, but are not content hashes.
- Complexity: chunked ownership, continuation, and indexing would spread through
  lexer, parser, diagnostics, and snapshots.
- Likely owners: language/resource model and stdlib/runtime storage; tooling for
  a precise capacity diagnostic. Compiler architecture must choose storage units.
