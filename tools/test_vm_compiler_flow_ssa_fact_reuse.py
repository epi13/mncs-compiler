#!/usr/bin/env python3
"""Exercise Language v3 fact reuse in the bounded Flow-to-SSA application.

The selected seven-module application runs Flow lowering and value SSA inside
one canonical VM artifact. Each source snapshot is compiled in a fresh
Stage-0 process while the Language fact and executable caches are retained.
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
from contextlib import nullcontext
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from ensure_locked_stage0_probe import ensure_locked_stage0_probe  # noqa: E402

os.environ["MNCS_NATIVE_APP_PROBE_BIN"] = str(ensure_locked_stage0_probe(ROOT))
os.environ.setdefault("MNCS_PROBE_BIN", os.environ["MNCS_NATIVE_APP_PROBE_BIN"])
import test_flow  # noqa: E402
import test_vm_flow_ssa_frontier as flow_frontier  # noqa: E402
import test_vm_native_application as app  # noqa: E402
import test_vm_emit  # noqa: E402
import test_vm_segment  # noqa: E402

MODULE_NAMES = ["source", "lexer", "parser", "segment", "decl", "flow", "ssa"]
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
}
ROOT_MODULE = flow_frontier.APP_MODULE
ROOT_FUNCTION = flow_frontier.APP_FUNCTION
ROOT_SOURCE_ID = "self-host/compiler-flow-ssa-frontier.mncs"
ROOT_SOURCE = flow_frontier.APP_SOURCE
REQUEST_SCHEMA = "mncs.compiler.application-build-request/5"
RESPONSE_SCHEMA = "mncs.compiler.application-build/8"
CACHE_SCHEMA = "mncs.compiler.application-build-cache-observation/5"
WORK_SCHEMA = "mncs.compiler.application-build-verification-work/3"
FACT_SCHEMA = "mncs.language-verification-fact/3"
STEP_BUDGET = flow_frontier.STEP_BUDGET


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
        "verification_policy": "compiler-flow-ssa-language-fact-v3/1",
    }


def cases() -> list[dict]:
    result = []
    for name, source, expected_shape, expected_iterations in flow_frontier.CASES:
        data = source.encode("utf-8")
        result.append({
            "id": name,
            "name": name,
            "source": source,
            "source_bytes": len(data),
            "source_sha256": hashlib.sha256(data).hexdigest(),
            "expected_shape": expected_shape,
            "expected_iterations": expected_iterations,
            "arguments": test_flow.logical_args(data),
        })
    return result


def compile_once(request: dict, cache: Path, label: str) -> tuple[dict, dict]:
    probe = app.Probe(
        cache_dir=cache / "program-cache",
        application_cache_dir=cache / "application-cache",
    )
    try:
        ready, _ = probe.send({"execution_status": True})
        if not isinstance(ready, dict) or "modules" not in ready:
            raise AssertionError("Stage-0 probe did not return its readiness handshake")
        build, metrics = probe.send({"compile_application": request}, timeout=900.0)
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
    outcomes = {"verified", "rejected"} if allow_rejected else {"verified"}
    if work.get("front_end_invocations") != 1 or work.get("front_end_outcome") not in outcomes:
        raise AssertionError(f"{label} did not freshly verify the current source: {work}")
    for field in ("produced_semantic_fact_count", "produced_declaration_fact_count"):
        if not isinstance(work.get(field), int):
            raise AssertionError(f"{label} omitted Language fact count {field}")
    return work


def edit_flow_body(modules: dict[str, str]) -> dict[str, str]:
    changed = dict(modules)
    source = changed["mncs.compiler.flow.v1"]
    start = source.index("fn lower_proven_unit<P:")
    end = source.index("\n}", start) + 2
    function = source[start:end]
    old = "if !(proof.ok)"
    if function.count(old) != 1:
        raise AssertionError("selected lower_proven_unit semantics-preserving edit anchor changed")
    changed["mncs.compiler.flow.v1"] = source[:start] + function.replace(
        old, "if proof.ok == false", 1
    ) + source[end:]
    return changed


def shape(functions: list[dict]) -> dict[str, int]:
    terms = [next(iter(block["terminator"]))
             for function in functions for block in function.get("blocks", [])]
    return {
        "functions": len(functions),
        "branches": sum(term == "ConditionalBranch" for term in terms),
        "jumps": sum(term == "Branch" for term in terms),
        "returns": sum(term == "Return" for term in terms),
        "failures": sum(term == "Failure" for term in terms),
        "switches": sum(term in {"FiniteSwitch", "ScalarSwitch"} for term in terms),
        "blocks": len(terms),
        "instructions": sum(len(block.get("instructions", []))
                             for function in functions for block in function.get("blocks", [])),
    }


def stage0_oracles(selected: list[dict]) -> list[dict]:
    probe = flow_frontier.Probe()
    result = []
    try:
        for case in selected:
            response = probe.send({"ssa": case["source"]})
            if response.get("ssa") is None or response.get("diagnostics"):
                raise AssertionError(f"Stage-0 rejected Flow-to-SSA case {case['name']}: {response}")
            actual = shape(response["ssa"]["functions"])
            expected = case["expected_shape"]
            if any(actual[key] != value for key, value in expected.items()):
                raise AssertionError(f"Stage-0 case changed {case['name']}: {actual} != {expected}")
            facts = flow_frontier.stage0_loop_instruction_facts(response["ssa"]["functions"])
            if len(facts) != 6 * case["expected_iterations"]:
                raise AssertionError(f"Stage-0 loop facts changed {case['name']}: {facts}")
            result.append({"shape": actual, "loop_instruction_facts": facts})
    finally:
        probe.close()
    return result


def run_vm(artifact: dict, selected: list[dict]) -> tuple[list[dict], dict]:
    with tempfile.TemporaryDirectory(prefix="mncs-flow-ssa-fact-vm-") as raw:
        temporary = Path(raw)
        artifact_path = temporary / "application.vm.json"
        artifact_path.write_text(json.dumps(artifact, sort_keys=True) + "\n")
        workload = [{
            "id": case["id"], "function": ROOT_FUNCTION,
            "args": case["arguments"], "type_args": [], "step_budget": STEP_BUDGET,
        } for case in selected]
        started = time.monotonic()
        document, reported_wall, peak_rss_kib, calls_bytes, results_bytes = (
            test_vm_segment.run_batch(artifact_path, workload, temporary, module=ROOT_MODULE)
        )
        wall = time.monotonic() - started
    if document.get("artifact_id") != artifact.get("artifact_id"):
        raise AssertionError("canonical VM admitted a different Flow-to-SSA artifact")
    rows = document.get("results", [])
    if len(rows) != len(selected):
        raise AssertionError("canonical VM omitted a selected Flow-to-SSA result")
    summaries = []
    for case, row in zip(selected, rows, strict=True):
        if row.get("id") != case["id"] or row.get("outcome") != {"kind": "completed"}:
            raise AssertionError(f"canonical VM failed {case['name']}: {row.get('outcome')}")
        returned = row.get("record", {}).get("returned", [])
        if len(returned) != 1:
            raise AssertionError(f"canonical VM returned malformed summary for {case['name']}")
        summary = test_flow.decode(test_vm_segment.vm_to_wire(returned[0]))
        if (not summary["valid"] or summary["function_count"] != 1
                or summary["supported_function_count"] != 1
                or summary["verified_function_count"] != 1
                or not summary["loop_instruction_counts_complete"]):
            raise AssertionError(f"Flow-to-SSA summary is not verified for {case['name']}: {summary}")
        loops = {
            "index_init": summary["loop_index_init_count"],
            "guard": summary["loop_guard_count"],
            "index_advance": summary["loop_index_advance_count"],
        }
        if any(value != case["expected_iterations"] for value in loops.values()):
            raise AssertionError(f"Flow loop operation inventory changed for {case['name']}: {loops}")
        expected = case["expected_shape"]
        pairs = {
            "function_count": "functions",
            "branch_count": "branches",
            "jump_count": "jumps",
            "return_count": "returns",
            "failure_count": "failures",
            "switch_count": "switches",
        }
        if any(summary[value_key] != expected[shape_key]
               for value_key, shape_key in pairs.items()):
            raise AssertionError(f"canonical VM Flow-to-SSA shape differs from Stage-0 for {case['name']}")
        summaries.append({"summary": summary, "loop_instruction_counts": loops,
                          "execution_steps": row["record"]["usage"]["steps"]})
    return summaries, {
        "wall_seconds": round(wall, 6),
        "reported_wall_seconds": reported_wall,
        "peak_rss_kib": peak_rss_kib,
        "calls_bytes": calls_bytes,
        "results_bytes": results_bytes,
    }


def main() -> dict:
    if not app.PROBE.is_file() or not test_vm_emit.VM_BIN.is_file():
        raise SystemExit("campaign Stage-0 probe or canonical VM binary is missing")
    modules = load_modules()
    changed_modules = edit_flow_body(modules)
    selected = cases()
    oracles = stage0_oracles(selected)
    comment_root = ROOT_SOURCE.replace(
        "fn run(pages", "// Shift the root declaration while preserving its semantics.\nfn run(pages", 1
    )
    if comment_root == ROOT_SOURCE:
        raise AssertionError("comment fixture did not move current source locations")
    started = time.monotonic()

    persistent_cache = os.environ.get("MNCS_FLOW_SSA_FACT_CACHE_DIR")
    if persistent_cache:
        cache_root = Path(persistent_cache).expanduser().resolve()
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_context = nullcontext(str(cache_root))
    else:
        cache_context = tempfile.TemporaryDirectory(
            prefix="mncs-flow-ssa-language-fact-reuse-"
        )
    with cache_context as raw:
        cache = Path(raw)
        cold, cold_metrics = compile_once(build_request(modules), cache / "cold", "cold Flow-to-SSA build")
        cold_work = require_work(cold, "cold Flow-to-SSA build")
        if (cold.get("status") != "verified"
                or cold.get("cache", {}).get("status") != "miss"
                or cold.get("cache", {}).get("publication") != "stored"
                or not cold_work["produced_semantic_fact_count"]):
            raise AssertionError("cold Flow-to-SSA application did not publish Language v3 facts")

        comment, comment_metrics = compile_once(
            build_request(modules, comment_root), cache / "cold", "Flow-to-SSA comment edit")
        comment_work = require_work(comment, "Flow-to-SSA comment edit")
        if (comment.get("status") != "verified"
                or comment.get("cache", {}).get("status") != "hit"
                or len(comment_work["reused_semantic_facts"])
                != cold_work["produced_semantic_fact_count"]
                or comment_work["recomputed_semantic_facts"]
                or comment_work["recomputed_declaration_facts"]
                or comment.get("artifact", {}).get("identity")
                != cold.get("artifact", {}).get("identity")):
            raise AssertionError("Flow-to-SSA comment edit did not reuse all verified facts")

        edited, edit_metrics = compile_once(
            build_request(changed_modules), cache / "cold", "Flow lower_proven_unit body edit")
        edit_work = require_work(edited, "Flow lower_proven_unit body edit")
        recomputed = set(edit_work["recomputed_semantic_facts"])
        reasons = {row["subject"]: row["reason"]
                   for row in edit_work["semantic_fact_recomputation_reasons"]}
        changed_subject = "mncs:0.2:body:mncs.compiler.flow.v1::lower_proven_unit"
        lower_unit_subject = "mncs:0.2:body:mncs.compiler.flow.v1::lower_unit"
        flow_specializations = {
            subject for subject in recomputed
            if subject.startswith(changed_subject + "__spec_")
        }
        lower_unit_specializations = {
            subject for subject in recomputed
            if subject.startswith(lower_unit_subject + "__spec_")
        }
        ssa_unit_subject = "mncs:0.2:body:mncs.compiler.ssa.v1::lower_flow_unit_value_ssa"
        ssa_unit_specializations = {
            subject for subject in recomputed
            if subject.startswith(ssa_unit_subject + "__spec_")
        }
        root_subject = f"mncs:0.2:body:{ROOT_MODULE}::{ROOT_FUNCTION}"
        dependency_changed_subjects = {
            lower_unit_subject,
            *lower_unit_specializations,
            ssa_unit_subject,
            *ssa_unit_specializations,
            "mncs:0.2:body:mncs.compiler.ssa.v1::lower_flow_unit_value_ssa_with_imported_schemas",
            "mncs:0.2:body:mncs.compiler.ssa.v1::verify_function",
            "mncs:0.2:body:mncs.compiler.ssa.v1::verify_function_source_flow",
            root_subject,
        }
        expected_recomputed = {
            changed_subject,
        } | flow_specializations | dependency_changed_subjects
        if (edited.get("status") != "verified"
                or edited.get("cache", {}).get("status") != "miss"
                or not flow_specializations
                or not lower_unit_specializations
                or not ssa_unit_specializations
                or recomputed != expected_recomputed
                or changed_subject not in recomputed
                or reasons.get(changed_subject) != "subject_changed"
                or any(reasons.get(subject) != "subject_changed"
                       for subject in flow_specializations)
                or any(reasons.get(subject) != "dependencies_changed"
                       for subject in dependency_changed_subjects)
                or not edit_work["reused_semantic_facts"]
                or edit_work["recomputed_declaration_facts"]
                or edited.get("artifact", {}).get("identity")
                == cold.get("artifact", {}).get("identity")):
            raise AssertionError("Flow body edit did not preserve and invalidate the actual Language dependency closure: "
                                 + json.dumps({"recomputed": sorted(recomputed), "reasons": reasons}, sort_keys=True)[:6000])
        if len(edit_work["reused_semantic_facts"]) + len(recomputed) != cold_work["produced_semantic_fact_count"]:
            raise AssertionError("Flow body edit lost independent verified body facts")

        invalid_root = ROOT_SOURCE.replace(
            "-> (result: FlowSsaFrontierSummary)", "-> (result: bool)", 1
        )
        if invalid_root == ROOT_SOURCE:
            raise AssertionError("invalid Flow root fixture failed to change its result type")
        invalid, invalid_metrics = compile_once(
            build_request(modules, invalid_root), cache / "cold", "invalid Flow root type")
        invalid_work = require_work(invalid, "invalid Flow root type", allow_rejected=True)
        if (invalid.get("status") != "rejected"
                or invalid_work.get("front_end_outcome") != "rejected"
                or not invalid.get("diagnostics")
                or invalid.get("artifact") is not None
                or invalid.get("cache", {}).get("publication") != "not_published"):
            raise AssertionError("prior Flow facts masked a fresh invalid-root diagnostic")

        vm_values, vm_metrics = run_vm(edited["artifact"]["value"], selected)
        for case, oracle, actual in zip(selected, oracles, vm_values, strict=True):
            expected = oracle["shape"]
            summary = actual["summary"]
            if (summary["block_count"] != expected["blocks"]
                    or summary["instruction_count"] - sum(actual["loop_instruction_counts"].values())
                    != expected["instructions"] - len(oracle["loop_instruction_facts"])):
                raise AssertionError(f"canonical VM SSA instructions differ outside compact loop ops for {case['name']}")

        lock = json.loads((ROOT / "mncs-language.lock.json").read_text(encoding="utf-8"))
        report = {
            "schema_version": "mncs.compiler.flow-ssa-language-fact-reuse/1",
            "status": "passed_language_fact_v3_flow_to_ssa_self_consumption",
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
            "contracts": {"request": REQUEST_SCHEMA, "response": RESPONSE_SCHEMA,
                          "verification_work": WORK_SCHEMA},
            "module_closure": [
                {"module": name, "path": str(path.relative_to(ROOT)),
                 "profile": PROFILES[name], "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                for name, path in MODULE_FILES.items()
            ],
            "workload": {
                "root_module": ROOT_MODULE,
                "root_function": ROOT_FUNCTION,
                "callable": "mncs.compiler.ssa.v1::lower_flow_unit_value_ssa<1024,1024>",
                "selected_cases": [
                    {"id": case["id"], "source_sha256": case["source_sha256"],
                     "source_bytes": case["source_bytes"],
                     "stage0_ssa_shape": oracle["shape"],
                     "expected_iterations": case["expected_iterations"],
                     "stage0_loop_instruction_facts": oracle["loop_instruction_facts"]}
                    for case, oracle in zip(selected, oracles, strict=True)
                ],
                "case_count": len(selected),
            },
            "verified_fact_reuse": {
                "cold_body_fact_count": cold_work["produced_semantic_fact_count"],
                "cold_declaration_fact_count": cold_work["produced_declaration_fact_count"],
                "comment_edit_reused_body_facts": len(comment_work["reused_semantic_facts"]),
                "comment_edit_recomputed_body_facts": len(comment_work["recomputed_semantic_facts"]),
                "comment_edit_reused_declaration_facts": len(comment_work["reused_declaration_facts"]),
                "comment_edit_recomputed_declaration_facts": len(comment_work["recomputed_declaration_facts"]),
                "body_edit_changed_subject": changed_subject,
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
                "stage0_matches_canonical_vm": True,
                "artifact_identity_before_edit": cold["artifact"]["identity"],
                "artifact_identity_after_edit": edited["artifact"]["identity"],
                "artifact_regenerated": True,
                "canonical_vm_artifact_identity": edited["artifact"]["identity"],
                "canonical_vm_summaries": [
                    {"case": case["id"], "summary": value["summary"],
                     "loop_instruction_counts": value["loop_instruction_counts"],
                     "steps": value["execution_steps"]}
                    for case, value in zip(selected, vm_values, strict=True)
                ],
            },
            "measurements": {
                "cold_build": cold_metrics,
                "comment_edit_build": comment_metrics,
                "body_edit_build": edit_metrics,
                "invalid_source_build": invalid_metrics,
                "canonical_vm_batch": vm_metrics,
                "cgroup_measurement_available": cold_metrics.get("cgroup_memory_peak_bytes") is not None,
                "whole_frontier_wall_seconds": round(time.monotonic() - started, 6),
            },
            "limitations": [
                "The closure covers Source, Lexer, Parser, Segment, Decl, Flow, and SSA; it does not verify the full Compiler project.",
                "The selected Flow-to-SSA workload contains two focused source cases and does not establish full CFG/IR parity.",
                "This does not establish Stage-1 or Stage-2 succession.",
            ],
            "stage1_claimed": False,
            "stage2_claimed": False,
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = ROOT / "evidence" / f"self-host-flow-ssa-language-fact-reuse-{stamp}.json"
        report["evidence_artifact"] = report_path.relative_to(ROOT).as_posix()
        with report_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    main()
