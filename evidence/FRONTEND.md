# Bounded deterministic frontend slice

This is compiler code in MNCS, compiled/elaborated and executed by
pinned Rust Stage-0. It is **not self-hosting**, a standalone compiler executable,
or evidence of native/backend parity. Rust remains the current compiler.

## Reproduce

From the repository root, with Rust/Cargo, Python 3, curl, and tar:

```sh
tools/bootstrap.sh
python3 tools/test_frontend.py
python3 tools/test_pressure.py
MNCS_LIBRARY_PATH=src .bootstrap/target/debug/mncs abi src/compiler/kernel.mncs
MNCS_LIBRARY_PATH=src .bootstrap/target/debug/mncs compile src/compiler/kernel.mncs --emit semantic,hir,ssa --output-dir .build/compiled
```

The bootstrap script downloads the exact locked revision into ignored
`.bootstrap/`; it never reads or writes the sibling checkout. Dependency locks
are retained for both Stage-0 and the test-only probe. Network is needed for
first bootstrap; no MNCS ecosystem service is required. The current pin and
all seven compiler source modules use Profile 0.18. The tests retain older
profile-less lexical snippets where that isolates version-independent byte
and token behavior; version-gated syntax is tracked separately in CP-0015.

The current-pin [`frontend-results.json`](frontend-results.json) records two
equal normalized execution-result digests over 5,887 requests, 196 lexical
sources, 1,586 compared tokens, and 18 header cases. It records 3,027,556
interpreter steps and 190.018 seconds for the twin. This is bounded corpus
evidence, not a proof of all-input equivalence. Re-running writes fresh results
to `.build/`.
[Compilation evidence](compile-results.json) records two byte-identical semantic,
HIR and SSA emissions. Compilation completed with 242 CMP301 unresolved
obligations and conservative fallbacks, not a fully discharged proof (CP-0007).

## Implemented contracts

| Unit | Deterministic input and result | Dependencies and verification |
| --- | --- | --- |
| `source` | Exact immutable byte view, length ≤64; guarded byte access, ASCII admission, span validity | No ambient input; out-of-range reads yield sentinel 256, distinct from every byte |
| `lexer.next_token` | Source plus cursor → kind, half-open byte span, diagnostic | Source byte access; lexical corpus checked against Rust; caller advances to `end` |
| `lexer.significant` | Source plus cursor → next nontrivia token or lexical failure | Same lexical rules; malformed comments are never discarded |
| `kernel.lex_summary` | Source → coverage, counts, first diagnostic span | ASCII admission, token computation, `token_shape` structural verifier |
| `parser.header` / `kernel.parse_header` | Source → version/module spans, next cursor, first error | ASCII admission then lexical obligations and header grammar; ends at module semicolon |
| `kernel.same_source` | Two exact byte values → equality | Full length/byte comparison; no hash collisions, no persistent hash claim |
| `source.line_col` | Byte offset → 1-based (line, column) | Newline count + bytes-since-newline, clamped past the end; byte-exact agreement with Stage-0 spans on the ASCII domain (non-ASCII columns count scalars there: CP-0002) |

The header grammar follows Stage-0: `mncs Version ; module Segment (. Segment)* ;`,
where Segment is an identifier or the keyword `mncs`. Version syntax is lexical;
profile support is a later semantic obligation. Thus `0.999` and Stage-0's unusual
`1.2..3` version token can form a header fact. Comments/whitespace may occur
between segments; the module span retains them. It is a spelling reference,
**not a normalized symbol or stable module identity**. Declarations after the
semicolon are not parsed. Whole-source ASCII admission still checks those bytes.

Parser `code == 0` with `state == 8` means header established, not valid program.
Error spans identify the first failed header/lexical obligation; expected-token
categories are encoded by `code`. Raw `next_token` is a byte primitive: arbitrary
non-ASCII token starts report diagnostic 3, and comment bodies may contain them.
Only the admitted ASCII domain is compared with Rust. Human line/column
rendering, Unicode validation, and exact Rust parser error messages are deferred.
Numeric token-kind values are explicitly defined by `src/compiler/token-kinds.json`;
they are a local wire vocabulary, not Rust enum discriminants. Result codes are
specified beside their MNCS record declarations.

## Fact/execution posture

- Natural unit: one bounded source/header request, evaluated serially and purely.
- Conceptual key: operation name/version plus **all exact input bytes and cursor**
  where relevant, together with compiler semantic/toolchain identity. No target
  or capability input affects these frontend facts. No persisted keys are emitted.
- Invalidation: changed bytes, cursor, or implementation semantics require a new
  computation. No cache or global mutable invalidation mechanism exists yet.
- Provenance: all spans refer to the caller's exact source; the caller retains it.
  Result records alone are not reusable across snapshots. Workspace association,
  source/module IDs, serialization and content-addressed storage are deferred.
- Invariants: tokens progress and cover bytes through EOF; spans stay within source;
  diagnostics are preserved; header success requires the terminating semicolon.
  `token_shape` verifies structure only, not semantic token classification.
- Concurrency safety: no shared mutable state, effects, allocation, service, or
  ambient filesystem access. Cancellation is not implemented; each request has
  a bounded amount of work and can fail at the Stage-0 execution-budget boundary.
- Target/capabilities: reference interpreter and Stage-0 compilation evidence only;
  pure kernel requires no host grants. No backend implementation added.
- Cacheability/reuse: these pure results are candidates for source-unit reuse,
  but caching a 64-byte request has no demonstrated economic benefit.
- Cost: step counts in the result report; conservative O(n²) lexical summary due
  to bounded cursor scans. Wall time includes reference elaboration, transport,
  assertion work, oracle work and two complete runs. No peak-memory or native
  performance claim is made.

## Coverage and limits

Lexical tests cover every ASCII byte, every keyword and operator, nested and
unterminated comments, numeric/range/version ambiguities, exact-capacity inputs,
and seeded random ASCII mixtures. Each Rust token kind/span and lexical diagnostic
is compared; independent MNCS shape checks verify coverage and progress. Header
cases compare spans with the Rust parser (explicit golden spans when Stage-0
discards its AST for envelope-only errors, CP-0006); a minimal declaration is appended only
to the *oracle* prefix because Stage-0 rejects otherwise empty modules. Negative
cases compare first error spans, not full recovery behavior. Guard tests include
EOF, maximal u64 cursor, invalid spans/tags, byte 255 and over-capacity source.

The host adapter is temporary test infrastructure (CP-0005). It calls pinned
Rust APIs and MNCS functions; Python does not lex or parse. The shared Stage-0
trust base means these tests cannot detect every toolchain miscompilation.

Implemented: bounded source primitives, ASCII lexical processing, spans,
structured lexical evidence, header syntax facts, exact source equality.
Partial: parser, source representation, diagnostics, request/fact boundaries.
Blocked for whole-module scale: source storage capacity (CP-0001); Unicode parity
needs scalar/classification support (CP-0002).
Deferred: import/dependency discovery, workspace graph, persistent identities,
symbols, semantics/types/effects/ownership, expression/declaration AST, IR,
optimization, backend lowering/artifacts, self-hosting, caching, service,
concurrency and all learned/distributed integration.
