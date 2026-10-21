#!/usr/bin/env python3
"""Apply Language fact v3 reuse to the bounded Compiler Decl frontier.

The selected application contains the current five-module dependency closure
and invokes mncs.compiler.decl.v1::check_unit on focused declaration cases.
Each source snapshot is compiled in a fresh Stage-0 probe process, while the
application cache carries only independently verified Language facts.
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
os.environ.setdefault(
    "MNCS_NATIVE_APP_PROBE_BIN",
    str(ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe"),
)
import test_decl as decl_tests  # noqa: E402
import test_vm_native_application as app  # noqa: E402
import test_vm_emit  # noqa: E402
import test_vm_segment  # noqa: E402

MODULE_FILES = {
    "mncs.compiler.source.v1": ROOT / "src/compiler/source.mncs",
    "mncs.compiler.lexer.v1": ROOT / "src/compiler/lexer.mncs",
    "mncs.compiler.parser.v1": ROOT / "src/compiler/parser.mncs",
    "mncs.compiler.segment.v1": ROOT / "src/compiler/segment.mncs",
    "mncs.compiler.decl.v1": ROOT / "src/compiler/decl.mncs",
}
PROFILES = {
    "mncs.compiler.source.v1": "0.19",
    "mncs.compiler.lexer.v1": "0.19",
    "mncs.compiler.parser.v1": "0.18",
    "mncs.compiler.segment.v1": "0.19",
    "mncs.compiler.decl.v1": "0.18",
}
ROOT_MODULE = "tests.compiler_decl_check_frontier"
ROOT_SOURCE_ID = "self-host/compiler-decl-check-frontier.mncs"
ROOT_SOURCE = """mncs 0.18;
module tests.compiler_decl_check_frontier;
use mncs.compiler.decl.v1 as decl;

fn run(pages: [[byte; up_to 1024]; up_to 1], stride: u64, total: u64) -> (result: decl.UnitCheck) {
    return decl.check_unit<1, 1024>(pages, stride, total);
}
"""
REQUEST_SCHEMA = "mncs.compiler.application-build-request/5"
RESPONSE_SCHEMA = "mncs.compiler.application-build/8"
CACHE_SCHEMA = "mncs.compiler.application-build-cache-observation/5"
WORK_SCHEMA = "mncs.compiler.application-build-verification-work/3"
FACT_SCHEMA = "mncs.language-verification-fact/3"
CHECKS = [decl_tests.CHECKS[index] for index in (0, 1, 2)]
STEP_BUDGET = decl_tests.EXECUTION_STEP_BUDGET


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
        "callable_roots": [{"module": ROOT_MODULE, "function": "run"}],
        "test_selection": {"kind": "all_source_cases"},
        "modules": modules,
        "generic_seeds": [],
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "compiler-decl-check-unit-language-fact-v3/1",
    }


def call_cases() -> list[dict]:
    result = []
    for index, (text, stage) in enumerate(CHECKS):
        data = text.encode("utf-8")
        if len(data) > decl_tests.STRIDE_BOUND:
            raise AssertionError("selected check_unit input exceeds one bounded page")
        result.append({
            "id": f"decl-check-unit-{index}-stage-{stage}",
            "text": text,
            "stage": stage,
            "arguments": decl_tests.logical_args(data),
            "type_arguments": [],
            "step_budget": STEP_BUDGET,
        })
    return result


def call_request(case: dict) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": ROOT_MODULE, "function": "run"},
        "arguments": case["arguments"],
        "type_arguments": case["type_arguments"],
        "step_budget": case["step_budget"],
    }


def compile_once(request: dict, cache: Path, label: str) -> tuple[dict, dict]:
    probe = app.Probe(
        cache_dir=cache / "program-cache",
        application_cache_dir=cache / "application-cache",
    )
    try:
        ready, _ = probe.send({"execution_status": True})
        if not isinstance(ready, dict) or "modules" not in ready:
            raise AssertionError("Stage-0 probe did not return its readiness handshake")
        build, metrics = probe.send({"compile_application": request}, timeout=600.0)
    finally:
        probe.close()
    if build.get("schema_version") != RESPONSE_SCHEMA:
        raise AssertionError(f"{label} returned the wrong application-build contract")
    return build, metrics


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
    if (work.get("front_end_invocations") != 1
            or work.get("front_end_outcome") not in ({"verified", "rejected"} if allow_rejected else {"verified"})):
        raise AssertionError(f"{label} did not verify the current source snapshot: {work}")
    if not isinstance(work.get("produced_semantic_fact_count"), int):
        raise AssertionError(f"{label} omitted Language body fact count")
    if not isinstance(work.get("produced_declaration_fact_count"), int):
        raise AssertionError(f"{label} omitted Language declaration fact count")
    return work


def run_oracle(modules: dict[str, str], cases: list[dict]) -> tuple[list[dict], dict]:
    probe = app.Probe(cache_dir=ROOT / ".build/probe-cache")
    try:
        response, metrics = probe.send({"project_oracle": {
            "root": ROOT_SOURCE,
            "modules": modules,
            "calls": [call_request(case) for case in cases],
        }}, timeout=600.0)
    finally:
        probe.close()
    if response.get("valid") is not True:
        raise AssertionError("Stage-0 rejected the selected Decl application: "
                             + json.dumps(response.get("diagnostics"), indent=2)[:5000])
    rows = response.get("executions", [])
    if len(rows) != len(cases) or any(row.get("status") != "returned" for row in rows):
        raise AssertionError("Stage-0 omitted a selected check_unit result: "
                             + json.dumps(rows, sort_keys=True)[:5000])
    values = []
    for case, row in zip(cases, rows):
        if len(row.get("returned", [])) != 1:
            raise AssertionError(f"Stage-0 returned a malformed result for {case['id']}")
        value = decl_tests.decode(row["returned"][0])
        decl_tests.check_check_unit(value, case["text"], case["stage"])
        values.append(value)
    return values, metrics


def run_vm(artifact: dict, cases: list[dict]) -> tuple[list[dict], dict]:
    with tempfile.TemporaryDirectory(prefix="mncs-decl-check-unit-vm-") as raw:
        temporary = Path(raw)
        artifact_path = temporary / "application.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        workload = [{
            "id": case["id"],
            "function": "run",
            "args": case["arguments"],
            "type_args": case["type_arguments"],
            "step_budget": case["step_budget"],
        } for case in cases]
        started = time.monotonic()
        document, reported_wall, peak_rss_kib, calls_bytes, results_bytes = (
            test_vm_segment.run_batch(artifact_path, workload, temporary, module=ROOT_MODULE)
        )
        wall = time.monotonic() - started
    rows = document.get("results", [])
    if len(rows) != len(cases):
        raise AssertionError("canonical VM omitted a selected check_unit result")
    values = []
    for case, row in zip(cases, rows):
        if row.get("id") != case["id"] or row.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {case['id']}: {row.get('outcome')}")
        returned = row.get("record", {}).get("returned", [])
        if len(returned) != 1:
            raise AssertionError(f"canonical VM returned a malformed result for {case['id']}")
        value = decl_tests.decode(test_vm_segment.vm_to_wire(returned[0]))
        decl_tests.check_check_unit(value, case["text"], case["stage"])
        values.append(value)
    return values, {
        "wall_seconds": round(wall, 6),
        "reported_wall_seconds": reported_wall,
        "peak_rss_kib": peak_rss_kib,
        "calls_bytes": calls_bytes,
        "results_bytes": results_bytes,
    }


def edit_check_unit_body(modules: dict[str, str]) -> dict[str, str]:
    changed = dict(modules)
    source = changed["mncs.compiler.decl.v1"]
    start = source.index("fn check_unit<P:")
    end = source.index("\n}\n", start) + 2
    function = source[start:end]
    old = "if !(u.ok)"
    if function.count(old) != 1:
        raise AssertionError("selected check_unit refactor anchor changed")
    changed["mncs.compiler.decl.v1"] = source[:start] + function.replace(
        old, "if u.ok == false", 1
    ) + source[end:]
    return changed


def diagnostic_codes(rows: list[dict]) -> list[str]:
    codes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if isinstance(row.get("code"), str):
            codes.append(row["code"])
        codes.extend(diagnostic_codes(row.get("related", [])))
    return codes


def main() -> dict:
    if not app.PROBE.is_file() or not test_vm_emit.VM_BIN.is_file():
        raise SystemExit("campaign Stage-0 probe or canonical VM binary is missing")
    modules = load_modules()
    cases = call_cases()
    changed_modules = edit_check_unit_body(modules)
    comment_root = ROOT_SOURCE + "\n// Source location and semantic comment-only edit.\n"
    started = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="mncs-decl-check-unit-fact-reuse-") as raw:
        cache = Path(raw)
        original_request = build_request(modules)
        cold, cold_metrics = compile_once(original_request, cache / "cold", "cold Decl build")
        cold_work = require_work(cold, "cold Decl build")
        if (cold.get("status") != "verified"
                or cold.get("cache", {}).get("status") != "miss"
                or cold.get("cache", {}).get("publication") != "stored"
                or not cold_work["produced_semantic_fact_count"]):
            raise AssertionError("cold Decl application did not publish Language v3 facts")

        comment_request = build_request(modules, comment_root)
        comment, comment_metrics = compile_once(
            comment_request, cache / "cold", "Decl comment edit")
        comment_work = require_work(comment, "Decl comment edit")
        if (comment.get("status") != "verified"
                or comment.get("cache", {}).get("status") != "hit"
                or len(comment_work["reused_semantic_facts"])
                != cold_work["produced_semantic_fact_count"]
                or comment_work["recomputed_semantic_facts"]
                or comment_work["recomputed_declaration_facts"]
                or comment.get("artifact", {}).get("identity")
                != cold.get("artifact", {}).get("identity")):
            raise AssertionError("comment edit did not reuse the complete Decl fact set")

        body_request = build_request(changed_modules)
        body, body_metrics = compile_once(body_request, cache / "cold", "Decl check_unit body edit")
        body_work = require_work(body, "Decl check_unit body edit")
        recomputed = set(body_work["recomputed_semantic_facts"])
        reused = set(body_work["reused_semantic_facts"])
        base_subject = "mncs:0.2:body:mncs.compiler.decl.v1::check_unit"
        changed_specializations = {
            subject for subject in recomputed
            if subject.startswith(base_subject + "__spec_")
        }
        root_subject = f"mncs:0.2:body:{ROOT_MODULE}::run"
        root_specializations = {
            subject for subject in recomputed
            if subject.startswith(root_subject + "__spec_")
        }
        expected_recomputed = {base_subject, root_subject} | changed_specializations
        reasons = {
            row["subject"]: row["reason"]
            for row in body_work["semantic_fact_recomputation_reasons"]
        }
        if (body.get("status") != "verified"
                or body.get("cache", {}).get("status") != "miss"
                or body_work.get("function_body_verification")
                != "recomputed_with_language_fact_reuse"
                or base_subject not in recomputed
                or not changed_specializations
                or root_specializations
                or recomputed != expected_recomputed
                or reasons.get(base_subject) != "subject_changed"
                or any(reasons.get(subject) != "subject_changed"
                       for subject in changed_specializations)
                or reasons.get(root_subject) != "dependencies_changed"
                or not reused
                or body_work["recomputed_declaration_facts"]
                or body.get("artifact", {}).get("identity")
                == cold.get("artifact", {}).get("identity")):
            raise AssertionError("Decl check_unit edit did not invalidate its exact fact closure: "
                                 + json.dumps({"recomputed": sorted(recomputed),
                                               "reasons": reasons}, sort_keys=True)[:5000])
        if len(reused) != cold_work["produced_semantic_fact_count"] - len(recomputed):
            raise AssertionError("Decl body edit did not retain every independent body fact")

        # A fresh invalid root source is compiled after the valid cache entries.
        invalid_root = ROOT_SOURCE.replace(
            "-> (result: decl.UnitCheck)", "-> (result: bool)", 1
        )
        if invalid_root == ROOT_SOURCE:
            raise AssertionError("invalid-source fixture did not change the root interface")
        invalid, invalid_metrics = compile_once(
            build_request(modules, invalid_root), cache / "cold", "Decl invalid root type")
        invalid_work = require_work(invalid, "Decl invalid root type", allow_rejected=True)
        codes = diagnostic_codes(invalid.get("diagnostics", []))
        if (invalid.get("status") != "rejected"
                or invalid_work.get("front_end_outcome") != "rejected"
                or not codes
                or invalid.get("artifact") is not None
                or invalid.get("cache", {}).get("publication") != "not_published"):
            raise AssertionError("prior Decl facts masked the current invalid root diagnostic: "
                                 + json.dumps({"status": invalid.get("status"),
                                               "codes": codes,
                                               "publication": invalid.get("cache", {}).get("publication")},
                                              sort_keys=True))

        expected, oracle_metrics = run_oracle(changed_modules, cases)
        actual, vm_metrics = run_vm(body["artifact"]["value"], cases)
        if actual != expected:
            raise AssertionError("Decl application canonical VM results differ from Stage-0")

        lock = json.loads((ROOT / "mncs-language.lock.json").read_text(encoding="utf-8"))
        result = {
            "schema_version": "mncs.compiler.decl-check-unit-language-fact-reuse/1",
            "status": "passed_language_fact_v3_decl_check_unit_self_consumption",
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
            "harness": str(Path(__file__).relative_to(ROOT)),
            "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "contracts": {
                "request": REQUEST_SCHEMA,
                "response": RESPONSE_SCHEMA,
                "verification_work": WORK_SCHEMA,
            },
            "module_closure": [
                {"module": name, "path": str(path.relative_to(ROOT)),
                 "profile": PROFILES[name], "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for name, path in MODULE_FILES.items()
            ],
            "workload": {
                "root_module": ROOT_MODULE,
                "callable": "mncs.compiler.decl.v1::check_unit<1,1024>",
                "selected_cases": [
                    {"id": case["id"], "expected_stage": case["stage"],
                     "source_bytes": len(case["text"].encode()),
                     "source_sha256": hashlib.sha256(case["text"].encode()).hexdigest()}
                    for case in cases
                ],
                "case_count": len(cases),
            },
            "verified_fact_reuse": {
                "cold_body_fact_count": cold_work["produced_semantic_fact_count"],
                "cold_declaration_fact_count": cold_work["produced_declaration_fact_count"],
                "comment_edit_reused_body_facts": len(comment_work["reused_semantic_facts"]),
                "comment_edit_recomputed_body_facts": len(comment_work["recomputed_semantic_facts"]),
                "comment_edit_reused_declaration_facts": len(comment_work["reused_declaration_facts"]),
                "comment_edit_recomputed_declaration_facts": len(comment_work["recomputed_declaration_facts"]),
                "changed_subject": base_subject,
                "body_edit_recomputed": sorted(recomputed),
                "body_edit_recomputation_reasons": reasons,
                "body_edit_reused_body_fact_count": len(reused),
                "body_edit_recomputed_declaration_facts": body_work["recomputed_declaration_facts"],
                "fresh_process_per_snapshot": True,
            },
            "invalid_source": {
                "status": invalid.get("status"),
                "diagnostic_codes": codes,
                "front_end_invocations": invalid_work.get("front_end_invocations"),
                "front_end_outcome": invalid_work.get("front_end_outcome"),
                "artifact_publication": invalid.get("cache", {}).get("publication"),
                "prior_success_did_not_mask_diagnostic": True,
            },
            "execution": {
                "stage0_matches_canonical_vm": True,
                "artifact_identity_before_edit": cold["artifact"]["identity"],
                "artifact_identity_after_edit": body["artifact"]["identity"],
                "edited_artifact_regenerated": body["artifact"]["identity"] != cold["artifact"]["identity"],
                "canonical_vm_artifact_identity": body["artifact"]["value"]["artifact_id"],
            },
            "measurements": {
                "cold_build": cold_metrics,
                "comment_edit_build": comment_metrics,
                "decl_body_edit_build": body_metrics,
                "invalid_source_build": invalid_metrics,
                "stage0_oracle": oracle_metrics,
                "canonical_vm_batch": vm_metrics,
                "whole_frontier_wall_seconds": round(time.monotonic() - started, 6),
                "cgroup_measurement_available": cold_metrics.get("peak_cgroup_memory_bytes") is not None,
            },
            "limitations": [
                "The bounded Decl application includes the full Decl source and its Source, Lexer, Parser, and Segment dependencies, but does not verify the full Compiler project.",
                "The selected canonical-VM batch contains three existing check_unit cases (valid, duplicate-name, unresolved-call) and is not a replacement for the 9-case direct frontier or full Test runner.",
                "Cgroup peak memory is unavailable; process metrics are workload observations, not guarantees.",
                "This does not establish Stage-1 or Stage-2 succession.",
            ],
            "stage1_claimed": False,
            "stage2_claimed": False,
        }

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence_path = ROOT / f"evidence/self-host-decl-check-unit-language-fact-reuse-{stamp}.json"
    evidence_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    result["evidence_artifact"] = str(evidence_path.relative_to(ROOT))
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
