# CP-0006 — Stage-0 envelope inference disagrees with leading-comment syntax

Status: open, re-confirmed (2026-09-12). Category: tooling,
compiler-architecture. Severity: medium. Frequency: occasional.
Upstream tracking: none; re-probed at 0.13 (MNE002 persists) and the
frontend suite now also pins the MNP008 parse-stage companion.

While comparing header facts, `/* leading comment */ mncs 0.10; module a;`
followed by a valid function reaches Rust parsing but loses its AST with MNE002:
“source header version does not match its envelope”. The source lexer/parser
allows trivia before the header; `SourceEnvelope::new` profile inference does
not agree for this spelling. `repro/leading-comment-envelope.mncs` reproduces it
with `mncs abi`; `python3 tools/test_pressure.py` checks MNE002 at the locked pin.

Workaround: the MNCS kernel establishes only syntax facts, leaving profile and
envelope validity as later obligations. Tests compare ordinary valid-header AST
spans directly. Cases where Stage-0 discards its AST solely for envelope errors
use explicit expected spans and Rust CST boundaries; they are not claimed as
full AST parity. Unknown profile/version spellings are also syntax-only tests,
not claims that Stage-0 accepts those programs.

Desired behavior: deterministic agreement between lexical header recognition and
profile inference, with syntax facts inspectable even when envelope validation
fails. Likely owners: Stage-0 tooling and compiler architecture, not new syntax.
Correctness: valid-looking commented input cannot be elaborated through this
Stage-0 entry path; the new header fact makes no full-program acceptance claim.
Safety/determinism: unchanged. Runtime, memory and compiler-performance impact
are unmeasured; test complexity increases because AST access is coupled to
unrelated validation. No upstream file was changed.

## Re-evaluation (Stage-0 `a7a8c05`, 2026-09-12): still valid

Re-probed with a 0.13 header spelling: MNE002 persists, now accompanied
by a parse-stage MNP008 profile gate for unknown versions (the frontend
suite's envelope branch was updated to require the envelope diagnostic
rather than stage exclusivity — same workaround, wider oracle shape).
The kernel contract is unchanged and still the right layering: syntax
facts first, envelope/profile validity as later obligations. No
language change needed; this stays a Stage-0 tooling agreement issue.
