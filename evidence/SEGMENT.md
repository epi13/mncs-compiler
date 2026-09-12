# Segment-vertical evidence (absolute offsets over four chunks)

Self-hosted MNCS implementation in `src/compiler/segment.mncs`: the same
lexical semantics as the single-chunk `source`/`lexer` modules over a
four-chunk logical source (≤256 bytes) with absolute offsets. All
behavior executes in MNCS on the pinned Rust Stage-0 reference
interpreter. This is **not self-hosting** and not backend parity; Rust
remains the current compiler.

## Reproduce

From the repository root (after `tools/bootstrap.sh`):

```sh
python3 tools/test_segment.py
python3 tools/test_frontend.py
```

`tools/test_segment.py` is the committed twin differential; its report
lands in `.build/segment-results.json` (promoted copy:
`evidence/segment-results.json`). The probe runs narrowed to
`source,lexer,segment` (see `MNCS_PROBE_MODULES`); `decl` stays excluded
while CP-0014 blocks its elaboration.

## What is implemented

`segment.total_valid` (chunks + total): the total must not exceed
supplied bytes. `segment.byte_at` (chunks + total + offset): absolute
fetch with the 256 sentinel past `total`, mirroring `source.byte_at`.
`segment.ascii` (chunks + total): four-link whole-source admission over
a shared `AsciiState`. `segment.span_valid`: bounds check against the
total. `segment.keyword` (chunks + total + span): word classification
identical to `lexer.keyword`, duplicated because single-chunk fetch
cannot cross chunk boundaries — any divergence is a compiler bug, and
this suite is the tripwire. `segment.scan_step` / `scan_fuel` /
`next_token` / `significant` / `skip_trivia`: absolute-offset twins of
the `lexer` functions with identical kind and diagnostic rules.

## 0.13 migration (current pin)

`segment.mncs` and `lexer.mncs` are profile-0.13 sources: sequential
fuel-chain links share one index name (`ai`/`si`/`gi` families → `i`,
proving CP-0009's sequential-reuse rule in real code); scan termination
uses native `!advance` (CP-0004); `lexer.punctuation` is a nested scalar
`match` with MNE140-checked defaults (CP-0010). The differential below
is the executable proof for all three.

## Differential results vs Stage-0 oracle

- Twin differential (`tools/test_segment.py`, two identical runs):
  10679 probe requests over 72 texts (16 fixed edge cases, 24 seeded
  random ASCII up to 256 bytes, 3 non-ASCII), 4803 tokens compared
  kind/span/diagnostic against the oracle, max interpreter steps 42565
  per request. Every sample runs under two chunkings (even-64 splits
  plus a seeded random split), so token continuation across chunk
  boundaries is covered, including mid-token and mid-comment splits.
- Checks per sample/split: total gate (exact/zero/over), byte fetch at
  strided offsets plus the sentinel past `total`, ASCII admission, full
  token walk with progress assertion, significant-token chain against
  oracle non-trivia tokens (diagnostic-bearing trivia is returned, never
  skipped — matching `parser.step`'s consumption rule), and `keyword`
  agreement on every word-shaped oracle token.
- Non-ASCII samples pin the ASCII gate: `ascii` is false and the first
  non-ASCII byte yields kind 7 / diagnostic 3 with a one-byte span.

## Scope limits

- Bounded-corpus evidence (≤256 bytes), not all-input equivalence.
- The oracle is driven with profile-less snippets, for which Stage-0
  preserves historical lexing; profile-gated tokenization (`!` → `not`
  at 0.13+) is a known gap owned by CP-0015, with versioned oracle
  inputs staged there.
- Cost: 25.8M interpreter steps total per run (~1421 s wall for the
  twin, dominated by probe boot and per-request transport). No
  peak-memory or native-performance claim (CP-0007).
