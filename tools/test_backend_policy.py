#!/usr/bin/env python3
"""Fail-closed backend-policy regression coverage.

Run directly (python3 tools/test_backend_policy.py) or under pytest.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_policy


# Independent behavioral contract. These expectations deliberately do not
# import the native catalogue or its host projection.
EXPECTED_POLICY = {
    "flow": {
        "class": "B",
        "backend": "research-bytecode",
        "reason": "CP-0024 breaks Cranelift retention; bytecode passes with identical digests",
    },
    "decl": {
        "class": "B",
        "backend": "cranelift",
        "reason": "native canary; bytecode parity already verified",
    },
    "sem": {
        "class": "B",
        "backend": "cranelift",
        "reason": "execution-dominated; native is ~3.5x faster than interpretation",
    },
    "frontend": {
        "class": "B",
        "backend": "cranelift",
        "reason": "executor-backed; stays native until the VM route covers the workload",
    },
    "segment": {
        "class": "B",
        "backend": "cranelift",
        "reason": "executor-backed; native anchor, with VM coverage via test_vm_segment.py",
        "vm_driver": "tools/test_vm_segment.py",
    },
    "cp0001": {
        "class": "B",
        "backend": "cranelift",
        "reason": "executor-backed; Cranelift retention assertions are a native canary",
    },
    "profile-surface": {
        "class": "B",
        "backend": "cranelift",
        "reason": "executor-backed; stays native until the VM route covers the workload",
    },
    "project": {
        "class": "B",
        "backend": "cranelift",
        "reason": "budget-bound: cannot fit the 8M interpreted-step budget; retention is the canary",
    },
}


def test_explicit_override_wins():
    assert backend_policy.resolve("decl", {"MNCS_PROBE_BACKEND": "research-bytecode"}) == "research-bytecode"
    assert backend_policy.resolve("flow", {"MNCS_PROBE_BACKEND": "cranelift"}) == "cranelift"
    assert backend_policy.resolve("unknown-suite", {"MNCS_PROBE_BACKEND": "cranelift"}) == "cranelift"
    assert backend_policy.resolve("x" * 65, {"MNCS_PROBE_BACKEND": "cranelift"}) == "cranelift"
    assert backend_policy.resolve("decl", {"MNCS_PROBE_BACKEND": "reference_interpreter"}) == "reference_interpreter"


def test_classified_suites_resolve_table_backend():
    for suite, expected in EXPECTED_POLICY.items():
        assert backend_policy.resolve(suite, {}) == expected["backend"], suite
        assert backend_policy.suite_class(suite) == expected["class"], suite
        assert backend_policy.SUITES[suite] == expected, suite


def test_unknown_suite_fails_closed():
    for suite in ("new-suite", "", "Decl", "FLOW", "x" * 65, "é" * 40):
        try:
            backend_policy.resolve(suite, {})
        except backend_policy.UnknownSuiteError:
            pass
        else:
            raise AssertionError(f"unclassified suite {suite!r} silently resolved")
        try:
            backend_policy.suite_class(suite)
        except backend_policy.UnknownSuiteError:
            pass
        else:
            raise AssertionError(f"unclassified suite {suite!r} has a class")


def test_unknown_suite_error_is_lookup_error():
    assert issubclass(backend_policy.UnknownSuiteError, LookupError)


def test_classification_is_abcd():
    assert set(backend_policy.SUITES) == set(EXPECTED_POLICY)
    for name, entry in EXPECTED_POLICY.items():
        assert entry["class"] in ("A", "B", "C", "D"), name
        assert entry["backend"], name
        assert entry["reason"], name


def test_defaults_matches_suites():
    assert backend_policy.DEFAULTS == {
        name: entry["backend"] for name, entry in EXPECTED_POLICY.items()
    }


def test_vm_drivers_exist():
    root = Path(__file__).resolve().parents[1]
    for name, entry in backend_policy.SUITES.items():
        driver = entry.get("vm_driver")
        if driver is not None:
            assert (root / driver).exists(), f"{name}: {driver} missing"


if __name__ == "__main__":
    test_explicit_override_wins()
    test_classified_suites_resolve_table_backend()
    test_unknown_suite_fails_closed()
    test_unknown_suite_error_is_lookup_error()
    test_classification_is_abcd()
    test_defaults_matches_suites()
    test_vm_drivers_exist()
    print("backend_policy: 7 checks passed")
