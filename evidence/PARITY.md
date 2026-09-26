# Current Rust Stage-0 parity

The locked Stage-0 reference is `mncs-language` revision
`4f9e1224e7f3cdb67fa4687d8f496717da1354aa`, Profile 0.18. The release CLI
and retained probe were bootstrapped from that exact lock. The compiler
frontend/declaration/model/codegen differential suites were executed at the
parent reference `b0f3e6447dbdefb5cd9fceeb43da2ab1909a7e70`; the only changes
between those revisions are the CLI `mncs impact` source-subject projection,
its test, evidence documentation, and badge data. The Rust compiler/model/
syntax/codegen library trees are unchanged, so RAVEL reuses their existing
identity-bound evidence. Their result files retain the exact revision at
which they ran; they have not been relabeled as 4f9 evidence. Earlier result
files retain their historical pins. The starting heads are in
[`campaign-20260925-start-heads.json`](campaign-20260925-start-heads.json).

## Current compiler vertical

`mncs-compiler` has one compiler-owned bounded project entry point,
`project.compile_project<M,N>`. Its immutable request contains ordered source
IDs and paths, a snapshot fingerprint, and exact per-source byte sequences in
a generic `up_to N` sequence. The project is capped at 64 sources; the checked
differential instantiates `N=1024`, beyond the former four by 64 byte ABI. The
native compiler parses each unit once, retains its `Unit` in `UnitProve`,
resolves declared module names and `use` edges/aliases against the snapshot,
detects duplicate modules and missing imports, and returns proof and typed
CFG facts for each module. Sorting is deterministic and tested against reverse
filesystem creation order.

The host boundary is currently `tools/test_project.py`: it walks the fixture
directory, reads bytes, sorts stable relative IDs, and constructs the native
snapshot. The compiler itself does not walk the host filesystem, and there is
not yet a production project-loading CLI. Source IDs/paths are capped at 256
bytes. The snapshot fingerprint is supplied by the host and native code checks
its length but does not verify it against the bytes. `N=1024` is the tested
source bound, not a claim that real compiler source files of any size are
accepted.

Module semantics are partial. The native resolver owns module-header lookup,
import edges, aliases, missing-module failures, duplicate module names, and
deterministic source ordering. It resolves imported scalar, effect-free
callable members, checks argument/result types, and records the resolved
source slot and declaration span on typed calls. Tests bind that owner to
Stage-0's semantic callable identity; the native typed IR does not yet carry a
canonical `SemanticId`. `dep.answer(42)` passes native proof and typed CFG; a
`bool` argument is rejected at bytes 101–105, matching Stage-0's MNE133 span.
The Stage-0 reference links both functions and produces two SSA functions.
Imported nominal type ownership, nested imported type lookup, and imported
effect/capability identity remain unsupported. The Stage-0 source map can hold
only one source per module key, so duplicate filesystem entries under one
module name have only a native duplicate-diagnostic assertion.

Proof and CFG share authoritative facts. `decl.prove_parsed_unit` consumes the
retained parsed `Unit`; `decl.prove_unit` remains the source convenience
wrapper. `flow.lower_unit` proves once and delegates to
`flow.lower_proven_unit`; project lowering passes parsed/proven modules to CFG.
The project path does not parse again to recover declarations already held.

## Verification topology and RAVEL planning

`.mncs/project.json` points at the repository-owned
`.mncs/verification-obligations.json`. It declares nine external integration
obligations and one locked bootstrap integrity obligation. Python/Rust
transports are classified honestly as external integrations, not native MNCS
tests. Subjects and invalidation dependencies let an isolated
`flow.lower_unit` change select only `mncs-compiler.flow-cfg-differential`;
parser/source changes expand to their frontend, declaration, proof, flow,
project, pressure, syntax, and production-call closure.

The original two-command RAVEL preflight (`mncs impact` and
`mncs test-inventory`) took 360.251 seconds before both commands timed out at
180 seconds. The result remains recorded as UNKNOWN. A later plan without
source test inventory also returned UNKNOWN because the compiler impact
artifact omitted its subject identity/fingerprint. That result is preserved in
[`campaign-20260925-source-plan-without-test-inventory-unknown.json`](campaign-20260925-source-plan-without-test-inventory-unknown.json).

The missing fact was a generic compiler command-artifact contract, not a
language or runtime pressure. `mncs impact` now emits a compact compiler-owned
`source_subject` with source artifact identity, module, profile, semantic
program identity, and production fingerprint. RAVEL uses it to bind plans to
repository-owned external obligations without requesting source test
inventory or making another frontend call. Direct source-test selection still
requests `--include-test-inventory` and receives impact plus test cases in one
front-end session. The finding, Commons search, repair, and evidence are in
[`campaign-20260925-source-subject-pressure.json`](campaign-20260925-source-subject-pressure.json)
and the language repository's development evidence.

At current RAVEL `main` (`4ab04a8`) with current Stage-0 and Commons heads:

- `source.byte_at` selected nine current obligations in 9.226 seconds, reused
  16 evidence references, and required no new execution.
- `flow.lower_unit` selected only the flow CFG obligation in 13.169 seconds,
  reused 16 references, and required no new execution.
- `project.compile_project` selected only the project/source obligation in
  14.896 seconds and required no new execution.
- A direct source plan with local test inventory took 0.623 seconds.

All three obligation plans are bounded `direct_dependents` results with
`sufficient_to_stop=false`; RAVEL selected the appropriate obligation closure
but did not execute it or claim repository-canonical stop sufficiency. Their
exact selected identities and times are recorded in
[`campaign-20260925-ravel-planning.json`](campaign-20260925-ravel-planning.json).
The current RAVEL `tests.test_impact` suite passed all 12 tests. Historical
UNKNOWNs remain UNKNOWN records; they are not rewritten as PASS or zero work.

## Current evidence

Every result file records its exact Stage-0 revision and backend mode.

| Area | Executed evidence | Current boundary |
| --- | --- | --- |
| Bootstrap | Lock `4f9e122`; exact release CLI/probe build; warm integrity check 0.142 s | PASS; the lock and `.bootstrap/revision` match |
| Frontend | 7,893 requests, 31.249 s, retained Cranelift sessions, b0f3 | Bounded ASCII lexical facts and header spans |
| Segments | 6,564 requests, 16.178 s, retained Cranelift sessions, b0f3 | Token/span corpus; ASCII and bounded cases |
| Declarations | 9 requests, 75.341 s, retained Cranelift session, b0f3 | Structures, first-error spans, local name/symbol checks |
| Semantic proof | 49 cases + 5 verifier verdicts, 76.713 s, b0f3 | Tested proof diagnostics and typed postfix operations, not Rust body/SSA parity |
| Typed CFG | 4 cases, 21 requests, 42.191 s, one retained Cranelift session, b0f3 | Branch/jump/return/failure blocks, proof attachment, target/reachability checks |
| Project/import | 21 requests, 53.083 s, `N=1024`, b0f3 | Multi-module imported scalar call and edge cases; nominal/effect/capability imports remain partial |
| Profile 0.18 syntax | 5 cases, 37.510 s, b0f3 | `next` accepted; four CP-0015 forms remain unsupported |
| Pressure reconciliation | 21 probes, b0f3 | Historical pressure states retained; see reconciliation JSON |
| Production calls | Two runs, 1.099/1.128 s, b0f3 | Byte-identical output; 213 CMP301 unresolved obligations remain in reference compile result |
| Stage-0 source identity artifact | [40 CLI semantic-command tests passed at 4f9](campaign-20260925-language-impact-cli-tests.json) | Impact exposes subject identity without serializing test inventory |
| RAVEL planning | 12 focused tests passed at `4ab04a8`; plans above | Native selection uses repository obligations and exact reusable evidence |

A matched one-request execution comparison used the same 107-byte
`flow.lower_unit` input at Stage-0 4f9. The reference interpreter took 89.928
seconds and 2,105,931 steps. The retained Cranelift path took 40.652 seconds
including cold session compilation, retained one session, and reported one
execution step. Both returned the same result hash. Full suite timing remains
in the canonical result files; the interpreter remains an independent but
much slower comparison path.

## Current parity matrix

| Capability | State | Executable evidence / limit |
| --- | --- | --- |
| Stage-0/profile pin | Profile 0.18 at `4f9e122` | Exact bootstrap lock; Rust compiler libraries unchanged from tested b0f3 parent |
| Source representation | Partial | Generic exact per-file bytes and ordered snapshot; 64-source cap, 256-byte metadata cap, tested `N=1024`; host discovers paths and supplies fingerprint |
| Parse/proof reuse | Partial | Parsed `Unit` retained through proof and CFG; imported proof facts remain partial |
| Current-profile parser | Partial; CP-0015 open | `next` fields/projections work; `!`, negative atoms, repeat literals, integer `match` remain unsupported |
| Module/import resolution | Partial | Native module identity, aliases, imported scalar callable resolution, missing-module diagnostics, duplicate modules, deterministic ordering |
| Callable/type/effect resolution | Partial | Imported scalar calls carry source-slot/declaration-span ownership; canonical callable identity, imported nominal types and imported effect/capability proof remain absent |
| Type/effect/capability proof | Partial | 49 semantic cases plus five verifier verdicts agree with tested Stage-0 subset |
| Typed operations | Partial | Type/span-bearing postfix operations are checked; these are not SSA values |
| Typed CFG | Partial | Four control-flow cases match tested diagnostics and shape; operations remain attached to source expressions/blocks |
| Value-carrying SSA | Absent | No SSA value IDs, block parameters/merge values, or native canonical callable IDs |
| Executable backend | Absent | No native target code or executable compiler output |
| Diagnostics | Partial | Tested semantic/CFG codes and spans agree; full message/recovery and leading-comment envelope parity remain incomplete |
| Unicode source | Absent for native frontend | Current Stage-0 accepts tested Unicode cases; native source admission remains ASCII-only (CP-0002) |
| Self-hosting | Absent | Stage-0 still loads and executes the MNCS compiler; no Stage-1/Stage-2 proof |

CP-0016's compiler-origin nested record/call runtime failure was repaired in
`mncs-language` at `b0f3e64`; current `main` also contains the source-subject
artifact repair at `4f9e122`. CP-0017 remains a compiler-local diagnostic fix
with the semantic differential passing. CP-0015 is partially reconciled:
`next` passes, while `!` fails at 66–67, a negative atom at 58–59, a repeat
literal at 60–60, and integer `match` at 67–67; Stage-0 accepts those exact
Profile 0.18 reproducers. The imported nominal/effect/capability gaps are
compiler work, not new language/runtime pressures.

## Narrowest next parity step

Add value-carrying SSA values and block parameters for joins, carrying the
resolved canonical imported callable identity into typed value flow and a body
verifier. The current imported-call reference is only a source slot and
declaration span. After that IR is verified, native target emission is the
next backend boundary.
