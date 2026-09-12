# Pressure Finding CP-0013

ID: CP-0013

Status: resolved (2026-09-12)

Category: language

Severity: low

Frequency: rare

## `next` was unusable as a record field name (resolved)

Upstream: profile 0.13 contextual `next` fields. Proven in real
compiler code: `StmtFrame.after` renamed back to `StmtFrame.next`
(`record`, two constructions, one projection) in the 0.13 migration;
parse-checked with elaboration proof staged after CP-0014. The 0.10
`repro/keyword-field-next.mncs` still fails exactly as documented
(MNP127/128/007), so old-profile behavior is preserved. History
preserved below.

`next` is the iterate-step keyword, and the parser reserves it in field
position: `record R { next: u64 }` fails with MNP127 (`expected '}' after
record fields`) at the `next` token, followed by MNP128/MNP007 cascades.
Renaming the field (e.g. to `after`) elaborates cleanly, so the only cost
is the rename — but a compiler that models control flow (frames, work
lists, resumable walks) naturally reaches for `next`/`after` vocabulary,
and only one of the two survives parsing.

## Compiler workload

The self-hosted statement prover threads suspended statement lists
through `StmtFrame`; the first field name tried was `next`, which the
self-hosted `decl.mncs` itself could not contain — the failure appeared
while elaborating the compiler, not a test. Minimized repro:
`pressure/repro/keyword-field-next.mncs`.

## Reproduce

```sh
MNCS_LIBRARY_PATH=src .bootstrap/target/debug/mncs abi pressure/repro/keyword-field-next.mncs
```

## Observed behavior

Diagnostics on the 0.10 profile Stage-0 (`mncs-language.lock.json`
revision `6906d0b1eee7`):

- MNP127 32 36 `expected '}' after record fields`
- MNP128 32 36 `record requires at least one field`
- MNP007 32 36 `unexpected token after the final function`

## Workaround

Rename the field. No semantic consequence; recorded because keyword
reservation in nominal (non-expression) positions is invisible until a
real program trips over it.
