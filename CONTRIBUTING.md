# Contributing

`mncs-compiler` is an experimental self-hosted compiler and language-pressure project. Changes should preserve the distinction between compiler implementation work and upstream language changes.

## Development rules

1. Work in this repository when advancing the MNCS compiler.
2. Do not modify `mncs-language` as part of the same compiler run.
3. Record genuine language/stdlib/backend/tooling pressure under `pressure/`.
4. Prefer small deterministic compiler contracts over broad mutable global state.
5. Keep the standalone compiler kernel usable without optional MNCS infrastructure.
6. Add evidence for behavior, performance, or parity claims.
7. Update RFCs when changing founding invariants or cross-repository ownership boundaries.

## Pull requests

A compiler PR should explain, where relevant:

- compiler subsystem changed,
- semantic behavior affected or preserved,
- new facts/obligations/invariants introduced,
- incremental/cache implications,
- target/capability implications,
- diagnostics/provenance changes,
- tests/evidence added,
- language pressure discovered,
- whether Rust Stage-0 behavior was compared.

## Pressure findings

Do not open an upstream language change merely because compiler code is awkward. First classify whether the pain belongs to:

- language semantics,
- stdlib/runtime,
- backend support,
- tooling,
- compiler architecture.

When an upstream fix is later made, return to this repository and demonstrate that the original workload is improved.

## Generated or foreign-language scaffolding

Temporary host-language tools may be acceptable only when clearly isolated and justified by a current MNCS limitation. They should not silently become the permanent compiler implementation.

## Architecture changes

Changes to any of the following should include an RFC update or new RFC:

- compiler kernel/coordinator boundary,
- fact identity or invalidation semantics,
- trusted correctness base,
- bootstrap layering,
- capability model,
- verification authority,
- service/session isolation,
- Rust succession policy.