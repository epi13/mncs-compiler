# CP-0028: Large-source trivia edits miss Language parse reuse

## Current status

Reproduced on 2026-10-23; Language-owned remapping fix is in progress.

## Workload and reproduction

The `mncs.test.digest::digest_app` application build compiles the real
`mncs-test/native/mncs/test/digest.mncs` root and imported assertion, suite,
and application modules. After the cold build, the harness appends one comment
to the root source and rebuilds the application in the same process.

Run `tools/test_vm_language_module_elaboration_cache.py` with the configured
MNCS workspace and repository roots. The harness requires the exact Language
revision from `mncs-language.lock.json` and checks the source-parse telemetry.

## Current behavior

The comment edit reused the three unchanged imported parse results. The root
source had the same significant tokens, but Language rejected its syntax-tree
rebase candidate, invoked the parser, and published another entry:

- exact source parse results reused: 3
- same-significant-token syntax trees reused: 0
- source identities parsed: 1 (the edited root)
- entries published: 1
- candidate entries rejected: 1

The build failed the harness's required root-reuse assertion before its
post-edit execution comparison. The cold digest application did compile and
match the canonical VM oracle.

## Why this is insufficient

Trivia edits preserve parser inputs, so a verified syntax tree should be
reusable after fresh lexing and independent source-location rebasing. Reusing
only imported modules still leaves the root parse and parse-cache publication
unnecessary. The existing small unit fixture did not cover the large digest
application AST shape that exposed the miss.

## Likely ownership and desired behavior

`mncs-language` owns syntax-tree identity, span rebasing, and parse reuse.
`mncs-compiler` owns application-build orchestration and reports the actual
Language observation. The root should reuse its syntax tree when the complete
significant-token sequence and parser assumptions match, while returning
current CST tokens and correctly refreshed AST, CST, and diagnostic locations.
Any unsupported or unmappable span must continue to fail closed to a fresh
parse.

## Evidence

The reproduction used `mncs-test` root source SHA-256
`fa45534cdebb4de683675bfb70eed15cfa177a5198f036c0e3b7fcab68b91c30` and
Language main revision `307e517ac4db9f46369e7e6e0638052bd223cd90`. No wall-time
or peak-RSS claim is made; the first run included two clean Rust release
builds, and the second reached the failing telemetry assertion using those
artifacts.
