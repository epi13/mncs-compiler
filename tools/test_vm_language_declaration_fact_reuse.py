#!/usr/bin/env python3
"""Exercise Language declaration-fact reuse through application-build/11."""
from __future__ import annotations

import copy
import hashlib
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
import test_vm_test_digest_app as digest_app

ROOT = app.ROOT
REQUEST_SCHEMA = "mncs.compiler.application-build-request/8"
RESPONSE_SCHEMA = "mncs.compiler.application-build/11"
CACHE_OBSERVATION_SCHEMA = "mncs.compiler.application-build-cache-observation/7"
VERIFICATION_WORK_SCHEMA = "mncs.compiler.application-build-verification-work/6"
FACT_SCHEMA = "mncs.language-declaration-verification-fact/1"
FUNCTION_DECLARATION_VERIFIER = "mncs-language-function-declaration-validator/1"
FINITE_TYPE_VERIFIER = "mncs-language-finite-type-validator/1"
FINITE_TYPE_POLICY = "mncs-language-finite-type-policy/1"
RECORD_TYPE_VERIFIER = "mncs-language-record-type-validator/1"
RECORD_TYPE_POLICY = "mncs-language-record-type-policy/1"
PROBE = Path(os.environ.get(
    "MNCS_NATIVE_APP_PROBE_BIN",
    str(ROOT / ".build/native-app-readiness/mncs-compiler-stage0-probe"),
)).expanduser().resolve()
VM_BIN = Path(os.environ.get(
    "MNCS_NATIVE_APP_VM_BIN",
    str(ROOT / ".build/native-app-readiness/mncs-vm"),
)).expanduser().resolve()
EVIDENCE = Path(os.environ.get(
    "MNCS_LANGUAGE_FACT_REUSE_EVIDENCE",
    str(ROOT / "evidence" / (
        f"language-declaration-fact-reuse-"
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}.json"
    )),
)).expanduser().resolve()


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


def build_digest_app_request() -> dict:
    return {
        "schema_version": REQUEST_SCHEMA,
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/test/digest.mncs",
        "root_source": digest_app.ROOT_SOURCE_PATH.read_text(encoding="utf-8"),
        "callable_roots": [{"module": "mncs.test.digest", "function": "digest_app"}],
        "test_selection": {"kind": "all_source_cases"},
        "modules": {
            name: path.read_text(encoding="utf-8")
            for name, path in digest_app.MODULE_PATHS.items()
        },
        "generic_seeds": [],
        "capabilities": ["test_artifact"],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def verify_digest_app_variant_execution(
    stage: dict,
    label: str,
    runner: Path,
    argument: dict,
    expected_result: dict,
    artifact_path: Path,
) -> dict:
    response = stage["response"]
    artifact = response.get("artifact", {}).get("value")
    interface = response.get("entry", {}).get("interface")
    if response.get("status") != "verified" or not isinstance(artifact, dict):
        raise AssertionError(f"{label} did not produce an executable digest_app artifact")
    if not isinstance(interface, dict):
        raise AssertionError(f"{label} omitted the digest_app callable interface")
    required_capabilities = artifact.get("requirements", {}).get("capabilities", [])
    if len(required_capabilities) != 1:
        raise AssertionError(f"{label} changed the used capability set: {required_capabilities}")
    dispatch = response.get("capability_dispatch", {}).get("bindings", [])
    aliases = sorted({row.get("runtime_capability_key") for row in dispatch})
    if aliases != ["test_artifact"]:
        raise AssertionError(f"{label} changed the runtime dispatch alias: {aliases}")
    artifact_path.write_text(
        json.dumps(artifact, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    digest_app.current_interface = interface
    document, results, outputs, metrics, _paths = digest_app.sample_runner_batch(
        runner,
        required_capabilities[0],
        aliases[0],
        [{"case": label, "provider_mode": "success", "argument": argument}],
        artifact_path=artifact_path,
    )
    result = results[0]
    returned = [app.decode_wire(value) for value in result.get("returned_wire", [])]
    if (document.get("artifact_id") != artifact.get("artifact_id")
            or result.get("outcome", {}).get("kind") != "completed"
            or len(returned) != 1
            or returned[0].get("exit_code") != 0
            or outputs[0] is None
            or app.decode_wire(outputs[0]) != expected_result):
        raise AssertionError(f"{label} changed effectful digest_app VM behavior: {result}")
    return {
        "status": "passed",
        "artifact_identity": artifact.get("artifact_id"),
        "application_exit": returned[0],
        "result_matches_stage0": True,
        "output_sha256": hashlib.sha256(
            json.dumps(outputs[0], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "vm_metrics": metrics,
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
    if work.get("language_finite_type_declaration_verification_fact_schema") != FACT_SCHEMA:
        raise AssertionError(f"{label} did not identify Language's finite-type fact contract")
    for field in ("reused_declaration_facts", "recomputed_declaration_facts",
                  "declaration_fact_recomputation_reasons"):
        if not isinstance(work.get(field), list):
            raise AssertionError(f"{label} omitted actual {field}")
    if type(work.get("produced_declaration_fact_count")) is not int:
        raise AssertionError(f"{label} omitted produced declaration-fact count")
    for field in ("reused_finite_type_declaration_facts",
                  "recomputed_finite_type_declaration_facts",
                  "finite_type_declaration_fact_recomputation_reasons"):
        if not isinstance(work.get(field), list):
            raise AssertionError(f"{label} omitted actual {field}")
    if type(work.get("produced_finite_type_declaration_fact_count")) is not int:
        raise AssertionError(f"{label} omitted produced finite-type fact count")
    if (work.get("language_record_type_declaration_verification_fact_schema") != FACT_SCHEMA
            or work.get("language_record_type_declaration_fact_verifier_identity")
            != RECORD_TYPE_VERIFIER
            or work.get("language_record_type_declaration_fact_policy_identity")
            != RECORD_TYPE_POLICY):
        raise AssertionError(f"{label} did not identify Language's record fact contract")
    for field in ("reused_record_type_declaration_facts",
                  "recomputed_record_type_declaration_facts",
                  "record_type_declaration_fact_recomputation_reasons"):
        if not isinstance(work.get(field), list):
            raise AssertionError(f"{label} omitted actual {field}")
    if type(work.get("produced_record_type_declaration_fact_count")) is not int:
        raise AssertionError(f"{label} omitted produced record fact count")
    return cache, work


def cache_entry(cache_dir: Path, response: dict) -> dict:
    key = response.get("cache", {}).get("key")
    if not isinstance(key, str) or not key.startswith("sha256:"):
        raise AssertionError("application response omitted its exact cache identity")
    path = cache_dir / "application-builds" / f"{key.removeprefix('sha256:')}.json"
    entry = json.loads(path.read_text(encoding="utf-8"))
    if entry.get("schema_version") != "mncs.compiler.application-build-cache-entry/8":
        raise AssertionError("V8 application cache omitted separately bound record-type facts")
    facts = entry.get("declaration_verification_facts")
    if not isinstance(facts, list) or not all(
            isinstance(row, dict)
            and row.get("schema_version") == FACT_SCHEMA
            and row.get("verifier_identity") == FUNCTION_DECLARATION_VERIFIER
            for row in facts):
        raise AssertionError("cache entry contains malformed Language declaration facts")
    if not entry.get("declaration_verification_facts_sha256"):
        raise AssertionError("cache entry does not bind declaration-fact bytes")
    finite_type_facts = entry.get("finite_type_declaration_verification_facts")
    if (not isinstance(finite_type_facts, list)
            or not finite_type_facts
            or not all(isinstance(row, dict)
                       and row.get("schema_version") == FACT_SCHEMA
                       and row.get("verifier_identity") == FINITE_TYPE_VERIFIER
                       and row.get("policy_identity") == FINITE_TYPE_POLICY
                       and row.get("result") == "verified"
                       and row.get("verification_witness_identity")
                       for row in finite_type_facts)):
        raise AssertionError("cache entry contains malformed Language finite-type facts")
    if not entry.get("finite_type_declaration_verification_facts_sha256"):
        raise AssertionError("cache entry does not bind finite-type fact bytes")
    record_type_facts = entry.get("record_type_declaration_verification_facts")
    if (not isinstance(record_type_facts, list)
            or not record_type_facts
            or not all(isinstance(row, dict)
                       and row.get("schema_version") == FACT_SCHEMA
                       and row.get("verifier_identity") == RECORD_TYPE_VERIFIER
                       and row.get("policy_identity") == RECORD_TYPE_POLICY
                       and row.get("result") == "verified"
                       and row.get("verification_witness_identity")
                       for row in record_type_facts)):
        raise AssertionError("cache entry contains malformed Language record-type facts")
    if not entry.get("record_type_declaration_verification_facts_sha256"):
        raise AssertionError("cache entry does not bind record-type fact bytes")
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
        + entry["finite_type_declaration_verification_facts"]
        + entry["record_type_declaration_verification_facts"]
    }


def measure_build(
    label: str,
    request: dict,
    cache_root: Path,
    *,
    compare_execution: bool,
    call_arguments: list[dict] | None = None,
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
                probe, request["root_source"], request["modules"],
                call=app.call_request(call_arguments),
            )
            artifact_value = response.get("artifact", {}).get("value")
            if not isinstance(artifact_value, dict):
                raise AssertionError(f"{label} omitted the canonical VM artifact value")
            vm_document, vm_metrics, _ = incremental.run_and_compare(
                artifact_value, execution, arguments=call_arguments
            )
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
    artifact = response.get("artifact")
    work = stage["verification_work"]
    return {
        "response_schema": response.get("schema_version"),
        "cache_schema": stage["cache"].get("schema_version"),
        "cache_status": stage["cache"].get("status"),
        "frontend_invocations": work.get("front_end_invocations"),
        "reused_declaration_fact_count": len(work["reused_declaration_facts"]),
        "recomputed_declaration_fact_count": len(work["recomputed_declaration_facts"]),
        "produced_declaration_fact_count": work["produced_declaration_fact_count"],
        "reused_finite_type_fact_count": len(work["reused_finite_type_declaration_facts"]),
        "recomputed_finite_type_fact_count": len(work["recomputed_finite_type_declaration_facts"]),
        "produced_finite_type_fact_count": work["produced_finite_type_declaration_fact_count"],
        "reused_record_type_fact_count": len(work["reused_record_type_declaration_facts"]),
        "recomputed_record_type_fact_count": len(work["recomputed_record_type_declaration_facts"]),
        "produced_record_type_fact_count": work["produced_record_type_declaration_fact_count"],
        "record_type_declaration_verification": work[
            "record_type_declaration_verification"
        ],
        "finite_type_declaration_verification": work[
            "finite_type_declaration_verification"
        ],
        "reused_function_body_fact_count": len(work.get("reused_semantic_facts", [])),
        "recomputed_function_body_fact_count": len(work.get("recomputed_semantic_facts", [])),
        "artifact_id": artifact.get("identity") if isinstance(artifact, dict) else None,
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
    generic_specialization_evidence: dict | None = None

    with tempfile.TemporaryDirectory(prefix=".language-record-fact-reuse-", dir=ROOT) as raw:
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
        if cold_work["produced_finite_type_declaration_fact_count"] < 1:
            raise AssertionError("cold build did not produce Language finite-type declaration facts")
        if cold_work["reused_declaration_facts"]:
            raise AssertionError("cold build reported declaration facts reused without prior evidence")
        if cold_work["reused_finite_type_declaration_facts"]:
            raise AssertionError("cold build reported finite-type facts reused without prior evidence")
        if cold_work["finite_type_declaration_verification"] != "recomputed":
            raise AssertionError("cold finite-type work was not reported as recomputed")
        if cold_work["produced_record_type_declaration_fact_count"] < 1:
            raise AssertionError("cold build did not produce Language record-type declaration facts")
        if cold_work["reused_record_type_declaration_facts"]:
            raise AssertionError("cold build reported record facts reused without prior evidence")
        if cold_work["record_type_declaration_verification"] != "recomputed":
            raise AssertionError("cold record-type work was not reported as recomputed")
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
        if unchanged["verification_work"]["reused_finite_type_declaration_facts"]:
            raise AssertionError("exact artifact hit inaccurately reported finite-type fact reuse")
        if unchanged["verification_work"]["finite_type_declaration_verification"] != "not_run":
            raise AssertionError("exact artifact hit reported Language finite-type checking")
        if unchanged["verification_work"]["reused_record_type_declaration_facts"]:
            raise AssertionError("exact artifact hit inaccurately reported record fact reuse")
        if unchanged["verification_work"]["record_type_declaration_verification"] != "not_run":
            raise AssertionError("exact artifact hit reported Language record-type checking")

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
        if not comment_work["reused_finite_type_declaration_facts"]:
            raise AssertionError("comment edit did not reuse Language finite-type declaration facts")
        if comment_work["recomputed_finite_type_declaration_facts"]:
            raise AssertionError("comment edit unnecessarily recomputed a finite-type fact")
        if comment_work["finite_type_declaration_verification"] != "reused_with_language_finite_type_facts":
            raise AssertionError("comment edit did not report finite-type fact reuse accurately")
        if not comment_work["reused_record_type_declaration_facts"]:
            raise AssertionError("comment edit did not reuse Language record-type facts")
        if comment_work["recomputed_record_type_declaration_facts"]:
            raise AssertionError("comment edit unnecessarily recomputed a record-type fact")
        if comment_work["record_type_declaration_verification"] != "reused_with_language_record_type_facts":
            raise AssertionError("comment edit did not report record-type fact reuse accurately")
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

        record_type_request = copy.deepcopy(original)
        old_request_record = "record ReadinessRequest { seed: u64, case_index: u64, expected_tests: u64 }"
        new_request_record = "record ReadinessRequest { seed: u64, case_index: u64, expected_tests: i64 }"
        if record_type_request["root_source"].count(old_request_record) != 1:
            raise AssertionError("the selected application no longer has its ReadinessRequest record")
        record_type_request["root_source"] = record_type_request["root_source"].replace(
            old_request_record, new_request_record, 1
        )
        old_error_field = "InvalidTestCount { expected: u64, found: u64 }"
        new_error_field = "InvalidTestCount { expected: u64, found: i64 }"
        if record_type_request["root_source"].count(old_error_field) != 1:
            raise AssertionError("the selected application no longer has its typed invalid-count outcome")
        record_type_request["root_source"] = record_type_request["root_source"].replace(
            old_error_field, new_error_field, 1
        )
        record_type_cache = cache_root / "record-types"
        record_type_baseline = measure_build(
            "nominal_record_dependency_baseline", original,
            record_type_cache, compare_execution=True,
        )
        stages["nominal_record_dependency_baseline"] = record_type_baseline
        record_type_edit = measure_build(
            "nominal_record_dependency_edit", record_type_request,
            record_type_cache, compare_execution=True,
            call_arguments=[app.readiness_request(
                42, 7, 4,
                identity=(
                    "mncs:0.2:record-type:tests.native_readiness::ReadinessRequest::"
                    "case_index%3Au64%3Bexpected_tests%3Ai64%3Bseed%3Au64%3B"
                ),
                expected_tests_signed=True,
            )],
        )
        stages["nominal_record_dependency_edit"] = record_type_edit
        record_type_work = record_type_edit["verification_work"]
        recomputed_record_facts = record_type_work["recomputed_record_type_declaration_facts"]
        request_fact = next(
            (subject for subject in recomputed_record_facts
             if "::ReadinessRequest::" in str(subject)),
            None,
        )
        report_fact = next(
            (subject for subject in recomputed_record_facts
             if "::ReadinessReport::" in str(subject)),
            None,
        )
        if request_fact is None or report_fact is None:
            raise AssertionError(
                "record field edit did not invalidate its own and nested dependent record facts: "
                f"{recomputed_record_facts}"
            )
        if len(recomputed_record_facts) != 2 or not record_type_work[
                "reused_record_type_declaration_facts"]:
            raise AssertionError("record edit did not isolate invalidation to the changed dependency closure")
        record_reasons = record_type_work["record_type_declaration_fact_recomputation_reasons"]
        request_reason = next(
            (row.get("reason") for row in record_reasons
             if "::ReadinessRequest::" in str(row.get("subject"))),
            None,
        )
        report_reason = next(
            (row.get("reason") for row in record_reasons
             if "::ReadinessReport::" in str(row.get("subject"))),
            None,
        )
        if request_reason != "missing" or report_reason != "dependencies_changed":
            raise AssertionError(
                "record edit did not report exact subject/dependency invalidation reasons: "
                f"{record_reasons}"
            )
        if record_type_work["record_type_declaration_verification"] != (
                "recomputed_with_language_record_type_fact_reuse"):
            raise AssertionError("record edit did not report mixed Language fact reuse accurately")
        execution_checks.append({
            "stage": "nominal_record_dependency_edit",
            "stage0_vm_equal": True,
            "recomputed_record_type_facts": recomputed_record_facts,
            "recomputation_reasons": record_reasons,
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
        if (not body_work["reused_finite_type_declaration_facts"]
                or body_work["recomputed_finite_type_declaration_facts"]):
            raise AssertionError("body-only edit did not retain finite-type declaration facts")
        if (not body_work["reused_record_type_declaration_facts"]
                or body_work["recomputed_record_type_declaration_facts"]):
            raise AssertionError("body-only edit did not retain record-type declaration facts")
        if body_work["finite_type_declaration_verification"] != "reused_with_language_finite_type_facts":
            raise AssertionError("body-only edit did not report finite-type fact reuse accurately")
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
            "mncs:0.2:body:mncs.test.generative::replay",
            "mncs:0.2:body:tests.native_readiness::run",
        }
        if set(reachable_recomputed) != expected_recomputed:
            raise AssertionError(
                "reachable imported body edit did not reverify exactly its current "
                f"dependent closure: {reachable_recomputed}"
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

        generic_request = copy.deepcopy(original)
        generic_assertions = generic_request["modules"]["mncs.test.assertions"]
        old_generic_body = "next same = same && expected[i] == actual[i];"
        new_generic_body = "next same = same && expected[i] == expected[i];"
        if generic_assertions.count(old_generic_body) != 1:
            raise AssertionError("generic equals_bytes body changed unexpectedly")
        generic_variant_request = copy.deepcopy(generic_request)
        generic_variant_request["modules"]["mncs.test.assertions"] = (
            generic_assertions.replace(old_generic_body, new_generic_body, 1)
        )
        generic_cache = cache_root / "generic-specialization"
        generic_baseline = measure_build(
            "generic_specialization_baseline", generic_request,
            generic_cache, compare_execution=True,
        )
        generic_edit = measure_build(
            "generic_specialization_body_edit", generic_variant_request,
            generic_cache, compare_execution=True,
        )
        stages["generic_specialization_body_edit"] = generic_edit
        generic_work = generic_edit["verification_work"]
        generic_recomputed = set(generic_work.get("recomputed_semantic_facts", []))
        generic_reused = set(generic_work.get("reused_semantic_facts", []))
        generic_specialized_subjects = {
            subject for subject in generic_recomputed
            if "mncs.test.assertions::equals_bytes__spec_" in subject
        }
        generic_changed_subject = "mncs:0.2:body:mncs.test.assertions::equals_bytes"
        generic_consumer_subject = "mncs:0.2:body:tests.native_readiness::run"
        expected_generic_recomputed = {
            generic_changed_subject,
            generic_consumer_subject,
            *generic_specialized_subjects,
        }
        generic_reasons = generic_work.get("semantic_fact_recomputation_reasons", [])
        if (generic_edit["response"].get("status") != "verified"
                or generic_work.get("front_end_invocations") != 1
                or len(generic_specialized_subjects) != 1
                or generic_recomputed != expected_generic_recomputed
                or not any(row.get("subject") == generic_changed_subject
                           and row.get("reason") == "subject_changed"
                           for row in generic_reasons)
                or not any(row.get("subject") in generic_specialized_subjects
                           and row.get("reason") == "subject_changed"
                           for row in generic_reasons)
                or not any(row.get("subject") == generic_consumer_subject
                           and row.get("reason") == "dependencies_changed"
                           for row in generic_reasons)):
            raise AssertionError(
                "generic implementation edit did not reverify its specialization and "
                f"consumer precisely: facts={sorted(generic_recomputed)}, reasons={generic_reasons}"
            )
        generic_baseline_facts = set(
            generic_baseline["verification_work"].get("recomputed_semantic_facts", [])
        )
        if generic_reused != generic_baseline_facts - expected_generic_recomputed:
            raise AssertionError(
                "generic implementation edit did not reuse every independent body fact"
            )
        if not any("mncs.test.generative::shrink" in subject for subject in generic_reused):
            raise AssertionError("generic edit failed to reuse independent Test body facts")
        if (generic_edit["response"].get("artifact", {}).get("identity")
                == generic_baseline["response"].get("artifact", {}).get("identity")):
            raise AssertionError("generic implementation edit reused a stale executable artifact")
        generic_success_entry = cache_entry(
            generic_cache / "application-cache", generic_edit["response"]
        )
        generic_invalid_request = copy.deepcopy(generic_variant_request)
        generic_suite = generic_invalid_request["modules"]["mncs.test.suite"]
        generic_old_enum = "enum SuiteClassification { PASSED, TEST_FAILURE, UNSUPPORTED }"
        generic_invalid_enum = (
            "enum SuiteClassification { PASSED, TEST_FAILURE, UNSUPPORTED, "
            "UNSUPPORTED }"
        )
        if generic_suite.count(generic_old_enum) != 1:
            raise AssertionError("generic candidate fixture lost SuiteClassification")
        generic_invalid_request["modules"]["mncs.test.suite"] = generic_suite.replace(
            generic_old_enum, generic_invalid_enum, 1
        )
        generic_invalid = measure_build(
            "invalid_source_with_prior_language_facts", generic_invalid_request,
            generic_cache, compare_execution=False,
        )
        stages["invalid_source_with_prior_language_facts"] = generic_invalid
        generic_invalid_response = generic_invalid["response"]
        if (generic_invalid_response.get("status") != "rejected"
                or generic_invalid_response.get("artifact") is not None):
            raise AssertionError(
                "invalid source reused prior verified facts or executable artifacts"
            )
        if not any(
            row.get("code") == "MNE172"
            and any(
                related.get("code") == "MNE121"
                and "finite variant is duplicated" in str(related.get("message"))
                for related in row.get("related", [])
            )
            for row in generic_invalid_response.get("diagnostics", [])
        ):
            raise AssertionError(
                "invalid source with a prior fact candidate omitted its fresh diagnostic"
            )
        generic_invalid_key = generic_invalid_response.get("cache", {}).get(
            "key", ""
        ).removeprefix("sha256:")
        generic_invalid_entry = (
            generic_cache / "application-cache" / "application-builds"
            / f"{generic_invalid_key}.json"
        )
        if generic_invalid_entry.exists():
            raise AssertionError("rejected source published a reusable application cache entry")
        generic_specialization_evidence = {
            "baseline_cache_key": generic_baseline["response"].get("cache", {}).get("key"),
            "changed_cache_key": generic_edit["response"].get("cache", {}).get("key"),
            "prior_verified_entry_reused_as_candidate_source": bool(generic_success_entry),
            "recomputed_semantic_facts": sorted(generic_recomputed),
            "recomputation_reasons": generic_reasons,
            "reused_semantic_fact_count": len(generic_reused),
            "reused_independent_example": next(
                subject for subject in sorted(generic_reused)
                if "mncs.test.generative::shrink" in subject
            ),
            "artifact_regenerated": True,
            "stage0_oracle_valid": generic_edit["oracle_valid"],
            "stage0_vm_equal": True,
            "invalid_source_candidate": {
                "status": generic_invalid_response.get("status"),
                "diagnostic_code": "MNE172",
                "related_diagnostic_code": "MNE121",
                "artifact_published": False,
                "cache_entry_published": generic_invalid_entry.exists(),
            },
        }

        generic_argument_modules = {
            "lib.generic_arguments": (
                "mncs 0.10;\n"
                "module lib.generic_arguments;\n"
                "fn first<N: Nat>(values: [u64; N]) -> (result: u64) "
                "{ return values[0]; }\n"
                "fn independent(value: u64) -> (result: u64) "
                "{ return value; }\n"
            ),
        }
        generic_argument_root = (
            "mncs 0.10;\n"
            "module app.generic_arguments;\n"
            "use lib.generic_arguments as g;\n"
            "fn entry_two(values: [u64; 2]) -> (result: u64) "
            "{ return g.first<2>(values); }\n"
            "fn entry_three(values: [u64; 3]) -> (result: u64) "
            "{ return g.first<3>(values); }\n"
            "fn other(value: u64) -> (result: u64) "
            "{ return g.independent(value); }\n"
        )
        generic_argument_request = {
            "schema_version": REQUEST_SCHEMA,
            "target": "mncs-vm-0.1",
            "root_source_id": "tests/native_generic_arguments.mncs",
            "root_source": generic_argument_root,
            "callable_roots": [
                {"module": "app.generic_arguments", "function": name}
                for name in ("entry_two", "entry_three", "other")
            ],
            "test_selection": {"kind": "all_source_cases"},
            "modules": generic_argument_modules,
            "generic_seeds": [],
            "capabilities": [],
            "effect_assumptions": [],
            "optimization_policy": "default",
            "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
        }
        generic_argument_variant = copy.deepcopy(generic_argument_request)
        old_generic_argument_call = (
            "fn entry_three(values: [u64; 3]) -> (result: u64) "
            "{ return g.first<3>(values); }"
        )
        new_generic_argument_call = (
            "fn entry_three(values: [u64; 4]) -> (result: u64) "
            "{ return g.first<4>(values); }"
        )
        if generic_argument_variant["root_source"].count(old_generic_argument_call) != 1:
            raise AssertionError("generic argument fixture lost its entry_three specialization")
        generic_argument_variant["root_source"] = generic_argument_variant[
            "root_source"
        ].replace(old_generic_argument_call, new_generic_argument_call, 1)
        generic_argument_cache = cache_root / "generic-argument-edit"
        generic_argument_baseline = measure_build(
            "generic_argument_baseline", generic_argument_request,
            generic_argument_cache, compare_execution=False,
        )
        generic_argument_edit = measure_build(
            "generic_argument_edit", generic_argument_variant,
            generic_argument_cache, compare_execution=False,
        )
        stages["generic_argument_edit"] = generic_argument_edit
        generic_argument_work = generic_argument_edit["verification_work"]
        generic_argument_recomputed = set(
            generic_argument_work.get("recomputed_semantic_facts", [])
        )
        generic_argument_reused = set(
            generic_argument_work.get("reused_semantic_facts", [])
        )
        new_generic_specializations = {
            subject for subject in generic_argument_recomputed
            if "lib.generic_arguments::first__spec_" in subject
        }
        changed_generic_caller = "mncs:0.2:body:app.generic_arguments::entry_three"
        expected_generic_argument_recomputed = {
            changed_generic_caller,
            *new_generic_specializations,
        }
        generic_argument_reasons = generic_argument_work.get(
            "semantic_fact_recomputation_reasons", []
        )
        reused_generic_specializations = {
            subject for subject in generic_argument_reused
            if "lib.generic_arguments::first__spec_" in subject
        }
        if (generic_argument_baseline["response"].get("status") != "verified"
                or generic_argument_edit["response"].get("status") != "verified"
                or generic_argument_work.get("front_end_invocations") != 1
                or len(new_generic_specializations) != 1
                or generic_argument_recomputed != expected_generic_argument_recomputed
                or len(reused_generic_specializations) != 1
                or not any(subject.endswith("::entry_two") for subject in generic_argument_reused)
                or not any(subject.endswith("::other") for subject in generic_argument_reused)
                or not any(row.get("subject") == changed_generic_caller
                           and row.get("reason") == "subject_changed"
                           for row in generic_argument_reasons)
                or not any(row.get("subject") in new_generic_specializations
                           and row.get("reason") == "missing"
                           for row in generic_argument_reasons)):
            raise AssertionError(
                "generic argument edit did not isolate the new specialization and caller: "
                f"recomputed={sorted(generic_argument_recomputed)}, "
                f"reused={sorted(generic_argument_reused)}, reasons={generic_argument_reasons}"
            )
        if (generic_argument_edit["response"].get("artifact", {}).get("identity")
                == generic_argument_baseline["response"].get("artifact", {}).get("identity")):
            raise AssertionError("generic argument edit reused an artifact with a stale closure")
        generic_specialization_evidence["generic_argument_edit"] = {
            "baseline_cache_key": generic_argument_baseline["response"].get(
                "cache", {}
            ).get("key"),
            "changed_cache_key": generic_argument_edit["response"].get(
                "cache", {}
            ).get("key"),
            "recomputed_semantic_facts": sorted(generic_argument_recomputed),
            "recomputation_reasons": generic_argument_reasons,
            "reused_specialization_fact": next(iter(reused_generic_specializations)),
            "reused_independent_callers": sorted(
                subject for subject in generic_argument_reused
                if subject.endswith("::entry_two") or subject.endswith("::other")
            ),
            "artifact_regenerated": True,
        }
        execution_checks.append({
            "stage": "generic_specialization_body_edit", "stage0_vm_equal": True,
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

        finite_type_request = copy.deepcopy(original)
        suite = finite_type_request["modules"]["mncs.test.suite"]
        old_enum = "enum SuiteClassification { PASSED, TEST_FAILURE, UNSUPPORTED }"
        new_enum = (
            "enum SuiteClassification { PASSED, TEST_FAILURE, UNSUPPORTED, "
            "INCREMENTAL_FACT_PROBE }"
        )
        if suite.count(old_enum) != 1:
            raise AssertionError("the selected Test fixture no longer has SuiteClassification")
        finite_type_request["modules"]["mncs.test.suite"] = suite.replace(old_enum, new_enum, 1)
        finite_type_edit = measure_build(
            "imported_finite_type_edit", finite_type_request,
            cache_root / "cold", compare_execution=True,
        )
        stages["imported_finite_type_edit"] = finite_type_edit
        finite_type_work = finite_type_edit["verification_work"]
        changed_type_facts = [
            str(subject)
            for subject in finite_type_work["recomputed_finite_type_declaration_facts"]
            if "SuiteClassification" in str(subject)
        ]
        if len(changed_type_facts) != 1:
            raise AssertionError(
                "the imported finite-type edit did not reverify its exact declaration: "
                f"{finite_type_work['recomputed_finite_type_declaration_facts']}"
            )
        if not finite_type_work["reused_finite_type_declaration_facts"]:
            raise AssertionError("finite-type edit failed to reuse independent verified types")
        if len(finite_type_work["recomputed_finite_type_declaration_facts"]) != 1:
            raise AssertionError("finite-type edit invalidated unrelated type declarations")
        finite_type_reasons = finite_type_work["finite_type_declaration_fact_recomputation_reasons"]
        if not any(
            "SuiteClassification" in str(row.get("subject"))
            and row.get("reason") == "subject_changed"
            for row in finite_type_reasons
        ):
            raise AssertionError("finite-type edit omitted its subject-fingerprint invalidation reason")
        if finite_type_work["finite_type_declaration_verification"] != "recomputed_with_language_finite_type_fact_reuse":
            raise AssertionError("finite-type edit reported its mixed reuse and recomputation inaccurately")
        execution_checks.append({
            "stage": "imported_finite_type_edit", "stage0_vm_equal": True,
            "recomputed_finite_type": changed_type_facts[0],
        })

        invalid_type_request = copy.deepcopy(original)
        suite = invalid_type_request["modules"]["mncs.test.suite"]
        invalid_enum = (
            "enum SuiteClassification { PASSED, TEST_FAILURE, UNSUPPORTED, "
            "UNSUPPORTED }"
        )
        invalid_type_request["modules"]["mncs.test.suite"] = suite.replace(
            old_enum, invalid_enum, 1
        )
        invalid_cache_root = cache_root / "invalid-type"
        invalid_type = measure_build(
            "invalid_finite_type_edit", invalid_type_request,
            invalid_cache_root, compare_execution=False,
        )
        stages["invalid_finite_type_edit"] = invalid_type
        invalid_response = invalid_type["response"]
        if invalid_response.get("status") != "rejected":
            raise AssertionError("invalid finite-type source was accepted from prior facts")
        if (invalid_response.get("cache", {}).get("verification_work", {}).get(
                "finite_type_declaration_verification") != "not_reported"):
            raise AssertionError(
                "rejected source claimed finite-type fact work that Language did not report"
            )
        if invalid_response.get("artifact") is not None:
            raise AssertionError("invalid finite-type source reused an executable artifact")
        if not any(
            row.get("code") == "MNE172"
            and any(
                related.get("code") == "MNE121"
                and "finite variant is duplicated" in str(related.get("message"))
                for related in row.get("related", [])
            )
            for row in invalid_response.get("diagnostics", [])
        ):
            raise AssertionError(
                "invalid finite-type edit omitted its fresh duplicate-variant diagnostic: "
                f"{invalid_response.get('diagnostics')}"
            )
        invalid_key = invalid_response.get("cache", {}).get("key", "").removeprefix("sha256:")
        invalid_entry = (
            invalid_cache_root / "application-cache" / "application-builds"
            / f"{invalid_key}.json"
        )
        if invalid_entry.exists():
            raise AssertionError("rejected finite-type verification published a reusable cache entry")

        digest_request = build_digest_app_request()
        digest_body_identity = "mncs:0.2:body:mncs.test.digest::digest_app"
        digest_fixture = json.loads(digest_app.FIXTURE_PATH.read_text(encoding="utf-8"))
        digest_argument = digest_fixture["execution"]["inputs"][0]
        digest_expected_result = digest_fixture["execution"]["stage0_results"][0][0]
        digest_stages = {}

        policy_cache = cache_root / "digest-app-policy-assumptions"
        policy_baseline = measure_build(
            "digest_app_policy_baseline", digest_request, policy_cache,
            compare_execution=False,
        )
        stages["digest_app_policy_baseline"] = policy_baseline
        policy_baseline_work = policy_baseline["verification_work"]
        if (policy_baseline["response"].get("status") != "verified"
                or digest_body_identity not in policy_baseline_work["recomputed_semantic_facts"]):
            raise AssertionError("cold effectful digest_app build did not verify its Language body")
        policy_variant_request = copy.deepcopy(digest_request)
        policy_variant_request["capabilities"] = ["test_artifact", "test_artifact.metadata"]
        policy_variant_request["effect_assumptions"] = [{
            "capability": "test_artifact",
            "policy": "bounded-artifact/2",
        }]
        policy_variant = measure_build(
            "digest_app_changed_host_assumptions", policy_variant_request,
            policy_cache, compare_execution=False,
        )
        stages["digest_app_changed_host_assumptions"] = policy_variant
        policy_work = policy_variant["verification_work"]
        if (policy_variant["response"].get("status") != "verified"
                or policy_variant["response"].get("cache", {}).get("key")
                == policy_baseline["response"].get("cache", {}).get("key")
                or policy_work.get("front_end_invocations") != 1
                or policy_work.get("recomputed_semantic_facts")
                or set(policy_work.get("reused_semantic_facts", []))
                != set(policy_baseline_work.get("recomputed_semantic_facts", []))
                or policy_work.get("backend_artifact") == "reused"):
            raise AssertionError(
                "changed host admission assumptions must reuse guarded Language facts "
                "while rebuilding the request-specific artifact"
            )
        digest_stages["changed_host_assumptions"] = {
            "source_snapshot_unchanged": (
                policy_variant["response"].get("source_snapshot", {}).get("identity")
                == policy_baseline["response"].get("source_snapshot", {}).get("identity")
            ),
            "artifact_identity_unchanged": (
                policy_variant["response"].get("artifact", {}).get("identity")
                == policy_baseline["response"].get("artifact", {}).get("identity")
            ),
            "reused_semantic_facts": policy_work["reused_semantic_facts"],
            "recomputed_semantic_facts": policy_work["recomputed_semantic_facts"],
            "front_end_invocations": policy_work["front_end_invocations"],
            "backend_artifact": policy_work["backend_artifact"],
        }

        effect_cache = cache_root / "digest-app-effect-declaration"
        effect_baseline = measure_build(
            "digest_app_effect_baseline", digest_request, effect_cache,
            compare_execution=False,
        )
        stages["digest_app_effect_baseline"] = effect_baseline
        effect_variant_request = copy.deepcopy(digest_request)
        old_effects = (
            "    effect structured_read authorized_by test_artifact\n"
            "    effect structured_write authorized_by test_artifact"
        )
        new_effects = (
            "    effect structured_write authorized_by test_artifact\n"
            "    effect structured_read authorized_by test_artifact"
        )
        if effect_variant_request["root_source"].count(old_effects) != 1:
            raise AssertionError("digest_app effect declaration block changed unexpectedly")
        effect_variant_request["root_source"] = effect_variant_request["root_source"].replace(
            old_effects, new_effects, 1
        )
        effect_variant = measure_build(
            "digest_app_effect_declaration_edit", effect_variant_request,
            effect_cache, compare_execution=False,
        )
        stages["digest_app_effect_declaration_edit"] = effect_variant
        effect_work = effect_variant["verification_work"]
        effect_reasons = effect_work.get("semantic_fact_recomputation_reasons", [])
        if (effect_variant["response"].get("status") != "verified"
                or effect_work.get("recomputed_semantic_facts") != [digest_body_identity]
                or not any(row.get("subject") == digest_body_identity
                           and row.get("reason") == "effect_assumptions_changed"
                           for row in effect_reasons)
                or digest_body_identity not in effect_baseline["verification_work"].get(
                    "recomputed_semantic_facts", [])):
            raise AssertionError(
                "effect declaration edit did not isolate body-fact invalidation: "
                f"{effect_reasons}"
            )
        if set(effect_work.get("reused_semantic_facts", [])) != (
                set(effect_baseline["verification_work"].get("recomputed_semantic_facts", []))
                - {digest_body_identity}):
            raise AssertionError("effect declaration edit failed to reuse independent body facts")
        digest_stages["effect_declaration_edit"] = {
            "recomputed_semantic_facts": effect_work["recomputed_semantic_facts"],
            "recomputation_reasons": effect_reasons,
            "reused_semantic_fact_count": len(effect_work["reused_semantic_facts"]),
            "backend_artifact": effect_work["backend_artifact"],
        }

        capability_cache = cache_root / "digest-app-capability-declaration"
        capability_baseline = measure_build(
            "digest_app_capability_baseline", digest_request, capability_cache,
            compare_execution=False,
        )
        stages["digest_app_capability_baseline"] = capability_baseline
        capability_variant_request = copy.deepcopy(digest_request)
        old_capability = "    capability test_artifact\n"
        if capability_variant_request["root_source"].count(old_capability) != 1:
            raise AssertionError("digest_app capability declaration changed unexpectedly")
        capability_variant_request["root_source"] = capability_variant_request[
            "root_source"
        ].replace(old_capability, old_capability + "    capability digest_probe\n", 1)
        capability_variant_request["capabilities"] = ["test_artifact", "digest_probe"]
        capability_variant = measure_build(
            "digest_app_capability_declaration_edit", capability_variant_request,
            capability_cache, compare_execution=False,
        )
        stages["digest_app_capability_declaration_edit"] = capability_variant
        capability_work = capability_variant["verification_work"]
        capability_reasons = capability_work.get(
            "semantic_fact_recomputation_reasons", []
        )
        if (capability_variant["response"].get("status") != "verified"
                or capability_work.get("recomputed_semantic_facts") != [digest_body_identity]
                or not any(row.get("subject") == digest_body_identity
                           and row.get("reason") == "capability_assumptions_changed"
                           for row in capability_reasons)):
            raise AssertionError(
                "capability declaration edit did not isolate body-fact invalidation: "
                f"{capability_reasons}"
            )
        if set(capability_work.get("reused_semantic_facts", [])) != (
                set(capability_baseline["verification_work"].get(
                    "recomputed_semantic_facts", [])) - {digest_body_identity}):
            raise AssertionError("capability declaration edit failed to reuse independent body facts")
        digest_stages["capability_declaration_edit"] = {
            "recomputed_semantic_facts": capability_work["recomputed_semantic_facts"],
            "recomputation_reasons": capability_reasons,
            "reused_semantic_fact_count": len(capability_work["reused_semantic_facts"]),
            "backend_artifact": capability_work["backend_artifact"],
        }

        digest_runner, digest_runner_sha, digest_runner_lock_sha = digest_app.build_runner()
        digest_vm_checks = []
        with tempfile.TemporaryDirectory(prefix="digest-app-fact-vm-", dir=ROOT / ".build") as raw:
            artifact_path = Path(raw) / "digest-app.vm.json"
            for label, stage in (
                ("changed_host_assumptions", policy_variant),
                ("effect_declaration_edit", effect_variant),
                ("capability_declaration_edit", capability_variant),
            ):
                digest_vm_checks.append(verify_digest_app_variant_execution(
                    stage,
                    label,
                    digest_runner,
                    digest_argument,
                    digest_expected_result,
                    artifact_path,
                ))
                digest_vm_checks[-1]["stage"] = label
        digest_stages["canonical_vm_provider"] = {
            "runner_sha256": digest_runner_sha,
            "runner_lock_sha256": digest_runner_lock_sha,
            "checks": digest_vm_checks,
        }

        language_lock = json.loads((ROOT / "mncs-language.lock.json").read_text(encoding="utf-8"))
        evidence = {
            "schema_version": "mncs.compiler.language-declaration-fact-reuse-evidence/3",
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
                "cache_entry_schema": "mncs.compiler.application-build-cache-entry/8",
            },
            "stages": {name: compact(stage) for name, stage in stages.items()},
            "generic_specialization_fact_reuse": generic_specialization_evidence,
            "effectful_digest_app_fact_reuse": digest_stages,
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
            "finite_type_declaration_fact_reuse": {
                "comment_edit_reused": len(comment_work["reused_finite_type_declaration_facts"]),
                "comment_edit_recomputed": len(comment_work["recomputed_finite_type_declaration_facts"]),
                "body_edit_reused": len(body_work["reused_finite_type_declaration_facts"]),
                "body_edit_recomputed": len(body_work["recomputed_finite_type_declaration_facts"]),
                "imported_type_edit_reused": len(
                    finite_type_work["reused_finite_type_declaration_facts"]),
                "imported_type_edit_recomputed": finite_type_work[
                    "recomputed_finite_type_declaration_facts"],
                "imported_type_edit_recomputation_reasons": finite_type_reasons,
                "imported_type_edit_vm_equal": True,
                "invalid_type_edit_status": invalid_response.get("status"),
                "invalid_type_edit_fresh_diagnostic": "MNE121",
                "invalid_type_edit_cache_published": invalid_entry.exists(),
            },
            "record_type_declaration_fact_reuse": {
                "comment_edit_reused": len(comment_work["reused_record_type_declaration_facts"]),
                "comment_edit_recomputed": len(comment_work["recomputed_record_type_declaration_facts"]),
                "body_edit_reused": len(body_work["reused_record_type_declaration_facts"]),
                "body_edit_recomputed": len(body_work["recomputed_record_type_declaration_facts"]),
                "nominal_edit_reused": len(record_type_work["reused_record_type_declaration_facts"]),
                "nominal_edit_recomputed": recomputed_record_facts,
                "nominal_edit_recomputation_reasons": record_reasons,
                "comment_edit_verification": comment_work[
                    "record_type_declaration_verification"
                ],
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
