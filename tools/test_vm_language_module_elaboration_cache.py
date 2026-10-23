#!/usr/bin/env python3
"""Exercise Language imported-module elaboration reuse on mncs-test's digest app."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parents[1]
# The workspace's inherited TMPDIR is on a quota-limited Btrfs subvolume.
# This test atomically replaces cache-index files across successive builds, so
# keep its disposable cache and process artifacts on the host tmpfs instead.
os.environ["TMPDIR"] = "/tmp"
tempfile.tempdir = "/tmp"
locked_language = json.loads((ROOT / "mncs-language.lock.json").read_text(encoding="utf-8"))
os.environ.setdefault(
    "MNCS_BOOTSTRAP_TARGET_DIR",
    f"/tmp/mncs-language-stage0-{locked_language['revision'][:7]}",
)
from ensure_locked_stage0_probe import ensure_locked_stage0_probe

LOCKED_PROBE = ensure_locked_stage0_probe(ROOT)
os.environ["MNCS_NATIVE_APP_PROBE_BIN"] = str(LOCKED_PROBE)
os.environ["MNCS_PROBE_BIN"] = str(LOCKED_PROBE)

import test_application_build_provider as provider_tests
import test_native_application_incremental as incremental
import test_vm_native_application as app
import test_vm_test_digest_app as digest_app

# Prefer the current VM checkout over the Compiler worktree's copied VM
# binary, which may predate the pinned Language/SSA wire schema.
if os.environ.get("MNCS_LANGUAGE_MODULE_CACHE_VM_BIN"):
    app.VM_BIN = Path(os.environ["MNCS_LANGUAGE_MODULE_CACHE_VM_BIN"])
else:
    vm_common_dir = Path(subprocess.check_output(
        ["git", "-C", str(digest_app.VM_ROOT), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        text=True,
    ).strip())
    canonical_vm_root = vm_common_dir.resolve().parent
    for candidate in (
        canonical_vm_root / "target/release/mncs-vm",
        canonical_vm_root / "target/debug/mncs-vm",
    ):
        if candidate.is_file():
            app.VM_BIN = candidate
            break

REQUEST_SCHEMA = "mncs.compiler.application-build-request/13"
RESPONSE_SCHEMA = "mncs.compiler.application-build/16"
WORK_SCHEMA = "mncs.compiler.application-build-verification-work/12"
MODULE_CACHE_SCHEMA = "mncs.language-module-elaboration-cache/3"
MODULE_OBSERVATION_SCHEMA = "mncs.language-module-elaboration-cache-observation/1"
SOURCE_PARSE_CACHE_SCHEMA = "mncs.language-source-parse-cache/3"
SOURCE_PARSE_OBSERVATION_SCHEMA = "mncs.language-source-parse-cache-observation/3"
SOURCE_PARSE_POLICY_IDENTITY = "mncs-language-source-parser/1"
EXPECTED_MODULES = set(digest_app.MODULE_PATHS)
GENERIC_SUMMARIZE_SEED = [{
    "module": "mncs.test.suite",
    "function": "summarize",
    "type_arguments": [{"kind": "nat", "value": 4}],
}]


def request(
    root_source: str,
    modules: dict[str, str],
    *,
    generic_seeds: list[dict] | None = None,
) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA,
        "target": "mncs-vm-0.1",
        "root_source_id": digest_app.ROOT_SOURCE_PATH.relative_to(
            digest_app.TEST_ROOT
        ).as_posix(),
        "root_source": root_source,
        "callable_roots": [{"module": digest_app.ENTRY_MODULE, "function": "build_digest"}],
        "test_selection": {"kind": "all_source_cases"},
        "modules": modules,
        "generic_seeds": list(generic_seeds or []),
        "capabilities": ["test_artifact"],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def work_for(label: str, response: dict) -> tuple[dict, dict]:
    if response.get("schema_version") != RESPONSE_SCHEMA:
        raise AssertionError(
            f"{label}: unexpected response schema {response.get('schema_version')}"
        )
    work = response.get("cache", {}).get("verification_work", {})
    if work.get("schema_version") != WORK_SCHEMA:
        raise AssertionError(f"{label}: unexpected verification-work schema: {work}")
    if work.get("language_verification_fact_schema") != "mncs.language-verification-fact/4":
        raise AssertionError(f"{label}: Compiler did not consume Language fact v4: {work}")
    cache = work.get("module_elaboration_cache", {})
    if (cache.get("schema_version") != MODULE_CACHE_SCHEMA
            or cache.get("observation_schema_version") != MODULE_OBSERVATION_SCHEMA
            or cache.get("linker_policy_identity") != "mncs-language-module-linker/2"):
        raise AssertionError(f"{label}: missing Language module-cache contract: {cache}")
    observation = cache.get("observation")
    if cache.get("status") != "observed" or not isinstance(observation, dict):
        raise AssertionError(f"{label}: Language did not report actual module work: {cache}")
    if work.get("front_end_invocations") != 1:
        raise AssertionError(
            f"{label}: expected a fresh Language frontend invocation, got "
            f"{work.get('front_end_invocations')}"
        )
    source_parse = work.get("source_parse_cache", {})
    if (source_parse.get("schema_version") != SOURCE_PARSE_CACHE_SCHEMA
            or source_parse.get("observation_schema_version") != SOURCE_PARSE_OBSERVATION_SCHEMA
            or source_parse.get("parser_policy_identity") != SOURCE_PARSE_POLICY_IDENTITY):
        raise AssertionError(f"{label}: missing Language source-parse cache contract: {source_parse}")
    if (source_parse.get("status") != "observed"
            or not isinstance(source_parse.get("observation"), dict)):
        raise AssertionError(f"{label}: Language did not report source-parse work: {source_parse}")
    return work, observation


def module_set(observation: dict, field: str) -> set[str]:
    values = observation.get(field)
    if not isinstance(values, list):
        raise AssertionError(f"expected {field} to be a list: {observation}")
    return {str(value) for value in values}


def fact_set(work: dict, field: str) -> set[str]:
    values = work.get(field)
    if not isinstance(values, list):
        raise AssertionError(f"expected {field} to be a list: {work}")
    return {str(value) for value in values}


def source_parse_observation(work: dict) -> dict:
    source_parse = work.get("source_parse_cache", {})
    observation = source_parse.get("observation")
    if not isinstance(observation, dict):
        raise AssertionError(f"Language source-parse observation is missing: {source_parse}")
    return observation


def assert_modules(
    label: str,
    observation: dict,
    *,
    reused: set[str],
    linked: set[str],
    invalidated: set[str],
) -> None:
    actual = {
        "reused": module_set(observation, "reused_module_subjects"),
        "linked": module_set(observation, "linked_module_subjects"),
        "invalidated": module_set(observation, "invalidated_module_subjects"),
    }
    expected = {"reused": reused, "linked": linked, "invalidated": invalidated}
    if actual != expected:
        raise AssertionError(f"{label}: module cache work differs: actual={actual}, expected={expected}")


def digest_call(argument: dict) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": digest_app.ENTRY_MODULE, "function": "build_digest"},
        "arguments": [argument],
        "type_arguments": [],
        "step_budget": 3_000_000,
    }


def canonical_vm_result(artifact: dict, argument: dict, label: str) -> tuple[list, dict]:
    with tempfile.TemporaryDirectory(prefix=f"mncs-module-cache-{label}-") as raw:
        directory = Path(raw)
        artifact_path = directory / "application.vm.json"
        args_path = directory / "args.json"
        type_args_path = directory / "type-args.json"
        artifact_path.write_text(
            json.dumps(artifact, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        args_path.write_text(json.dumps([argument], separators=(",", ":")), encoding="utf-8")
        type_args_path.write_text("[]", encoding="utf-8")
        completed = subprocess.run(
            [
                str(app.VM_BIN), "run", "--artifact", str(artifact_path),
                "--callable", f"{digest_app.ENTRY_MODULE}::build_digest",
                "--args", str(args_path), "--type-args", str(type_args_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if completed.returncode:
            raise AssertionError(
                f"{label}: canonical VM exited {completed.returncode}; "
                f"artifact_id={artifact.get('artifact_id')!r}; "
                f"stdout={completed.stdout[-3000:]!r}; stderr={completed.stderr[-3000:]!r}"
            )
        document = json.loads(completed.stdout)
        if (document.get("outcome") != {"kind": "completed"}
                or document.get("record", {}).get("artifact_id") != artifact.get("artifact_id")):
            raise AssertionError(f"{label}: VM receipt does not bind the selected artifact")
        values = [app.decode_wire(app.vm_to_wire(row))
                  for row in document.get("record", {}).get("returned", [])]
        return values, document.get("record", {})


def assert_execution_equivalent(
    probe: app.Probe,
    label: str,
    response: dict,
    root_source: str,
    modules: dict[str, str],
    argument: dict,
) -> dict:
    artifact = response.get("artifact", {}).get("value")
    if not isinstance(artifact, dict):
        raise AssertionError(f"{label}: verified VM artifact is missing")
    oracle, execution, oracle_metrics = incremental.oracle_one(
        probe, root_source, modules, call=digest_call(argument)
    )
    if not oracle.get("valid"):
        raise AssertionError(f"{label}: Stage-0 oracle rejected source: {oracle.get('diagnostics')}")
    expected = [app.decode_wire(row) for row in execution.get("returned", [])]
    actual, record = canonical_vm_result(artifact, argument, label)
    if actual != expected:
        raise AssertionError(f"{label}: canonical VM differs from Stage-0 oracle")
    return {
        "output_equal_to_stage0": True,
        "returned_value_count": len(actual),
        "artifact_identity": response.get("artifact", {}).get("identity"),
        "stage0_metrics": oracle_metrics,
        "vm_run_identity": record.get("artifact_id"),
    }


def cache_entry_path(cache_root: Path, response: dict) -> Path:
    key = response.get("cache", {}).get("key")
    if not isinstance(key, str) or not key.startswith("sha256:"):
        raise AssertionError("application response omitted its content-addressed cache key")
    return cache_root / "application-builds" / f"{key.removeprefix('sha256:')}.json"


def stage_summary(row: dict) -> dict:
    summary = {
        "cache_status": row.get("cache_status"),
        "build_metrics": row.get("build_metrics"),
    }
    for field in ("status", "diagnostic_count", "artifact_published", "artifact_identity"):
        if field in row:
            summary[field] = row[field]
    module_observation = row.get("module_observation")
    if isinstance(module_observation, dict):
        summary["module_observation"] = {
            "linked_module_subjects": module_observation.get("linked_module_subjects", []),
            "reused_module_subjects": module_observation.get("reused_module_subjects", []),
            "invalidated_module_subjects": module_observation.get("invalidated_module_subjects", []),
            "published_entry_count": module_observation.get("published_entry_count", 0),
            "rejected_entry_count": module_observation.get("rejected_entry_count", 0),
        }
    work = row.get("verification_work")
    if isinstance(work, dict):
        reused = sorted(fact_set(work, "reused_semantic_facts"))
        recomputed = sorted(fact_set(work, "recomputed_semantic_facts"))
        summary["semantic_work"] = {
            "front_end_invocations": work.get("front_end_invocations"),
            "reused_fact_count": len(reused),
            "recomputed_fact_count": len(recomputed),
            "reused_subjects_sample": reused[:4],
            "recomputed_subjects_sample": recomputed[:4],
            "from_assertion_body_fact_recomputed": (
                "mncs:0.2:body:mncs.test.assertions::from_assertion" in recomputed
            ),
        }
        parse_observation = source_parse_observation(work)
        summary["source_parse_work"] = {
            "reused_exact_source_count": len(parse_observation.get("reused_source_identities", [])),
            "reused_significant_tokens_source_count": len(
                parse_observation.get("reused_same_significant_tokens_source_identities", [])
            ),
            "parsed_source_count": len(parse_observation.get("parsed_source_identities", [])),
            "published_entry_count": parse_observation.get("published_entry_count", 0),
        }
    if "execution" in row:
        summary["execution"] = row["execution"]
    return summary


def run_isolated_module_edit(
    probe_binary: Path,
    directory: Path,
    label: str,
    root_source: str,
    modules: dict[str, str],
    edited_root: str,
    edited_modules: dict[str, str],
    *,
    reused_modules: set[str],
    linked_modules: set[str],
    invalidated_modules: set[str],
    recomputed_subject_suffixes: set[str],
    argument: dict,
    generic_seeds: list[dict] | None = None,
) -> dict[str, dict]:
    """Prove one interface edit against a fresh application-cache baseline."""
    cache_root = directory / f"{label}-application-cache"
    probe = app.Probe(
        directory / f"{label}-program-cache",
        executable=probe_binary,
        application_cache_dir=cache_root,
    )
    result: dict[str, dict] = {}
    try:
        ready, _ = probe.send({"execution_status": True})
        if not isinstance(ready, dict) or "modules" not in ready:
            raise AssertionError(f"{label}: Stage-0 probe did not return its readiness handshake")

        cold, cold_metrics = probe.send(
            {"compile_application": request(
                root_source, modules, generic_seeds=generic_seeds
            )}, timeout=180
        )
        cold_work, cold_observation = work_for(f"{label}_cold", cold)
        assert_modules(
            f"{label}_cold", cold_observation,
            reused=set(), linked=EXPECTED_MODULES, invalidated=set(),
        )
        if cold.get("status") != "verified" or not fact_set(
            cold_work, "recomputed_semantic_facts"
        ):
            raise AssertionError(f"{label}: isolated cold baseline did not verify all facts")
        result["cold"] = {
            "cache_status": cold.get("cache", {}).get("status"),
            "artifact_identity": cold.get("artifact", {}).get("identity"),
            "build_metrics": cold_metrics,
            "module_observation": cold_observation,
            "verification_work": cold_work,
        }

        edited, edited_metrics = probe.send(
            {"compile_application": request(
                edited_root, edited_modules, generic_seeds=generic_seeds
            )}, timeout=180
        )
        edited_work, edited_observation = work_for(label, edited)
        assert_modules(
            label, edited_observation,
            reused=reused_modules,
            linked=linked_modules,
            invalidated=invalidated_modules,
        )
        if edited.get("status") != "verified":
            raise AssertionError(
                f"{label}: interface edit failed verification: {edited.get('diagnostics')}"
            )

        recomputed = fact_set(edited_work, "recomputed_semantic_facts")
        reused = fact_set(edited_work, "reused_semantic_facts")
        missing = sorted(
            suffix for suffix in recomputed_subject_suffixes
            if not any(subject.endswith(suffix) for subject in recomputed)
        )
        if missing or not recomputed or not reused:
            raise AssertionError(
                f"{label}: Language did not report the expected exact invalidation and reuse; "
                f"missing={missing}, recomputed={sorted(recomputed)}, reused={sorted(reused)}"
            )
        result["edited"] = {
            "cache_status": edited.get("cache", {}).get("status"),
            "artifact_identity": edited.get("artifact", {}).get("identity"),
            "build_metrics": edited_metrics,
            "module_observation": edited_observation,
            "verification_work": edited_work,
            "execution": assert_execution_equivalent(
                probe, label, edited, edited_root, edited_modules, argument
            ),
        }
    finally:
        result["probe_tail"] = probe.close()
    return result


def main() -> None:
    provider_tests.ensure_current_probe()
    probe_binary = app.PROBE
    if not app.VM_BIN.is_file():
        raise SystemExit(f"canonical VM binary does not exist: {app.VM_BIN}")
    root_source = digest_app.ROOT_SOURCE_PATH.read_text(encoding="utf-8")
    modules = {
        name: path.read_text(encoding="utf-8")
        for name, path in digest_app.MODULE_PATHS.items()
    }
    fixture = json.loads(digest_app.FIXTURE_PATH.read_text(encoding="utf-8"))
    argument = fixture["execution"]["inputs"][0]
    stages: dict[str, dict] = {}

    with tempfile.TemporaryDirectory(prefix="mncs-test-module-elaboration-cache-") as raw:
        directory = Path(raw)
        cache_root = directory / "application-cache"
        probe = app.Probe(
            directory / "program-cache",
            executable=probe_binary,
            application_cache_dir=cache_root,
        )
        try:
            ready, _ = probe.send({"execution_status": True})
            if not isinstance(ready, dict) or "modules" not in ready:
                raise AssertionError("Stage-0 probe did not return its readiness handshake")

            cold, cold_metrics = probe.send(
                {"compile_application": request(root_source, modules)}, timeout=180
            )
            cold_work, cold_observation = work_for("cold", cold)
            assert_modules(
                "cold", cold_observation, reused=set(), linked=EXPECTED_MODULES,
                invalidated=set(),
            )
            if (cold.get("status") != "verified"
                    or cold_observation.get("published_entry_count") != len(EXPECTED_MODULES)):
                raise AssertionError(f"cold build did not publish every imported module: {cold}")
            if not fact_set(cold_work, "recomputed_semantic_facts"):
                raise AssertionError("cold build did not report actual Language verification work")
            stages["cold"] = {
                "cache_status": cold.get("cache", {}).get("status"),
                "build_metrics": cold_metrics,
                "module_observation": cold_observation,
                "verification_work": cold_work,
                "execution": assert_execution_equivalent(
                    probe, "cold", cold, root_source, modules, argument
                ),
            }

            comment_root = root_source + "\n// root-only source location refresh\n"
            comment, comment_metrics = probe.send(
                {"compile_application": request(comment_root, modules)}, timeout=180
            )
            comment_work, comment_observation = work_for("root_comment_edit", comment)
            assert_modules(
                "root_comment_edit", comment_observation,
                reused=EXPECTED_MODULES, linked=set(), invalidated=set(),
            )
            if (comment.get("status") != "verified"
                    or comment.get("artifact", {}).get("identity")
                    != cold.get("artifact", {}).get("identity")):
                raise AssertionError("root-only comment edit changed the executable artifact")
            if (not fact_set(comment_work, "reused_semantic_facts")
                    or fact_set(comment_work, "recomputed_semantic_facts")):
                raise AssertionError("root-only edit did not reuse its verified Language body facts")
            comment_parse = source_parse_observation(comment_work)
            if (not comment_parse.get("reused_source_identities")
                    or not comment_parse.get("reused_same_significant_tokens_source_identities")
                    or comment_parse.get("parsed_source_identities")
                    or comment_parse.get("published_entry_count") != 0):
                raise AssertionError(
                    "root-only comment edit did not reuse exact imported parses and the root syntax tree "
                    f"without parsing or publishing a new source entry: {comment_parse}"
                )
            stages["root_comment_edit"] = {
                "cache_status": comment.get("cache", {}).get("status"),
                "build_metrics": comment_metrics,
                "module_observation": comment_observation,
                "verification_work": comment_work,
                "execution": assert_execution_equivalent(
                    probe, "root-comment", comment, comment_root, modules, argument
                ),
            }

            changed_modules = dict(modules)
            original_assertions = changed_modules["mncs.test.assertions"]
            changed_modules["mncs.test.assertions"] = original_assertions.replace(
                "assertions: 1,", "assertions: 2,", 1
            )
            if changed_modules["mncs.test.assertions"] == original_assertions:
                raise AssertionError("test fixture no longer contains the selected imported body")
            changed, changed_metrics = probe.send(
                {"compile_application": request(comment_root, changed_modules)}, timeout=180
            )
            changed_work, changed_observation = work_for("imported_body_edit", changed)
            assert_modules(
                "imported_body_edit", changed_observation,
                reused={"mncs.std.application.v1", "mncs.test.suite"},
                linked={"mncs.test.assertions"},
                invalidated={"mncs.test.assertions"},
            )
            if changed.get("status") != "verified":
                raise AssertionError(f"imported body edit failed verification: {changed.get('diagnostics')}")
            changed_body = "mncs:0.2:body:mncs.test.assertions::from_assertion"
            if (changed_body not in fact_set(changed_work, "recomputed_semantic_facts")
                    or changed_body in fact_set(changed_work, "reused_semantic_facts")
                    or not fact_set(changed_work, "reused_semantic_facts")):
                raise AssertionError(
                    "imported body edit did not reverify the changed function while reusing independent facts"
                )
            stages["imported_body_edit"] = {
                "cache_status": changed.get("cache", {}).get("status"),
                "build_metrics": changed_metrics,
                "module_observation": changed_observation,
                "verification_work": changed_work,
                "execution": assert_execution_equivalent(
                    probe, "imported-body-edit", changed, comment_root,
                    changed_modules, argument,
                ),
            }

            # The real digest root constructs TestResult values and folds them
            # through mncs.test.suite. Add one unused nominal field, update its
            # constructors, then verify the actual application still behaves
            # the same while Language invalidates the assertion/suite closure.
            assertion_source = modules["mncs.test.assertions"]
            test_result_record = (
                "record TestResult {\n"
                "    verdict: Verdict,\n"
                "    verdict_code: u64,\n"
                "    failure_kind: FailureKind,\n"
                "    failure_code: u64,\n"
                "    assertions: u64,\n"
                "    failures: u64,\n"
                "    expected: i64,\n"
                "    actual: i64,\n"
                "    assertion_code: u64\n"
                "}"
            )
            extended_test_result_record = test_result_record.replace(
                "    assertion_code: u64\n",
                "    assertion_code: u64,\n    extension_identity: u64\n",
                1,
            )
            nominal_assertions, record_count = assertion_source.replace(
                test_result_record, extended_test_result_record, 1
            ), assertion_source.count(test_result_record)
            nominal_assertions, assertion_ctor_count = re.subn(
                r"(?m)^([ \t]*)assertion_code: assertion\.code$",
                r"\1assertion_code: assertion.code,\n\1extension_identity: 0",
                nominal_assertions,
            )
            nominal_assertions, zero_ctor_count = re.subn(
                r"(?m)^([ \t]*)assertion_code: 0$",
                r"\1assertion_code: 0,\n\1extension_identity: 0",
                nominal_assertions,
            )
            nominal_root_marker = (
                "        assertion_code: test.assertion_code\n"
                "    };\n}\n\nfn to_failure"
            )
            nominal_root = root_source.replace(
                nominal_root_marker,
                "        assertion_code: test.assertion_code,\n"
                "        extension_identity: 0\n"
                "    };\n}\n\nfn to_failure",
                1,
            )
            if (record_count != 1 or assertion_ctor_count != 2 or zero_ctor_count != 3
                    or nominal_assertions == assertion_source
                    or nominal_root == root_source):
                raise AssertionError(
                    "digest fixture no longer has the expected TestResult declaration/constructors"
                )
            nominal_modules = dict(modules)
            nominal_modules["mncs.test.assertions"] = nominal_assertions
            nominal = run_isolated_module_edit(
                probe_binary,
                directory,
                "imported_nominal_field_addition",
                root_source,
                modules,
                nominal_root,
                nominal_modules,
                reused_modules={"mncs.std.application.v1"},
                linked_modules={"mncs.test.assertions", "mncs.test.suite"},
                invalidated_modules={"mncs.test.assertions", "mncs.test.suite"},
                recomputed_subject_suffixes={
                    "mncs.test.digest::to_result",
                    "mncs.test.digest::build_digest",
                },
                argument=argument,
            )
            stages["imported_nominal_cold"] = nominal["cold"]
            stages["imported_nominal_field_addition"] = nominal["edited"]

            # Renaming an imported function's result binding preserves runtime
            # values but changes its verified interface. build_digest calls
            # this function directly, so its Language body fact must be fresh.
            suite_source = modules["mncs.test.suite"]
            suite_signature = (
                "fn observe(summary: SuiteSummary, test: TestResult) "
                "-> (result: SuiteSummary)"
            )
            changed_suite_source = suite_source.replace(
                suite_signature,
                "fn observe(summary: SuiteSummary, test: TestResult) "
                "-> (observation: SuiteSummary)",
                1,
            )
            if changed_suite_source == suite_source:
                raise AssertionError("test fixture no longer exposes the selected imported signature")
            signature_modules = dict(modules)
            signature_modules["mncs.test.suite"] = changed_suite_source
            signature = run_isolated_module_edit(
                probe_binary,
                directory,
                "imported_function_result_name_edit",
                root_source,
                modules,
                root_source,
                signature_modules,
                reused_modules={"mncs.std.application.v1", "mncs.test.assertions"},
                linked_modules={"mncs.test.suite"},
                invalidated_modules={"mncs.test.suite"},
                recomputed_subject_suffixes={"mncs.test.digest::build_digest"},
                argument=argument,
            )
            stages["imported_signature_cold"] = signature["cold"]
            stages["imported_function_result_name_edit"] = signature["edited"]

            # Force a real generic specialization in the same multi-module
            # application, then change its implementation while preserving
            # the digest root's observable behavior. The specialization's
            # own verified body must be recomputed while independent facts
            # remain reusable.
            generic_suite = modules["mncs.test.suite"]
            generic_body = "    return summary;\n}"
            changed_generic_suite, generic_body_count = generic_suite.replace(
                generic_body,
                "    return verdict_for(summary);\n}",
                1,
            ), generic_suite.count(generic_body)
            if generic_body_count != 1 or changed_generic_suite == generic_suite:
                raise AssertionError("test suite no longer exposes the selected generic body")
            generic_modules = dict(modules)
            generic_modules["mncs.test.suite"] = changed_generic_suite
            generic_specialization = run_isolated_module_edit(
                probe_binary,
                directory,
                "generic_summarize_body_edit",
                root_source,
                modules,
                root_source,
                generic_modules,
                reused_modules={"mncs.std.application.v1", "mncs.test.assertions"},
                linked_modules={"mncs.test.suite"},
                invalidated_modules={"mncs.test.suite"},
                recomputed_subject_suffixes={"mncs.test.suite::summarize"},
                argument=argument,
                generic_seeds=GENERIC_SUMMARIZE_SEED,
            )
            generic_edited = generic_specialization["edited"]
            generic_recomputed = fact_set(
                generic_edited["verification_work"], "recomputed_semantic_facts"
            )
            generic_reused = fact_set(
                generic_edited["verification_work"], "reused_semantic_facts"
            )
            if not any(
                subject.endswith("mncs.test.suite::summarize")
                for subject in generic_recomputed
            ):
                raise AssertionError(
                    "generic template body was not reverified: "
                    f"{sorted(generic_recomputed)}"
                )
            if not any(
                "mncs.test.suite::summarize__spec_" in subject
                for subject in generic_recomputed
            ):
                raise AssertionError(
                    "generic specialization body was not reverified: "
                    f"{sorted(generic_recomputed)}"
                )
            if not any(
                subject.endswith("mncs.test.digest::digest_app")
                for subject in generic_reused
            ):
                raise AssertionError(
                    "independent effectful root body was not reused: "
                    f"{sorted(generic_reused)}"
                )
            if generic_edited["artifact_identity"] == generic_specialization["cold"]["artifact_identity"]:
                raise AssertionError(
                    "changed generic specialization retained the prior executable artifact"
                )
            stages["generic_specialization_cold"] = generic_specialization["cold"]
            stages["generic_summarize_body_edit"] = generic_edited

            # Keep the tamper-negative case isolated so its deliberately
            # corrupted receipt cannot poison the fresh-process reuse case.
            tampered_cache_root = directory / "tampered-application-cache"
            shutil.copytree(cache_root, tampered_cache_root)
            stored_path = cache_entry_path(tampered_cache_root, changed)
            entry = json.loads(stored_path.read_text(encoding="utf-8"))
            if (entry.get("schema_version") != "mncs.compiler.application-build-cache-entry/12"
                    or not isinstance(entry.get("module_elaboration_cache_sha256"), str)):
                raise AssertionError("Compiler did not persist the checksummed Language module cache")
            entry["module_elaboration_cache_sha256"] = "sha256:" + "0" * 64
            stored_path.write_text(
                json.dumps(entry, sort_keys=True, separators=(",", ":")), encoding="utf-8"
            )
            tampered_probe = app.Probe(
                directory / "tampered-program-cache",
                executable=probe_binary,
                application_cache_dir=tampered_cache_root,
            )
            try:
                ready, _ = tampered_probe.send({"execution_status": True})
                if not isinstance(ready, dict) or "modules" not in ready:
                    raise AssertionError("tamper-negative process did not return its readiness handshake")
                tampered, tampered_metrics = tampered_probe.send(
                    {"compile_application": request(comment_root, changed_modules)}, timeout=180
                )
                tampered_work, tampered_observation = work_for("tampered_cache_digest", tampered)
                # Rejecting the outer executable-cache receipt runs a full
                # fresh frontend because its nested Language cache digest no
                # longer authenticates the independently stored module cache.
                assert_modules(
                    "tampered_cache_digest", tampered_observation,
                    reused=set(), linked=EXPECTED_MODULES, invalidated=set(),
                )
                if tampered.get("status") != "verified":
                    raise AssertionError("tampered cache digest prevented a fresh correct build")
                stages["tampered_cache_digest"] = {
                    "cache_status": tampered.get("cache", {}).get("status"),
                    "build_metrics": tampered_metrics,
                    "module_observation": tampered_observation,
                    "verification_work": tampered_work,
                }
            finally:
                tampered_probe_tail = tampered_probe.close()
        finally:
            probe_tail = probe.close()

        fresh_probe = app.Probe(
            directory / "fresh-program-cache",
            executable=probe_binary,
            application_cache_dir=cache_root,
        )
        try:
            ready, _ = fresh_probe.send({"execution_status": True})
            if not isinstance(ready, dict) or "modules" not in ready:
                raise AssertionError("fresh process did not return its readiness handshake")
            fresh_root = comment_root + "\n// fresh process source snapshot\n"
            fresh, fresh_metrics = fresh_probe.send(
                {"compile_application": request(fresh_root, changed_modules)}, timeout=180
            )
            fresh_work, fresh_observation = work_for("fresh_process", fresh)
            assert_modules(
                "fresh_process", fresh_observation,
                reused=EXPECTED_MODULES, linked=set(), invalidated=set(),
            )
            if (fresh.get("status") != "verified"
                    or fresh.get("artifact", {}).get("identity")
                    != changed.get("artifact", {}).get("identity")):
                raise AssertionError("fresh process did not reuse the persisted module cache and artifact")
            if (not fact_set(fresh_work, "reused_semantic_facts")
                    or fact_set(fresh_work, "recomputed_semantic_facts")):
                raise AssertionError("fresh process did not reuse persisted Language verification facts")
            stages["fresh_process"] = {
                "cache_status": fresh.get("cache", {}).get("status"),
                "build_metrics": fresh_metrics,
                "module_observation": fresh_observation,
                "verification_work": fresh_work,
                "execution": assert_execution_equivalent(
                    fresh_probe, "fresh-process", fresh, fresh_root,
                    changed_modules, argument,
                ),
            }

            invalid_root = fresh_root.replace("module mncs.test.digest;", "module ;", 1)
            invalid, invalid_metrics = fresh_probe.send(
                {"compile_application": request(invalid_root, changed_modules)}, timeout=180
            )
            invalid_artifact = invalid.get("artifact") or {}
            if (invalid.get("status") == "verified"
                    or invalid_artifact.get("value") is not None
                    or not invalid.get("diagnostics")):
                raise AssertionError("invalid current source was masked by an earlier successful cache entry")
            stages["invalid_source"] = {
                "status": invalid.get("status"),
                "diagnostic_count": len(invalid.get("diagnostics", [])),
                "artifact_published": invalid_artifact.get("value") is not None,
                "build_metrics": invalid_metrics,
            }
            fresh_probe_tail = fresh_probe.close()
        except BaseException:
            if fresh_probe.proc.poll() is None:
                fresh_probe.proc.kill()
                fresh_probe.proc.wait(timeout=5)
            raise

    language_lock = json.loads((ROOT / "mncs-language.lock.json").read_text(encoding="utf-8"))
    tracked_patch = subprocess.check_output(
        ["git", "-C", str(ROOT), "diff", "--binary", "HEAD"],
    )
    print(json.dumps({
        "identity": "ev_compiler_digest_application_generic_specialization_invalidation_20261023",
        "schema_version": "mncs.compiler.language-module-elaboration-cache-regression/1",
        "compiler_head": digest_app.git(ROOT, "rev-parse", "HEAD"),
        "compiler_tracked_patch_sha256": hashlib.sha256(tracked_patch).hexdigest(),
        "test_harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "language_head": language_lock["revision"],
        "test_head": digest_app.git(digest_app.TEST_ROOT, "rev-parse", "HEAD"),
        "temporary_root": tempfile.gettempdir(),
        "bootstrap_target": os.environ["MNCS_BOOTSTRAP_TARGET_DIR"],
        "source_module_count": len(EXPECTED_MODULES) + 1,
        "actual_language_modules_reused": True,
        "generic_specialization_seed": GENERIC_SUMMARIZE_SEED,
        "canonical_vm_binary": str(app.VM_BIN),
        "canonical_vm_binary_sha256": hashlib.sha256(app.VM_BIN.read_bytes()).hexdigest(),
        "compiler_probe_tail": probe_tail,
        "tampered_probe_tail": tampered_probe_tail,
        "fresh_probe_tail": fresh_probe_tail,
        "stages": {name: stage_summary(row) for name, row in stages.items()},
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
