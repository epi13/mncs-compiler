# CP-0002 — Byte text helpers do not provide Stage-0 Unicode classification

Status: open. Category: stdlib-runtime, compiler-architecture. Severity: high for
frontend parity. Frequency: common. Upstream tracking: none.

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
