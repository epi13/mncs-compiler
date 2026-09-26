# Segment-vertical evidence (absolute offsets over four chunks)

MNCS compiler implementation in `src/compiler/segment.mncs`: the same
lexical semantics as the single-chunk `source`/`lexer` modules over a
four-chunk logical source (≤256 bytes) with absolute offsets. All
behavior executes in MNCS on the pinned Rust Stage-0 reference
interpreter. Current compiler modules declare profile 0.18 and Stage-0 is
pinned to `709ba00810099e6965bb47dec14ed19e9e1ae6f8`; the last current-pin
segment suite result is recorded separately from the historical corpus. This is
**not self-hosting** and not backend parity; Rust
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
`source,lexer,segment` (see `MNCS_PROBE_MODULES`); `decl` is enabled again after the current Stage-0 bool-payload fix; this
segment-only probe remains narrowed to its own affected closure.

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

## Current-profile implementation

The segment and lexer modules now declare Profile 0.18. The implementation
uses sequential iteration-link reuse, native prefix negation, and scalar
`match`; their original pressure resolutions remain historical evidence in
CP-0004, CP-0009, and CP-0010. Current pressure status comes from the 0.18
revalidation, while this differential isolates the bounded byte/token path.

## Differential results vs Stage-0 oracle

- Current-pin twin differential (`tools/test_segment.py`, two identical runs):
  10,679 probe requests over 72 texts (16 fixed edge cases, 24 seeded
  random ASCII up to 256 bytes, 3 non-ASCII), 4803 tokens compared
  kind/span/diagnostic against the oracle, max interpreter steps 42565
  per request. The twin result digest is
  `cbc1a90641b5e589d497ad5a6636e61e38a03296a58242d18661eaaa1b2148fb`,
  with 25,755,576 total steps and 1,318.891 seconds elapsed. Every sample
  runs under two chunkings (even-64 splits
  plus a seeded random split), so token continuation across chunk
  boundaries is covered, including mid-token and mid-comment splits.
- Checks per sample/split: total gate (exact/zero/over), byte fetch at
  strided offsets plus the sentinel past `total`, ASCII admission, full
  token walk with progress assertion, significant-token chain against
  oracle non-trivia tokens (diagnostic-bearing trivia is returned, never
  skipped — matching `parser.step`'s consumption rule), and `keyword`
  agreement on every word-shaped oracle token.
- Non-ASCII samples pin the ASCII gate: `ascii` is false and the first
  non-ASCII byte yields kind 7 / diagnostic 3 with a one-byte span. This
  remains a native frontend gap because current Rust accepts the Unicode
  module probe (CP-0002).

## Scope limits

- Bounded-corpus evidence (≤256 bytes), not all-input equivalence.
- The scanner corpus uses profile-less or historical syntax inputs to isolate
  version-independent lexical behavior. Version-gated declaration forms are
  tested separately in CP-0015 against full Profile 0.18 modules; all five
  currently reproduce as native parser gaps.
- Cost: the current twin took 1,318.891 seconds in Stage-0 and used 25.76M
  interpreter steps. No peak-memory or native-performance claim is made
  (CP-0007); linked artifact cost is measured separately in
  [`compile-results.json`](compile-results.json).
