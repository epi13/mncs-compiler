"""Helpers for deriving differential parity aggregates and ambient diagnosis summaries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def summarize_differential_cases(
    expected_case_names: Sequence[str],
    cases: Mapping[str, Mapping[str, Any]] | Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Derive a trustworthy aggregate verdict from expected case names and per-case outcomes.

    Missing, duplicated, or failed (`matches_stage0 is not True`) cases are never
    treated as passing.
    """
    expected_list = list(expected_case_names)
    expected_set = set(expected_list)
    seen_counts: dict[str, int] = {}
    case_map: dict[str, Mapping[str, Any]] = {}

    if isinstance(cases, Mapping):
        for name, entry in cases.items():
            key = str(name)
            seen_counts[key] = seen_counts.get(key, 0) + 1
            if isinstance(entry, Mapping):
                case_map[key] = entry
            else:
                case_map[key] = {"matches_stage0": False}
    else:
        for entry in cases:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str):
                seen_counts["<unnamed>"] = seen_counts.get("<unnamed>", 0) + 1
                case_map["<unnamed>"] = {"matches_stage0": False}
                continue
            key = entry["name"]
            seen_counts[key] = seen_counts.get(key, 0) + 1
            case_map[key] = entry

    duplicate_cases = [name for name, count in seen_counts.items() if count > 1]
    missing_cases = [name for name in expected_list if name not in case_map]
    unexpected_cases = [name for name in seen_counts if name not in expected_set]

    passed_cases: list[str] = []
    failed_cases: list[str] = []
    for name in expected_list:
        if name not in case_map:
            continue
        if case_map[name].get("matches_stage0") is True:
            passed_cases.append(name)
        else:
            failed_cases.append(name)

    all_ok = (
        len(expected_list) > 0
        and not missing_cases
        and not duplicate_cases
        and not failed_cases
        and len(passed_cases) == len(expected_list)
    )
    if all_ok:
        status = "passed"
    elif failed_cases or duplicate_cases:
        status = "failed"
    else:
        status = "incomplete"

    return {
        "status": status,
        "all_expected_match_stage0": all_ok,
        "expected_count": len(expected_list),
        "observed_count": sum(seen_counts.values()),
        "passed_count": len(passed_cases),
        "passed_cases": passed_cases,
        "failed_cases": failed_cases,
        "missing_cases": missing_cases,
        "duplicate_cases": duplicate_cases,
        "unexpected_cases": unexpected_cases,
    }


def extract_ambient_compiler_investigation(
    env_entry: Mapping[str, Any],
    *,
    strict: bool = False,
) -> dict[str, Any]:
    """Extract the compiler_investigation capsule delivered by `mncs-env enter`.

    Environment projects the selected-callable diagnosis under
    `env_entry["diagnostic"]["compiler_investigation"]`.
    """
    diag_block = env_entry.get("diagnostic")
    ci = None
    source_path = None
    if isinstance(diag_block, Mapping) and isinstance(diag_block.get("compiler_investigation"), Mapping):
        ci = diag_block["compiler_investigation"]
        source_path = "diagnostic.compiler_investigation"
    elif isinstance(env_entry.get("compiler_investigation"), Mapping):
        ci = env_entry["compiler_investigation"]
        source_path = "compiler_investigation"

    if ci is None:
        if strict:
            raise ValueError("Environment entry payload did not include diagnostic.compiler_investigation")
        return {
            "present": False,
            "source_path": None,
            "status": None,
            "freshness": None,
            "reused": None,
            "delivery_completeness": None,
            "target": None,
            "revision": None,
            "specialization": None,
            "last_completed_phase": None,
            "failing_phase": None,
            "artifact_id": None,
            "measurements": None,
            "raw": None,
        }

    prov = ci.get("provenance") if isinstance(ci.get("provenance"), Mapping) else {}
    return {
        "present": True,
        "source_path": source_path,
        "status": ci.get("status"),
        "freshness": ci.get("freshness"),
        "reused": ci.get("reused"),
        "delivery_completeness": ci.get("delivery_completeness"),
        "target": prov.get("target"),
        "revision": prov.get("revision"),
        "specialization": prov.get("specialization"),
        "last_completed_phase": prov.get("last_completed_phase"),
        "failing_phase": prov.get("failing_phase"),
        "artifact_id": prov.get("artifact_id"),
        "measurements": ci.get("measurements"),
        "raw": dict(ci),
    }
