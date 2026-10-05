"""Backend selection policy for compiler test execution.

Classification (see evidence/BACKEND-NEUTRALITY-AUDIT.md):

- A (truly native): native ABI, calling convention, JIT layout,
  codegen behavior. Cranelift explicitly. No compiler suite is
  class A today; the backend canaries (C11 emission, native
  retention assertions) select their backend explicitly.
- B (needs an executor, not native code): semantic regression,
  compiler-helper execution, differentials, deterministic
  transformations. Cheapest retained backend covering the
  operations: research-bytecode today, canonical VM as the
  artifact route matures.
- C (backend-independent): oracle/parse transports and bare CLI
  invocations. No backend session required.
- D (cross-backend conformance): deliberate matrices; semantic-only
  digests make cross-backend equality checkable.

An explicit MNCS_PROBE_BACKEND (including the reference_interpreter
pseudo-name) always wins. The table below only sets the default when
the caller expresses no choice.
"""

import os

# Suite name -> default backend. B-class suites migrate here as each
# is verified on the cheaper backend; the row documents the proof.
DEFAULTS = {
    # B-class, verified: Cranelift retention is broken (CP-0024:
    # contained worker panic, retained 0), bytecode passes with
    # identical semantic digests.
    "flow": "research-bytecode",
    # B-class, verified on bytecode with an identical semantic
    # digest to the Cranelift run; Cranelift retained as the native
    # canary until the execution-cost picture favors a flip.
    "decl": "cranelift",
    # B-class but execution-dominated: 198 proof requests take ~11
    # min under interpretation vs seconds native (16:37 bytecode vs
    # 4:43 Cranelift), so this suite stays native. Revisit if the
    # interpreter gets faster or the requests get cheaper.
    "sem": "cranelift",
    "frontend": "cranelift",
    "segment": "cranelift",
    "cp0001": "cranelift",
    "profile-surface": "cranelift",
    # B-class but budget-bound: compile_project cannot fit the
    # pin-maximum 8M interpreted steps (native reports 1/request),
    # so the project family (test_project and test_imported_nominal,
    # which shares its Probe) stays native until CP-0024 is fixed or
    # the workload is chunked. Retention assertions are the canary.
    "project": "cranelift",
}


def resolve(suite, env=None):
    """Return the backend name for a suite.

    Explicit MNCS_PROBE_BACKEND wins (including
    'reference_interpreter', which harnesses implement by unsetting
    the variable for the probe). Otherwise the policy-table default.
    Unknown suites fall back to cranelift (historical default).
    """
    env = os.environ if env is None else env
    explicit = env.get("MNCS_PROBE_BACKEND")
    if explicit:
        return explicit
    return DEFAULTS.get(suite, "cranelift")
