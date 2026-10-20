#!/usr/bin/env python3
"""Compile real Compiler modules and prove Language fact reuse on source edits."""
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
sys.path.insert(0, str(ROOT / "tools"))
os.environ.setdefault(
    "MNCS_NATIVE_APP_PROBE_BIN",
    str(ROOT / "tools/stage0-probe/target/release/mncs-compiler-stage0-probe"),
)
import test_vm_native_application as app  # noqa: E402
import test_vm_emit  # noqa: E402
import test_vm_segment  # noqa: E402

MODULE_FILES = {
    "mncs.compiler.source.v1": ROOT / "src/compiler/source.mncs",
    "mncs.compiler.segment.v1": ROOT / "src/compiler/segment.mncs",
}
FULL_MODULE_FILES = {
    **MODULE_FILES,
    "mncs.compiler.lexer.v1": ROOT / "src/compiler/lexer.mncs",
}
ROOT_MODULE = "tests.compiler_source_frontier"
ROOT_SOURCE_ID = "self-host/compiler-source-frontier.mncs"
ROOT_SOURCE = """mncs 0.18;
module tests.compiler_source_frontier;
use mncs.compiler.segment.v1 as segment;

fn check_span(total: u64, start: u64, end: u64) -> (result: bool) {
    return segment.span_valid_global(total, start, end);
}
"""
CHANGED_FUNCTION = "mncs:0.2:body:mncs.compiler.source.v1::utf8_first_continuation_valid"
REUSED_EXECUTION_PATH = {
    "mncs:0.2:body:mncs.compiler.source.v1::span_valid_global",
    "mncs:0.2:body:mncs.compiler.segment.v1::span_valid_global",
    f"mncs:0.2:body:{ROOT_MODULE}::check_span",
}


def u64(value: int) -> dict:
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


CASES = [
    ("valid-span", [u64(8), u64(2), u64(8)]),
    ("reversed-span", [u64(8), u64(7), u64(3)]),
    ("past-end-span", [u64(8), u64(2), u64(9)]),
]


def request(modules: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": ROOT_SOURCE_ID,
        "root_source": ROOT_SOURCE,
        "callable_roots": [{"module": ROOT_MODULE, "function": "check_span"}],
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "compiler-source-self-consumption-fact-reuse/1",
    }


def load_modules() -> dict[str, str]:
    source = MODULE_FILES["mncs.compiler.source.v1"].read_text()
    segment = MODULE_FILES["mncs.compiler.segment.v1"].read_text()
    source_functions = [
        extract_function(source, name)
        for name in ("utf8_continuation", "utf8_first_continuation_valid", "span_valid_global")
    ]
    segment_function = extract_function(segment, "span_valid_global")
    return {
        "mncs.compiler.source.v1": (
            "mncs 0.18;\nmodule mncs.compiler.source.v1;\n\n"
            + "\n\n".join(source_functions) + "\n"
        ),
        "mncs.compiler.segment.v1": (
            "mncs 0.18;\nmodule mncs.compiler.segment.v1;\n"
            "use mncs.compiler.source.v1 as source;\n\n"
            + segment_function + "\n"
        ),
    }


def extract_function(source: str, name: str) -> str:
    start = source.index(f"fn {name}(")
    brace = source.index("{", start)
    depth = 0
    for offset in range(brace, len(source)):
        if source[offset] == "{":
            depth += 1
        elif source[offset] == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError(f"Compiler source function {name} is missing its closing brace")


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
    if not isinstance(work.get("reused_semantic_facts"), list):
        raise AssertionError(f"{label} omitted actual reused fact identities")
    if not isinstance(work.get("recomputed_semantic_facts"), list):
        raise AssertionError(f"{label} omitted actual recomputed fact identities")
    return work


def compile_fresh(request_value: dict, cache: Path, label: str) -> tuple[dict, dict]:
    probe = app.Probe(cache_dir=cache / "probe-cache",
                      application_cache_dir=cache / "application-cache")
    try:
        build, metrics = probe.send({"compile_application": request_value}, timeout=300.0)
    finally:
        probe.close()
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


def diagnostic_tree(rows: list[dict]) -> list[dict]:
    found = []
    for row in rows:
        if isinstance(row, dict):
            found.append({"code": row.get("code"), "message": row.get("message")})
            found.extend(diagnostic_tree(row.get("related", [])))
    return found


def full_module_attempt(cache: Path) -> tuple[dict, dict]:
    modules = {
        name: path.read_text().replace("mncs 0.19;", "mncs 0.18;", 1)
        for name, path in FULL_MODULE_FILES.items()
    }
    probe = app.Probe(cache_dir=cache / "probe-cache",
                      application_cache_dir=cache / "application-cache")
    try:
        build, metrics = probe.send({"compile_application": request(modules)}, timeout=300.0)
    finally:
        probe.close()
    if build.get("status") == "verified":
        return {"status": "verified_full_modules", "artifact_identity":
                build.get("artifact", {}).get("identity"), "diagnostics": []}, metrics
    diagnostics = diagnostic_tree(build.get("diagnostics", []))
    profile_rows = [row for row in diagnostics
                    if row.get("code") == "MNE182"
                    and "profile ceiling 1024" in str(row.get("message"))]
    if build.get("status") != "rejected" or not profile_rows:
        raise AssertionError("full Compiler modules failed for an unexpected reason: "
                             + json.dumps(diagnostics[:20], indent=2)[:5000])
    return {
        "status": "rejected_by_current_profile_traversal_bound",
        "diagnostic_count": len(diagnostics),
        "profile_bound_diagnostic_count": len(profile_rows),
        "sample_diagnostic": profile_rows[0]["message"],
    }, metrics


def oracle(modules: dict[str, str], cache: Path) -> list[dict]:
    probe = app.Probe(cache_dir=cache / "probe-cache",
                      application_cache_dir=cache / "application-cache")
    try:
        response, _ = probe.send({"project_oracle": {
            "root": ROOT_SOURCE,
            "modules": modules,
            "calls": [{
                "schema_version": "0.1",
                "target": {"module": ROOT_MODULE, "function": "check_span"},
                "arguments": args,
                "type_arguments": [],
                "step_budget": 100_000,
            } for _, args in CASES],
        }}, timeout=300.0)
    finally:
        probe.close()
    if not response.get("valid"):
        raise AssertionError("Stage-0 oracle rejected Compiler sources: "
                             + json.dumps(response.get("diagnostics"), indent=2)[:5000])
    executions = response.get("executions", [])
    if len(executions) != len(CASES) or any(row.get("status") != "returned" for row in executions):
        raise AssertionError("Stage-0 oracle did not return every source frontier case")
    return [row["returned"] for row in executions]


def execute_vm(artifact: dict, temporary_root: Path) -> tuple[list[list[dict]], dict]:
    artifact_path = temporary_root / "application.vm.json"
    calls_path = temporary_root / "calls.json"
    artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
    calls = [{"id": case_id, "request": {
        "schema_version": "0.1",
        "target": {"module": ROOT_MODULE, "function": "check_span"},
        "arguments": args,
        "type_arguments": [],
        "step_budget": 100_000,
    }} for case_id, args in CASES]
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
    if len(rows) != len(CASES):
        raise AssertionError("canonical VM omitted a selected span call")
    returned = []
    for (case_id, _), row in zip(CASES, rows):
        if row.get("id") != case_id or row.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM case failed: {case_id}: {row.get('outcome')}")
        returned.append([test_vm_segment.vm_to_wire(value)
                         for value in row["record"]["returned"]])
    peak_rss_kib = next((int(line.split(":", 1)[1].strip().split()[0])
                         for line in completed.stderr.splitlines()
                         if "Maximum resident set size" in line), None)
    return returned, {"wall_seconds": round(wall, 6), "peak_rss_kib": peak_rss_kib,
                      "calls_bytes": calls_path.stat().st_size,
                      "results_bytes": output_path.stat().st_size}


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
    replacement = original.replace("return true;", "return false;", 1)
    edited["mncs.compiler.source.v1"] = source[:start] + replacement + source[end:]
    return comment, edited


def main() -> dict:
    if not app.PROBE.is_file() or not test_vm_emit.VM_BIN.is_file():
        raise SystemExit("campaign Stage-0 probe or canonical VM binary is missing")
    modules = load_modules()
    comment_modules, edited_modules = source_edit(modules)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-compiler-source-fact-reuse-") as cache_name:
        cache = Path(cache_name)
        full_modules_observation, full_modules_metrics = full_module_attempt(
            cache / "full-module-attempt")
        cold, cold_metrics = compile_fresh(request(modules), cache, "cold source frontier")
        cold_work = cold.get("cache", {}).get("verification_work", {})
        if (cold.get("cache", {}).get("status") != "miss"
                or cold.get("cache", {}).get("publication") != "stored"
                or cold_work.get("produced_semantic_fact_count", 0) <= 0):
            raise AssertionError("cold source frontier did not publish verified Language facts: "
                                 + json.dumps({"cache": cold.get("cache"),
                                               "work": cold_work}, indent=2)[:5000])

        comment_build, comment_metrics = compile_fresh(
            request(comment_modules), cache, "comment-edited source frontier")
        comment_work = verification_work(comment_build, "comment-edited source frontier")
        if not comment_work["reused_semantic_facts"]:
            raise AssertionError("comment-only Compiler source edit reused no Language facts")
        if comment_work["recomputed_semantic_facts"]:
            raise AssertionError("comment-only Compiler source edit rechecked semantic bodies")

        edited_build, edited_metrics = compile_fresh(
            request(edited_modules), cache, "unreachable-body-edited source frontier")
        edited_work = verification_work(edited_build, "unreachable-body-edited source frontier")
        reused = set(edited_work["reused_semantic_facts"])
        recomputed = set(edited_work["recomputed_semantic_facts"])
        if CHANGED_FUNCTION not in recomputed:
            raise AssertionError("edited Compiler function body was not reverified")
        if not REUSED_EXECUTION_PATH.issubset(reused):
            raise AssertionError("the unchanged Compiler execution path was not reused: "
                                 + json.dumps(sorted(reused_execution_path :=
                                                     REUSED_EXECUTION_PATH - reused)))
        if not reused:
            raise AssertionError("body edit reused no independent Language facts")

        if (cold.get("artifact", {}).get("identity")
                != comment_build.get("artifact", {}).get("identity")
                or cold.get("artifact", {}).get("identity")
                != edited_build.get("artifact", {}).get("identity")):
            raise AssertionError("an unrelated or comment-only source edit changed the executable closure")

        expected = oracle(edited_modules, cache)
        with tempfile.TemporaryDirectory(prefix="mncs-compiler-source-vm-") as vm_name:
            vm_values, vm_metrics = execute_vm(
                edited_build["artifact"]["value"], Path(vm_name))
        if vm_values != expected:
            raise AssertionError("canonical VM source frontier differs from Stage-0 oracle: "
                                 + json.dumps({"vm": vm_values, "stage0": expected}, indent=2))

        language_lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
        vm_revision = subprocess.check_output(
            ["git", "-C", str(test_vm_emit.VM_REPO), "rev-parse", "HEAD"], text=True
        ).strip()
        result = {
            "schema_version": "mncs.compiler.self-consumption-source-fact-reuse/1",
            "status": "passed_real_compiler_module_fact_reuse_frontier",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "compiler_branch": subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
            "stage0_revision": language_lock["revision"],
            "stage0_probe_sha256": hashlib.sha256(app.PROBE.read_bytes()).hexdigest(),
            "vm_revision": vm_revision,
            "vm_binary_sha256": hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest(),
            "harness": "tools/test_vm_compiler_source_fact_reuse.py",
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "target_modules": [
                {
                    "module": name,
                    "source_path": str(path.relative_to(ROOT)),
                    "full_source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "selected_functions": (["utf8_continuation", "utf8_first_continuation_valid",
                                            "span_valid_global"] if name.endswith("source.v1")
                                           else ["span_valid_global"]),
                    "verified_fragment_sha256": hashlib.sha256(
                        modules[name].encode("utf-8")).hexdigest(),
                }
                for name, path in MODULE_FILES.items()
            ],
            "full_module_admission": {
                **full_modules_observation,
                "source_modules": [
                    {"module": name, "source_path": str(path.relative_to(ROOT)),
                     "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                    for name, path in FULL_MODULE_FILES.items()
                ],
                "measurement": full_modules_metrics,
            },
            "application": {
                "entry": f"{ROOT_MODULE}::check_span",
                "source_module_count": len(MODULE_FILES),
                "artifact_identity": edited_build["artifact"]["identity"],
                "artifact_json_bytes": len(json.dumps(
                    edited_build["artifact"]["value"], sort_keys=True,
                    separators=(",", ":")).encode("utf-8")),
                "canonical_vm_invocations": len(CASES),
                "canonical_vm_matches_stage0": True,
                "returned_values": [row[0]["boolean"]["value"] for row in vm_values],
            },
            "verified_fact_reuse": {
                "contract": "mncs.language-verification-fact/1",
                "cold_facts_produced": cold_work["produced_semantic_fact_count"],
                "comment_edit_reused": len(comment_work["reused_semantic_facts"]),
                "comment_edit_recomputed": len(comment_work["recomputed_semantic_facts"]),
                "body_edit_reused": len(reused),
                "body_edit_recomputed": sorted(recomputed),
                "edited_subject": CHANGED_FUNCTION,
                "unchanged_execution_path_reused": sorted(REUSED_EXECUTION_PATH),
                "fresh_process_per_snapshot": True,
                "artifact_reused_for_unchanged_closure": True,
            },
            "measurements": {
                "full_module_attempt": full_modules_metrics,
                "cold_build": cold_metrics,
                "comment_edit_build": comment_metrics,
                "body_edit_build": edited_metrics,
                "canonical_vm_execution": vm_metrics,
                "whole_frontier_wall_seconds": round(time.monotonic() - started, 6),
                "cgroup_measurement_available": cold_metrics.get("peak_cgroup_memory_bytes") is not None,
            },
            "limitations": [
                "This compiles a selected dependency slice extracted from the actual Compiler source and segment modules; it does not compile the full modules or full Compiler.",
                "The edited UTF-8 validator is outside the selected executable closure; its changed body is reverified while the source-span path is reused.",
                "This does not establish Stage-1 or Stage-2 succession.",
            ],
            "scope": "A verified dependency slice extracted from the current Compiler source and segment modules was built as one native application. A fresh process reused Language verification facts after a comment-only snapshot change. A second fresh process reverified the edited UTF-8 function, reused the unchanged source-span execution path, and retained the same executable closure. Three canonical VM calls matched the Stage-0 oracle.",
        }
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = ROOT / f"evidence/self-host-source-module-fact-reuse-{stamp}.json"
    evidence.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["evidence_artifact"] = str(evidence.relative_to(ROOT))
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
