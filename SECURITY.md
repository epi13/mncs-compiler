# Security

`mncs-compiler` will eventually be a long-lived service capable of compiling untrusted or machine-generated code across multiple backends. Security boundaries therefore belong in the architecture from the beginning even when the first implementation is local and standalone.

## Trust boundaries

Potentially privileged or untrusted operations include:

- macros and compile-time execution,
- filesystem access,
- process spawning and external toolchains,
- dynamic libraries/plugins,
- JIT/native execution,
- PTX/CUDA execution,
- eBPF/kernel interaction,
- remote Fabric workers,
- generated artifact validation.

## Principle

Prefer explicit, narrow operation-level capabilities over ambient authority in a long-lived compiler process.

A compiler operation should eventually be able to state required capabilities such as filesystem read, declared-output write, process execution, GPU execution, or kernel interaction. Absence of a capability must not be silently bypassed by fallback code with broader authority.

## Correctness and adaptive systems

Learned models and historical observations are not trusted for semantic correctness. They may propose or rank work, but deterministic validation remains authoritative.

## Fault isolation

Do not isolate every compiler function into a process. Use process/sandbox boundaries where the trust or crash surface justifies the cost, especially around external tools, compile-time user code, GPU/runtime validation, and experimental backends.

Immutable/content-addressed shared compiler state is preferred because failed requests should not leave partially mutated semantic state visible to other sessions.

## Reporting

Until a dedicated private reporting channel is established, avoid publishing sensitive exploit details in public issues before maintainers have had a reasonable opportunity to assess them.