#!/usr/bin/env python3
"""Probe real compiler modules through native project proof, flow, and SSA."""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import test_project as project

sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "compiler"


def _source_module_name(source, module):
    header = module["flow"]["proof"]["unit"]["header"]
    return source[header["module_start"]:header["module_end"]].decode()


def _module_summary(module, source):
    proof = module["flow"]["proof"]
    flow_functions = project.flist(module["flow"]["functions"])
    ssa = module["value_ssa"]
    summary = {
        "source_id": bytes(module["source_id"]).decode(),
        "module": _source_module_name(source, module),
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "proof_ok": proof["ok"],
        "proof_obligation_count": len(project.flist(proof["obls"])),
        "flow_valid": module["flow"]["valid"],
        "flow_function_count": len(flow_functions),
        "flow_verified_function_count": sum(1 for function in flow_functions if function["verified"]),
        "ssa_valid": ssa["valid"],
        "ssa_function_count": ssa["function_count"],
        "ssa_supported_function_count": ssa["supported_function_count"],
        "ssa_verified_function_count": ssa["verified_function_count"],
        "ssa_value_count": ssa["value_count"],
        "ssa_instruction_count": ssa["instruction_count"],
        "ssa_unsupported_block_count": ssa["unsupported_block_count"],
        "first_unsupported": {
            "function": ssa["first_unsupported_function"],
            "block": ssa["first_unsupported_block"],
            "kind": ssa["first_unsupported_kind"],
        } if ssa["unsupported_block_count"] else None,
    }
    if not proof["ok"]:
        failed = [obligation for obligation in project.flist(proof["obls"])
                  if obligation["status"] == 1]
        summary["proof_failure"] = {
            "error_span": [proof["err_start"], proof["err_end"]],
            "failed_obligation_count": len(failed),
            "first_failed_obligation": failed[0] if failed else None,
        }
    return summary


def _load_sources(module_names):
    selected = None if not module_names else set(module_names)
    paths = sorted(SOURCE_ROOT.glob("*.mncs"), key=lambda path: path.name.encode())
    if selected is not None:
        paths = [path for path in paths if path.stem in selected]
        missing = selected - {path.stem for path in paths}
        if missing:
            raise ValueError(f"unknown compiler modules: {sorted(missing)}")
    return [(path.name, path, path.read_bytes()) for path in paths]


def _module_name_from_source(source):
    found = re.search(rb"\bmodule\s+([^;]+);", source)
    return found.group(1).decode() if found else ""


def _seed_target_specialization():
    seed = {
        "module": project.PROJECT_MODULE,
        "function": "compile_project_target",
        "type_arguments": [
            {"kind": "nat", "value": 1024},
            {"kind": "nat", "value": 1024},
        ],
    }
    try:
        seeds = json.loads(os.environ.get("MNCS_PROBE_GENERIC_SEEDS", "[]"))
    except ValueError:
        seeds = []
    if not any(item.get("module") == seed["module"]
               and item.get("function") == seed["function"]
               and item.get("type_arguments") == seed["type_arguments"]
               for item in seeds if isinstance(item, dict)):
        seeds.append(seed)
    os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(seeds)


def run(module_names=None, target_last=False):
    if target_last:
        _seed_target_specialization()
    lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    probe = project.Probe()
    try:
        execution = probe.send({"execution_status": True})
        identities = project.identity_map(probe)
        sources = _load_sources(module_names)
        request = project.request_value(identities, sources)
        target_stem = None
        target_source_index = None
        if target_last:
            if not module_names:
                raise ValueError("--target-last requires an explicit --modules selection")
            target_stem = module_names[-1]
            target_source_index = next(i for i, (source_id, _, _) in enumerate(sources)
                                       if Path(source_id).stem == target_stem)
            request["target"] = {"module": project.PROJECT_MODULE, "function": "compile_project_target"}
            request["arguments"].append(project.integer(target_source_index))
        response = probe.send(request)
        page_count = sum((len(source) + project.STRIDE_DEFAULT - 1) // project.STRIDE_DEFAULT for _, _, source in sources)
        module_rows = []
        stage0_oracle = {"status": "not-run", "reason": "whole compiler project exceeds the step-counted oracle envelope; focused Stage-0 differentials are run separately"}
        native = None
        if response.get("status") == "returned" and response.get("returned"):
            probe.steps.append(response["steps"])
            native = project.decode(response["returned"][0])
            for module in project.flist(native["modules"]):
                source_index = module["source_index"]
                source = sources[source_index][2]
                module_rows.append(_module_summary(module, source))
            if module_names:
                root_stem = module_names[-1]
                root_index = next(i for i, (source_id, _, _) in enumerate(sources)
                                  if Path(source_id).stem == root_stem)
                module_map = {
                    _module_name_from_source(source): source.decode()
                    for i, (_, _, source) in enumerate(sources) if i != root_index
                }
                oracle = probe.send({"project_oracle": {
                    "root": sources[root_index][2].decode(),
                    "modules": module_map,
                }})
                stage0_oracle = {
                    "status": ("valid" if oracle.get("valid") else
                               "invalid" if "valid" in oracle else
                               oracle.get("status", "not-returned")),
                    "valid": oracle.get("valid"),
                    "diagnostics": oracle.get("diagnostics"),
                    "linked_functions": len(oracle.get("program", {}).get("functions", [])) if oracle.get("program") else 0,
                    "ssa_functions": len(oracle.get("ssa", {}).get("functions", [])) if oracle.get("ssa") else 0,
                }
        else:
            stage0_oracle = {"status": "not-run", "reason": "native request was rejected before project execution"}
        report = {
            "schema_version": 1,
            "kind": "compiler-module-pipeline-frontier",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler_head": head,
            "compiler_branch": subprocess.run(
                ["git", "branch", "--show-current"], cwd=ROOT, check=True,
                capture_output=True, text=True,
            ).stdout.strip(),
            "stage0_revision": lock["revision"],
            "stage0_source_profile": lock["source_profile"],
            "selected_modules": module_names or "all",
            "lowering_scope": "target-module" if target_last else "all-modules",
            "target_source_index": target_source_index,
            "compiler_source_count": len(sources),
            "input_total_page_count_at_stride_1024": page_count,
            "input_page_bound": 1024,
            "compiler_source_sha256": hashlib.sha256(b"".join(
                source_id.encode() + b"\0" + hashlib.sha256(source).digest()
                for source_id, _, source in sources
            )).hexdigest(),
            "execution_backend": execution["backend"],
            "retained_sessions": execution["retained_sessions"],
            "native_request_status": response.get("status"),
            "native_failure": response.get("failure"),
            "native_project_valid": native["valid"] if native else None,
            "native_project_ssa_valid": native["value_ssa_valid"] if native else None,
            "module_count": len(module_rows),
            "modules": module_rows,
            "diagnostics": project.flist(native["diagnostics"]) if native else [],
            "stage0_oracle": stage0_oracle,
            "requests": probe.requests,
            "native_execution_steps_total": sum(probe.steps),
            "result_sha256": probe.digest.hexdigest(),
        }
        selection = "all" if not module_names else "-".join(module_names)
        if target_stem is not None:
            selection += "-target-" + target_stem
        artifact = ROOT / ".build" / "campaign-artifacts" / "compiler-facts" / (
            f"campaign-{datetime.now(timezone.utc):%Y%m%d}-compiler-module-pipeline-{selection}.json"
        )
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps(report, indent=2) + "\n")
        report["evidence_artifact"] = str(artifact.relative_to(ROOT))
        return report
    finally:
        probe.close()


if __name__ == "__main__":
    names = None
    args = sys.argv[1:]
    target_last = "--target-last" in args
    args = [arg for arg in args if arg != "--target-last"]
    if args:
        if args[0] != "--modules" or len(args) < 2:
            raise SystemExit("usage: probe_compiler_module_frontier.py [--modules module_stem ...] [--target-last]")
        names = args[1:]
    print(json.dumps(run(names, target_last=target_last), indent=2))
