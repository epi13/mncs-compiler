#!/usr/bin/env python3
"""Build full Compiler source modules at Profile 0.19 and verify fact reuse."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
LANGUAGE_FACT_SCHEMA = "mncs.language-verification-fact/3"
LANGUAGE_FACT_VERIFIER_ID = "mncs-language-function-body-validator/4"
sys.path.insert(0, str(ROOT / "tools"))
from ensure_locked_stage0_probe import ensure_locked_stage0_probe  # noqa: E402

# This frontier records the Stage-0 implementation pinned by this repository.
# A process-wide probe override can point at a stale linked-worktree build and
# make dependency-invalidation failures look like Language regressions, so use
# the lock-checked bootstrap path for this workload.
os.environ.pop("MNCS_NATIVE_APP_PROBE_BIN", None)
os.environ.pop("MNCS_PROBE_BIN", None)
os.environ["MNCS_NATIVE_APP_PROBE_BIN"] = str(ensure_locked_stage0_probe(ROOT))
import test_cp0001 as cp0001  # noqa: E402
import test_vm_native_application as app  # noqa: E402
import test_vm_emit  # noqa: E402
import test_vm_segment  # noqa: E402

MODULE_FILES = {
    "mncs.compiler.source.v1": ROOT / "src/compiler/source.mncs",
    "mncs.compiler.lexer.v1": ROOT / "src/compiler/lexer.mncs",
    "mncs.compiler.segment.v1": ROOT / "src/compiler/segment.mncs",
}
ROOT_MODULE = "tests.compiler_source_frontier"
ROOT_SOURCE_ID = "self-host/compiler-source-frontier.mncs"
ROOT_SOURCE = """mncs 0.19;
module tests.compiler_source_frontier;
use mncs.compiler.segment.v1 as segment;
use mncs.compiler.source.v1 as source;

fn check_span(total: u64, start: u64, end: u64) -> (result: bool) {
    return segment.span_valid_global(total, start, end);
}

fn check_ascii(pages: [[byte; up_to 8]; up_to 2], stride: u64, total: u64) -> (result: bool) {
    return source.ascii_global<2, 8>(pages, stride, total);
}
"""
CHANGED_FUNCTION = "mncs:0.2:body:mncs.compiler.source.v1::utf8_first_continuation_valid"
EXPECTED_BODY_RECOMPUTED = {
    CHANGED_FUNCTION,
    "mncs:0.2:body:mncs.compiler.source.v1::utf8_scalar",
    "mncs:0.2:body:mncs.compiler.source.v1::utf8_scalar_global",
    "mncs:0.2:body:mncs.compiler.source.v1::utf8_validation_step",
    "mncs:0.2:body:mncs.compiler.source.v1::validate_utf8",
    "mncs:0.2:body:mncs.compiler.source.v1::validate_utf8_global",
    "mncs:0.2:body:mncs.compiler.lexer.v1::batch_step",
    "mncs:0.2:body:mncs.compiler.lexer.v1::lex_tokens_from",
    "mncs:0.2:body:mncs.compiler.lexer.v1::next_token",
    "mncs:0.2:body:mncs.compiler.lexer.v1::next_token_global",
    "mncs:0.2:body:mncs.compiler.lexer.v1::scan_block_comment_scalar_global",
    "mncs:0.2:body:mncs.compiler.lexer.v1::scan_line_comment_global",
    "mncs:0.2:body:mncs.compiler.lexer.v1::scan_line_comment_window",
    "mncs:0.2:body:mncs.compiler.lexer.v1::scan_step",
    "mncs:0.2:body:mncs.compiler.lexer.v1::scan_unicode_scalar_global",
    "mncs:0.2:body:mncs.compiler.lexer.v1::significant",
    "mncs:0.2:body:mncs.compiler.lexer.v1::significant_global",
    "mncs:0.2:body:mncs.compiler.lexer.v1::skip_trivia",
    "mncs:0.2:body:mncs.compiler.lexer.v1::skip_trivia_global",
    "mncs:0.2:body:mncs.compiler.segment.v1::lex_batch_from",
    "mncs:0.2:body:mncs.compiler.segment.v1::next_token",
    "mncs:0.2:body:mncs.compiler.segment.v1::next_token_global",
    "mncs:0.2:body:mncs.compiler.segment.v1::significant",
    "mncs:0.2:body:mncs.compiler.segment.v1::significant_global",
}
CHANGED_INTERFACE_FUNCTION = "mncs:0.2:body:mncs.compiler.source.v1::byte_at_global"
REUSED_EXECUTION_PATH = {
    "mncs:0.2:body:mncs.compiler.source.v1::span_valid_global",
    "mncs:0.2:body:mncs.compiler.segment.v1::span_valid_global",
    f"mncs:0.2:body:{ROOT_MODULE}::check_span",
    f"mncs:0.2:body:{ROOT_MODULE}::check_ascii",
}


def u64(value: int) -> dict:
    return cp0001.integer(value)


def vm_repository_for_binary(binary: Path) -> tuple[Path, str]:
    """Report the checkout that contains the exact VM binary under test."""
    resolved = binary.resolve(strict=True)
    root = Path(subprocess.check_output(
        ["git", "-C", str(resolved.parent), "rev-parse", "--show-toplevel"],
        text=True,
    ).strip()).resolve()
    if root not in resolved.parents:
        raise AssertionError(f"VM binary is outside its reported Git checkout: {resolved}")
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
    ).strip()
    return root, revision


def cases() -> list[dict]:
    rows = [
        ("valid-span", "check_span", [u64(8), u64(2), u64(8)]),
        ("reversed-span", "check_span", [u64(8), u64(7), u64(3)]),
        ("past-end-span", "check_span", [u64(8), u64(2), u64(9)]),
    ]
    for case_id, raw, expected in (
        ("ascii-pages", b"ABCDEFGH", True),
        ("non-ascii-page", b"ABC\xffDEFG", False),
        ("empty-ascii", b"", True),
    ):
        stride = 4
        rows.append((case_id, "check_ascii", [
            cp0001.pages_value(cp0001.chunk(raw, stride)), u64(stride), u64(len(raw)),
        ]))
    return [{"id": case_id, "function": function, "arguments": arguments}
            for case_id, function, arguments in rows]


def request(modules: dict[str, str], root_source: str = ROOT_SOURCE) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": ROOT_SOURCE_ID,
        "root_source": root_source,
        "callable_roots": [
            {"module": ROOT_MODULE, "function": "check_span"},
            {"module": ROOT_MODULE, "function": "check_ascii"},
        ],
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "compiler-full-module-profile-019-fact-reuse/1",
    }


def load_modules() -> dict[str, str]:
    modules = {name: path.read_text() for name, path in MODULE_FILES.items()}
    if any(not source.startswith("mncs 0.19;") for source in modules.values()):
        raise AssertionError("Compiler self-consumption modules must declare Profile 0.19")
    return modules


def diagnostic_tree(rows: list[dict]) -> list[dict]:
    found = []
    for row in rows:
        if isinstance(row, dict):
            found.append({"code": row.get("code"), "message": row.get("message")})
            found.extend(diagnostic_tree(row.get("related", [])))
    return found


def profile_018_attempt(cache: Path, modules: dict[str, str]) -> tuple[dict, dict]:
    baseline = {name: source.replace("mncs 0.19;", "mncs 0.18;", 1)
                for name, source in modules.items()}
    baseline_root = ROOT_SOURCE.replace("mncs 0.19;", "mncs 0.18;", 1)
    probe = app.Probe(cache_dir=cache / "probe-cache",
                      application_cache_dir=cache / "application-cache")
    try:
        build, metrics = probe.send({"compile_application": request(baseline, baseline_root)},
                                    timeout=300.0)
    finally:
        probe.close()
    if build.get("status") == "verified":
        return {"status": "verified_under_profile_018", "diagnostic_count": 0}, metrics
    diagnostics = diagnostic_tree(build.get("diagnostics", []))
    profile_rows = [row for row in diagnostics
                    if row.get("code") == "MNE182"
                    and "profile ceiling 1024" in str(row.get("message"))]
    if build.get("status") != "rejected" or not profile_rows:
        raise AssertionError("Profile 0.18 baseline failed for an unexpected reason: "
                             + json.dumps(diagnostics[:20], indent=2)[:5000])
    return {
        "status": "rejected_by_profile_018_traversal_bound",
        "diagnostic_count": len(diagnostics),
        "profile_bound_diagnostic_count": len(profile_rows),
        "sample_diagnostic": profile_rows[0]["message"],
    }, metrics


def verification_work(build: dict, label: str) -> dict:
    cache = build.get("cache", {})
    work = cache.get("verification_work", {})
    if (cache.get("schema_version") != "mncs.compiler.application-build-cache-observation/4"
            or work.get("schema_version") != "mncs.compiler.application-build-verification-work/2"):
        raise AssertionError(f"{label} omitted versioned verification-work telemetry")
    if work.get("front_end_invocations") != 1 or work.get("front_end_outcome") != "verified":
        raise AssertionError(f"{label} did not freshly verify the changed source snapshot: {work}")
    if work.get("function_body_verification") != "recomputed_with_language_fact_reuse":
        raise AssertionError(f"{label} did not report Language-owned body reuse: {work}")
    if (not isinstance(work.get("reused_semantic_facts"), list)
            or not isinstance(work.get("recomputed_semantic_facts"), list)):
        raise AssertionError(f"{label} omitted actual Language fact identities")
    if work.get("language_verification_fact_schema") != LANGUAGE_FACT_SCHEMA:
        raise AssertionError(f"{label} reported an unexpected Language fact schema: {work}")
    return work


def compile_attempt(request_value: dict, cache: Path) -> tuple[dict, dict]:
    probe = app.Probe(cache_dir=cache / "probe-cache",
                      application_cache_dir=cache / "application-cache")
    try:
        build, metrics = probe.send({"compile_application": request_value}, timeout=300.0)
    finally:
        probe.close()
    return build, metrics


def compile_fresh(request_value: dict, cache: Path, label: str) -> tuple[dict, dict]:
    build, metrics = compile_attempt(request_value, cache)
    if (build.get("status") != "verified"
            or build.get("schema_version") != "mncs.compiler.application-build/5"
            or build.get("unresolved_obligations") != []):
        raise AssertionError(f"{label} did not produce a verified application: "
                             + json.dumps({k: build.get(k) for k in
                                           ("status", "schema_version", "diagnostics",
                                            "unresolved_obligations")}, indent=2)[:5000])
    artifact = build.get("artifact", {}).get("value")
    if not isinstance(artifact, dict) or artifact.get("schema_version") != "mncs.vm.artifact/1":
        raise AssertionError(f"{label} omitted its canonical VM artifact")
    return build, metrics


def persisted_fact_verifier_identities(cache: Path, build: dict, label: str) -> list[str]:
    cache_key = build.get("cache", {}).get("key")
    if not isinstance(cache_key, str) or not cache_key.startswith("sha256:"):
        raise AssertionError(f"{label} did not report its published Language fact cache key")
    cache_file = (cache / "application-cache" / "application-builds"
                  / f"{cache_key.removeprefix('sha256:')}.json")
    if not cache_file.is_file():
        raise AssertionError(f"{label} did not publish its application fact cache: {cache_file}")
    entry = json.loads(cache_file.read_text())
    facts = entry.get("verification_facts")
    if entry.get("key") != cache_key or not isinstance(facts, list):
        raise AssertionError(f"{label} cache entry does not match its reported identity")
    serialized_facts = json.dumps(facts, separators=(",", ":"), ensure_ascii=False).encode()
    facts_digest = "sha256:" + hashlib.sha256(serialized_facts).hexdigest()
    if entry.get("verification_facts_sha256") != facts_digest:
        raise AssertionError(f"{label} persisted Language facts failed their cache digest")
    if len(facts) != build.get("cache", {}).get("verification_work", {}).get(
            "produced_semantic_fact_count"):
        raise AssertionError(f"{label} cache fact count differs from Language telemetry")
    if not facts or any(
            fact.get("schema_version") != LANGUAGE_FACT_SCHEMA
            or fact.get("result") != "verified"
            or not isinstance(fact.get("verification_witness_identity"), str)
            or len(fact["verification_witness_identity"]) != 64
            for fact in facts if isinstance(fact, dict)):
        raise AssertionError(f"{label} cache contains a non-verified or malformed Language fact")
    if any(not isinstance(fact, dict) for fact in facts):
        raise AssertionError(f"{label} cache contains a malformed Language fact row")
    identities = sorted({fact.get("verifier_identity") for fact in facts})
    if not identities or any(not isinstance(identity, str) for identity in identities):
        raise AssertionError(f"{label} facts do not identify their Language verifier")
    return identities


def source_edit(modules: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    comment = dict(modules)
    comment["mncs.compiler.source.v1"] += "\n// Incremental source snapshot comment edit.\n"
    edited = dict(comment)
    source = edited["mncs.compiler.source.v1"]
    start = source.index("fn utf8_first_continuation_valid(")
    brace = source.index("{", start)
    depth = 0
    end = None
    for offset in range(brace, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                end = offset + 1
                break
    if end is None:
        raise AssertionError("Compiler source edit fixture could not locate function body")
    original = source[start:end]
    if original.count("return true;") != 1:
        raise AssertionError("Compiler source edit fixture no longer matches utf8 validator")
    edited["mncs.compiler.source.v1"] = (
        source[:start] + original.replace("return true;", "return false;", 1) + source[end:]
    )
    return comment, edited


def interface_edit(modules: dict[str, str]) -> dict[str, str]:
    """Rename one imported API parameter while preserving positional behavior."""
    changed = dict(modules)
    source = changed["mncs.compiler.source.v1"]
    start = source.index("fn byte_at_global<")
    end = source.index("\n}\n", start) + 2
    function = source[start:end]
    if function.count("total") != 2 or "total: u64" not in function:
        raise AssertionError("Compiler source interface edit anchor changed")
    changed["mncs.compiler.source.v1"] = (
        source[:start] + function.replace("total", "source_length") + source[end:]
    )
    return changed


def invalid_body_edit(modules: dict[str, str]) -> dict[str, str]:
    """Introduce a fresh body type error while retaining prior successful facts."""
    changed = dict(modules)
    source = changed["mncs.compiler.source.v1"]
    start = source.index("fn byte_at_global<")
    end = source.index("\n}\n", start) + 2
    function = source[start:end]
    if (function.count("-> (result: u64)") != 1
            or function.count("return view[intra] as u64;") != 1):
        raise AssertionError("Compiler source invalid-body edit anchor changed")
    function = function.replace("return view[intra] as u64;", "return true;", 1)
    changed["mncs.compiler.source.v1"] = source[:start] + function + source[end:]
    return changed


def oracle(modules: dict[str, str], cache: Path, workload: list[dict]) -> list[list[dict]]:
    probe = app.Probe(cache_dir=cache / "probe-cache",
                      application_cache_dir=cache / "application-cache")
    try:
        response, _ = probe.send({"project_oracle": {
            "root": ROOT_SOURCE,
            "modules": modules,
            "calls": [{
                "schema_version": "0.1",
                "target": {"module": ROOT_MODULE, "function": row["function"]},
                "arguments": row["arguments"],
                "type_arguments": [],
                "step_budget": 1_000_000,
            } for row in workload],
        }}, timeout=300.0)
    finally:
        probe.close()
    if not response.get("valid"):
        raise AssertionError("Stage-0 oracle rejected Compiler modules: "
                             + json.dumps(response.get("diagnostics"), indent=2)[:5000])
    executions = response.get("executions", [])
    if len(executions) != len(workload) or any(row.get("status") != "returned" for row in executions):
        raise AssertionError("Stage-0 oracle did not return every Compiler frontier case")
    return [row["returned"] for row in executions]


def execute_vm(artifact: dict, temporary_root: Path, workload: list[dict]) -> tuple[list[list[dict]], dict]:
    artifact_path = temporary_root / "application.vm.json"
    calls_path = temporary_root / "calls.json"
    artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
    calls = [{"id": row["id"], "request": {
        "schema_version": "0.1",
        "target": {"module": ROOT_MODULE, "function": row["function"]},
        "arguments": row["arguments"],
        "type_arguments": [],
        "step_budget": 1_000_000,
    }} for row in workload]
    calls_path.write_text(json.dumps(calls, sort_keys=True) + "\n")
    test_vm_emit.ensure_vm()
    output_path = temporary_root / "results.json"
    started = time.monotonic()
    completed = subprocess.run([
        "/usr/bin/time", "-v", str(test_vm_emit.VM_BIN), "batch",
        "--artifact", str(artifact_path), "--calls", str(calls_path),
        "--output", str(output_path),
    ], capture_output=True, text=True)
    wall = time.monotonic() - started
    if completed.returncode:
        raise AssertionError("canonical VM application execution failed: "
                             + completed.stderr[-3000:])
    document = json.loads(output_path.read_text())
    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM receipt does not bind the selected artifact")
    rows = document.get("results", [])
    if len(rows) != len(workload):
        raise AssertionError("canonical VM omitted a selected Compiler callable")
    returned = []
    for case, result in zip(workload, rows):
        if result.get("id") != case["id"] or result.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM case failed: {case['id']}: {result.get('outcome')}")
        returned.append([test_vm_segment.vm_to_wire(value)
                         for value in result["record"]["returned"]])
    peak_rss_kib = next((int(line.split(":", 1)[1].strip().split()[0])
                         for line in completed.stderr.splitlines()
                         if "Maximum resident set size" in line), None)
    return returned, {"wall_seconds": round(wall, 6), "peak_rss_kib": peak_rss_kib,
                      "calls_bytes": calls_path.stat().st_size,
                      "results_bytes": output_path.stat().st_size}


def main() -> dict:
    if not app.PROBE.is_file() or not test_vm_emit.VM_BIN.is_file():
        raise SystemExit("campaign Stage-0 probe or canonical VM binary is missing")
    modules = load_modules()
    comment_modules, edited_modules = source_edit(modules)
    workload = cases()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-compiler-full-module-fact-reuse-") as cache_name:
        cache = Path(cache_name)
        profile_018, profile_018_metrics = profile_018_attempt(cache / "profile-018", modules)
        cold, cold_metrics = compile_fresh(request(modules), cache, "full-module cold build")
        cold_work = cold.get("cache", {}).get("verification_work", {})
        if (cold.get("cache", {}).get("status") != "miss"
                or cold.get("cache", {}).get("publication") != "stored"
                or cold_work.get("produced_semantic_fact_count", 0) <= 0
                or cold_work.get("front_end_outcome") != "verified"):
            raise AssertionError("Profile 0.19 full modules did not publish verified Language facts")
        cold_verifier_identities = persisted_fact_verifier_identities(
            cache, cold, "full-module cold build")

        comment_build, comment_metrics = compile_fresh(
            request(comment_modules), cache, "full-module comment edit")
        comment_verifier_identities = persisted_fact_verifier_identities(
            cache, comment_build, "full-module comment edit")
        comment_work = verification_work(comment_build, "full-module comment edit")
        if (not comment_work["reused_semantic_facts"]
                or comment_work["recomputed_semantic_facts"]):
            raise AssertionError("comment edit did not reuse every verified body fact")

        edited_build, edited_metrics = compile_fresh(
            request(edited_modules), cache, "full-module non-root helper body edit")
        edited_verifier_identities = persisted_fact_verifier_identities(
            cache, edited_build, "full-module non-root helper body edit")
        edited_work = verification_work(edited_build, "full-module non-root helper body edit")
        reused = set(edited_work["reused_semantic_facts"])
        recomputed = set(edited_work["recomputed_semantic_facts"])
        body_reasons = edited_work.get("semantic_fact_recomputation_reasons")
        if recomputed != EXPECTED_BODY_RECOMPUTED or not REUSED_EXECUTION_PATH.issubset(reused):
            raise AssertionError("full Compiler modules did not reverify the exact changed-body dependency closure: "
                                 + json.dumps({"recomputed": sorted(recomputed),
                                               "expected_recomputed": sorted(EXPECTED_BODY_RECOMPUTED),
                                               "recomputation_reasons": body_reasons,
                                               "missing_execution_path": sorted(
                                                   REUSED_EXECUTION_PATH - reused)}, indent=2))
        if not isinstance(body_reasons, list):
            raise AssertionError("changed-body verification omitted Language dependency invalidation reasons")
        reason_by_subject = {
            row.get("subject"): row.get("reason")
            for row in body_reasons if isinstance(row, dict)
        }
        if (set(reason_by_subject) != recomputed
                or reason_by_subject.get(CHANGED_FUNCTION) != "subject_changed"
                or any(reason != "dependencies_changed"
                       for subject, reason in reason_by_subject.items()
                       if subject != CHANGED_FUNCTION)):
            raise AssertionError("changed-body Language facts did not report exact subject and dependency invalidations: "
                                 + json.dumps(body_reasons, indent=2))
        if (cold.get("artifact", {}).get("identity")
                != comment_build.get("artifact", {}).get("identity")
                or cold.get("artifact", {}).get("identity")
                != edited_build.get("artifact", {}).get("identity")):
            raise AssertionError("comment or unreachable-body edit changed the executable closure")

        # Keep a separate cold fact set so the interface delta is compared to
        # the exact unedited three-module snapshot. The renamed input label is
        # part of the imported callable interface; all callsites remain
        # positional and the application semantics remain unchanged.
        interface_modules = interface_edit(modules)
        interface_cache = cache / "interface-cache"
        interface_baseline, interface_baseline_metrics = compile_fresh(
            request(modules), interface_cache, "interface-invalidation baseline")
        interface_baseline_verifier_identities = persisted_fact_verifier_identities(
            interface_cache, interface_baseline, "interface-invalidation baseline")
        interface_baseline_work = interface_baseline.get("cache", {}).get(
            "verification_work", {})
        if interface_baseline_work.get("produced_semantic_fact_count") != 103:
            raise AssertionError("interface baseline did not retain all 103 Language facts")
        interface_build, interface_metrics = compile_fresh(
            request(interface_modules), interface_cache, "imported interface edit")
        interface_verifier_identities = persisted_fact_verifier_identities(
            interface_cache, interface_build, "imported interface edit")
        interface_work = verification_work(interface_build, "imported interface edit")
        interface_recomputed = set(interface_work["recomputed_semantic_facts"])
        interface_reused = set(interface_work["reused_semantic_facts"])
        reasons = interface_work.get("semantic_fact_recomputation_reasons")
        if not isinstance(reasons, list):
            raise AssertionError("interface edit omitted Language fact invalidation reasons")
        reason_by_subject = {
            row.get("subject"): row.get("reason")
            for row in reasons if isinstance(row, dict)
        }
        if set(reason_by_subject) != interface_recomputed:
            raise AssertionError("Language omitted or misattributed a recomputation reason")
        expected_dependents = {
            "mncs:0.2:body:mncs.compiler.source.v1::utf8_scalar_global",
            "mncs:0.2:body:mncs.compiler.lexer.v1::keyword_global",
            "mncs:0.2:body:mncs.compiler.segment.v1::byte_at_global",
        }
        if reason_by_subject.get(CHANGED_INTERFACE_FUNCTION) != "subject_changed":
            raise AssertionError("changed exported function body was not freshly verified")
        if any(reason_by_subject.get(subject) != "dependencies_changed"
               for subject in expected_dependents):
            raise AssertionError("imported interface change did not invalidate its direct consumers: "
                                 + json.dumps(reason_by_subject, indent=2, sort_keys=True)[:6000])
        if not expected_dependents.issubset(interface_recomputed):
            raise AssertionError("imported interface consumers were not all reverified")
        independently_reused = {
            "mncs:0.2:body:mncs.compiler.source.v1::span_valid_global",
            "mncs:0.2:body:mncs.compiler.segment.v1::span_valid_global",
            f"mncs:0.2:body:{ROOT_MODULE}::check_span",
            f"mncs:0.2:body:{ROOT_MODULE}::check_ascii",
        }
        if (not independently_reused.issubset(interface_reused)
                or len(interface_recomputed) >= interface_baseline_work["produced_semantic_fact_count"]):
            raise AssertionError("interface edit failed to preserve independent verified facts")

        verifier_identities_by_snapshot = {
            "cold": cold_verifier_identities,
            "comment_edit": comment_verifier_identities,
            "body_edit": edited_verifier_identities,
            "interface_baseline": interface_baseline_verifier_identities,
            "interface_edit": interface_verifier_identities,
        }
        observed_verifier_identities = sorted({
            identity for identities in verifier_identities_by_snapshot.values()
            for identity in identities
        })
        if observed_verifier_identities != [LANGUAGE_FACT_VERIFIER_ID]:
            raise AssertionError("persisted facts do not identify the pinned Language verifier: "
                                 + json.dumps(verifier_identities_by_snapshot, indent=2))

        invalid_modules = invalid_body_edit(modules)
        invalid_cache = cache / "invalid-body-cache"
        invalid_baseline, _ = compile_fresh(
            request(modules), invalid_cache, "invalid-body baseline")
        if (invalid_baseline.get("cache", {}).get("verification_work", {}).get(
                "produced_semantic_fact_count") != 103):
            raise AssertionError("invalid-body baseline did not publish its prior facts")
        invalid_build, _ = compile_attempt(request(invalid_modules), invalid_cache)
        invalid_work = invalid_build.get("cache", {}).get("verification_work", {})
        invalid_diagnostics = diagnostic_tree(invalid_build.get("diagnostics", []))
        if (invalid_build.get("status") != "rejected"
                or not any(row.get("code") == "MNE103" for row in invalid_diagnostics)
                or invalid_work.get("front_end_invocations") != 1
                or invalid_work.get("front_end_outcome") != "rejected"
                or invalid_build.get("artifact") is not None
                or invalid_build.get("cache", {}).get("publication") != "not_published"):
            raise AssertionError("invalid body did not produce fresh frontend diagnostics without publication: "
                                 + json.dumps({"status": invalid_build.get("status"),
                                               "work": invalid_work,
                                               "diagnostics": invalid_diagnostics[:20]}, indent=2))
        invalid_recomputed = set(invalid_work.get("recomputed_semantic_facts", []))
        invalid_reused = set(invalid_work.get("reused_semantic_facts", []))
        if invalid_recomputed or invalid_reused or invalid_work.get("produced_semantic_fact_count") != 0:
            raise AssertionError("frontend-rejected source reused or published prior Language facts")

        expected = oracle(edited_modules, cache, workload)
        with tempfile.TemporaryDirectory(prefix="mncs-compiler-full-module-vm-") as vm_name:
            vm_values, vm_metrics = execute_vm(
                edited_build["artifact"]["value"], Path(vm_name), workload)
        if vm_values != expected:
            raise AssertionError("canonical VM differs from Stage-0: "
                                 + json.dumps({"vm": vm_values, "stage0": expected}, indent=2))

        interface_expected = oracle(interface_modules, interface_cache, workload)
        with tempfile.TemporaryDirectory(prefix="mncs-compiler-interface-vm-") as vm_name:
            interface_vm_values, interface_vm_metrics = execute_vm(
                interface_build["artifact"]["value"], Path(vm_name), workload)
        if interface_vm_values != interface_expected or interface_vm_values != vm_values:
            raise AssertionError("interface-only edit changed canonical VM semantics")

        language_lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
        vm_repository, vm_revision = vm_repository_for_binary(test_vm_emit.VM_BIN)
        result = {
            "schema_version": "mncs.compiler.self-consumption-full-module-fact-reuse/1",
            "status": "passed_full_source_lexer_segment_fact_reuse_frontier",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "compiler_branch": subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
            "stage0_revision": language_lock["revision"],
            "language_fact_implementation_revision": language_lock["revision"],
            "stage0_probe_sha256": hashlib.sha256(app.PROBE.read_bytes()).hexdigest(),
            "vm_revision": vm_revision,
            "vm_repository_root": str(vm_repository),
            "vm_binary_sha256": hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest(),
            "harness": "tools/test_vm_compiler_full_module_fact_reuse.py",
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "language_profile": "0.19",
            "profile_018_baseline": {**profile_018, "measurement": profile_018_metrics},
            "target_modules": [
                {"module": name, "source_path": str(path.relative_to(ROOT)),
                 "language_profile": "0.19",
                 "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for name, path in MODULE_FILES.items()
            ],
            "full_module_admission": {
                "status": "verified",
                "resolved_module_count": cold_work.get("resolved_module_count"),
                "program_function_count": cold_work.get("program_function_count"),
                "cold_verified_fact_count": cold_work["produced_semantic_fact_count"],
            },
            "application": {
                "callable_roots": ["check_span", "check_ascii"],
                "source_module_count": len(MODULE_FILES),
                "artifact_identity": edited_build["artifact"]["identity"],
                "artifact_json_bytes": len(json.dumps(
                    edited_build["artifact"]["value"], sort_keys=True,
                    separators=(",", ":")).encode("utf-8")),
                "canonical_vm_invocations": len(workload),
                "canonical_vm_matches_stage0": True,
                "returned_values": [row[0]["boolean"]["value"] for row in vm_values],
            },
            "verified_fact_reuse": {
                "contract": LANGUAGE_FACT_SCHEMA,
                "verifier_identity": observed_verifier_identities[0],
                "verifier_identities_by_snapshot": verifier_identities_by_snapshot,
                "cold_facts_produced": cold_work["produced_semantic_fact_count"],
                "comment_edit_reused": len(comment_work["reused_semantic_facts"]),
                "comment_edit_recomputed": len(comment_work["recomputed_semantic_facts"]),
                "body_edit_reused": len(reused),
                "body_edit_recomputed": sorted(recomputed),
                "body_edit_recomputation_reasons": body_reasons,
                "edited_subject": CHANGED_FUNCTION,
                "unchanged_execution_path_reused": sorted(REUSED_EXECUTION_PATH),
                "fresh_process_per_snapshot": True,
                "artifact_reused_for_unchanged_closure": True,
            },
            "imported_interface_invalidation": {
                "source_module": "mncs.compiler.source.v1",
                "exported_function": "byte_at_global",
                "edit": "renamed input total to source_length; positional callsites unchanged",
                "changed_subject": CHANGED_INTERFACE_FUNCTION,
                "changed_subject_reason": reason_by_subject[CHANGED_INTERFACE_FUNCTION],
                "dependency_changed_subjects": sorted(expected_dependents),
                "reused_independent_subjects": sorted(independently_reused),
                "all_recomputed_subjects": sorted(interface_recomputed),
                "all_recomputation_reasons": [
                    {"subject": subject, "reason": reason_by_subject[subject]}
                    for subject in sorted(reason_by_subject)
                ],
                "reused_fact_count": len(interface_reused),
                "recomputed_fact_count": len(interface_recomputed),
                "fresh_diagnostics": interface_build.get("diagnostics", []),
                "application_build_verified": interface_build.get("status") == "verified",
                "canonical_vm_matches_stage0": True,
            },
            "invalid_changed_body": {
                "source_module": "mncs.compiler.source.v1",
                "exported_function": "byte_at_global",
                "edit": "changed the u64 body return expression to a bool",
                "status": invalid_build.get("status"),
                "diagnostics": invalid_diagnostics,
                "expected_diagnostic_code": "MNE103",
                "front_end_invocations": invalid_work.get("front_end_invocations"),
                "front_end_outcome": invalid_work.get("front_end_outcome"),
                "language_fact_reuse_not_attempted_before_frontend_rejection": True,
                "produced_facts": invalid_work.get("produced_semantic_fact_count"),
                "reused_facts": sorted(invalid_reused),
                "recomputed_facts": sorted(invalid_recomputed),
                "cache_publication": invalid_build.get("cache", {}).get("publication"),
                "prior_success_did_not_mask_error": True,
            },
            "measurements": {
                "profile_018_attempt": profile_018_metrics,
                "cold_full_module_build": cold_metrics,
                "comment_edit_build": comment_metrics,
                "body_edit_build": edited_metrics,
                "canonical_vm_execution": vm_metrics,
                "interface_baseline_build": interface_baseline_metrics,
                "interface_edit_build": interface_metrics,
                "interface_canonical_vm_execution": interface_vm_metrics,
                "whole_frontier_wall_seconds": round(time.monotonic() - started, 6),
                "cgroup_measurement_available": cold_metrics.get("peak_cgroup_memory_bytes") is not None,
            },
            "limitations": [
                "This compiles and executes the full Source, Lexer, and Segment modules, not the full Compiler project.",
                "The changed UTF-8 helper is outside the selected executable closure; its exact transitive call dependents are reverified while independent bodies are reused.",
                "Profile 0.19 raises the sequence/view ceiling to 4096; its literal iteration ceiling remains 1024.",
                "This does not establish Stage-1 or Stage-2 succession.",
            ],
            "scope": "The full current Compiler Source, Lexer, and Segment modules declare Profile 0.19 and pass application-build verification together. Fresh processes demonstrate comment-only reuse, exact transitive invalidation after a changed UTF-8 helper, and exact invalidation after an imported callable-interface edit while independent body facts remain reusable. Source and Segment native application calls match Stage-0 before and after the interface-only edit.",
        }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = ROOT / f"evidence/self-host-full-module-fact-reuse-{stamp}.json"
    evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["evidence_artifact"] = str(evidence.relative_to(ROOT))
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
