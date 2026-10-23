# CP-0028: Large-source trivia edits miss Language parse reuse

## Current status

Resolved by Language main `d74402a057c085f928a81644e6195ab1e52a78b3` and
verified in the Compiler's real digest-application workload on 2026-10-23.

## Workload and reproduction

The `mncs.test.digest::digest_app` application build compiles the real
`mncs-test/native/mncs/test/digest.mncs` root and imported assertion, suite,
and application modules. After the cold build, the harness appends one comment
to the root source and rebuilds the application in the same process.

Run `tools/test_vm_language_module_elaboration_cache.py` with the configured
MNCS workspace and repository roots. The harness requires the exact Language
revision from `mncs-language.lock.json` and checks the source-parse telemetry.

## Original failure

The comment edit reused the three unchanged imported parse results. The root
source had the same significant tokens, but Language rejected its syntax-tree
rebase candidate, invoked the parser, and published another entry:

- exact source parse results reused: 3
- same-significant-token syntax trees reused: 0
- source identities parsed: 1 (the edited root)
- entries published: 1
- candidate entries rejected: 1

That run failed the required root-reuse assertion before its post-edit
execution comparison. The cold digest application compiled and matched the
canonical VM oracle.

## Why this is insufficient

Trivia edits preserve parser inputs, so a verified syntax tree should be
reusable after fresh lexing and independent source-location rebasing. Reusing
only imported modules still leaves the root parse and parse-cache publication
unnecessary. The existing small unit fixture did not cover the large digest
application AST shape that exposed the miss.

## Fix and current verification

`mncs-language` owns syntax-tree identity, span rebasing, and parse reuse.
Its rebaser now recognizes a source span only when all four numeric source
coordinates are present, so semantic `start`/`end` fields on aggregate AST
nodes no longer hide nested spans. It also rebases zero-width spans to the
corresponding current token boundary or current EOF. Unsupported anchors still
fail closed to a fresh parse.

The updated Compiler harness passes against exact Language revision
`d74402a057c085f928a81644e6195ab1e52a78b3`:

- Cold build parsed and published four sources; its VM output matched Stage-0.
- Root comment edit reused three exact imported parses and one syntax tree with
  unchanged significant tokens; it parsed and published zero sources, reused
  30 semantic facts, and matched Stage-0 output.
- Imported assertion-body edit parsed and published only that changed source,
  reused two exact imports and the root syntax tree, recomputed one changed
  function fact, reused 29 facts, and matched Stage-0 output.
- Fresh-process reuse reported the same parse and semantic reuse counts and the
  same VM artifact identity.
- Invalid source was rejected without publishing an artifact. A tampered cache
  receipt caused a cold four-source parse and verification.
- Final integration was rerun after merging current Compiler main; it passed at
  Compiler revision `45128fba33c5fb905731043261be9bf3a36ce6ac`. The cold build
  took 0.779 seconds and the comment-edit build took 0.467 seconds in this
  workload run. These timings are workload-specific; the compiler request's
  reported peak RSS was at most 55.7 MB, and cgroup peak memory was unavailable.

See the complete stage telemetry and artifact identities in
[`../evidence/language-module-elaboration-cache-20261023-language-d74402a.json`](../evidence/language-module-elaboration-cache-20261023-language-d74402a.json).

## Evidence

The workload used `mncs-test` root source SHA-256
`fa45534cdebb4de683675bfb70eed15cfa177a5198f036c0e3b7fcab68b91c30`,
Compiler harness revision `45128fba33c5fb905731043261be9bf3a36ce6ac`, and
Test repository revision `57802fba4dea04ad50004d3e1e6768b507b889bb`. Its
release Stage-0 and probe outputs were reused after the initial clean rebuilds
of 171 and 168 seconds. Per-request VM peak RSS was 55.7 MB; the cgroup
peak-memory counter was unavailable.
