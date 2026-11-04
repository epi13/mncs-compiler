"""Host adapter for Compiler-owned MNCS backend selection policy.

The native module classifies executable suites and selects their default
backend. This file transports requests and projects its typed result into the
legacy Python API used by compiler test consumers.
"""
from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "compiler" / "backend_policy.mncs"
MODULE = "mncs.compiler.backend_policy.v1"
_STEP_BUDGET = "10000"


class UnknownSuiteError(LookupError):
    """A suite has no native backend classification and no explicit choice."""


# These strings explain the verified rows to maintainers; executable class
# and backend decisions live in backend_policy.mncs.
_SUITE_REASONS = {
    "flow": "CP-0024 breaks Cranelift retention; bytecode passes with identical digests",
    "decl": "native canary; bytecode parity already verified",
    "sem": "execution-dominated; native is ~3.5x faster than interpretation",
    "frontend": "executor-backed; stays native until the VM route covers the workload",
    "segment": "executor-backed; native anchor, with VM coverage via test_vm_segment.py",
    "cp0001": "executor-backed; Cranelift retention assertions are a native canary",
    "profile-surface": "executor-backed; stays native until the VM route covers the workload",
    "project": "budget-bound: cannot fit the 8M interpreted-step budget; retention is the canary",
}
_VM_DRIVERS = {"segment": "tools/test_vm_segment.py"}
_KNOWN_CLASSES = {"A", "B", "C", "D"}
_KNOWN_BACKENDS = {"research-bytecode", "cranelift"}


def _mncs_binary() -> Path:
    configured = os.environ.get("MNCS_POLICY_BIN")
    if configured:
        return Path(configured).expanduser().resolve(strict=True)

    candidates: list[Path] = []
    for ancestor in (ROOT, *ROOT.parents):
        candidates.extend((
            ancestor / ".bootstrap" / "target" / "release" / "mncs",
            ancestor / ".bootstrap" / "target" / "debug" / "mncs",
        ))
        if ancestor.name == "mncs-language":
            candidates.extend((
                ancestor / "target" / "release" / "mncs",
                ancestor / "target" / "debug" / "mncs",
            ))
        candidates.extend((
            ancestor / "mncs-language" / "target" / "release" / "mncs",
            ancestor / "mncs-language" / "target" / "debug" / "mncs",
        ))
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise RuntimeError(
        "MNCS backend policy needs the pinned Language CLI; set MNCS_POLICY_BIN "
        "or build mncs-compiler's locked Stage-0 toolchain"
    )


def _typed_bytes(value: bytes) -> dict:
    return {"sequence": {"values": [
        {"byte": {"value": byte}} for byte in value
    ]}}


def _record_fields(value: object) -> dict[str, object]:
    record = value.get("record") if isinstance(value, dict) else None
    fields = record.get("fields") if isinstance(record, dict) else None
    if not isinstance(fields, list):
        raise RuntimeError("MNCS backend policy returned a non-record value")
    result = {}
    for item in fields:
        if not isinstance(item, list) or len(item) != 2 or not isinstance(item[0], str):
            raise RuntimeError("MNCS backend policy returned a malformed record field")
        result[item[0]] = item[1]
    return result


def _boolean(value: object, field: str) -> bool:
    boolean = value.get("boolean") if isinstance(value, dict) else None
    result = boolean.get("value") if isinstance(boolean, dict) else None
    if not isinstance(result, bool):
        raise RuntimeError(f"MNCS backend policy returned an invalid {field}")
    return result


def _text(value: object, field: str) -> str:
    sequence = value.get("sequence") if isinstance(value, dict) else None
    values = sequence.get("values") if isinstance(sequence, dict) else None
    if not isinstance(values, list):
        raise RuntimeError(f"MNCS backend policy returned an invalid {field}")
    octets = []
    for item in values:
        byte = item.get("byte") if isinstance(item, dict) else None
        number = byte.get("value") if isinstance(byte, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or not 0 <= number <= 255:
            raise RuntimeError(f"MNCS backend policy returned an invalid {field} byte")
        octets.append(number)
    try:
        return bytes(octets).decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError(f"MNCS backend policy returned invalid UTF-8 in {field}") from error


def _call(function: str, arguments: list[object]) -> object:
    command = [
        str(_mncs_binary()), "call", str(SOURCE),
        "--module", MODULE,
        "--function", function,
        "--args-json", json.dumps(arguments, separators=(",", ":")),
        "--no-cache",
        "--step-budget", _STEP_BUDGET,
    ]
    environment = dict(os.environ)
    # The selected test backend is input to this policy call, not to the
    # compiler process which executes the policy itself.
    environment.pop("MNCS_PROBE_BACKEND", None)
    try:
        completed = subprocess.run(
            command, cwd=ROOT, env=environment, text=True,
            capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"MNCS backend policy execution failed: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-2000:]
        raise RuntimeError(f"MNCS backend policy execution failed: {detail}")
    try:
        response = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("MNCS backend policy returned non-JSON output") from error
    if not isinstance(response, dict):
        raise RuntimeError("MNCS backend policy returned a non-object response")
    if response.get("status") != "returned" or not isinstance(response.get("call"), dict):
        raise RuntimeError(f"MNCS backend policy did not return: {response!r}")
    returned = response["call"].get("returned")
    if not isinstance(returned, list) or len(returned) != 1:
        raise RuntimeError("MNCS backend policy returned the wrong value count")
    return returned[0]


def _policy(value: object) -> dict[str, str]:
    fields = _record_fields(value)
    suite = _text(fields.get("suite"), "suite")
    classification = _text(fields.get("classification"), "classification")
    backend = _text(fields.get("backend"), "backend")
    if not suite or classification not in _KNOWN_CLASSES or backend not in _KNOWN_BACKENDS:
        raise RuntimeError("MNCS backend policy returned an unknown catalogue row")
    return {"suite": suite, "class": classification, "backend": backend}


@lru_cache(maxsize=1)
def _catalogue() -> dict[str, dict[str, str]]:
    value = _call("catalogue", [])
    sequence = value.get("sequence") if isinstance(value, dict) else None
    rows = sequence.get("values") if isinstance(sequence, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("MNCS backend policy returned an invalid catalogue")
    result: dict[str, dict[str, str]] = {}
    for raw in rows:
        policy = _policy(raw)
        suite = policy["suite"]
        if suite in result or suite not in _SUITE_REASONS:
            raise RuntimeError(f"MNCS backend policy returned an unexpected suite {suite!r}")
        projected = {
            "class": policy["class"],
            "backend": policy["backend"],
            "reason": _SUITE_REASONS[suite],
        }
        if suite in _VM_DRIVERS:
            projected["vm_driver"] = _VM_DRIVERS[suite]
        result[suite] = projected
    if result.keys() != _SUITE_REASONS.keys():
        raise RuntimeError("MNCS backend policy catalogue is incomplete")
    return result


def _native_decision(suite: str, has_override: bool) -> dict[str, object]:
    # The native ABI accepts at most 64 suite-name bytes. Truncating only
    # names beyond that admission bound preserves their native "unknown"
    # result because every classified identifier is shorter than 64 bytes.
    suite_bytes = suite.encode("utf-8")[:64]
    value = _call("resolve", [
        _typed_bytes(suite_bytes),
        {"boolean": {"value": has_override}},
    ])
    fields = _record_fields(value)
    return {
        "known": _boolean(fields.get("known"), "known"),
        "overridden": _boolean(fields.get("overridden"), "overridden"),
        "classification": _text(fields.get("classification"), "classification"),
        "backend": _text(fields.get("backend"), "backend"),
    }


@lru_cache(maxsize=32)
def _cached_decision(suite: str, has_override: bool) -> dict[str, object]:
    return _native_decision(suite, has_override)


SUITES = _catalogue()
DEFAULTS = {name: row["backend"] for name, row in SUITES.items()}


def _unknown_suite(suite: str) -> UnknownSuiteError:
    return UnknownSuiteError(
        f"suite {suite!r} has no backend classification (A/B/C/D); "
        "classify it in src/compiler/backend_policy.mncs or set "
        "MNCS_PROBE_BACKEND explicitly"
    )


def suite_class(suite: str) -> str:
    """Return the native A/B/C/D classification; unknown suites fail closed."""
    decision = _cached_decision(suite, False)
    if not decision["known"]:
        raise _unknown_suite(suite)
    return str(decision["classification"])


def resolve(suite: str, env=None) -> str:
    """Return the native default, unless MNCS_PROBE_BACKEND is explicit."""
    env = os.environ if env is None else env
    explicit = env.get("MNCS_PROBE_BACKEND")
    decision = _cached_decision(suite, bool(explicit))
    if decision["overridden"]:
        return explicit
    if not decision["known"]:
        raise _unknown_suite(suite)
    return str(decision["backend"])
