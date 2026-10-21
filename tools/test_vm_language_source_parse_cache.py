#!/usr/bin/env python3
"""Prove Language source parse-cache reuse through application-build/11."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ensure_locked_stage0_probe import ensure_locked_stage0_probe

ROOT = Path(__file__).resolve().parents[1]
os.environ["MNCS_NATIVE_APP_PROBE_BIN"] = str(ensure_locked_stage0_probe(ROOT))
os.environ.setdefault("MNCS_PROBE_BIN", os.environ["MNCS_NATIVE_APP_PROBE_BIN"])
import test_vm_native_application as app

REQUEST_SCHEMA = "mncs.compiler.application-build-request/8"
RESPONSE_SCHEMA = "mncs.compiler.application-build/11"
WORK_SCHEMA = "mncs.compiler.application-build-verification-work/6"
PARSE_CACHE_SCHEMA = "mncs.language-source-parse-cache/1"
PARSE_OBSERVATION_SCHEMA = "mncs.language-source-parse-cache-observation/1"
ROOT_ID = "cache/application_root.mncs"
ROOT_MODULE = "cache.application_root"
DEP_MODULE = "cache.dependency"
ROOT_SOURCE = f"""mncs 0.19;
module {ROOT_MODULE};
use {DEP_MODULE};
fn run() -> (result: u64) {{ return value(); }}
"""

EVIDENCE = Path(os.environ.get(
    "MNCS_SOURCE_PARSE_CACHE_EVIDENCE_PATH",
    ROOT / "evidence" / (
        f"language-source-parse-cache-application-"
        f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S.%fZ}.json"
    ),
)).expanduser()
if not EVIDENCE.is_absolute():
    EVIDENCE = ROOT / EVIDENCE

COMPILER_SOURCE_PATHS = (
    ".mncs/project.json",
    "mncs-language.lock.json",
    "tools/application_build_provider.py",
    "tools/stage0-probe/src/application.rs",
    "tools/test_application_build_provider.py",
    "tools/test_vm_language_source_parse_cache.py",
)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request_for(root: str, dependency: str) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA,
        "target": "mncs-vm-0.1",
        "root_source_id": ROOT_ID,
        "root_source": root,
        "callable_roots": [{"module": ROOT_MODULE, "function": "run"}],
        "test_selection": {"kind": "all_source_cases"},
        "modules": {DEP_MODULE: dependency},
        "generic_seeds": [],
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def dependency(value: int) -> str:
    return (
        f"mncs 0.19; module {DEP_MODULE}; "
        f"fn value() -> (result: u64) {{ return {value}; }}"
    )


def require_work(label: str, response: dict) -> tuple[dict, dict, dict]:
    if response.get("schema_version") != RESPONSE_SCHEMA:
        raise AssertionError(f"{label} returned {response.get('schema_version')!r}")
    cache = response.get("cache", {})
    work = cache.get("verification_work", {})
    parse = work.get("source_parse_cache", {})
    observation = parse.get("observation")
    if (work.get("schema_version") != WORK_SCHEMA
            or parse.get("schema_version") != PARSE_CACHE_SCHEMA
            or parse.get("observation_schema_version") != PARSE_OBSERVATION_SCHEMA
            or (parse.get("status") == "observed"
                and (not isinstance(observation, dict)
                     or observation.get("schema_version") != PARSE_OBSERVATION_SCHEMA))
            or (parse.get("status") == "not_run" and observation is not None)
            or parse.get("status") not in {"observed", "not_run"}):
        raise AssertionError(f"{label} omitted the Language-owned parse observation: {work}")
    return cache, work, parse


def vm_call(artifact: dict) -> dict:
    value = artifact.get("artifact", {}).get("value")
    if not isinstance(value, dict):
        raise AssertionError("verified application did not include a canonical VM artifact")
    with tempfile.TemporaryDirectory(prefix="mncs-language-parse-cache-vm-") as raw:
        directory = Path(raw)
        artifact_path = directory / "artifact.json"
        args_path = directory / "args.json"
        types_path = directory / "type-args.json"
        artifact_path.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        args_path.write_text("[]", encoding="utf-8")
        types_path.write_text("[]", encoding="utf-8")
        completed = subprocess.run(
            [str(app.VM_BIN), "run", "--artifact", str(artifact_path),
             "--callable", f"{ROOT_MODULE}::run", "--args", str(args_path),
             "--type-args", str(types_path)],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    document = json.loads(completed.stdout)
    if document.get("outcome") != {"kind": "completed"}:
        raise AssertionError(f"canonical VM call did not complete: {document.get('outcome')}")
    return document


def oracle_call(probe: app.Probe, request: dict) -> list:
    call = {
        "schema_version": "0.1",
        "target": {"module": ROOT_MODULE, "function": "run"},
        "arguments": [],
        "type_arguments": [],
        "step_budget": 10000,
    }
    oracle, _ = probe.send({"project_oracle": {
        "root": request["root_source"],
        "modules": request["modules"],
        "calls": [call],
    }})
    if not oracle.get("valid") or not oracle.get("executions"):
        raise AssertionError(f"Stage-0 oracle rejected current sources: {oracle.get('diagnostics')}")
    execution = oracle["executions"][0]
    if execution.get("status") != "returned":
        raise AssertionError(f"Stage-0 oracle did not return: {execution}")
    return [app.decode_wire(row) for row in execution.get("returned", [])]


def compile_stage(label: str, request: dict, application_cache: Path,
                  program_cache: Path) -> dict:
    probe = app.Probe(
        program_cache,
        executable=Path(os.environ.get("MNCS_NATIVE_APP_PROBE_BIN", str(app.PROBE))),
        application_cache_dir=application_cache,
    )
    try:
        ready, _ = probe.send({"execution_status": True})
        if not isinstance(ready, dict) or "modules" not in ready:
            raise AssertionError(f"{label} process did not return its readiness handshake")
        response, metrics = probe.send({"compile_application": request}, timeout=180.0)
        cache, work, parse_observation = require_work(label, response)
        observation = parse_observation.get("observation")
        if parse_observation.get("status") == "observed":
            assert isinstance(observation, dict)
        if response.get("status") != "verified":
            raise AssertionError(f"{label} did not verify: {response.get('diagnostics')}")
        expected = oracle_call(probe, request)
    finally:
        probe.close()
    document = vm_call(response)
    returned = [app.decode_wire(app.vm_to_wire(row))
                for row in document.get("record", {}).get("returned", [])]
    if returned != expected:
        raise AssertionError(f"{label} VM differs from Stage-0: {returned!r} != {expected!r}")
    return {
        "response": response,
        "cache": cache,
        "verification_work": work,
        "parse_cache_status": parse_observation.get("status"),
        "parse_observation": observation,
        "build_metrics": metrics,
        "stage0_returned": expected,
        "vm_returned": returned,
        "vm_artifact_id": document.get("record", {}).get("artifact_id"),
        "stage0_vm_equal": True,
    }


def summarize(stage: dict) -> dict:
    observation = stage["parse_observation"]
    work = stage["verification_work"]
    return {
        "cache_status": stage["cache"].get("status"),
        "parse_cache_status": stage["parse_cache_status"],
        "frontend_invocations": work.get("front_end_invocations"),
        "parsed_source_identities": observation.get("parsed_source_identities") if observation else None,
        "reused_source_identities": observation.get("reused_source_identities") if observation else None,
        "rejected_entry_count": observation.get("rejected_entry_count") if observation else None,
        "function_facts_reused": len(work.get("reused_semantic_facts", [])),
        "function_facts_recomputed": len(work.get("recomputed_semantic_facts", [])),
        "build_metrics": stage["build_metrics"],
        "vm_artifact_id": stage["vm_artifact_id"],
        "stage0_returned": stage["stage0_returned"],
        "stage0_vm_equal": stage["stage0_vm_equal"],
    }


def main() -> None:
    if not app.PROBE.is_file() or not app.VM_BIN.is_file():
        raise SystemExit(f"Stage-0 probe or canonical VM is missing: {app.PROBE}, {app.VM_BIN}")

    with tempfile.TemporaryDirectory(prefix=".language-source-parse-cache-", dir=ROOT) as raw:
        base = Path(raw)
        application_cache = base / "application-cache"
        program_cache = base / "program-cache"
        cold = compile_stage("cold", request_for(ROOT_SOURCE, dependency(42)),
                             application_cache, program_cache)
        cold_observation = cold["parse_observation"]
        assert isinstance(cold_observation, dict)
        cold_parsed = set(cold_observation.get("parsed_source_identities", []))
        if (cold["cache"].get("status") != "miss" or len(cold_parsed) != 2
                or cold_observation.get("published_entry_count") != 2):
            raise AssertionError(
                f"cold build did not parse and publish both source units: {cold_observation}"
            )

        warm = compile_stage("fresh_process_exact_hit", request_for(ROOT_SOURCE, dependency(42)),
                             application_cache, program_cache)
        if (warm["cache"].get("status") != "hit"
                or warm["verification_work"].get("front_end_invocations") != 0
                or warm["parse_cache_status"] != "not_run"
                or warm["parse_observation"] is not None
                or warm["verification_work"].get("reused_semantic_facts")):
            raise AssertionError("exact artifact hit claimed Language parse or fact reuse")

        comment_source = "// comment-only source revision\n" + ROOT_SOURCE
        comment = compile_stage("comment_edit", request_for(comment_source, dependency(42)),
                                application_cache, program_cache)
        comment_observation = comment["parse_observation"]
        assert isinstance(comment_observation, dict)
        comment_reused = set(comment_observation.get("reused_source_identities", []))
        comment_parsed = set(comment_observation.get("parsed_source_identities", []))
        if (comment["verification_work"].get("front_end_invocations") != 1
                or len(comment_reused) != 1 or not comment_reused <= cold_parsed
                or len(comment_parsed) != 1 or comment_parsed & cold_parsed):
            raise AssertionError("comment edit did not reuse the unchanged imported parse result")
        if not comment["verification_work"].get("reused_semantic_facts"):
            raise AssertionError("comment edit did not reuse verified Language function facts")

        changed = compile_stage("dependency_edit", request_for(comment_source, dependency(43)),
                                application_cache, program_cache)
        changed_observation = changed["parse_observation"]
        assert isinstance(changed_observation, dict)
        changed_reused = set(changed_observation.get("reused_source_identities", []))
        changed_parsed = set(changed_observation.get("parsed_source_identities", []))
        if (changed_reused != comment_parsed or len(changed_parsed) != 1
                or changed_parsed & (cold_parsed | comment_parsed)):
            raise AssertionError("dependency edit did not isolate parse invalidation to its source")
        if (comment["vm_returned"] != [42] or changed["vm_returned"] != [43]
                or comment["vm_artifact_id"] == changed["vm_artifact_id"]):
            raise AssertionError("changed imported semantics reused a stale executable artifact")

        invalid_root_source = comment_source.replace(
            "return value();", "return absent();", 1
        )
        if invalid_root_source == comment_source:
            raise AssertionError("invalid root fixture failed to change the call")
        probe = app.Probe(program_cache, executable=app.PROBE,
                          application_cache_dir=application_cache)
        try:
            invalid_root, _ = probe.send({
                "compile_application": request_for(invalid_root_source, dependency(43)),
            }, timeout=180.0)
        finally:
            probe.close()
        invalid_root_parse = (
            invalid_root.get("cache", {}).get("verification_work", {})
            .get("source_parse_cache", {}).get("observation", {})
        ) or {}
        root_diagnostics = invalid_root.get("diagnostics", [])
        root_location = (root_diagnostics[0].get("span")
                         if root_diagnostics and isinstance(root_diagnostics[0], dict)
                         else None)
        if (invalid_root.get("schema_version") != RESPONSE_SCHEMA
                or invalid_root.get("status") != "rejected"
                or invalid_root.get("artifact") is not None
                or len(root_diagnostics) == 0
                or not isinstance(root_location, dict)
                or root_location.get("line") != 5
                or set(invalid_root_parse.get("reused_source_identities", []))
                != changed_parsed):
            raise AssertionError(
                "comment-shifted invalid root did not retain fresh diagnostic locations "
                "while reusing the unchanged dependency parse"
            )

        invalid_request = request_for(comment_source, dependency(43).replace("return 43", "return absent"))
        probe = app.Probe(program_cache, executable=app.PROBE,
                          application_cache_dir=application_cache)
        try:
            invalid, _ = probe.send({"compile_application": invalid_request}, timeout=180.0)
        finally:
            probe.close()
        if invalid.get("status") != "rejected" or not invalid.get("diagnostics"):
            raise AssertionError("invalid fresh dependency was masked by earlier verified results")
        if invalid.get("artifact") is not None:
            raise AssertionError("invalid dependency unexpectedly produced an executable artifact")

        evidence = {
            "schema_version": "mncs.compiler.language-source-parse-cache-application-evidence/1",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "compiler_source_sha256": {
                relative: sha256_file(ROOT / relative) for relative in COMPILER_SOURCE_PATHS
            },
            "stage0_probe_sha256": sha256_file(app.PROBE),
            "canonical_vm_sha256": sha256_file(app.VM_BIN),
            "language_lock": json.loads((ROOT / "mncs-language.lock.json").read_text()),
            "stages": {
                "cold": summarize(cold),
                "fresh_process_exact_hit": summarize(warm),
                "comment_edit": summarize(comment),
                "dependency_edit": summarize(changed),
            },
            "source_location_refresh": {
                "invalid_root_rejected": True,
                "diagnostic_code": root_diagnostics[0].get("code"),
                "diagnostic_span": root_location,
                "expected_line_after_comment_edit": 5,
                "unchanged_dependency_parse_reused": True,
                "artifact_publication": "not_published",
            },
            "invalid_dependency_rejected": True,
            "parse_cache_contract": PARSE_CACHE_SCHEMA,
            "parse_observation_contract": PARSE_OBSERVATION_SCHEMA,
        }
        EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(json.dumps({"evidence": str(EVIDENCE), "stages": evidence["stages"],
                      "invalid_dependency_rejected": True}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
