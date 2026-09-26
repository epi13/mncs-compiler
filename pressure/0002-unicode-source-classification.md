# CP-0002 — Byte text helpers do not provide Stage-0 Unicode classification

Status: open; reproduced as a native compiler gap

> Historical description and reproductions below preserve the original
> finding. See the current reconciliation at the end of this file and the
> pressure index for live status.

compiler-architecture. Severity: high for frontend parity. Frequency:
common. Upstream tracking: `mncs.std.text_utf8.v1` (INGEST-P-003
substrate) landed after this pressure; property tables still missing.

## Workload and evidence

Rust Stage-0's `mncs-syntax/src/source.rs::lex` uses `char::is_whitespace`,
`is_alphabetic`, and `is_alphanumeric`, and advances by UTF-8 scalar width. The
pinned `library/std/text_scan.mncs` explicitly scans bytes without deriving UTF-8
validity; `text_view.mncs` carries producer-attested validity. These inspected
helpers do not supply the scalar traversal/classification this lexer requires.
This is a stdlib inventory finding, not proof that Unicode is inexpressible in MNCS.

`tests/fixtures/frontend.json` includes `mncs 0.10; module café;`.
`python3 tools/test_frontend.py` requires parser code 8 and lexical-summary code 3
for non-ASCII input. Raw `next_token` also checks byte 255 and returns a distinct
unsupported-input diagnostic, not Stage-0's MNL002. Raw tokens inside comments
may include arbitrary bytes; only the whole-source APIs enforce the ASCII domain.

## Workaround, desired behavior, and impact

Restrict whole-source requests to ASCII and report the entire source span as
unsupported; do not claim Unicode agreement. Desired: versioned Unicode property
tables plus validated scalar traversal and stable byte-offset spans, or a
compiler-owned MNCS implementation if this is not appropriate for stdlib.

- Correctness: valid Unicode MNCS modules are rejected by this bootstrap slice.
- Safety: byte guards prevent out-of-range access; no decoding assumptions.
- Runtime/compiler cost/memory: Unicode tables and decoding costs not measured.
- Determinism: Unicode table version must be part of future semantic identity.
- Complexity: decoder, classification tables, invalid UTF-8 policy, and scalar
  line/column rendering are required before removing the ASCII restriction.
- Likely owners: stdlib/runtime and compiler architecture; no need demonstrated
  for a new syntax feature.

## Re-evaluation (Stage-0 `a7a8c05`, 2026-09-12): partially resolved

Half of the desired capability now exists. `mncs.std.text_utf8.v1`
provides bounded UTF-8 validation (first-bad-byte index), scalar
stepping with a progress guarantee (`ScalarStep { scalar, next, valid }`),
and a documented-narrow case fold — as generic functions over
`[byte; up_to N]` views, usable at any ceiling. The decoder the original
report asked for is therefore available as a stdlib substrate (with
execution corpora on all five backends per the language docs).

What remains is precise and narrower than the original entry: Unicode
*property tables* — scalar White_Space / Alphabetic / Alphanumeric
classification matching Rust `char::*` semantics — plus the policy for
scalar-width advance and invalid-UTF-8 diagnostics in the lexer. Only
byte-level `is_space` exists (JSON modules), and the fold covers just
ASCII A-Z plus Latin-1 capitals by explicit design. Adoption path: the
test-transport probe must first resolve `mncs.std.*` imports (it
currently maps only `src/compiler/*`), then a scalar lexer can be built
on `step_scalar_generic` and differentially pinned.

Minimal reproducer for the remainder: any non-ASCII module (e.g. the
`café` fixture) is still rejected by the ASCII gate on both sides of
the differential, so parity holds by mutual refusal, not by agreement.

## Current reconciliation (2026-09-25)

Current Stage-0 accepts the Unicode module probe under profile 0.18. The compiler-owned frontend remains ASCII-only. This is a frontend parity issue; the language already accepts the input.
