# Current Rust Stage-0 parity

The campaign uses the exact Profile 0.18 Stage-0 pin at mncs-language
revision 843c5bcca7476bb6600218f6410a3da7ef5d96d5. The compiler, language,
RAVEL, and Commons changes began from clean worktrees at the heads recorded in
[campaign start heads](campaign-20260926-start-heads.json). The focused source,
SSA, frontend, proof, CFG, pressure, and bootstrap results are retained in the
campaign evidence files linked below.

## Current compiler vertical

The single native project entry point remains project.compile_project<M,N>.
It consumes an ordered source snapshot, parses each tested unit once, retains
the parsed Unit through proof and CFG lowering, resolves module headers and
use edges/aliases, and passes the established facts downstream. The bounded
campaign snapshot has two modules and 1,103 source bytes. The test host still
discovers files and transports their bytes; the compiler owns project
semantics for the tested subset.

The host-provided snapshot fingerprint is not semantic authority. The
compiler cannot derive or authenticate it from the snapshot yet, so results
explicitly report fingerprint_authenticated=false. A changed fingerprint
does not change the compilation verdict. Compiler-issued module, callable,
and typed facts are the semantic inputs to later stages.

Imported callable resolution now carries the exact compiler identity through
typed calls. For example, the multi-module fixture carries
mncs:0.2:function:demo.dep::answer into SSA, alongside the root function
identity mncs:0.2:function:demo.merge_root::main. The native representation
constructs these identities from the parsed profile, declaring module, and
declaration name. The prior source-slot/declaration-span locator may still be
used to find the declaration, but it no longer stands in for callable
identity or dispatch.

Native project proof and verified SSA now preserve declaring-module ownership
for imported finite, record, and nested nominal identities, plus effect and
capability identities; see the imported-nominal SSA evidence. The current C11
structural adapter remains a narrower scalar, effect-free call slice. Local
type/effect facts continue to come from the native proof stage.

## Bounded value SSA

The new ssa module builds on the typed CFG. It assigns explicit dense SSA value
identities, types produced and consumed values, represents values crossing
edges as block arguments, and represents joins with typed block parameters.
It emits branch, jump, return, and failure terminators for the supported
slice, with local scalar values and resolved imported scalar calls.

The verifier checks value definitions and uniqueness, use-before-definition,
instruction result and input types, callable identity and call argument
types, branch conditions, block argument arity/types, valid targets, return
types, and terminators. The two-module fixture has four root blocks, six
block parameters, and nine dense values. The verifier accepts the valid graph
and rejects nine targeted invalid mutations. Its facts match the pinned
Rust Stage-0 body/SSA identities and control-flow shape. This is a bounded SSA
slice, not full Rust SSA or compiler parity.

Parsing, proof, CFG, and SSA pass their upstream facts forward. No extra parse
or proof pass is introduced to recover data already available upstream.

## RAVEL selection and planning

RAVEL selected verification from compiler-issued program identities. Its
native planner initially failed because the checked-in planner descriptor held
an old interface identity; the descriptor was updated only after `mncs abi`
reported the current identity. The final native plans used inventory revision
10, had no Python fallback, and reused current identity-bound PASS evidence for
every selected obligation. The broad `source.byte_at` plan selected eight
current obligations; project selected two, SSA one, declarations five, parser
six, kernel two, segment one, flow one, and lexer selected none. The Stage-0
pressure suite is correctly retained as `reference_only`, since it does not
execute native compiler source.

The nine final planning requests took 95.428 seconds total (1.534–25.976
seconds each). Each is a bounded `direct_dependents` plan: it selects and
validates obligations but does not execute tests or claim repository-wide stop
sufficiency. Historical UNKNOWN plans remain UNKNOWN. The exact plans,
obligation decisions, source fingerprints, and evidence hashes are retained in
the `campaign-20260926-final-*-plan.json` files and
[`campaign-20260926-current-evidence.json`](campaign-20260926-current-evidence.json).

## Current evidence

All campaign result files name the exact Stage-0 revision. MNCS code executed
through retained Cranelift sessions; the Rust Stage-0 probe provides the
independent differential oracle. The interpreter was used as a secondary
reference during verifier debugging, not as the normal suite runner.

| Area | Current executable evidence | Boundary |
| --- | --- | --- |
| Bootstrap | Updated lock and marked .bootstrap tree match 843c5bc; warm bootstrap exits 0 | Establishes reproducible Stage-0 provisioning, not self-hosting |
| Profile 0.18 frontend | 21 positive, negative, and old-profile cases; 49.725 s; all conformant | Covers `!`, negative integer atoms, repeat literals, `next`, and integer match; grammar remains bounded |
| Frontend and segment | 7,893 frontend requests / 196 sources / 1,759 tokens in 32.714 s; 6,564 segment requests / 43 texts / 3,161 tokens in 15.951 s; two identical runs each | Retained Cranelift, one step per request; Unicode remains explicitly unsupported by the native frontend |
| Declarations | 9 requests, repeated twice identically; one retained Cranelift session; 100.977 s | Structural parsing, first-error spans, and declaration verdicts for the recorded corpus |
| Semantic proof | 49 cases plus five proof verdicts, repeated twice; 97.732 s | Tested proof diagnostics and typed facts, not full Rust body semantics |
| Typed CFG | Four control-flow cases, 21 requests, repeated twice; 56.109 s | Branch/jump/return and reachability facts for the tested slice |
| Project and value SSA | 34 requests; 125.137 s; two modules; canonical imported call identity and verified merge values | Scalar imported calls and structured control flow; no native target code |
| Stage-0 compile cost | Two identical linked artifact runs in 1.113 / 1.109 s | Four output artifacts per run, byte-identical; not native compiler cost or peak memory |
| Pressure probes | Four expected rejections and one supported control; 0.0122 s | Locked Stage-0 reference outcomes only; not native compiler verification |
| RAVEL plans | Nine native plans in 95.428 s total; all selected obligations current | Reuses identity-bound PASS records; direct-dependents plans do not execute tests |

The retained Cranelift request step count is one per request, with one retained
session per focused module group. This bounds repeated execution cost, but no
like-for-like wall-time speedup was demonstrated. The project suite grew from
53.083 s to 125.137 s as it added project identity and SSA verification, while
declaration, semantic, and CFG suites also grew. RAVEL's nine-root planning pass
took 95.428 s total and was not faster than its prior warm measurements. The
edit-to-check loop stayed bounded to the affected closure, while cold execution
and planner startup remain measurable costs to reduce.

The original interpreter comparison remains historical evidence only: a
matched 107-byte flow.lower_unit request at Stage-0 4f9e122 took 89.928 s and
2,105,931 interpreter steps versus 40.652 s on retained Cranelift. It is not
relabelled as a measurement at the current 843c5bc pin.

## Current parity matrix

| Capability | State | Evidence and limit |
| --- | --- | --- |
| Stage-0/profile pin | Current | Exact 843c5bc lock and successful bootstrap preflight |
| Project resolution | Partial | Headers, imports, aliases, duplicate/missing checks, stable ordering, and one-parse fact reuse for the tested snapshot |
| Snapshot fingerprint | Unauthenticated data | Host value is reported as unauthenticated and does not affect semantic validity |
| Callable identity | Implemented for tested imported calls | Exact Stage-0 callable/declaration identity reaches typed calls and verified SSA |
| Imported nominal types and effects | Partial; verified SSA slice | Declaring-module ownership for imported finite, record, nested nominal, effect, and capability identities reaches verified value SSA; aggregate C11 lowering remains absent |
| Current-profile syntax | Current on the tested forms | CP-0015 syntax forms and old-profile gates match Stage-0; unsupported grammar remains explicit |
| Semantic proof | Partial | 49-case differential and verifier controls |
| Typed CFG | Partial | Tested branches, joins, returns, and reachability |
| Value-carrying SSA | Partial | Dense IDs, typed operations, block arguments/parameters, terminators, canonical imported call identity, and body verification |
| Native C11 output | Partial; scalar structural slice | The test harness projects verified imported-call/CFG SSA into explicitly unattested structural input for `mncs-language`; constants, pure scalar imports, `u64` addition, and branch/join execute with pinned Stage-0 parity. No proof-carrying adapter or aggregate lowering. See `campaign-20260928-agent-native-native-backend-vertical.json`. |
| Unicode source | Absent in native frontend | Existing compiler Unicode pressure remains outside this slice |
| Self-hosting | Absent | Stage-0 still compiles/executes the MNCS compiler; no Stage-1 proof |

## Commons pressure reconciliation

CP-0014 remains open in the native project path. The current compiler head
rejects the exact bool enum-payload project reproducer that locked Stage-0
accepts; `campaign-20260928-agent-native-cp0014-current.json` records that
result. `decl.mncs` still defers finite matches and enum construction, and
`ssa.mncs` rejects `TMatch`, so the parser-to-SSA path is incomplete. CP-0015
is resolved for the tested Profile 0.18 syntax, proof failures, and older
profile controls: all 21 current-head cases conform to locked Stage-0 in
`campaign-20260928-agent-native-profile-surface-results.json`. CP-0010
integer match dispatch and CP-0013 next-field behavior remain confirmed, and
the bootstrap refresh issue is resolved.
CP-0001's remaining per-source ceiling,
CP-0002 Unicode refusal, CP-0005's host test transport, CP-0006's current
Stage-0 envelope behavior, and CP-0007's linked artifact cost were rechecked.
Imported nominal ownership now reaches verified SSA; aggregate C11 lowering
and general proof-carrying backend admission remain open. The registry
observations and lifecycle evidence are in Commons.

The compiler-specific part of language pressure P1-014 is partially resolved:
the compiler-produced nested flow records now cross into retained SSA
execution successfully after the language codegen fix. The store host's
nominal record construction remains a separate unresolved boundary. The
existing host test transport is usable and kept this campaign's suite
bounded; replacing that transport with an MNCS-native harness remains an open
tooling pressure.

## Narrowest next parity step

Complete CP-0014's finite-match and enum-construction parse/proof/SSA path,
then extend C11 only as verified aggregate/nominal SSA permits. Replace the
test-only structural projection with a backend input tied to compiler
proof/provenance before treating it as authoritative. Do not infer broad
backend parity from the small scalar executable. The project-loading driver,
broader syntax/type coverage, and self-hosting remain separate later work.
