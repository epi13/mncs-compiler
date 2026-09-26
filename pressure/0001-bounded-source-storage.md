# CP-0001 — Whole compiler sources exceed bounded sequence capacity

Status: partially resolved; native project inputs cross 256 bytes but remain capped at 1,024 bytes per source

> Historical description and reproductions below preserve the original
> finding. See the current reconciliation at the end of this file and the
> pressure index for live status.

stdlib-runtime, tooling. Severity: high for whole-module compilation
(was blocking at 64 bytes). Frequency: pervasive. Upstream tracking:
profile 0.13 raised ceilings (see re-evaluation); no dedicated storage
API yet.

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

## Re-evaluation (Stage-0 `a7a8c05`, 2026-09-12): partially resolved

Probed on the current pin (`mncs abi`, profile 0.13): `[byte; up_to 65]`
and `[byte; up_to 1024]` elaborate; `[byte; up_to 1025]` is refused
(MNE105, then MNE161 cascades). Counted_iteration bounds rose the same
way (1..=1024, MNE142 past it). The ceiling is now 16x higher, and the
0.10-profile `repro/source-65.mncs` still yields MNE105
(`tools/test_pressure.py` green), so old-profile behavior is preserved.

What remains: 1024 bytes still cannot hold real modules
(`decl.mncs` is ~267KB); there is still no owned/shared source storage
with views, no growable arenas, no stable content identity. The
four-chunk laboratory interface (256B units) is unchanged. Severity
drops from blocking to high: chunked compilation can now span 4KB per
unit-shape change, but whole-module compilation still needs the storage
API this pressure originally asked for.

## Current reconciliation (2026-09-25)

Current Stage-0 (`b0f3e644`) accepts the preserved 65-byte source. The old
64-byte language limit is stale. The native `compile_project` API now receives
exact per-file byte sequences separately from source metadata; its project
probe crosses the old 256-byte boundary and exercises one 1,024-byte source.
The representation has no four-chunk compatibility path. Current Profile 0.18
caps the generic source length at 1,024 and the snapshot at 64 modules, so this
does not yet ingest this repository's 268 KB declaration module. The remaining
gap is compiler source representation/project scale; it is not a request to
raise a generic language bound. See
[`evidence/campaign-20260925-project-results.json`](../evidence/campaign-20260925-project-results.json).
