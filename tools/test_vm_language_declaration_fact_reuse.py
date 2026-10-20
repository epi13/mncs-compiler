#!/usr/bin/env python3
"""Exercise Language declaration-fact reuse through application-build/8."""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_vm_native_application as app
import test_native_application_incremental as incremental

ROOT = app.ROOT
REQUEST_SCHEMA = "mncs.compiler.application-build-request/5"
RESPONSE_SCHEMA = "mncs.compiler.application-build/8"
CACHE_OBSERVATION_SCHEMA = "mncs.compiler.application-build-cache-observation/5"
VERIFICATION_WORK_SCHEMA = "mncs.compiler.application-build-verification-work/3"
FACT_SCHEMA = "mncs.language-declaration-verification-fact/1"
PROBE = Path(os.environ.get(
    "MNCS_NATIVE_APP_PROBE_BIN",
    str(ROOT / ".build/native-app-readiness/mncs-compiler-stage0-probe"),
)).expanduser().resolve()
VM_BIN = Path(os.environ.get(
    "MNCS_NATIVE_APP_VM_BIN",
    str(ROOT / ".build/native-app-readiness/mncs-vm"),
)).expanduser().resolve()
EVIDENCE = ROOT / "evidence" / (
    f"language-declaration-fact-reuse-"
    f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}.json"
)


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def build_request(root: str, modules: dict[str, str]) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA,
        "target": "mncs-vm-0.1",
        "root_source_id": app.APP_SOURCE_ID,
        "root_source": root,
        "callable_roots": [{"module": app.APP_MODULE, "function": "run"}],
        "test_selection": {"kind": "all_source_cases"},
        "modules": modules,
        "generic_seeds": [
            {"module": "mncs.test.assertions", "function": "equals_bytes",
             "type_arguments": [{"kind": "nat", "value": 4}]},
            {"module": "mncs.test.suite", "function": "summarize",
             "type_arguments": [{"kind": "nat", "value": 4}]},
        ],
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def clean_metrics(metrics: dict) -> dict:
    """Keep unavailable RSS/CPU observations null rather than reporting zero."""
    result = dict(metrics)
    for key in ("cpu_seconds", "peak_rss_bytes", "peak_cgroup_memory_bytes"):
        if result.get(key) in (0, 0.0):
            result[key] = None
    return result


def require_contract(label: str, response: dict) -> tuple[dict, dict]:
    if response.get("schema_version") != RESPONSE_SCHEMA:
        raise AssertionError(f"{label} returned the wrong response contract")
    cache = response.get("cache", {})
    work = cache.get("verification_work", {})
    if (cache.get("schema_version") != CACHE_OBSERVATION_SCHEMA
            or work.get("schema_version") != VERIFICATION_WORK_SCHEMA):
        raise AssertionError(f"{label} omitted declaration-fact work telemetry")
    if work.get("language_declaration_verification_fact_schema") != FACT_SCHEMA:
        raise AssertionError(f"{label} did not identify Language's declaration-fact contract")
    for field in ("reused_declaration_facts", "recomputed_declaration_facts",
                  "declaration_fact_recomputation_reasons"):
        if not isinstance(work.get(field), list):
            raise AssertionError(f"{label} omitted actual {field}")
    if type(work.get("produced_declaration_fact_count")) is not int:
        raise AssertionError(f"{label} omitted produced declaration-fact count")
    return cache, work


def cache_entry(cache_dir: Path, response: dict) -> dict:
    key = response.get("cache", {}).get("key")
    if not isinstance(key, str) or not key.startswith("sha256:"):
        raise AssertionError("application response omitted its exact cache identity")
    path = cache_dir / "application-builds" / f"{key.removeprefix('sha256:')}.json"
    entry = json.loads(path.read_text(encoding="utf-8"))
    if entry.get("schema_version") != "mncs.compiler.application-build-cache-entry/6":
        raise AssertionError("V6 application cache omitted Language verification facts")
    facts = entry.get("declaration_verification_facts")
    if not isinstance(facts, list) or not all(
            isinstance(row, dict) and row.get("schema_version") == FACT_SCHEMA
            for row in facts):
        raise AssertionError("cache entry contains malformed Language declaration facts")
    if not entry.get("declaration_verification_facts_sha256"):
        raise AssertionError("cache entry does not bind declaration-fact bytes")
    source_parse_cache = entry.get("source_parse_cache")
    if (not isinstance(source_parse_cache, dict)
            or source_parse_cache.get("schema_version") != "mncs.language-source-parse-cache/1"
            or not entry.get("source_parse_cache_sha256")):
        raise AssertionError("cache entry does not bind Language source parse results")
    return entry


def fact_locations(entry: dict) -> dict[str, str | None]:
    return {
        json.dumps(row.get("subject"), sort_keys=True): row.get("source_location_identity")
        for row in entry["declaration_verification_facts"]
    }


def measure_build(
    label: str,
    request: dict,
    cache_root: Path,
    *,
    compare_execution: bool,
) -> dict:
    probe = app.Probe(
        cache_root / "program-cache", executable=PROBE,
        application_cache_dir=cache_root / "application-cache",
    )
    try:
        ready, _ = probe.send({"execution_status": True})
        if not isinstance(ready, dict) or "modules" not in ready:
            raise AssertionError("Stage-0 probe did not return its readiness handshake")
        response, metrics = probe.send({"compile_application": request}, timeout=180.0)
        cache, work = require_contract(label, response)
        if compare_execution:
            oracle, execution, oracle_metrics = incremental.oracle_one(
                probe, request["root_source"], request["modules"]
            )
            artifact_value = response.get("artifact", {}).get("value")
            if not isinstance(artifact_value, dict):
                raise AssertionError(f"{label} omitted the canonical VM artifact value")
            vm_document, vm_metrics, _ = incremental.run_and_compare(artifact_value, execution)
        else:
            oracle = None
            oracle_metrics = None
            vm_document = None
            vm_metrics = None
    finally:
        probe.close()
    return {
        "response": response,
        "cache": cache,
        "verification_work": work,
        "build_metrics": clean_metrics(metrics),
        "oracle_valid": oracle.get("valid") if oracle is not None else None,
        "oracle_metrics": clean_metrics(oracle_metrics) if oracle_metrics is not None else None,
        "vm_receipt": vm_document.get("record") if vm_document is not None else None,
        "vm_metrics": clean_metrics(vm_metrics) if vm_metrics is not None else None,
    }


def compact(stage: dict) -> dict:
    response = stage["response"]
    work = stage["verification_work"]
    return {
        "response_schema": response.get("schema_version"),
        "cache_schema": stage["cache"].get("schema_version"),
        "cache_status": stage["cache"].get("status"),
        "frontend_invocations": work.get("front_end_invocations"),
        "reused_declaration_fact_count": len(work["reused_declaration_facts"]),
        "recomputed_declaration_fact_count": len(work["recomputed_declaration_facts"]),
        "produced_declaration_fact_count": work["produced_declaration_fact_count"],
        "reused_function_body_fact_count": len(work.get("reused_semantic_facts", [])),
        "recomputed_function_body_fact_count": len(work.get("recomputed_semantic_facts", [])),
        "artifact_id": response.get("artifact", {}).get("identity"),
        "build_metrics": stage["build_metrics"],
        "oracle_valid": stage["oracle_valid"],
        "vm_metrics": stage["vm_metrics"],
        "vm_artifact_id": (stage["vm_receipt"] or {}).get("artifact_id"),
    }


def main() -> None:
    if not PROBE.is_file() or not VM_BIN.is_file():
        raise SystemExit(f"Stage-0 probe or canonical VM is missing: {PROBE}, {VM_BIN}")
    app.VM_BIN = VM_BIN
    modules = {name: path.read_text(encoding="utf-8")
               for name, path in app.MODULE_FILES.items()}
    original = build_request(app.APP_SOURCE, modules)
    stages: dict[str, dict] = {}
    execution_checks: list[dict] = []

    with tempfile.TemporaryDirectory(prefix=".language-declaration-fact-reuse-", dir=ROOT) as raw:
        cache_root = Path(raw)
        artifact_path = cache_root / "vm-artifact.json"
        app.ARTIFACT_PATH = artifact_path

        cold = measure_build("cold", original, cache_root / "cold", compare_execution=True)
        stages["cold"] = cold
        cold_work = cold["verification_work"]
        if cold["cache"].get("status") != "miss" or cold_work.get("front_end_invocations") != 1:
            raise AssertionError("cold build did not run one verified Language frontend")
        if cold_work["produced_declaration_fact_count"] < 1:
            raise AssertionError("cold build did not produce Language declaration facts")
        if cold_work["reused_declaration_facts"]:
            raise AssertionError("cold build reported declaration facts reused without prior evidence")
        execution_checks.append({"stage": "cold", "stage0_vm_equal": True})
        application_cache = cache_root / "cold" / "application-cache"
        entry_before = cache_entry(application_cache, cold["response"])

        unchanged = measure_build(
            "fresh_process_unchanged", original, cache_root / "cold", compare_execution=False
        )
        stages["fresh_process_unchanged"] = unchanged
        if (unchanged["cache"].get("status") != "hit"
                or unchanged["verification_work"].get("front_end_invocations") != 0
                or unchanged["verification_work"]["reused_declaration_facts"]):
            raise AssertionError("exact artifact hit inaccurately reported Language fact reuse")

        comment_request = copy.deepcopy(original)
        comment_request["root_source"] = "// location refresh across a comment-only edit\n" + app.APP_SOURCE
        comment = measure_build(
            "comment_edit", comment_request, cache_root / "cold", compare_execution=True
        )
        stages["comment_edit"] = comment
        comment_work = comment["verification_work"]
        if comment_work.get("front_end_invocations") != 1:
            raise AssertionError("comment edit must run the current frontend")
        if not comment_work["reused_declaration_facts"]:
            raise AssertionError("comment edit did not reuse any Language declaration facts")
        if comment_work["recomputed_declaration_facts"]:
            raise AssertionError("comment edit unnecessarily recomputed a declaration fact")
        entry_after = cache_entry(application_cache, comment["response"])
        before_locations = fact_locations(entry_before)
        after_locations = fact_locations(entry_after)
        shared_subjects = set(before_locations) & set(after_locations)
        refreshed_locations = [subject for subject in shared_subjects
                               if before_locations[subject] != after_locations[subject]]
        if not refreshed_locations:
            raise AssertionError("comment edit reused declarations without refreshing source locations")
        if (comment["response"].get("artifact", {}).get("identity")
                != cold["response"].get("artifact", {}).get("identity")):
            raise AssertionError("comment-only edit changed the executable VM artifact")
        execution_checks.append({
            "stage": "comment_edit", "stage0_vm_equal": True,
            "refreshed_source_location_count": len(refreshed_locations),
        })

        body_request = copy.deepcopy(comment_request)
        generative = body_request["modules"]["mncs.test.generative"]
        old_body = "fn shrink(value: u64) -> (result: u64) {\n    return value / 2;\n}"
        new_body = "fn shrink(value: u64) -> (result: u64) {\n    return value / 3;\n}"
        if old_body not in generative:
            raise AssertionError("the selected real Test fixture no longer has the expected uncalled shrink body")
        body_request["modules"]["mncs.test.generative"] = generative.replace(old_body, new_body, 1)
        body_edit = measure_build(
            "uncalled_body_edit", body_request, cache_root / "cold", compare_execution=True
        )
        stages["uncalled_body_edit"] = body_edit
        body_work = body_edit["verification_work"]
        if not any("shrink" in str(row) for row in body_work.get("recomputed_semantic_facts", [])):
            raise AssertionError("uncalled shrink body edit did not recompute its Language body fact")
        if not body_work["reused_declaration_facts"] or body_work["recomputed_declaration_facts"]:
            raise AssertionError("body-only edit did not retain unchanged declaration facts")
        if (body_edit["response"].get("artifact", {}).get("identity")
                != cold["response"].get("artifact", {}).get("identity")):
            raise AssertionError("uncalled body edit changed the selected executable artifact")
        execution_checks.append({"stage": "uncalled_body_edit", "stage0_vm_equal": True})

        reachable_request = copy.deepcopy(comment_request)
        generative = reachable_request["modules"]["mncs.test.generative"]
        old_advance = "return state *% 6364136223846793005 +% 1442695040888963407;"
        new_advance = "return state *% 6364136223846793006 +% 1442695040888963407;"
        if generative.count(old_advance) != 1:
            raise AssertionError("the selected Test fixture no longer has the expected advance_random body")
        reachable_request["modules"]["mncs.test.generative"] = generative.replace(
            old_advance, new_advance, 1
        )
        reachable_baseline = measure_build(
            "reachable_body_baseline", comment_request,
            cache_root / "reachable", compare_execution=True,
        )
        stages["reachable_body_baseline"] = reachable_baseline
        reachable_body_edit = measure_build(
            "reachable_imported_body_edit", reachable_request,
            cache_root / "reachable", compare_execution=True,
        )
        stages["reachable_imported_body_edit"] = reachable_body_edit
        reachable_work = reachable_body_edit["verification_work"]
        reachable_recomputed = [
            str(row) for row in reachable_work.get("recomputed_semantic_facts", [])
        ]
        reachable_reused = [
            str(row) for row in reachable_work.get("reused_semantic_facts", [])
        ]
        expected_recomputed = {
            "mncs:0.2:body:mncs.test.generative::advance_random",
            "mncs:0.2:body:mncs.test.generative::draw_case",
        }
        if set(reachable_recomputed) != expected_recomputed:
            raise AssertionError(
                "reachable imported body edit did not reverify exactly its changed "
                f"function and direct caller: {reachable_recomputed}"
            )
        baseline_facts = {
            str(row) for row in reachable_baseline["verification_work"].get(
                "recomputed_semantic_facts", []
            )
        }
        if set(reachable_reused) != baseline_facts - expected_recomputed:
            raise AssertionError(
                "reachable imported body edit did not reuse every independent body "
                f"fact: reused={reachable_reused}, baseline={sorted(baseline_facts)}"
            )
        if not any("mncs.test.generative::shrink" in row for row in reachable_reused):
            raise AssertionError("reachable imported body edit failed to reuse independent shrink fact")
        if (not reachable_work.get("reused_declaration_facts")
                or reachable_work.get("recomputed_declaration_facts")):
            raise AssertionError("reachable body edit did not reuse unchanged declaration facts")
        if (reachable_body_edit["response"].get("artifact", {}).get("identity")
                == reachable_baseline["response"].get("artifact", {}).get("identity")):
            raise AssertionError("reachable imported body edit reused the prior executable artifact")
        execution_checks.append({
            "stage": "reachable_imported_body_edit", "stage0_vm_equal": True,
        })

        interface_request = copy.deepcopy(body_request)
        generative = interface_request["modules"]["mncs.test.generative"]
        old_signature = "fn shrink(value: u64) -> (result: u64)"
        new_signature = "fn shrink(value: i64) -> (result: i64)"
        if old_signature not in generative:
            raise AssertionError("selected uncalled shrink declaration identity was not found")
        interface_request["modules"]["mncs.test.generative"] = generative.replace(
            old_signature, new_signature, 1
        )
        interface = measure_build(
            "uncalled_imported_interface_edit", interface_request,
            cache_root / "cold", compare_execution=True,
        )
        stages["uncalled_imported_interface_edit"] = interface
        interface_work = interface["verification_work"]
        if not interface_work["reused_declaration_facts"]:
            raise AssertionError("imported interface edit did not preserve independent declaration facts")
        if not any("shrink" in str(row) for row in interface_work["recomputed_declaration_facts"]):
            raise AssertionError("changed imported declaration was not reverified")
        execution_checks.append({
            "stage": "uncalled_imported_interface_edit", "stage0_vm_equal": True,
        })

        language_lock = json.loads((ROOT / "mncs-language.lock.json").read_text(encoding="utf-8"))
        evidence = {
            "schema_version": "mncs.compiler.language-declaration-fact-reuse-evidence/1",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "compiler": {
                "repository": "mncs-compiler",
                "base_revision": git("rev-parse", "HEAD"),
                "worktree": str(ROOT),
            },
            "language": {
                "repository": "mncs-language",
                "revision": language_lock.get("revision"),
                "fact_schema": FACT_SCHEMA,
            },
            "workload": {
                "repository": "mncs-test",
                "source_modules": sorted(modules),
                "root_module": app.APP_MODULE,
                "callable_roots": [{"module": app.APP_MODULE, "function": "run"}],
                "selected_tests": "all_source_cases",
                "cache_entry_schema": "mncs.compiler.application-build-cache-entry/6",
            },
            "stages": {name: compact(stage) for name, stage in stages.items()},
            "execution_checks": execution_checks,
            "source_location_refresh": {
                "status": "verified",
                "shared_subject_count": len(shared_subjects),
                "refreshed_subject_count": len(refreshed_locations),
            },
            "declaration_fact_reuse": {
                "comment_edit_reused": len(comment_work["reused_declaration_facts"]),
                "comment_edit_recomputed": len(comment_work["recomputed_declaration_facts"]),
                "uncalled_body_edit_reused": len(body_work["reused_declaration_facts"]),
                "uncalled_body_edit_recomputed": len(body_work["recomputed_declaration_facts"]),
                "reachable_body_edit_reused": len(
                    reachable_work.get("reused_declaration_facts", [])),
                "reachable_body_edit_recomputed": len(
                    reachable_work.get("recomputed_declaration_facts", [])),
                "imported_interface_edit_reused": len(interface_work["reused_declaration_facts"]),
                "imported_interface_edit_recomputed": len(interface_work["recomputed_declaration_facts"]),
                "imported_interface_edit_artifact_identity_changed": (
                    interface["response"].get("artifact", {}).get("identity")
                    != cold["response"].get("artifact", {}).get("identity")
                ),
            },
            "reachable_imported_body_dependency_invalidation": {
                "changed_module": "mncs.test.generative",
                "changed_function": "advance_random",
                "reused_semantic_facts": reachable_work.get("reused_semantic_facts", []),
                "recomputed_semantic_facts": reachable_work.get("recomputed_semantic_facts", []),
                "declaration_fact_reuse": {
                    "reused": reachable_work.get("reused_declaration_facts", []),
                    "recomputed": reachable_work.get("recomputed_declaration_facts", []),
                },
                "artifact_identity_changed": (
                    reachable_body_edit["response"].get("artifact", {}).get("identity")
                    != reachable_baseline["response"].get("artifact", {}).get("identity")
                ),
                "stage0_vm_equal": True,
            },
            "artifact_equivalence": "stage0-oracle equals canonical VM for every changed snapshot",
        }
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps({"status": "passed", "evidence": str(EVIDENCE), **evidence},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
