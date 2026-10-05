#!/usr/bin/env python3
"""Fail-closed backend-policy regression coverage.

Run directly (python3 tools/test_backend_policy.py) or under pytest.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backend_policy


def test_explicit_override_wins():
    assert backend_policy.resolve("decl", {"MNCS_PROBE_BACKEND": "research-bytecode"}) == "research-bytecode"
    assert backend_policy.resolve("flow", {"MNCS_PROBE_BACKEND": "cranelift"}) == "cranelift"
    assert backend_policy.resolve("unknown-suite", {"MNCS_PROBE_BACKEND": "cranelift"}) == "cranelift"
    assert backend_policy.resolve("decl", {"MNCS_PROBE_BACKEND": "reference_interpreter"}) == "reference_interpreter"


def test_classified_suites_resolve_table_backend():
    assert backend_policy.resolve("flow", {}) == "research-bytecode"
    assert backend_policy.resolve("decl", {}) == "cranelift"
    assert backend_policy.resolve("sem", {}) == "cranelift"
    assert backend_policy.resolve("project", {}) == "cranelift"


def test_unknown_suite_fails_closed():
    for suite in ("new-suite", "", "Decl", "FLOW"):
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
    for name, entry in backend_policy.SUITES.items():
        assert entry["class"] in ("A", "B", "C", "D"), name
        assert entry["backend"], name
        assert entry["reason"], name


def test_defaults_matches_suites():
    assert backend_policy.DEFAULTS == {
        name: entry["backend"] for name, entry in backend_policy.SUITES.items()
    }


if __name__ == "__main__":
    test_explicit_override_wins()
    test_classified_suites_resolve_table_backend()
    test_unknown_suite_fails_closed()
    test_unknown_suite_error_is_lookup_error()
    test_classification_is_abcd()
    test_defaults_matches_suites()
    print("backend_policy: 6 checks passed")
