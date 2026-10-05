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

The policy is fail-closed: a suite with no row here raises
UnknownSuiteError instead of silently becoming Cranelift-backed. A
new executable suite must answer what it is proving (A/B/C/D) and
earn its row; a one-off run can still set MNCS_PROBE_BACKEND
explicitly.
"""

import os


class UnknownSuiteError(LookupError):
    """A suite has no backend classification and the caller expressed
    no explicit backend choice."""


# Suite name -> classification row. B-class suites migrate their
# backend entry as each is verified on the cheaper backend; the row
# documents the proof.
SUITES = {
    # B-class, verified: Cranelift retention is broken (CP-0024:
    # contained worker panic, retained 0), bytecode passes with
    # identical semantic digests.
    "flow": {
        "class": "B",
        "backend": "research-bytecode",
        "reason": "CP-0024 breaks Cranelift retention; bytecode passes with identical digests",
    },
    # B-class, verified on bytecode with an identical semantic
    # digest to the Cranelift run; Cranelift retained as the native
    # canary until the execution-cost picture favors a flip.
    "decl": {
        "class": "B",
        "backend": "cranelift",
        "reason": "native canary; bytecode parity already verified",
    },
    # B-class but execution-dominated: 198 proof requests take ~11
    # min under interpretation vs seconds native (16:37 bytecode vs
    # 4:43 Cranelift), so this suite stays native. Revisit if the
    # interpreter gets faster or the requests get cheaper.
    "sem": {
        "class": "B",
        "backend": "cranelift",
        "reason": "execution-dominated; native is ~3.5x faster than interpretation",
    },
    # B-class: executes frontend helpers through the probe.
    "frontend": {
        "class": "B",
        "backend": "cranelift",
        "reason": "executor-backed; stays native until the VM route covers the workload",
    },
    # B-class: executes segment helpers through the probe. Also
    # covered on the canonical VM via tools/test_vm_segment.py
    # (direct bytes + batch); the Cranelift row stays as the native
    # anchor.
    "segment": {
        "class": "B",
        "backend": "cranelift",
        "reason": "executor-backed; native anchor, with VM coverage via test_vm_segment.py",
        "vm_driver": "tools/test_vm_segment.py",
    },
    # B-class: lexer differential; the Cranelift retained-session
    # assertions are a native canary.
    "cp0001": {
        "class": "B",
        "backend": "cranelift",
        "reason": "executor-backed; Cranelift retention assertions are a native canary",
    },
    # B-class: executes decl proof helpers through the probe.
    "profile-surface": {
        "class": "B",
        "backend": "cranelift",
        "reason": "executor-backed; stays native until the VM route covers the workload",
    },
    # B-class but budget-bound: compile_project cannot fit the
    # pin-maximum 8M interpreted steps (native reports 1/request),
    # so the project family (test_project and test_imported_nominal,
    # which shares its Probe) stays native until CP-0024 is fixed or
    # the workload is chunked. Retention assertions are the canary.
    "project": {
        "class": "B",
        "backend": "cranelift",
        "reason": "budget-bound: cannot fit the 8M interpreted-step budget; retention is the canary",
    },
}

# Backwards-compatible view: suite -> default backend.
DEFAULTS = {name: entry["backend"] for name, entry in SUITES.items()}


def suite_class(suite):
    """Return the A/B/C/D classification for a suite.

    Raises UnknownSuiteError for unclassified suites.
    """
    try:
        return SUITES[suite]["class"]
    except KeyError:
        raise UnknownSuiteError(
            f"suite {suite!r} has no backend classification (A/B/C/D); "
            "classify it in tools/backend_policy.py or set MNCS_PROBE_BACKEND explicitly"
        ) from None


def resolve(suite, env=None):
    """Return the backend name for a suite.

    Explicit MNCS_PROBE_BACKEND wins (including
    'reference_interpreter', which harnesses implement by unsetting
    the variable for the probe). Otherwise the policy-table default.
    Unknown suites raise UnknownSuiteError: a new suite must be
    classified, never silently default.
    """
    env = os.environ if env is None else env
    explicit = env.get("MNCS_PROBE_BACKEND")
    if explicit:
        return explicit
    try:
        return SUITES[suite]["backend"]
    except KeyError:
        raise UnknownSuiteError(
            f"suite {suite!r} has no backend classification (A/B/C/D); "
            "classify it in tools/backend_policy.py or set MNCS_PROBE_BACKEND explicitly"
        ) from None
