#!/usr/bin/env python3
"""Exercise Language fact-seeded dependency summaries through application-build/15."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[1]
from ensure_locked_stage0_probe import ensure_locked_stage0_probe

LOCKED_PROBE = ensure_locked_stage0_probe(ROOT)
os.environ["MNCS_NATIVE_APP_PROBE_BIN"] = str(LOCKED_PROBE)
os.environ["MNCS_PROBE_BIN"] = str(LOCKED_PROBE)

import test_application_build_provider as provider_tests
import test_native_application_incremental as incremental
import test_vm_native_application as app

REQUEST_SCHEMA = "mncs.compiler.application-build-request/12"
RESPONSE_SCHEMA = "mncs.compiler.application-build/15"
WORK_SCHEMA = "mncs.compiler.application-build-verification-work/11"
FACT_SCHEMA = "mncs.language-verification-fact/4"
SUMMARY_SCHEMA = "mncs.language-verification-dependency-summary-cache/2"
SUMMARY_OBSERVATION_SCHEMA = "mncs.language-verification-dependency-summary-cache-observation/2"
SUMMARY_POLICY = "mncs-language-verification-dependency-extractor/1"
MODULE = "mncs.dependency_cache"
ROOT_SOURCE = """mncs 0.18;
module tests.native_readiness;

use mncs.dependency_cache as dependency_cache;

fn run() -> (result: u64) {
    return dependency_cache.value();
}

fn idle(value: u64) -> (result: u64) {
    return value + 1;
}
"""
MODULE_SOURCE = """mncs 0.18;
module mncs.dependency_cache;

fn value() -> (result: u64) {
    return 5;
}

fn imported_idle(value: u64) -> (result: u64) {
    return value + 2;
}
"""
EVIDENCE = ROOT / "evidence" / (
    "language-verification-dependency-summary-cache-"
    f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}.json"
)


def git(*args: str, cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def measured_metrics(value: dict) -> dict:
    result = dict(value)
    if result.get("peak_rss_bytes") == 0:
        result["peak_rss_bytes"] = None
    return result


def dependency_summary_subjects(stage: dict, field: str) -> list:
    work = stage.get("verification_work")
    if not isinstance(work, dict):
        return []
    cache = work.get("verification_dependency_summary_cache")
    if not isinstance(cache, dict):
        return []
    observation = cache.get("observation")
    if not isinstance(observation, dict):
        return []
    values = observation.get(field)
    return values if isinstance(values, list) else []


def request(root_source: str, module_source: str) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA,
        "target": "mncs-vm-0.1",
        "root_source_id": app.APP_SOURCE_ID,
        "root_source": root_source,
        "callable_roots": [{"module": app.APP_MODULE, "function": "run"}],
        "test_selection": {"kind": "all_source_cases"},
        "modules": {MODULE: module_source},
        "generic_seeds": [],
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def work_for(label: str, response: dict) -> tuple[dict, dict]:
    cache = response.get("cache", {})
    work = cache.get("verification_work", {})
    if response.get("schema_version") != RESPONSE_SCHEMA:
        raise AssertionError(f"{label}: unexpected response schema {response.get('schema_version')}")
    if work.get("schema_version") != WORK_SCHEMA:
        raise AssertionError(f"{label}: unexpected verification-work schema {work.get('schema_version')}")
    if work.get("language_verification_fact_schema") != FACT_SCHEMA:
        raise AssertionError(f"{label}: Compiler did not consume Language fact v4: {work}")
    summary = work.get("verification_dependency_summary_cache", {})
    if (summary.get("schema_version") != SUMMARY_SCHEMA
            or summary.get("observation_schema_version") != SUMMARY_OBSERVATION_SCHEMA
            or summary.get("policy_identity") != SUMMARY_POLICY):
        raise AssertionError(f"{label}: missing Language dependency-summary cache contract: {summary}")
    observation = summary.get("observation")
    if summary.get("status") != "observed" or not isinstance(observation, dict):
        raise AssertionError(f"{label}: Language cache did not report actual frontend work: {summary}")
    return work, observation


def subjects(rows: object) -> set[str]:
    if not isinstance(rows, list):
        raise AssertionError(f"expected a subject list, got {rows!r}")
    return {str(row) for row in rows}


def assert_units(label: str, work: dict, observation: dict,
                 *, reused_summary: set[str], recomputed_summary: set[str],
                 reused_facts: set[str], recomputed_facts: set[str],
                 fact_seeded_summary: set[str] | None = None) -> None:
    expected_fact_seeded = fact_seeded_summary or set()
    actual_fact_seeded = subjects(observation.get("fact_seeded_function_subjects"))
    actual_reused_summary = subjects(observation.get("reused_function_subjects"))
    actual_recomputed_summary = subjects(observation.get("recomputed_function_subjects"))
    actual_reused_facts = subjects(work.get("reused_semantic_facts"))
    actual_recomputed_facts = subjects(work.get("recomputed_semantic_facts"))
    if (actual_fact_seeded != expected_fact_seeded
            or actual_reused_summary != reused_summary
            or actual_recomputed_summary != recomputed_summary):
        raise AssertionError(
            f"{label}: incorrect dependency-summary units: "
            f"fact_seeded={sorted(actual_fact_seeded)}, "
            f"reused={sorted(actual_reused_summary)}, "
            f"recomputed={sorted(actual_recomputed_summary)}"
        )
    if actual_reused_facts != reused_facts or actual_recomputed_facts != recomputed_facts:
        raise AssertionError(
            f"{label}: incorrect verified body facts: "
            f"reused={sorted(actual_reused_facts)}, recomputed={sorted(actual_recomputed_facts)}"
        )


def call_request() -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": app.APP_MODULE, "function": "run"},
        "arguments": [],
        "type_arguments": [],
        "step_budget": 200_000,
    }


def execute_and_compare(probe: app.Probe, label: str, response: dict,
                        root_source: str, module_source: str,
                        expected: int) -> dict:
    artifact = response.get("artifact", {}).get("value")
    if not isinstance(artifact, dict):
        raise AssertionError(f"{label}: verified application artifact missing")
    oracle, execution, oracle_metrics = incremental.oracle_one(
        probe, root_source, {MODULE: module_source}, call=call_request()
    )
    if not oracle.get("valid"):
        raise AssertionError(f"{label}: Stage-0 oracle rejected source: {oracle.get('diagnostics')}")
    actual_path = app.ARTIFACT_PATH
    try:
        app.ARTIFACT_PATH = Path(tempfile.gettempdir()) / (
            f"mncs-dependency-summary-{os.getpid()}-{label}.vm.json"
        )
        vm, vm_metrics = app.run_vm(artifact, arguments=[])
    finally:
        app.ARTIFACT_PATH = actual_path
    if vm.get("outcome") != {"kind": "completed"}:
        raise AssertionError(f"{label}: canonical VM did not complete: {vm.get('outcome')}")
    if vm.get("record", {}).get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError(f"{label}: VM receipt does not bind the selected artifact")
    actual_values = [app.decode_wire(app.vm_to_wire(row))
                     for row in vm.get("record", {}).get("returned", [])]
    oracle_values = [app.decode_wire(row) for row in execution.get("returned", [])]
    if actual_values != oracle_values or actual_values != [expected]:
        raise AssertionError(
            f"{label}: canonical VM, Stage-0, and expected output disagree: "
            f"vm={actual_values}, stage0={oracle_values}, expected={[expected]}"
        )
    return {
        "expected_output": expected,
        "stage0_output": oracle_values,
        "canonical_vm_output": actual_values,
        "artifact_identity": response.get("artifact", {}).get("identity"),
        "oracle_wall_seconds": oracle_metrics.get("wall_seconds"),
        "vm_metrics": measured_metrics(vm_metrics),
    }


def assert_frontend(label: str, response: dict, expected: int) -> tuple[dict, dict]:
    if response.get("status") != "verified":
        raise AssertionError(f"{label}: application did not verify: {response.get('status')}")
    cache = response.get("cache", {})
    work = cache.get("verification_work", {})
    if work.get("front_end_invocations") != expected:
        raise AssertionError(
            f"{label}: expected {expected} Language frontend invocation(s), "
            f"got {work.get('front_end_invocations')}"
        )
    return work, cache


def main() -> None:
    build_info = provider_tests.ensure_current_probe()
    if not app.VM_BIN.is_file():
        raise SystemExit(f"canonical VM binary does not exist: {app.VM_BIN}")

    root_hash = git("rev-parse", "HEAD")
    language_lock = json.loads((ROOT / "mncs-language.lock.json").read_text(encoding="utf-8"))
    source_paths = [
        ".mncs/project.json",
        "docs/native-application-build-contract.md",
        "mncs-language.lock.json",
        "tools/application_build_provider.py",
        "tools/stage0-probe/src/application.rs",
        "tools/test_application_build_provider.py",
        "tools/test_application_build_test_inventory.py",
        Path(__file__).resolve().relative_to(ROOT).as_posix(),
    ]
    source_hashes = {
        path: sha256((ROOT / path).read_bytes()) for path in source_paths
    }
    tracked_patch = subprocess.run(
        ["git", "diff", "--binary", "HEAD"], cwd=ROOT, check=True,
        capture_output=True,
    ).stdout
    base_root = ROOT_SOURCE
    base_module = MODULE_SOURCE
    comment_root = base_root + "\n// comment-only source snapshot\n"
    idle_body_root = base_root.replace("return value + 1;", "return value + 9;", 1)
    reachable_module = base_module.replace("return 5;", "return 8;", 1)
    fresh_process_root = base_root + "\n// fresh-process comment edit\n"

    all_body_facts = {
        "mncs:0.2:body:tests.native_readiness::run",
        "mncs:0.2:body:tests.native_readiness::idle",
        "mncs:0.2:body:mncs.dependency_cache::value",
        "mncs:0.2:body:mncs.dependency_cache::imported_idle",
    }
    changed_idle = "mncs:0.2:body:tests.native_readiness::idle"
    changed_value = "mncs:0.2:body:mncs.dependency_cache::value"
    changed_run = "mncs:0.2:body:tests.native_readiness::run"
    all_summary_subjects = {
        "mncs:0.2:function:tests.native_readiness::run",
        "mncs:0.2:function:tests.native_readiness::idle",
        "mncs:0.2:function:mncs.dependency_cache::value",
        "mncs:0.2:function:mncs.dependency_cache::imported_idle",
    }
    changed_idle_summary = "mncs:0.2:function:tests.native_readiness::idle"
    changed_value_summary = "mncs:0.2:function:mncs.dependency_cache::value"

    with tempfile.TemporaryDirectory(prefix="mncs-dependency-summary-cache-") as raw:
        directory = Path(raw)
        cache_root = directory / "application-cache"
        probe = app.Probe(
            directory / "program-cache",
            executable=app.PROBE,
            application_cache_dir=cache_root,
        )
        stages: dict[str, dict] = {}
        try:
            ready, ready_metrics = probe.send({"execution_status": True})
            if not isinstance(ready, dict) or "modules" not in ready:
                raise AssertionError("Stage-0 probe did not return its readiness handshake")

            cold, cold_metrics = probe.send({"compile_application": request(base_root, base_module)})
            cold_work, cold_cache = assert_frontend("cold", cold, 1)
            _, cold_observation = work_for("cold", cold)
            assert_units(
                "cold", cold_work, cold_observation,
                reused_summary=set(), recomputed_summary=all_summary_subjects,
                reused_facts=set(), recomputed_facts=all_body_facts,
            )
            stages["cold"] = {
                "cache_status": cold_cache.get("status"),
                "build_metrics": measured_metrics(cold_metrics),
                "verification_work": cold_work,
                "execution": execute_and_compare(probe, "cold", cold, base_root, base_module, 5),
            }

            unchanged, unchanged_metrics = probe.send(
                {"compile_application": request(base_root, base_module)}
            )
            unchanged_work, unchanged_cache = assert_frontend("unchanged", unchanged, 0)
            unchanged_summary = unchanged_work.get("verification_dependency_summary_cache", {})
            if (unchanged_cache.get("status") != "hit"
                    or unchanged.get("artifact", {}).get("identity")
                    != cold.get("artifact", {}).get("identity")
                    or unchanged_summary.get("status") != "not_run"
                    or unchanged_work.get("reused_semantic_facts") != []):
                raise AssertionError("unchanged cache hit reported work that did not run")
            stages["unchanged"] = {
                "cache_status": unchanged_cache.get("status"),
                "build_metrics": measured_metrics(unchanged_metrics),
                "verification_work": unchanged_work,
            }

            comment, comment_metrics = probe.send(
                {"compile_application": request(comment_root, base_module)}
            )
            comment_work, comment_cache = assert_frontend("comment_edit", comment, 1)
            _, comment_observation = work_for("comment_edit", comment)
            assert_units(
                "comment_edit", comment_work, comment_observation,
                reused_summary=all_summary_subjects, recomputed_summary=set(),
                reused_facts=all_body_facts, recomputed_facts=set(),
            )
            if (comment_cache.get("status") != "hit"
                    or comment.get("artifact", {}).get("identity")
                    != cold.get("artifact", {}).get("identity")):
                raise AssertionError("comment-only edit did not reuse independent semantic and backend work")
            stages["comment_edit"] = {
                "source_sha256": sha256(comment_root.encode()),
                "cache_status": comment_cache.get("status"),
                "build_metrics": measured_metrics(comment_metrics),
                "verification_work": comment_work,
                "execution": execute_and_compare(
                    probe, "comment-edit", comment, comment_root, base_module, 5
                ),
            }

            idle_edit, idle_metrics = probe.send(
                {"compile_application": request(idle_body_root, base_module)}
            )
            idle_work, idle_cache = assert_frontend("unreachable_body_edit", idle_edit, 1)
            _, idle_observation = work_for("unreachable_body_edit", idle_edit)
            assert_units(
                "unreachable_body_edit", idle_work, idle_observation,
                reused_summary=all_summary_subjects - {changed_idle_summary},
                recomputed_summary={changed_idle_summary},
                reused_facts=all_body_facts - {changed_idle},
                recomputed_facts={changed_idle},
            )
            if idle_edit.get("artifact", {}).get("identity") != cold.get("artifact", {}).get("identity"):
                raise AssertionError("unreachable body edit changed the executable artifact")
            stages["unreachable_body_edit"] = {
                "root_source_sha256": sha256(idle_body_root.encode()),
                "cache_status": idle_cache.get("status"),
                "build_metrics": measured_metrics(idle_metrics),
                "verification_work": idle_work,
                "execution": execute_and_compare(
                    probe, "unreachable-body-edit", idle_edit, idle_body_root, base_module, 5
                ),
            }

            reachable, reachable_metrics = probe.send(
                {"compile_application": request(idle_body_root, reachable_module)}
            )
            reachable_work, reachable_cache = assert_frontend("reachable_body_edit", reachable, 1)
            _, reachable_observation = work_for("reachable_body_edit", reachable)
            assert_units(
                "reachable_body_edit", reachable_work, reachable_observation,
                reused_summary=all_summary_subjects - {changed_value_summary},
                recomputed_summary={changed_value_summary},
                reused_facts=all_body_facts - {changed_value, changed_run},
                recomputed_facts={changed_value, changed_run},
            )
            if (reachable_cache.get("status") != "miss"
                    or reachable.get("artifact", {}).get("identity")
                    == idle_edit.get("artifact", {}).get("identity")):
                raise AssertionError("reachable imported edit did not rebuild the executable closure")
            stages["reachable_imported_body_edit"] = {
                "module_source_sha256": sha256(reachable_module.encode()),
                "cache_status": reachable_cache.get("status"),
                "build_metrics": measured_metrics(reachable_metrics),
                "verification_work": reachable_work,
                "execution": execute_and_compare(
                    probe, "reachable-imported-body-edit", reachable,
                    idle_body_root, reachable_module, 8,
                ),
            }
            probe_tail = probe.close()
        except BaseException:
            if probe.proc.poll() is None:
                probe.proc.kill()
                probe.proc.wait(timeout=5)
            raise

        fresh_probe = app.Probe(
            directory / "fresh-program-cache",
            executable=app.PROBE,
            application_cache_dir=cache_root,
        )
        try:
            ready, fresh_ready_metrics = fresh_probe.send({"execution_status": True})
            if not isinstance(ready, dict) or "modules" not in ready:
                raise AssertionError("fresh Stage-0 process did not return its readiness handshake")
            fresh, fresh_metrics = fresh_probe.send(
                {"compile_application": request(fresh_process_root, reachable_module)}
            )
            fresh_work, fresh_cache = assert_frontend("fresh_process_comment_edit", fresh, 1)
            _, fresh_observation = work_for("fresh_process_comment_edit", fresh)
            assert_units(
                "fresh_process_comment_edit", fresh_work, fresh_observation,
                reused_summary=set(),
                recomputed_summary={changed_idle_summary},
                reused_facts=all_body_facts - {changed_idle},
                recomputed_facts={changed_idle},
                fact_seeded_summary=all_summary_subjects - {changed_idle_summary},
            )
            if (fresh_cache.get("status") != "hit"
                    or fresh.get("artifact", {}).get("identity")
                    != reachable.get("artifact", {}).get("identity")):
                raise AssertionError("fresh process failed to reuse current verified facts and artifact")
            stages["fresh_process_comment_edit"] = {
                "root_source_sha256": sha256(fresh_process_root.encode()),
                "cache_status": fresh_cache.get("status"),
                "readiness_metrics": measured_metrics(fresh_ready_metrics),
                "build_metrics": measured_metrics(fresh_metrics),
                "verification_work": fresh_work,
                "execution": execute_and_compare(
                    fresh_probe, "fresh-process-comment-edit", fresh,
                    fresh_process_root, reachable_module, 8,
                ),
            }

            invalid_root = fresh_process_root.replace(
                "fn run() -> (result: u64) {",
                "fn run( -> (result: u64) {",
                1,
            )
            invalid, invalid_metrics = fresh_probe.send(
                {"compile_application": request(invalid_root, reachable_module)}
            )
            invalid_artifact = invalid.get("artifact")
            invalid_artifact_value = (
                invalid_artifact.get("value") if isinstance(invalid_artifact, dict) else None
            )
            if (invalid.get("status") == "verified"
                    or invalid_artifact_value is not None
                    or not invalid.get("diagnostics")):
                raise AssertionError("invalid fresh source reused a prior successful artifact")
            stages["invalid_source"] = {
                "status": invalid.get("status"),
                "diagnostic_count": len(invalid.get("diagnostics", [])),
                "artifact_published": invalid_artifact_value is not None,
                "build_metrics": measured_metrics(invalid_metrics),
            }
            fresh_probe_tail = fresh_probe.close()
        except BaseException:
            if fresh_probe.proc.poll() is None:
                fresh_probe.proc.kill()
                fresh_probe.proc.wait(timeout=5)
            raise

        with tempfile.TemporaryDirectory(
            prefix=".dependency-summary-provider-", dir=ROOT
        ) as provider_raw:
            provider_directory = Path(provider_raw)
            provider_request_path = provider_directory / "request.json"
            provider_tests.write_json(
                provider_request_path,
                request(idle_body_root, reachable_module),
            )
            provider_cache = provider_directory / "cache"
            provider_output = provider_directory / "output"
            provider_output.mkdir()
            provider_summary, provider_response, provider_elapsed, provider_metrics = (
                provider_tests.invoke(
                    [
                        "--request-file",
                        provider_request_path.relative_to(ROOT).as_posix(),
                        "--cache-dir",
                        provider_cache.relative_to(ROOT).as_posix(),
                    ],
                    output_dir=provider_output,
                    cache_dir=provider_cache,
                )
            )
            provider_work, provider_cache_observation = assert_frontend(
                "environment_provider_request_12", provider_response, 1
            )
            _, provider_dependency_observation = work_for(
                "environment_provider_request_12", provider_response
            )
            assert_units(
                "environment_provider_request_12", provider_work,
                provider_dependency_observation,
                reused_summary=set(), recomputed_summary=all_summary_subjects,
                reused_facts=set(), recomputed_facts=all_body_facts,
            )
            if (provider_summary.get("response_schema") != RESPONSE_SCHEMA
                    or provider_summary.get("status") != "stored"
                    or provider_response.get("status") != "verified"
                    or provider_cache_observation.get("status") != "miss"
                    or provider_response.get("artifact", {}).get("identity")
                    != reachable.get("artifact", {}).get("identity")):
                raise AssertionError(
                    "Environment provider did not route request/12 to its verified /15 result"
                )
            stages["environment_provider_request_12"] = {
                "provider_summary": provider_summary,
                "response_schema": provider_response.get("schema_version"),
                "cache_status": provider_cache_observation.get("status"),
                "verification_work": provider_work,
                "provider_wall_seconds": round(provider_elapsed, 6),
                "provider_metrics": measured_metrics(provider_metrics),
            }

    evidence = {
        "schema_version": "mncs.compiler.language-dependency-summary-cache-evidence/2",
        "compiler_repository": str(ROOT),
        "compiler_revision_before_test": root_hash,
        "compiler_branch": git("branch", "--show-current"),
        "compiler_tracked_working_tree_delta_sha256": sha256(tracked_patch),
        "compiler_source_file_sha256": source_hashes,
        "language_locked_revision": language_lock["revision"],
        "language_source_profile": language_lock["source_profile"],
        "request_schema": REQUEST_SCHEMA,
        "response_schema": RESPONSE_SCHEMA,
        "verification_work_schema": WORK_SCHEMA,
        "language_verification_fact_schema": FACT_SCHEMA,
        "language_cache_schema": SUMMARY_SCHEMA,
        "language_cache_observation_schema": SUMMARY_OBSERVATION_SCHEMA,
        "language_cache_policy_identity": SUMMARY_POLICY,
        "language_cache_scope": "bounded process-local structural summaries; not verification evidence",
        "root_source": {"module": app.APP_MODULE, "sha256": sha256(ROOT_SOURCE.encode())},
        "dependency_source": {"module": MODULE, "sha256": sha256(MODULE_SOURCE.encode())},
        "probe_build": build_info,
        "stages": stages,
        "stage0_process_stderr": {"initial_process": probe_tail, "fresh_process": fresh_probe_tail},
        "language_dependency_summaries_actually_reused": (
            sum(
                len(dependency_summary_subjects(stage, "reused_function_subjects"))
                + len(dependency_summary_subjects(stage, "fact_seeded_function_subjects"))
                for stage in stages.values()
            )
        ),
        "fresh_process_fact_seeded_dependency_summaries": len(
            stages["fresh_process_comment_edit"]["verification_work"]
            ["verification_dependency_summary_cache"]["observation"]
            ["fact_seeded_function_subjects"]
        ),
        "language_verified_facts_reused": sum(
            len(stage["verification_work"].get("reused_semantic_facts", []))
            for stage in stages.values()
            if isinstance(stage.get("verification_work"), dict)
        ),
        "fresh_process_verified_facts_reused": len(
            stages["fresh_process_comment_edit"]["verification_work"].get(
                "reused_semantic_facts", []
            )
        ),
        "invalid_source_reused_previous_success": False,
        "execution_matches_stage0": True,
        "measurement_notes": (
            "Per-request wall and CPU use process deltas. Peak process RSS is sampled from /proc; "
            "a missing or zero RSS sample is recorded as null. Cgroup peak memory remains null "
            "when the host does not expose it."
        ),
    }
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "evidence": str(EVIDENCE),
        "language_dependency_summaries_actually_reused": evidence[
            "language_dependency_summaries_actually_reused"
        ],
        "language_verified_facts_reused": evidence["language_verified_facts_reused"],
        "fresh_process_verified_facts_reused": evidence[
            "fresh_process_verified_facts_reused"
        ],
        "stages": list(stages),
    }, indent=2))


if __name__ == "__main__":
    main()
