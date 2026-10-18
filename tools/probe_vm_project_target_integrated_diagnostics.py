#!/usr/bin/env python3
"""Build and execute the integrated Project diagnostic-location entrypoint."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
PROJECT_MODULE = "mncs.compiler.project.v1"
ENTRY_FUNCTION = "compile_project_target_with_diagnostic_locations"
TYPE_ARGS = [
    {"kind": "nat", "value": 1024},
    {"kind": "nat", "value": 1024},
]
STEP_BUDGET = 8_000_000
CACHE_DIR = ROOT / ".build/project-integrated-diagnostic-app-cache"
ARTIFACT_PATH = ROOT / ".build/project-integrated-diagnostic-app.vm.json"
RUN_DIR = ROOT / ".build/project-integrated-diagnostic-app-run"
EVIDENCE_PATH = ROOT / "evidence/project-target-integrated-diagnostic-locations-canonical-vm-20261018.json"
PROBE = ROOT / "tools/stage0-probe/target/release/mncs-compiler-stage0-probe"

os.environ["MNCS_PROBE_BACKEND"] = "cranelift"
os.environ["MNCS_PROBE_MODULES"] = "source,lexer,parser,segment,decl,flow,ssa,project"
os.environ["MNCS_PROBE_EXECUTION_MODULES"] = PROJECT_MODULE
os.environ["MNCS_PROBE_REQUEST_TIMEOUT_S"] = "300"
os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
    {"module": PROJECT_MODULE, "function": "compile_project_target",
     "type_arguments": TYPE_ARGS},
    {"module": PROJECT_MODULE, "function": "locate_project_diagnostics_global",
     "type_arguments": TYPE_ARGS},
    {"module": PROJECT_MODULE, "function": ENTRY_FUNCTION,
     "type_arguments": TYPE_ARGS},
])

sys.path.insert(0, str(ROOT / "tools"))
import test_project as project  # noqa: E402
import test_vm_emit  # noqa: E402
import test_vm_native_application as app  # noqa: E402
from test_vm_segment import vm_to_wire  # noqa: E402


def _build_request() -> dict:
    source_paths = sorted((ROOT / "src/compiler").glob("*.mncs"))
    names: dict[Path, str] = {}
    contents: dict[Path, str] = {}
    for path in source_paths:
        source = path.read_text()
        match = re.search(r"\bmodule\s+([^;]+);", source)
        if match is None:
            raise ValueError(f"module declaration missing in {path.name}")
        names[path] = match.group(1)
        contents[path] = source
    root = ROOT / "src/compiler/project.mncs"
    return {
        "schema_version": "mncs.compiler.application-build-request/2",
        "target": "mncs-vm-0.1",
        "root_source_id": "src/compiler/project.mncs",
        "root_source": contents[root],
        "callable_roots": [{
            "module": PROJECT_MODULE,
            "function": "module_resolution_cache_lookup_done",
        }],
        "modules": {
            names[path]: contents[path]
            for path in source_paths if path != root
        },
        "generic_seeds": [
            {"module": PROJECT_MODULE, "function": name,
             "type_arguments": TYPE_ARGS}
            for name in (
                "compile_project_target",
                "locate_project_diagnostics_global",
                ENTRY_FUNCTION,
            )
        ],
        "capabilities": [],
        "verification_policy": "mncs-compiler-project-integrated-diagnostics-canonical-vm/1",
    }


def _build_application(request: dict) -> tuple[dict, dict, float, str]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    probe = app.Probe(
        cache_dir=CACHE_DIR,
        executable=PROBE,
        application_cache_dir=CACHE_DIR / "application",
    )
    started = time.monotonic()
    try:
        response, metrics = probe.send(
            {"compile_application": request}, timeout=300.0,
        )
        wall = time.monotonic() - started
        stderr = probe.close()
        return response, metrics, wall, stderr[-2000:]
    except BaseException:
        if probe.proc.poll() is None:
            probe.proc.kill()
            probe.proc.wait(timeout=5)
        raise


def _cases() -> list[dict]:
    missing = (
        b"mncs 0.18;\nmodule demo.root;\n"
        b"use demo.absent as absent;\n"
        b"fn main() -> (r: u64) { return 1; }\n"
    )
    multi_page = (
        b"mncs 0.18;\nmodule demo.multi_page;\n"
        + b"\n" * 1100
        + b"use demo.absent as absent;\n"
        + b"fn main() -> (r: u64) { return 1; }\n"
    )
    parse_failure = (
        b"mncs 0.18;\nmodule demo.broken;\n"
        b"fn main( -> (r: u64) { return 1; }\n"
    )
    valid = (
        b"mncs 0.18;\nmodule demo.valid;\n"
        b"fn main() -> (r: u64) { return 1; }\n"
    )
    return [
        {"id": "valid-project", "source_id": "valid.mncs", "source": valid,
         "expected_valid": True, "expected_variant": None},
        {"id": "missing-import", "source_id": "missing.mncs", "source": missing,
         "expected_valid": False, "expected_variant": 3,
         "expected_span": (missing.index(b"demo.absent"),
                           missing.index(b"demo.absent") + len(b"demo.absent"))},
        {"id": "multi-page-missing-import", "source_id": "multi-page.mncs",
         "source": multi_page, "expected_valid": False, "expected_variant": 3,
         "expected_span": (multi_page.index(b"demo.absent"),
                           multi_page.index(b"demo.absent") + len(b"demo.absent"))},
        {"id": "parse-failure", "source_id": "broken.mncs", "source": parse_failure,
         "expected_valid": False, "expected_variant": 4},
        {"id": "invalid-page-count", "source_id": "invalid-pages.mncs",
         "source": missing, "expected_valid": False, "expected_variant": 1,
         "invalid_page_count": True},
    ]


def _request(probe, identities: dict, case: dict) -> dict:
    request = project.request_value(
        identities, [(case["source_id"], case["source_id"], case["source"])],
    )
    request["target"]["function"] = ENTRY_FUNCTION
    request["arguments"].append(project.integer(0))
    request["step_budget"] = STEP_BUDGET
    if case.get("invalid_page_count"):
        sources = project.wire_sequence(
            project.wire_field(request["arguments"][0], "sources"),
        )
        project.set_wire_field(
            sources[0], "page_count", project.integer(0),
        )
    return request


def _decoded_list(value: dict) -> list[dict]:
    rows = []
    while value["$v"] == 1:
        rows.append(value["$p"]["head"])
        value = value["$p"]["tail"]
    assert value["$v"] == 0, value
    return rows


def _line_col(source: bytes, offset: int) -> tuple[int, int]:
    prefix = source[:offset]
    last_newline = prefix.rfind(b"\n")
    return prefix.count(b"\n") + 1, offset - last_newline


def _validate_case(case: dict, decoded: dict, oracle: dict | None) -> dict:
    result = decoded["result"]
    diagnostics = _decoded_list(result["diagnostics"])
    locations = _decoded_list(decoded["diagnostic_locations"])
    assert result["valid"] is case["expected_valid"], (case["id"], result)
    if oracle is not None:
        assert oracle["valid"] is case["expected_valid"], (case["id"], oracle)
        oracle_diagnostics = oracle.get("diagnostics", [])
        assert isinstance(oracle_diagnostics, list)
        assert len(oracle_diagnostics) == len(diagnostics), (
            case["id"], oracle_diagnostics, diagnostics,
        )
    if case["expected_variant"] is None:
        assert diagnostics == [] and locations == [], (case["id"], decoded)
        return {"id": case["id"], "valid": True, "diagnostic_count": 0}
    assert diagnostics, (case["id"], decoded)
    assert diagnostics[0]["$v"] == case["expected_variant"], (case["id"], diagnostics)
    assert len(locations) == len(diagnostics), (case["id"], locations, diagnostics)
    if case["expected_variant"] == 1:
        assert locations[0]["has_span"] is False
        assert locations[0]["mapped"] is False
        return {"id": case["id"], "valid": False,
                "diagnostic_variant": case["expected_variant"], "mapped": False}
    assert locations[0]["has_span"] is True and locations[0]["mapped"] is True
    diag_payload = diagnostics[0]["$p"]
    start, end = diag_payload["start"], diag_payload["end"]
    if "expected_span" in case:
        assert (start, end) == case["expected_span"], (case["id"], start, end)
    expected_start, expected_end = _line_col(case["source"], start), _line_col(case["source"], end)
    assert (locations[0]["start"]["line"], locations[0]["start"]["col"]) == expected_start
    assert (locations[0]["end"]["line"], locations[0]["end"]["col"]) == expected_end
    return {
        "id": case["id"], "valid": False,
        "diagnostic_variant": case["expected_variant"],
        "byte_span": [start, end],
        "start": {"line": expected_start[0], "col": expected_start[1]},
        "end": {"line": expected_end[0], "col": expected_end[1]},
        "mapped": True,
    }


def _run_vm_batch(cases: list[dict], requests: list[dict]) -> tuple[list[dict], float, int | None, int]:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    calls_path = RUN_DIR / "calls.json"
    output_path = RUN_DIR / "results.json"
    calls = []
    for case, request in zip(cases, requests, strict=True):
        calls.append({
            "id": case["id"],
            "request": {
                "schema_version": "0.1",
                "target": {"module": PROJECT_MODULE, "function": ENTRY_FUNCTION},
                "arguments": request["arguments"],
                "type_arguments": TYPE_ARGS,
                "step_budget": STEP_BUDGET,
            },
        })
    calls_path.write_text(json.dumps(calls, separators=(",", ":")))
    test_vm_emit.ensure_vm()
    started = time.monotonic()
    completed = subprocess.run(
        ["/usr/bin/time", "-v", str(test_vm_emit.VM_BIN), "batch",
         "--artifact", str(ARTIFACT_PATH), "--calls", str(calls_path),
         "--output", str(output_path)],
        capture_output=True, text=True, timeout=300.0,
    )
    wall = time.monotonic() - started
    peak_kib = None
    for line in completed.stderr.splitlines():
        if "Maximum resident set size" in line:
            peak_kib = int(line.split(":", 1)[1].strip().split()[0])
    document = (json.loads(output_path.read_text()) if output_path.is_file()
                else json.loads(completed.stdout))
    rows = document.get("results", [])
    assert completed.returncode == 0 and len(rows) == len(cases), {
        "return_code": completed.returncode, "result_count": len(rows),
        "stderr_tail": completed.stderr[-3000:],
    }
    return rows, wall, peak_kib, calls_path.stat().st_size


def main() -> dict:
    test_vm_emit.ensure_vm()
    build_request = _build_request()
    build_response, build_metrics, build_wall, build_stderr = _build_application(
        build_request,
    )
    if build_response.get("status") != "verified":
        raise AssertionError({
            "status": build_response.get("status"),
            "diagnostics": build_response.get("diagnostics"),
        })
    artifact = (build_response.get("artifact") or {}).get("value")
    if not isinstance(artifact, dict):
        raise AssertionError("verified application build omitted its VM artifact")
    ARTIFACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = ARTIFACT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(artifact, sort_keys=True) + "\n")
    temporary.replace(ARTIFACT_PATH)

    cases = _cases()
    probe = project.Probe()
    try:
        backend = probe.send({"execution_status": True})
        identities = project.identity_map(probe)
        requests = [_request(probe, identities, case) for case in cases]
        native = []
        oracle_by_case = []
        native_seconds = 0.0
        for case, request in zip(cases, requests, strict=True):
            started = time.monotonic()
            response = probe.send(request)
            native_seconds += time.monotonic() - started
            assert response.get("status") == "returned", (case["id"], response)
            decoded = project.decode(response["returned"][0])
            oracle = None
            if not case.get("invalid_page_count"):
                oracle = probe.send({"project_oracle": {
                    "root": case["source"].decode(), "modules": {},
                }})
            native.append(decoded)
            oracle_by_case.append(oracle)
    finally:
        probe.close()

    vm_rows, vm_wall, vm_peak_kib, call_bytes = _run_vm_batch(cases, requests)
    if backend.get("backend") != "cranelift":
        raise AssertionError({"backend": backend})
    case_results = []
    for case, cranelift, oracle, vm_row in zip(
        cases, native, oracle_by_case, vm_rows, strict=True,
    ):
        outcome = vm_row.get("outcome", {})
        if outcome != {"kind": "completed"}:
            raise AssertionError({"case": case["id"], "outcome": outcome})
        returned = vm_row["record"]["returned"]
        assert len(returned) == 1, (case["id"], returned)
        canonical = project.decode(vm_to_wire(returned[0]))
        assert canonical == cranelift, {
            "case": case["id"], "cranelift": cranelift,
            "canonical_vm": canonical,
        }
        case_results.append(_validate_case(case, canonical, oracle))

    report = {
        "schema_version": "mncs.compiler.project-target-integrated-diagnostic-locations-canonical-vm/1",
        "status": "passed_with_selected_frontier",
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
        "compiler_branch": "campaign/native-application-readiness-20261013",
        "compiler_head": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
            capture_output=True, text=True,
        ).stdout.strip(),
        "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
        "target_module": PROJECT_MODULE,
        "target_function": ENTRY_FUNCTION,
        "type_arguments": [1024, 1024],
        "application_build_request_schema": build_request["schema_version"],
        "application_build_response_schema": build_response.get("schema_version"),
        "application_build_identity": (build_response.get("artifact") or {}).get("identity"),
        "application_artifact_identity": artifact.get("artifact_id"),
        "application_artifact_json_bytes": ARTIFACT_PATH.stat().st_size,
        "application_artifact_json_sha256": hashlib.sha256(ARTIFACT_PATH.read_bytes()).hexdigest(),
        "application_callable_count": len(artifact.get("callables", [])),
        "generic_entrypoints": [row.get("generic_function") for row in artifact.get("generic_entrypoints", [])],
        "application_build_cache_status": build_response.get("cache", {}).get("status"),
        "application_build_wall_seconds": round(build_wall, 6),
        "application_build_metrics": build_metrics,
        "application_build_probe_stderr_tail": build_stderr[-1200:],
        "cranelift_backend": backend.get("backend"),
        "case_count": len(cases),
        "case_results": case_results,
        "canonical_vm_batch_wall_seconds": round(vm_wall, 6),
        "canonical_vm_batch_peak_rss_kib": vm_peak_kib,
        "canonical_vm_calls_json_bytes": call_bytes,
        "canonical_vm_results_json_bytes": (RUN_DIR / "results.json").stat().st_size,
        "canonical_vm_returned_all_cases": True,
        "canonical_vm_cranelift_exact_result_agreement": True,
        "stage0_oracle_checked_case_count": sum(x is not None for x in oracle_by_case),
        "cranelift_request_total_seconds": round(native_seconds, 6),
        "language_verified_facts_reused": 0,
        "stage1_claimed": False,
        "stage2_claimed": False,
        "evidence_scope": (
            "One compile-once Project application artifact executed five typed requests in one canonical VM batch: "
            "valid input, missing import, a multi-page missing import, parse failure, and an invalid page descriptor. "
            "Every canonical VM result exactly matched the Cranelift result; Stage-0 oracle validity and diagnostic "
            "counts matched for four source-validity cases, and mapped byte spans produced the current source line/column. "
            "This is a selected integrated Project entrypoint frontier, not full Project-module proof, Language fact reuse, "
            "Forge execution, Stage-1, or Stage-2 succession."
        ),
    }
    EVIDENCE_PATH.write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    report = main()
    print(json.dumps(report, indent=2))
