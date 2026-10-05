# Canonical VM path cleanup — 2026-10-05

Session `ses_b3e2482c01aec62b` is Store-backed and fresh, entered through
`mncs-environment`. Explicit compiler/VM claims had no conflicts. Observed
clean baseline mains were compiler `03e0caa`, VM `a3ab04c`; remote fetch
confirmed both. Stage-0 remains `a3ac17df69e68f6373cbff336db0a572667d73da`.
Foreign worktrees and language/memory repositories were preserved. This is
artifact/runtime substrate work, with no new compiler syntax and no
CP-0024/CP-0025 implementation.

## Physical artifact accounting

The full decl suite's original artifact is **100,695,968 bytes** with its
Python JSON transport formatting, or **97,865,397 compact bytes**. An older
`/tmp/direct-decl.json` is 122.5 MB and has 849 functions: it is not this
suite's baseline. Fresh emission has 502 functions, 5,328 blocks and 14,251
instructions. This distinction prevents comparing different compiler graphs.

| Original compact decl section | Bytes |
| --- | ---: |
| Semantic binding table | 47,518,681 |
| SSA functions, including instructions and types | 24,505,432 |
| Transformation/evidence records | 12,775,183 |
| Trace/source correlation | 6,389,964 |
| Obligations | 6,293,953 |
| Record types and generic specialization facts | 221,787 |
| Outer callables/exports/generic routes/requirements/provenance/refusals and punctuation | about 160,000 |

The binding table itself contains about 39.2 MB of source references, 7.8 MB
of bindings and 0.5 MB of scopes. Repeated strings and object field names
account for **68,003,934 redundant encoded string bytes**: 2,334,253 string
occurrences but 175,789 unique strings. The size is predominantly repeated
identity/correlation/evidence data, not executable instructions alone.
Inspection of the concrete SSA/provenance types and full section accounting
found no embedded `Program`, HIR body, compiler session, provider environment,
or cache object. The artifact was frozen selected SSA plus its useful evidence,
but its serialization duplicated much of that immutable information.

The shared-node encoding interns identical strings, objects, arrays and type/
metadata structures. All original SSA fields and outer contract facts compare
exactly equal after decoding. No provenance, type, bound, callable, specialization
or debug-correlation fact was discarded. New artifact IDs are deterministic
content identities of the new encoding; callable/SSA identities are preserved.
No external table or compression dependency is required. Old plain-SSA encoded
VM files must be regenerated; the one canonical loader now requires the DAG.

| Suite | Original suite JSON | New suite JSON | Compact before → after |
| --- | ---: | ---: | ---: |
| segment | 8,509,421 | 4,363,080 | 8,239,851 → 4,039,143 |
| decl | 100,695,968 | 50,715,964 | 97,865,397 → 47,239,943 |

New decl has 502,316 shared nodes. Physical node payloads are strings 25.7 MB,
object index pairs 17.7 MB, array references 2.1 MB and numeric nodes 1.1 MB,
plus enclosing punctuation and small outer sections. This is a material
lossless reduction, not a claim that the remaining graph is minimal or
irreducible. Compiler-selected evidence and functions are deliberately retained.

## Measured phases, repeated work and limitations

Measurements use the same debug VM profile before/after, real suite calls,
and identical finite execution budgets. The instrumented example counts
allocations/reallocations and requested allocation bytes. It drops encoded
file bytes before admission and drops each result
after digesting it; CLI batch retains all results, so RSS differs. Other bounded
validation workloads ran on this machine: wall-time ranges are observations,
not a statistically isolated claim about CPU speed. Exact allocation counts,
section bytes, commands' phase outputs and repeated return digests are in
[the measurements](vm-efficiency-measurements.json).

| Phase | Segment before → after | Decl before → after |
| --- | --- | --- |
| Compiler emission, warm frontend cache | 2.16–2.17 s → 2.19 s | 35.0–35.9 s → 37.3–37.7 s |
| File read | about 0.003 s → 0.002 s | 0.036 s → 0.018 s |
| Typed decode/load, including DAG validation | 0.114 s → 0.148 s | 1.274 s → 1.592 s |
| Admission, identity validation and immutable indexes | 0.638 s → 0.522 s | 7.416 s → 6.013 s |
| Session construction | about 2 μs → 2 μs | about 3 μs → 3 μs |
| Instrumented execution, two runs | 6.83–7.50 s → 5.70 s | 25.9–26.4 s → 28.4–28.7 s |
| Full CLI batch | 10.99 s → 9.24 s | 34.94–36.94 s → 34.26–34.29 s |
| CLI peak RSS, KiB | 420,088 → 415,284 | 471,504–471,748 → 282,052–282,084 |
| Instrumented runner peak RSS, KiB | 170,120 → 171,320 | 367,848 → 274,408 |
| Per-batch execution allocations | 14,222,378 → 9,807,223 | 48,798,114 → 43,648,384 |

Emission and decode are slightly slower; the DAG encoder adds work while
upstream selected-SSA compilation still dominates emission. Warm probe-cache
hits reuse elaboration/native backend artifacts, not sealed direct VM emission:
repeated emit requests still compile selected SSA. No new artifact cache was
added, and this remaining cost is not hidden. Admission still hashes all facts;
its lower cost is not reduced validation. Instrumented decl execution did not
show a wall-time improvement, despite less allocation churn. The reliable decl
gains are roughly halved artifact bytes, about 40% lower CLI RSS, fewer allocations,
and modestly lower complete batch wall time. Most VM time remains execution,
not session construction; no claim is made about beating cached native code.

Batch already decoded/admitted/opened one session per process. The avoidable
work was **inside each call**: `Engine::new` rebuilt identity/function/block
indexes. Admission now builds those once. It also computes nested-loop reset
relationships once. Execution borrows block identity strings, resets nested
counters through those indexes, and allocates iteration-counter keys only on
first insertion. Mutable frames, counters, effects and usage remain fresh per
call. Compiler emission no longer clones the full selected SSA, freezes the
shared graph once for sealing/transport, and VM identity validation serializes
borrowed fields instead of cloning the whole artifact. Final CLI loading uses the
same decoder/admission functions but releases its owned encoded buffer after
decode, before identity validation. This removes another ~47 MB of peak
retention for decl: CLI RSS is now ~282,000 KiB rather than the intermediate
~328,000 KiB. The initial full three-backend proof records the earlier loader;
final full VM batches and a Stage-1 VM repeat preserve its exact return digests
and metering. Final complete batch wall is ~34.3 s; the small wall advantage
should be treated conservatively.

Every before/after batch summary and per-call return digest agrees exactly.
Decl still uses 17,919,512 VM steps total, 6,428,040 maximum per call, live-set
peak 28,871 cells, depth 21, zero effects. No instruction/iteration charging or
memory limits were raised. Nested activation, memory live-set, debug identity,
structured exhaustion and test-isolation regressions remain covered.

## Contracts and migration

`migrate.rs`, `ResearchPayload`, and `compile_to_backend*` are deleted from
`mncs-vm`, with **zero remaining callers**. The five view/generic/differential/
source/CLI call sites, corpus harness and dump example use the **same** compiler-
owned `tools/vm_emit.rs` as the pinned probe. Oracle comparisons still execute
upstream `execute_ssa`; no legacy artifact conversion consumer justified keeping
a compatibility test. This does not retire upstream research-bytecode users or
promote a new upstream backend registry entry.

Before, decl's driver manually supplied another 8M-step VM envelope. Now the
suite defines its request fuel once and every executor receives that request.
VM batch rows contain `{id, request: ExecutionRequest}`. `CallSpec::from_request`
propagates request step/depth budgets, supplies finite defaults for other
resources, permits explicit envelope tightening, and retains artifact-declared
bounds as independent tighter limits. Records report effective limits and
measured VM usage separately. VM steps are instructions/terminator edges,
not universal CPU cost. Missing fuel fails decoding; zero exhausts; unknown or
duplicate override dimensions fail; grants/policies require explicit bindings.

## Validation and final pressure

- 64 VM tests pass: admission, capability mediation, CLI, source/generic/corpus
  differentials, debug, nested iteration, structured budgets and live memory.
- Four codec tests pass: lossless numeric/Unicode/struct roundtrip, canonical
  ordering, malformed references/keys/unreachable nodes and exponential refusal.
- Eight probe-cache tests pass, including corrupt/legacy refusal, source/seeds/
  backend changes, lock/provisioned-tree/toolchain mutation and repin-safe misses.
- Seven backend-policy checks pass, including unclassified-suite fail-closed
  behavior and explicit override. Direct-emission oracle differential passes.
- Full segment 6,564/6,564 and decl 23/23 pass independent suite expectations
  on reference, VM and Cranelift; semantic digests agree. See the
  [segment](vm-efficiency-segment-comparison.json) and
  [decl](vm-efficiency-decl-comparison.json) reports. Decl reference execution
  took 1,094.2 s, VM batch 33.7 s, cached Cranelift path 20.0 s.
- Final smoke comparisons exercise the edited canonical request and temporary-
  directory drivers. TemporaryDirectory cleanup now covers success and failure.

The final [Stage-1 stress witness](vm-efficiency-stage1-frontier.json) feeds all
5,950 bytes of `src/compiler/parser.mncs` through `decl.parse_unit<256,1024>`.
Reference, VM and Cranelift agree exactly: the VM completes in 3,853,232 steps
within the automatically propagated 8M request and returns the precise Stage-1
frontier at bytes **710–711**, line **14**, the `{` in `Header { code: 0, ... }`.
No new VM envelope blocker appeared. The narrowest next compiler work is
**record literal/constructor parsing at that expression**, not a runtime rewrite.
No syntax implementation was attempted. The reference stress took 243.2 s;
VM batch 17.2 s includes decode/admission; cached native path 20.7 s.

No CP-0024/CP-0025 observation has changed: native decl/segment canaries remain;
project JIT and portable-WASM stack work were not attempted.

## Disk, cache and continuation

Probe cache v2 stayed at seven files, **45,453,172 bytes**. Its newest file
predates this session; warm frontend/native probes reused it without new cache
writes. Existing toolchain/pin identity and fail-closed invalidation remain.
No second persistent cache or test-space allocation increase was introduced.
Benchmark scratch reached about 254 MiB; transport temporaries are now scoped
and cleaned on failure as well as success. Disposable build/scratch files
created by this run are removed after evidence capture; existing caches and
foreign temporaries/build outputs are preserved. Final small measurement and
comparison reports are checked in; local final artifacts/request streams are
bounded replay material under `.build/vm-efficiency` (95,812,483 bytes including logs).
About 0.93 GiB of newly created disposable probe/codec build roots and 109 MB
of this run's transport temporaries were removed; existing target roots were
not cleaned. The existing VM target is observed at 9.2 GiB; its total growth
was not separately baselined, so no precise overall-build-disk claim is made.

The next session should use the cleaned direct path for a narrow record-literal
compiler run, retain native canaries, and keep CP-0024/CP-0025 separate. The
Environment checkpoint carries exact delivered heads and claim cleanup state;
main branches are merged and pushed under the workspace delivery instruction.

