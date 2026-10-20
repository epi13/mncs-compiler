#!/usr/bin/env python3
"""Exercise Language verified-fact reuse through Compiler's Project module.

The application reaches a real generic Project helper through an MNCS root
callable. Every source snapshot is compiled by a fresh Stage-0 process while
the application and Language fact caches persist for the duration of the run.
"""
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
PROJECTS = Path("/home/epi13/Documents/Projects")
os.environ.setdefault(
    "MNCS_NATIVE_APP_PROBE_BIN",
    str(ROOT / ".build/native-app-readiness/mncs-compiler-stage0-probe"),
)
os.environ.setdefault("MNCS_PROBE_BIN", os.environ["MNCS_NATIVE_APP_PROBE_BIN"])
os.environ.setdefault("MNCS_VM_ROOT", str(PROJECTS / "mncs-vm"))
os.environ.setdefault("MNCS_VM_BIN", str(PROJECTS / "mncs-vm/target/release/mncs-vm"))
os.environ.setdefault("MNCS_NATIVE_APP_VM_BIN", os.environ["MNCS_VM_BIN"])

import test_vm_native_application as app  # noqa: E402
import test_vm_project_import_name_cache_frontier as reference  # noqa: E402
import test_vm_emit  # noqa: E402
import test_vm_segment  # noqa: E402

PROJECT = reference.project
MODULE_NAMES = ["source", "lexer", "parser", "segment", "decl", "flow", "ssa", "project"]
MODULE_FILES = {
    f"mncs.compiler.{name}.v1": ROOT / "src/compiler" / f"{name}.mncs"
    for name in MODULE_NAMES
}
PROFILES = {
    "mncs.compiler.source.v1": "0.19",
    "mncs.compiler.lexer.v1": "0.19",
    "mncs.compiler.parser.v1": "0.18",
    "mncs.compiler.segment.v1": "0.19",
    "mncs.compiler.decl.v1": "0.18",
    "mncs.compiler.flow.v1": "0.18",
    "mncs.compiler.ssa.v1": "0.18",
    "mncs.compiler.project.v1": "0.18",
}
ROOT_MODULE = "tests.compiler_project_fact_frontier"
ROOT_FUNCTION = "run"
ROOT_SOURCE_ID = "self-host/compiler-project-fact-frontier.mncs"
ROOT_SOURCE = """mncs 0.18;
module tests.compiler_project_fact_frontier;
use mncs.compiler.project.v1 as project;

fn run(sources: [project.ProjectSource; up_to 64],
       flat_pages: [[byte; up_to 1024]; up_to 1],
       source_index: u64, left_start: u64, left_end: u64,
       right_start: u64, right_end: u64) -> (result: bool) {
    return project.module_resolution_cache_key_equal<1, 1024>(
        sources, flat_pages, source_index, left_start, left_end,
        right_start, right_end);
}

fn unused_snapshot_helper(value: u64) -> (result: u64) {
    return value;
}
"""
REQUEST_SCHEMA = "mncs.compiler.application-build-request/5"
RESPONSE_SCHEMA = "mncs.compiler.application-build/8"
CACHE_SCHEMA = "mncs.compiler.application-build-cache-observation/5"
WORK_SCHEMA = "mncs.compiler.application-build-verification-work/3"
FACT_SCHEMA = "mncs.language-verification-fact/3"
TARGET_SUBJECT = (
    "mncs:0.2:body:mncs.compiler.project.v1::module_resolution_cache_key_equal"
)
UNREACHABLE_SUBJECT = (
    "mncs:0.2:body:tests.compiler_project_fact_frontier::unused_snapshot_helper"
)
STEP_BUDGET = reference.STEP_BUDGET


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def load_modules() -> dict[str, str]:
    modules = {name: path.read_text(encoding="utf-8")
               for name, path in MODULE_FILES.items()}
    for name, source in modules.items():
        if not source.startswith(f"mncs {PROFILES[name]};"):
            raise AssertionError(f"unexpected source profile for {name}")
    return modules


def build_request(modules: dict[str, str], root_source: str = ROOT_SOURCE) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA,
        "target": "mncs-vm-0.1",
        "root_source_id": ROOT_SOURCE_ID,
        "root_source": root_source,
        "callable_roots": [{"module": ROOT_MODULE, "function": ROOT_FUNCTION}],
        "test_selection": {"kind": "all_source_cases"},
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "compiler-project-language-fact-v3/1",
    }


def compile_once(request: dict, cache: Path, label: str) -> tuple[dict, dict]:
    print(f"Compiling {label} in a fresh Stage-0 process...", flush=True)
    probe = app.Probe(
        cache_dir=cache / "program-cache",
        application_cache_dir=cache / "application-cache",
    )
    try:
        ready, _ = probe.send({"execution_status": True})
        if not isinstance(ready, dict) or "modules" not in ready:
            raise AssertionError("Stage-0 probe did not return its readiness handshake")
        result, metrics = probe.send({"compile_application": request}, timeout=1200.0)
    finally:
        probe.close()
    if result.get("schema_version") != RESPONSE_SCHEMA:
        raise AssertionError(f"{label} returned the wrong application-build contract")
    print(f"Finished {label}: status={result.get('status')} "
          f"wall={metrics.get('wall_seconds')}s", flush=True)
    return result, metrics


def require_work(build: dict, label: str, *, allow_rejected: bool = False) -> dict:
    cache = build.get("cache", {})
    work = cache.get("verification_work", {})
    if (cache.get("schema_version") != CACHE_SCHEMA
            or work.get("schema_version") != WORK_SCHEMA
            or work.get("language_verification_fact_schema") != FACT_SCHEMA):
        raise AssertionError(f"{label} omitted versioned Language fact telemetry")
    for field in ("reused_semantic_facts", "recomputed_semantic_facts",
                  "reused_declaration_facts", "recomputed_declaration_facts",
                  "semantic_fact_recomputation_reasons",
                  "declaration_fact_recomputation_reasons"):
        if not isinstance(work.get(field), list):
            raise AssertionError(f"{label} omitted actual {field}")
    outcomes = {"verified", "rejected"} if allow_rejected else {"verified"}
    if work.get("front_end_invocations") != 1 or work.get("front_end_outcome") not in outcomes:
        raise AssertionError(f"{label} did not freshly verify this source snapshot: {work}")
    for field in ("produced_semantic_fact_count", "produced_declaration_fact_count"):
        if not isinstance(work.get(field), int):
            raise AssertionError(f"{label} omitted Language fact count {field}")
    return work


def reference_cases() -> tuple[dict, list[dict], list[bool]]:
    probe = PROJECT.Probe()
    try:
        status = probe.send({"execution_status": True})
        identities = PROJECT.identity_map(probe)
        cases = [
            {"id": "equal-short-module-name", "raw": b"demo.dep;demo.dep",
             "left": (0, 8), "right": (9, 17), "expected": True},
            {"id": "mismatch-at-sixth-byte", "raw": b"demo.dep;demo.foo",
             "left": (0, 8), "right": (9, 17), "expected": False},
            {"id": "equal-empty-name", "raw": b";",
             "left": (0, 0), "right": (1, 1), "expected": True},
            {"id": "different-length-names", "raw": b"demo.dep;demo.de",
             "left": (0, 8), "right": (9, 16), "expected": False},
            {"id": "out-of-range-name-span", "raw": b"demo.dep",
             "left": (0, 8), "right": (1, 9), "expected": False},
        ]
        for case in cases:
            request = reference._request(
                identities, case["raw"], case["left"], case["right"])
            result = probe.send(request)
            if result.get("status") != "returned":
                raise AssertionError(f"Stage-0 Project rejected {case['id']}: {result}")
            value = PROJECT.decode(result["returned"][0])
            if value is not case["expected"]:
                raise AssertionError(f"Stage-0 Project changed {case['id']}: {value}")
            case["arguments"] = request["arguments"]
        return status, cases, [case["expected"] for case in cases]
    finally:
        probe.close()


def vm_values(artifact: dict, cases: list[dict]) -> tuple[list[bool], dict]:
    with tempfile.TemporaryDirectory(prefix="mncs-project-fact-vm-") as raw:
        temporary = Path(raw)
        artifact_path = temporary / "application.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        workload = [{
            "id": case["id"], "function": ROOT_FUNCTION,
            "args": case["arguments"], "type_args": [], "step_budget": STEP_BUDGET,
        } for case in cases]
        document, wall, peak_rss_kib, calls_bytes, results_bytes = (
            test_vm_segment.run_batch(
                artifact_path, workload, temporary, module=ROOT_MODULE)
        )
    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different Project application artifact")
    rows = document.get("results", [])
    if len(rows) != len(cases):
        raise AssertionError("canonical VM omitted Project application results")
    values = []
    for case, row in zip(cases, rows, strict=True):
        if row.get("id") != case["id"] or row.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {case['id']}: {row.get('outcome')}")
        returned = row.get("record", {}).get("returned", [])
        if len(returned) != 1:
            raise AssertionError(f"canonical VM returned malformed result for {case['id']}")
        values.append(PROJECT.decode(test_vm_segment.vm_to_wire(returned[0])))
    return values, {
        "wall_seconds": round(wall, 6),
        "peak_rss_kib": peak_rss_kib,
        "calls_bytes": calls_bytes,
        "results_bytes": results_bytes,
    }


def edit_project_body(modules: dict[str, str]) -> dict[str, str]:
    changed = dict(modules)
    name = "mncs.compiler.project.v1"
    source = changed[name]
    start = source.index("fn module_resolution_cache_key_equal<")
    end = source.index("\n}", start) + 2
    body = source[start:end]
    old = "if length > 64 { return false; }"
    if body.count(old) != 1:
        raise AssertionError("Project fact-reuse edit anchor changed")
    changed[name] = source[:start] + body.replace(
        old, "if length >= 65 { return false; }", 1) + source[end:]
    return changed


def edit_unreachable_body(root_source: str) -> str:
    old = "fn unused_snapshot_helper(value: u64) -> (result: u64) {\n    return value;\n}"
    new = "fn unused_snapshot_helper(value: u64) -> (result: u64) {\n    return 0;\n}"
    if root_source.count(old) != 1:
        raise AssertionError("uncalled Project snapshot helper edit anchor changed")
    return root_source.replace(old, new, 1)


def source_hashes() -> list[dict]:
    return [{
        "module": module,
        "path": str(path.relative_to(ROOT)),
        "profile": PROFILES[module],
        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    } for module, path in MODULE_FILES.items()]


def main() -> dict:
    if not app.PROBE.is_file():
        raise SystemExit(f"campaign Stage-0 probe is missing: {app.PROBE}")
    test_vm_emit.ensure_vm()
    modules = load_modules()
    edited_modules = edit_project_body(modules)
    print("Running Stage-0 Project reference cases...", flush=True)
    reference_status, cases, expected = reference_cases()
    print(f"Stage-0 Project reference passed {len(cases)} cases.", flush=True)
    sys.path.insert(0, str(test_vm_emit.VM_REPO / "python"))
    from mncs_vm_client import inspect_runtime  # noqa: E402

    vm_runtime = inspect_runtime(test_vm_emit.VM_BIN)
    comment_root = ROOT_SOURCE.replace(
        "fn run(", "// Refresh root locations after a comment-only edit.\nfn run(", 1)
    if comment_root == ROOT_SOURCE:
        raise AssertionError("comment fixture failed to move current source locations")
    unreachable_root = edit_unreachable_body(ROOT_SOURCE)
    invalid_root = unreachable_root.replace(
        "-> (result: bool)", "-> (result: u64)", 1)
    if invalid_root == unreachable_root:
        raise AssertionError("invalid root fixture did not change the return type")

    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="mncs-project-language-fact-reuse-") as raw:
        cache = Path(raw)
        cold, cold_metrics = compile_once(
            build_request(modules), cache, "cold Project application")
        cold_work = require_work(cold, "cold Project application")
        if (cold.get("status") != "verified"
                or cold.get("cache", {}).get("status") != "miss"
                or cold.get("cache", {}).get("publication") != "stored"
                or cold_work["produced_semantic_fact_count"] <= 0):
            raise AssertionError("cold Project application did not publish verified Language facts")

        comment, comment_metrics = compile_once(
            build_request(modules, comment_root), cache, "Project comment edit")
        comment_work = require_work(comment, "Project comment edit")
        if (comment.get("status") != "verified"
                or len(comment_work["reused_semantic_facts"])
                != cold_work["produced_semantic_fact_count"]
                or comment_work["recomputed_semantic_facts"]
                or comment_work["recomputed_declaration_facts"]
                or comment.get("artifact", {}).get("identity")
                != cold.get("artifact", {}).get("identity")):
            raise AssertionError("Project comment edit did not reuse all verified facts")

        unreachable, unreachable_metrics = compile_once(
            build_request(modules, unreachable_root), cache,
            "uncalled Project snapshot helper body edit")
        unreachable_work = require_work(
            unreachable, "uncalled Project snapshot helper body edit")
        unreachable_reasons = {
            row["subject"]: row["reason"]
            for row in unreachable_work["semantic_fact_recomputation_reasons"]
        }
        if (unreachable.get("status") != "verified"
                or unreachable_work["recomputed_semantic_facts"] != [UNREACHABLE_SUBJECT]
                or unreachable_reasons.get(UNREACHABLE_SUBJECT) != "subject_changed"
                or unreachable_work["recomputed_declaration_facts"]
                or len(unreachable_work["reused_semantic_facts"])
                != cold_work["produced_semantic_fact_count"] - 1
                or UNREACHABLE_SUBJECT in unreachable_work["reused_semantic_facts"]
                or len(set(unreachable_work["reused_semantic_facts"]))
                != len(unreachable_work["reused_semantic_facts"])
                or len(unreachable_work["reused_semantic_facts"]) + 1
                != cold_work["produced_semantic_fact_count"]):
            raise AssertionError(
                "uncalled Project body edit did not reverify only its own Language fact: "
                + json.dumps({
                    "recomputed": unreachable_work["recomputed_semantic_facts"],
                    "reasons": unreachable_reasons,
                }, sort_keys=True)[:4000]
            )

        edited, edit_metrics = compile_once(
            build_request(edited_modules, unreachable_root), cache,
            "Project helper body edit")
        edit_work = require_work(edited, "Project helper body edit")
        recomputed = set(edit_work["recomputed_semantic_facts"])
        reasons = {row["subject"]: row["reason"]
                   for row in edit_work["semantic_fact_recomputation_reasons"]}
        target_facts = {
            fact for fact in recomputed
            if fact == TARGET_SUBJECT or fact.startswith(TARGET_SUBJECT + "__spec_")
        }
        root_subject = f"mncs:0.2:body:{ROOT_MODULE}::{ROOT_FUNCTION}"
        if (edited.get("status") != "verified"
                or not target_facts
                or TARGET_SUBJECT not in recomputed
                or reasons.get(TARGET_SUBJECT) != "subject_changed"
                or root_subject not in recomputed
                or reasons.get(root_subject) != "dependencies_changed"
                or not edit_work["reused_semantic_facts"]
                or edit_work["recomputed_declaration_facts"]
                or len(edit_work["reused_semantic_facts"]) + len(recomputed)
                != cold_work["produced_semantic_fact_count"]):
            raise AssertionError(
                "Project body edit did not reverify its subject and caller while retaining independent facts: "
                + json.dumps({"recomputed": sorted(recomputed), "reasons": reasons}, sort_keys=True)[:6000]
            )

        invalid, invalid_metrics = compile_once(
            build_request(modules, invalid_root), cache, "invalid Project root result type")
        invalid_work = require_work(
            invalid, "invalid Project root result type", allow_rejected=True)
        if (invalid.get("status") != "rejected"
                or invalid_work.get("front_end_outcome") != "rejected"
                or not invalid.get("diagnostics")
                or invalid.get("artifact") is not None
                or invalid.get("cache", {}).get("publication") != "not_published"):
            raise AssertionError("prior Project facts masked a fresh invalid-source diagnostic")

        values, vm_metrics = vm_values(edited.get("artifact", {}).get("value"), cases)
        if values != expected:
            raise AssertionError(f"Project application differs from the Stage-0 oracle: {values} != {expected}")

        lock = json.loads((ROOT / "mncs-language.lock.json").read_text(encoding="utf-8"))
        total_wall = time.monotonic() - started
        report = {
            "schema_version": "mncs.compiler.project-language-fact-reuse/1",
            "status": "passed_language_fact_v3_project_self_consumption",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
            "compiler_revision": git("rev-parse", "HEAD"),
            "compiler_branch": git("branch", "--show-current"),
            "language_revision": lock["revision"],
            "language_fact_schema": FACT_SCHEMA,
            "verifier_identity": "mncs-language-function-body-validator/4",
            "stage0_probe_sha256": hashlib.sha256(app.PROBE.read_bytes()).hexdigest(),
            "vm_revision": subprocess.check_output(
                ["git", "-C", str(test_vm_emit.VM_REPO), "rev-parse", "HEAD"], text=True
            ).strip(),
            "vm_binary_sha256": hashlib.sha256(test_vm_emit.VM_BIN.read_bytes()).hexdigest(),
            "vm_runtime_build_origin": {
                "status": vm_runtime.get("build_origin", {}).get("status"),
                "source_revisions": vm_runtime.get("build_origin", {}).get("source_revisions"),
                "mismatch_count": vm_runtime.get("build_origin", {}).get("mismatch_count"),
                "mismatches": vm_runtime.get("build_origin", {}).get("mismatches"),
            },
            "harness": str(Path(__file__).relative_to(ROOT)),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "contracts": {"request": REQUEST_SCHEMA, "response": RESPONSE_SCHEMA,
                          "cache": CACHE_SCHEMA, "verification_work": WORK_SCHEMA},
            "module_closure": source_hashes(),
            "workload": {
                "root_module": ROOT_MODULE,
                "root_function": ROOT_FUNCTION,
                "callable": "mncs.compiler.project.v1::module_resolution_cache_key_equal<1,1024>",
                "reference_backend": reference_status.get("backend"),
                "cases": [{"id": case["id"], "expected": case["expected"]}
                          for case in cases],
                "case_count": len(cases),
            },
            "verified_fact_reuse": {
                "cold_body_fact_count": cold_work["produced_semantic_fact_count"],
                "cold_declaration_fact_count": cold_work["produced_declaration_fact_count"],
                "comment_reused_body_fact_count": len(comment_work["reused_semantic_facts"]),
                "comment_recomputed_body_facts": comment_work["recomputed_semantic_facts"],
                "comment_reused_declaration_fact_count": len(comment_work["reused_declaration_facts"]),
                "comment_recomputed_declaration_facts": comment_work["recomputed_declaration_facts"],
                "uncalled_body_edit_subject": UNREACHABLE_SUBJECT,
                "uncalled_body_edit_recomputed": unreachable_work["recomputed_semantic_facts"],
                "uncalled_body_edit_recomputation_reasons": unreachable_reasons,
                "uncalled_body_edit_reused_body_fact_count": len(
                    unreachable_work["reused_semantic_facts"]),
                "uncalled_body_edit_recomputed_declaration_facts": (
                    unreachable_work["recomputed_declaration_facts"]),
                "body_edit_subject": TARGET_SUBJECT,
                "body_edit_recomputed": sorted(recomputed),
                "body_edit_recomputation_reasons": reasons,
                "body_edit_reused_body_fact_count": len(edit_work["reused_semantic_facts"]),
                "body_edit_recomputed_declaration_facts": edit_work["recomputed_declaration_facts"],
                "fresh_process_per_snapshot": True,
            },
            "invalid_source": {
                "status": invalid.get("status"),
                "diagnostic_codes": [row.get("code") for row in invalid.get("diagnostics", [])
                                     if isinstance(row, dict)],
                "front_end_invocations": invalid_work.get("front_end_invocations"),
                "front_end_outcome": invalid_work.get("front_end_outcome"),
                "artifact_publication": invalid.get("cache", {}).get("publication"),
                "prior_success_did_not_mask_diagnostic": True,
            },
            "execution": {
                "stage0_values": expected,
                "canonical_vm_values": values,
                "stage0_vm_equivalent": values == expected,
                "artifact_identity": edited.get("artifact", {}).get("identity"),
                "canonical_vm": vm_metrics,
            },
            "measurements": {
                "total_wall_seconds": round(total_wall, 6),
                "cold_build": cold_metrics,
                "comment_edit": comment_metrics,
                "uncalled_body_edit": unreachable_metrics,
                "body_edit": edit_metrics,
                "invalid_source": invalid_metrics,
                "isolated_cgroup_peak_bytes": None,
                "isolated_cgroup_measurement": "unavailable",
            },
            "claim_boundary": "selected Project function application only; whole-compiler succession remains unproven",
        }
        output = ROOT / "evidence" / "project-language-fact-reuse-20261020.json"
        if output.exists():
            suffix = datetime.now(timezone.utc).strftime("%H%M%S")
            output = output.with_name(f"project-language-fact-reuse-20261020T{suffix}Z.json")
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return report


if __name__ == "__main__":
    print(json.dumps(main(), indent=2))
