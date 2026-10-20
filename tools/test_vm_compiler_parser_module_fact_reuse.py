#!/usr/bin/env python3
"""Verify fact reuse and canonical execution across Compiler Parser dependencies."""
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
import test_cp0001 as cp0001  # noqa: E402
import test_vm_compiler_full_module_fact_reuse as common  # noqa: E402
import test_vm_emit  # noqa: E402
import test_vm_segment  # noqa: E402

MODULE_FILES = {
    "mncs.compiler.source.v1": ROOT / "src/compiler/source.mncs",
    "mncs.compiler.lexer.v1": ROOT / "src/compiler/lexer.mncs",
    "mncs.compiler.parser.v1": ROOT / "src/compiler/parser.mncs",
}
PROFILES = {
    "mncs.compiler.source.v1": "0.19",
    "mncs.compiler.lexer.v1": "0.19",
    "mncs.compiler.parser.v1": "0.18",
}
ROOT_MODULE = "tests.compiler_parser_frontier"
ROOT_SOURCE_ID = "self-host/compiler-parser-frontier.mncs"
ROOT_SOURCE = """mncs 0.19;
module tests.compiler_parser_frontier;
use mncs.compiler.parser.v1 as parser;

fn header_code(text: [byte; up_to 64]) -> (result: u64) {
    let parsed: parser.Header = parser.header<64>(text);
    return parsed.code;
}
"""
CALLABLE_ROOTS = [{"module": ROOT_MODULE, "function": "header_code"}]
CHANGED_FUNCTION = "mncs:0.2:body:mncs.compiler.parser.v1::finish"
EXPECTED_RECOMPUTED = {
    CHANGED_FUNCTION,
    "mncs:0.2:body:mncs.compiler.parser.v1::finish__spec_4dd6cbd1",
    "mncs:0.2:body:mncs.compiler.parser.v1::header",
    "mncs:0.2:body:mncs.compiler.parser.v1::header__spec_4dd6cbd1",
}


def request(modules: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": ROOT_SOURCE_ID,
        "root_source": ROOT_SOURCE,
        "callable_roots": CALLABLE_ROOTS,
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "verification_policy": "compiler-parser-module-fact-reuse/1",
    }


def load_modules() -> dict[str, str]:
    modules = {name: path.read_text() for name, path in MODULE_FILES.items()}
    for name, source in modules.items():
        if not source.startswith(f"mncs {PROFILES[name]};"):
            raise AssertionError(f"unexpected source profile for {name}")
    return modules


def cases() -> list[dict]:
    return [
        {"id": "valid-header", "raw": b"mncs 0.19; module alpha;", "expected_code": 0},
        {"id": "wrong-first-token", "raw": b"module alpha;", "expected_code": 2},
        {"id": "missing-version-semicolon", "raw": b"mncs 0.19 module alpha;", "expected_code": 4},
        {"id": "invalid-utf8", "raw": b"\xff", "expected_code": 8},
    ]


def input_value(raw: bytes) -> dict:
    return cp0001.byte_sequence(raw)


def attempt(request_value: dict, cache: Path) -> tuple[dict, dict]:
    return common.compile_attempt(request_value, cache)


def compile_fresh(request_value: dict, cache: Path, label: str) -> tuple[dict, dict]:
    return common.compile_fresh(request_value, cache, label)


def verification_work(build: dict, label: str) -> dict:
    return common.verification_work(build, label)


def oracle(modules: dict[str, str], cache: Path, workload: list[dict]) -> list[dict]:
    probe = common.app.Probe(cache_dir=cache / "probe-cache",
                             application_cache_dir=cache / "application-cache")
    try:
        response, _ = probe.send({"project_oracle": {
            "root": ROOT_SOURCE,
            "modules": modules,
            "calls": [{
                "schema_version": "0.1",
                "target": {"module": ROOT_MODULE, "function": "header_code"},
                "arguments": [input_value(row["raw"])],
                "type_arguments": [],
                "step_budget": 1_000_000,
            } for row in workload],
        }}, timeout=300.0)
    finally:
        probe.close()
    if response.get("valid") is not True:
        raise AssertionError("Stage-0 rejected the Parser application: "
                             + json.dumps(response.get("diagnostics"), indent=2)[:5000])
    rows = response.get("executions", [])
    if len(rows) != len(workload) or any(row.get("status") != "returned" for row in rows):
        raise AssertionError("Stage-0 did not return every Parser header case")
    values = [row["returned"][0] for row in rows]
    expected = [cp0001.integer(row["expected_code"]) for row in workload]
    if values != expected:
        raise AssertionError("Stage-0 Parser results differ from the header oracle")
    return values


def execute_vm(artifact: dict, temporary_root: Path, workload: list[dict]) -> tuple[list[dict], dict]:
    artifact_path = temporary_root / "application.vm.json"
    calls_path = temporary_root / "calls.json"
    output_path = temporary_root / "results.json"
    artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
    calls = [{"id": row["id"], "request": {
        "schema_version": "0.1",
        "target": {"module": ROOT_MODULE, "function": "header_code"},
        "arguments": [input_value(row["raw"])],
        "type_arguments": [],
        "step_budget": 1_000_000,
    }} for row in workload]
    calls_path.write_text(json.dumps(calls, sort_keys=True) + "\n")
    test_vm_emit.ensure_vm()
    started = time.monotonic()
    completed = subprocess.run([
        "/usr/bin/time", "-v", str(test_vm_emit.VM_BIN), "batch",
        "--artifact", str(artifact_path), "--calls", str(calls_path),
        "--output", str(output_path),
    ], capture_output=True, text=True)
    wall = time.monotonic() - started
    if completed.returncode:
        raise AssertionError("canonical VM Parser execution failed: "
                             + completed.stderr[-3000:])
    document = json.loads(output_path.read_text())
    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM receipt does not bind the current artifact")
    rows = document.get("results", [])
    if len(rows) != len(workload):
        raise AssertionError("canonical VM omitted a Parser header case")
    values = []
    for case, result in zip(workload, rows):
        if result.get("id") != case["id"] or result.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM case failed: {case['id']}: {result.get('outcome')}")
        returned = result.get("record", {}).get("returned", [])
        if len(returned) != 1:
            raise AssertionError(f"malformed Parser result for {case['id']}")
        values.append(test_vm_segment.vm_to_wire(returned[0]))
    peak_rss_kib = next((int(line.split(":", 1)[1].strip().split()[0])
                         for line in completed.stderr.splitlines()
                         if "Maximum resident set size" in line), None)
    return values, {"wall_seconds": round(wall, 6), "peak_rss_kib": peak_rss_kib,
                    "calls_bytes": calls_path.stat().st_size,
                    "results_bytes": output_path.stat().st_size}


def profile_018_attempt(cache: Path, modules: dict[str, str], workload: list[dict]) -> tuple[dict, dict]:
    baseline = dict(modules)
    for name in ("mncs.compiler.source.v1", "mncs.compiler.lexer.v1"):
        baseline[name] = baseline[name].replace("mncs 0.19;", "mncs 0.18;", 1)
    build, metrics = attempt(request(baseline), cache)
    if build.get("status") == "verified":
        return {"status": "verified_under_profile_018", "diagnostic_count": 0}, metrics
    diagnostics = common.diagnostic_tree(build.get("diagnostics", []))
    profile_rows = [row for row in diagnostics
                    if row.get("code") == "MNE182"
                    and "profile ceiling 1024" in str(row.get("message"))]
    if build.get("status") != "rejected" or not profile_rows:
        raise AssertionError("Profile 0.18 Parser closure failed unexpectedly: "
                             + json.dumps(diagnostics[:20], indent=2)[:5000])
    return {
        "status": "rejected_by_profile_018_traversal_bound",
        "diagnostic_count": len(diagnostics),
        "profile_bound_diagnostic_count": len(profile_rows),
        "sample_diagnostic": profile_rows[0]["message"],
    }, metrics


def edit_reachable_parser_body(modules: dict[str, str]) -> dict[str, str]:
    changed = dict(modules)
    source = changed["mncs.compiler.parser.v1"]
    start = source.index("fn finish<N:")
    end = source.index("\n}\n", start) + 2
    function = source[start:end]
    before = "if st.code != 0 || st.state == 8"
    if function.count(before) != 1:
        raise AssertionError("Parser finish edit anchor changed")
    changed["mncs.compiler.parser.v1"] = (
        source[:start] + function.replace(before, "if st.state == 8 || st.code != 0", 1)
        + source[end:]
    )
    return changed


def main() -> dict:
    if not common.app.PROBE.is_file() or not test_vm_emit.VM_BIN.is_file():
        raise SystemExit("campaign Stage-0 probe or canonical VM binary is missing")
    modules = load_modules()
    comment_modules = dict(modules)
    comment_modules["mncs.compiler.parser.v1"] += "\n// Parser snapshot comment edit.\n"
    edited_modules = edit_reachable_parser_body(modules)
    workload = cases()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-compiler-parser-fact-reuse-") as cache_name:
        cache = Path(cache_name)
        profile_018, profile_018_metrics = profile_018_attempt(cache / "profile-018", modules, workload)
        cold, cold_metrics = compile_fresh(request(modules), cache, "Parser dependency closure cold build")
        cold_work = cold.get("cache", {}).get("verification_work", {})
        if (cold.get("cache", {}).get("status") != "miss"
                or cold.get("cache", {}).get("publication") != "stored"
                or cold_work.get("front_end_outcome") != "verified"
                or cold_work.get("produced_semantic_fact_count", 0) <= 0):
            raise AssertionError("Parser dependency closure did not publish verified Language facts")

        comment_build, comment_metrics = compile_fresh(
            request(comment_modules), cache, "Parser dependency closure comment edit")
        comment_work = verification_work(comment_build, "Parser dependency closure comment edit")
        fact_count = cold_work["produced_semantic_fact_count"]
        if (len(comment_work["reused_semantic_facts"]) != fact_count
                or comment_work["recomputed_semantic_facts"]):
            raise AssertionError("Parser comment edit did not reuse every verified body fact")
        if (cold.get("artifact", {}).get("identity")
                != comment_build.get("artifact", {}).get("identity")):
            raise AssertionError("Parser comment edit changed the executable closure")

        edited_build, edited_metrics = compile_fresh(
            request(edited_modules), cache, "Parser reachable body edit")
        edited_work = verification_work(edited_build, "Parser reachable body edit")
        recomputed = set(edited_work["recomputed_semantic_facts"])
        reused = set(edited_work["reused_semantic_facts"])
        if recomputed != EXPECTED_RECOMPUTED:
            raise AssertionError("reachable Parser edit did not reverify its exact generic dependency closure: "
                                 + json.dumps({"recomputed": sorted(recomputed)}, indent=2))
        reasons = {
            row["subject"]: row["reason"]
            for row in edited_work.get("semantic_fact_recomputation_reasons", [])
        }
        expected_reasons = {
            CHANGED_FUNCTION: "subject_changed",
            "mncs:0.2:body:mncs.compiler.parser.v1::finish__spec_4dd6cbd1": "subject_changed",
            "mncs:0.2:body:mncs.compiler.parser.v1::header": "dependencies_changed",
            "mncs:0.2:body:mncs.compiler.parser.v1::header__spec_4dd6cbd1": "dependencies_changed",
        }
        if reasons != expected_reasons:
            raise AssertionError("Parser body edit reported incorrect fact invalidation reasons: "
                                 + json.dumps(reasons, indent=2, sort_keys=True))
        if (f"mncs:0.2:body:{ROOT_MODULE}::header_code" not in reused
                or "mncs:0.2:body:mncs.compiler.parser.v1::step" not in reused):
            raise AssertionError("unaffected Parser/application caller facts were not reused")
        if (edited_build.get("artifact", {}).get("identity")
                == cold.get("artifact", {}).get("identity")
                or edited_work.get("backend_artifact") != "generated"):
            raise AssertionError("reachable Parser body edit reused stale executable code")

        expected = oracle(edited_modules, cache, workload)
        with tempfile.TemporaryDirectory(prefix="mncs-compiler-parser-vm-") as vm_name:
            actual, vm_metrics = execute_vm(edited_build["artifact"]["value"], Path(vm_name), workload)
        if actual != expected:
            raise AssertionError("canonical VM differs from Stage-0 Parser results: "
                                 + json.dumps({"vm": actual, "stage0": expected}, indent=2))

        lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
        result = {
            "schema_version": "mncs.compiler.parser-dependency-fact-reuse/1",
            "status": "passed_full_parser_dependency_fact_reuse_frontier",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "compiler_branch": subprocess.check_output(
                ["git", "branch", "--show-current"], cwd=ROOT, text=True).strip(),
            "stage0_revision": lock["revision"],
            "stage0_probe_sha256": hashlib.sha256(common.app.PROBE.read_bytes()).hexdigest(),
            "vm_revision": subprocess.check_output(
                ["git", "-C", str(test_vm_emit.VM_REPO), "rev-parse", "HEAD"], text=True
            ).strip(),
            "vm_binary_sha256": hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest(),
            "harness": "tools/test_vm_compiler_parser_module_fact_reuse.py",
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "modules": [
                {"module": name, "source_path": str(path.relative_to(ROOT)),
                 "language_profile": PROFILES[name],
                 "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for name, path in MODULE_FILES.items()
            ],
            "full_module_admission": {
                "status": "verified",
                "resolved_module_count": cold_work.get("resolved_module_count"),
                "program_function_count": cold_work.get("program_function_count"),
                "cold_verified_fact_count": fact_count,
                "profile_mix": {"source": "0.19", "lexer": "0.19", "parser": "0.18"},
            },
            "profile_018_baseline": {**profile_018, "measurement": profile_018_metrics},
            "application": {
                "callable_root": "header_code",
                "selected_parser_function": "mncs.compiler.parser.v1::header<64>",
                "case_count": len(workload),
                "expected_codes": [row["expected_code"] for row in workload],
                "canonical_vm_matches_stage0": True,
                "artifact_identity": edited_build["artifact"]["identity"],
            },
            "verified_fact_reuse": {
                "contract": "mncs.language-verification-fact/1",
                "cold_facts_produced": fact_count,
                "comment_edit_reused": len(comment_work["reused_semantic_facts"]),
                "comment_edit_recomputed": len(comment_work["recomputed_semantic_facts"]),
                "reachable_body_edit_subject": CHANGED_FUNCTION,
                "reachable_body_edit_recomputed": sorted(recomputed),
                "reachable_body_edit_recomputation_reasons": reasons,
                "reachable_body_edit_reused": len(reused),
                "reused_parser_and_application_callers": sorted(
                    subject for subject in reused
                    if subject in {
                        f"mncs:0.2:body:{ROOT_MODULE}::header_code",
                        "mncs:0.2:body:mncs.compiler.parser.v1::step",
                    }
                ),
                "fresh_process_per_snapshot": True,
            },
            "measurements": {
                "cold_build": cold_metrics,
                "comment_edit_build": comment_metrics,
                "reachable_body_edit_build": edited_metrics,
                "canonical_vm_execution": vm_metrics,
                "whole_frontier_wall_seconds": round(time.monotonic() - started, 6),
                "cgroup_measurement_available": cold_metrics.get("peak_cgroup_memory_bytes") is not None,
            },
            "limitations": [
                "This verifies the full current Parser, Source, and Lexer modules in an application; it does not verify the full Compiler project.",
                "The selected application executes Parser.header, not the global parser frontier.",
                "This does not establish Stage-1 or Stage-2 succession.",
            ],
            "scope": "The current Parser module at Profile 0.18 and its current Source and Lexer dependencies at Profile 0.19 are fully verified together in application-build. A comment edit reuses every Language fact; an equivalent reachable finish-body edit recomputes the changed body, its generic specialization, and the exact dependent header bodies while preserving independent Parser/application facts, regenerating the executable artifact, and retaining Stage-0-equivalent canonical VM behavior.",
        }
    output = ROOT / f"evidence/self-host-parser-module-fact-reuse-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["evidence_artifact"] = str(output.relative_to(ROOT))
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
