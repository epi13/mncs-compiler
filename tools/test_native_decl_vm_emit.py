#!/usr/bin/env python3
"""Emit unchanged declaration callables and run them in a fresh canonical VM."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import tempfile
import time
from unittest.mock import patch

import compiler_diagnosis
import native_vm_artifact
import probe_compiler_module_frontier as frontier
import test_native_segment_lex_batch_vm_emit as batch
import test_project as project
import test_sem as sem

ROOT = batch.ROOT
DECL_MODULE = "mncs.compiler.decl.v1"
EMITTER = "mncs.compiler.vm_emit.v1"
PRODUCER = "compile_and_emit_project_vm_callable"
PROJECT_STRIDE = project.STRIDE_DEFAULT
MEMORY_LIMIT = 8 * 1024 ** 3
DEFAULT_STEP_BUDGET = 8_000_000
CONTROL_CALLABLE = "mncs:0.2:function:mncs.test.small_finite.v1::score"
LOCKED_ORACLE_ROOT = batch.single_emit.MAIN_ROOT
LOCKED_ORACLE_PROBE = LOCKED_ORACLE_ROOT / ".bootstrap/target/release/mncs-compiler-stage0-probe"

UNIT_CALLER_SRC = """\
mncs 0.19;
module mncs.test.parse_unit_signatures_caller.v1;
use mncs.compiler.decl.v1 as decl;

fn call_parse_unit_signatures(pages: [[byte; up_to 32]; up_to 4], stride: u64, total: u64) -> (result: decl.Unit) {
    return decl.parse_unit_signatures<4, 32>(pages, stride, total);
}
"""
UNIT_CALLER = "mncs:0.2:function:mncs.test.parse_unit_signatures_caller.v1::call_parse_unit_signatures"
DECLARATION_CASES = {
    "parse_use": (batch.TARGET_PARSE_USE_CALLER, batch.PARSE_USE_CALLER_SRC),
    "parse_unit_signatures": (UNIT_CALLER, UNIT_CALLER_SRC),
}

USE_FIELDS = (
    "ok", "err_start", "err_end", "module_start", "module_end",
    "alias_start", "alias_end", "has_alias", "cursor",
)
# The real uses_step passes the end of the use token, before following trivia.
USE_CASES = (
    ("bare", b"use alpha;", 32, 3, (True, 0, 0, 4, 9, 9, 9, False, 10)),
    ("aliased", b"use mncs.alpha.beta as beta;", 32, 3, (True, 0, 0, 4, 19, 23, 27, True, 28)),
    ("trivia", b"/*c*/ use /*x*/ dotted . path /*y*/ as tag ;", 16, 9, (True, 0, 0, 16, 29, 39, 42, True, 44)),
    ("split_name", b"use alphabet.part as alias;", 9, 3, (True, 0, 0, 4, 17, 21, 26, True, 27)),
    ("same_input_other_layout", b"use alphabet.part as alias;", 16, 3, (True, 0, 0, 4, 17, 21, 26, True, 27)),
    ("bad_first_segment", b"use .bad;", 32, 3, (False, 4, 5, 3, 3, 0, 0, False, 3)),
    ("bad_dotted_segment", b"use foo. .bar;", 32, 3, (False, 9, 10, 3, 3, 0, 0, False, 3)),
    ("double_dot_token", b"use foo..bar;", 32, 3, (False, 7, 9, 4, 7, 0, 0, False, 3)),
    ("keyword_alias", b"use foo as mncs;", 32, 3, (False, 11, 15, 4, 7, 0, 0, False, 3)),
    ("missing_alias", b"use foo as ;", 32, 3, (False, 11, 12, 4, 7, 0, 0, False, 3)),
    ("missing_semicolon", b"use alpha as beta", 32, 3, (False, 17, 17, 4, 9, 0, 0, False, 3)),
)
UNIT_CASES = (
    ("valid_unit", b"mncs 0.19; module m; use a.b as c; record R { value: u64 } fn f(x: u64) -> (r: u64) { return x; }", 32),
    ("malformed_declaration", b"mncs 0.19; module m; fn f(x: ) -> (r: u64) { return 0; }", 32),
)


def execution_cases(case):
    fixtures = (
        ((label, raw, stride, [cursor], dict(zip(USE_FIELDS, values)))
         for label, raw, stride, cursor, values in USE_CASES)
        if case == "parse_use"
        else ((label, raw, stride, [], None) for label, raw, stride in UNIT_CASES)
    )
    for label, raw, stride, extra, expected in fixtures:
        pages = project.chunk(raw, stride)
        assert len(pages) <= 4 and stride <= 32
        args = [project.pages_value(pages), project.integer(stride), project.integer(len(raw))]
        yield label, stride, args + [project.integer(value) for value in extra], expected


def stage0_results(case):
    type_args = [sem.nat_arg(4), sem.nat_arg(32)]
    oracle = batch.single_emit.Stage0ModuleOracle(
        DECL_MODULE,
        [ROOT / "src", batch.single_emit.STDLIB_ROOT / "library"],
        generic_seeds=[(case, type_args)],
        probe_bin=LOCKED_ORACLE_PROBE,
        toolchain_identity_root=LOCKED_ORACLE_ROOT,
    )
    try:
        results = {}
        for label, _, args, expected in execution_cases(case):
            response = oracle.call(case, args, type_arguments=type_args)
            assert response["status"] == "returned", (label, response)
            record = response["returned"][0]
            value = sem.decode(record)
            if expected is not None:
                assert value == expected, (label, value, expected)
            else:
                assert value["ok"] == (label == "valid_unit"), (label, value)
            results[label] = (record["record"]["type_identity"], value)
        return results
    finally:
        oracle.close()


def run_execution(case, target, artifact_path):
    oracle = stage0_results(case)
    module, function = target.removeprefix("mncs:0.2:function:").split("::")
    vm = batch.single_emit.FreshVmSession(artifact_path)
    results = {}
    try:
        for label, stride, args, _ in execution_cases(case):
            response = vm.call(module, function, args)
            assert response["outcome"]["kind"] == "completed", (label, response)
            returned = response["execution"]["returned"][0]
            name = "UseDecl" if case == "parse_use" else "Unit"
            assert returned["record"]["name"] == name, (label, returned)
            oracle_identity, oracle_value = oracle[label]
            assert returned["record"]["type_identity"] == oracle_identity, (label, returned, oracle_identity)
            value = sem.decode(returned)
            assert value == oracle_value, (label, value, oracle_value)
            usage = response["record"]["usage"]
            results[label] = {
                "stride": stride, "returned": value, "matches_stage0": True,
                "steps": usage["steps"], "max_memory_cells": usage["max_memory_cells"],
            }
    finally:
        vm.close()
    return {
        "status": "returned", "cases": results, "vm_peak_rss_bytes": vm.peak_rss_bytes,
        "max_memory_cells": max(result["max_memory_cells"] for result in results.values()),
    }


def run_control(artifact_path):
    module = "mncs.test.small_finite.v1"
    color = f"mncs:0.2:finite-type:{module}::Color"
    variant = f"mncs:0.2:finite-variant:{module}::Color"
    cases = [
        ("Empty", 0, [], 6),
        ("Rgb", 1, [[field, project.integer(value)] for field, value in zip("rgb", (4, 5, 6))], 654),
    ]
    vm = batch.single_emit.FreshVmSession(artifact_path)
    results = {}
    try:
        for name, disc, payload, expected in cases:
            result = vm.call(module, "score", [{"finite": {
                "type_identity": color, "variant_identity": f"{variant}::{name}",
                "discriminant": disc, "payload": payload,
            }}])
            assert result["outcome"]["kind"] == "completed", result
            actual = sem.decode(result["execution"]["returned"][0])
            assert actual == expected, (name, actual, expected)
            usage = result["record"]["usage"]
            results[name] = {
                "returned": actual, "steps": usage["steps"],
                "max_memory_cells": usage["max_memory_cells"],
            }
    finally:
        vm.close()
    return {
        "status": "returned", "cases": results, "vm_peak_rss_bytes": vm.peak_rss_bytes,
        "max_memory_cells": max(result["max_memory_cells"] for result in results.values()),
    }


def assert_callable_specialization(emitted, case):
    identity = f"mncs:0.2:function:{DECL_MODULE}::{case}"
    specializations = [
        [arg["value"] for arg in native_vm_artifact.flist(function["nat_args"])]
        for function in native_vm_artifact.flist(emitted["functions"])
        if compiler_diagnosis.decode_semantic_id(function["identity"]) == identity
    ]
    assert specializations == [[4, 32]], (identity, specializations)


def select_sources(case):
    if case == "control":
        return CONTROL_CALLABLE, [
            ("00_small_finite.mncs", Path("00_small_finite.mncs"), batch.SMALL_FINITE_SRC.encode()),
        ]
    target, wrapper = DECLARATION_CASES[case]
    sources = batch._segment_sources(batch._lexer_sources())
    for number, name in ((4, "parser"), (5, "decl")):
        path = ROOT / "src" / "compiler" / f"{name}.mncs"
        sources.append((f"{number:02}_{path.name}", path, path.read_bytes()))
    wrapper_id = f"06_{case}_caller.mncs"
    sources.append((wrapper_id, Path(wrapper_id), wrapper.encode()))
    sources.sort(key=lambda entry: entry[0].encode())
    return target, sources


def source_manifest(sources):
    return [
        {
            "source_id": source_id,
            "bytes": len(raw),
            "pages": len(project.chunk(raw, PROJECT_STRIDE)),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        for source_id, _, raw in sources
    ]


def run_request(target, sources, page_bound, step_budget):
    targs = [sem.nat_arg(page_bound), sem.nat_arg(PROJECT_STRIDE)]
    env = {
        "MNCS_LIBRARY_PATH": f"{ROOT / 'src'}:{batch.single_emit.STDLIB_ROOT / 'library'}",
        "MNCS_PROBE_BIN": str(batch.single_emit.PROBE_BIN),
        "MNCS_PROBE_MODULES": "source,lexer,parser,segment,decl,flow,ssa,project",
        "MNCS_PROBE_LIBRARY_MODULES": f"{EMITTER},mncs.std.text_utf8.v1",
        "MNCS_PROBE_EXECUTION_MODULES": EMITTER,
        "MNCS_PROBE_BACKEND": batch.backend_policy.resolve("project"),
        "MNCS_PROBE_BACKEND_ROOTS": PRODUCER,
        "MNCS_PROBE_CACHE_DIR": str(batch.single_emit.CACHE_DIR),
        "MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES": str(MEMORY_LIMIT),
        "MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES": os.environ.get(
            "MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES", "1",
        ),
        "MNCS_PROBE_GENERIC_SEEDS": json.dumps([
            {"module": EMITTER, "function": PRODUCER, "type_arguments": targs},
        ]),
        "MNCS_PROBE_TELEMETRY": "1",
    }
    started = time.monotonic()
    with patch.dict(os.environ, env):
        probe = project.Probe()
        transport_failed = False
        phase = "stage0_probe_startup"
        try:
            records = {r["name"]: r for r in probe.send({"record_types": EMITTER})}
            encoded = project.encode_project_request_bytes(
                records, sources, page_bound=page_bound, target_index=len(sources) - 1,
                target_module=EMITTER, target_function=PRODUCER,
                extra_arguments=[batch.punct._make_semantic_id_wire(records, target)],
                step_budget=step_budget,
            )
            phase = "stage0_probe_request"
            response = probe.send_encoded_decoded(encoded, module=EMITTER, record_digest=False)
        except (project.ProbeExited, project.ProbeTimeout) as exc:
            transport_failed = True
            child = next(iter(probe._procs.values()))
            observation = exc.observation
            return None, {
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "backend": env["MNCS_PROBE_BACKEND"],
                "child_peak_rss_bytes": (observation.get("resource_summary") or {}).get("max_kernel_rss_high_water_bytes"),
                "failure": {
                    "phase": phase,
                    "rejecting_component": "mncs-compiler:tools/stage0-probe",
                    "detail": str(exc),
                    "termination": observation.get("termination"),
                    "classification": observation.get("status"),
                    "return_code": observation.get("return_code"),
                    "stderr_tail": probe._stderr_tail(child).decode(errors="replace")[-3000:],
                },
            }
        finally:
            try:
                probe.close()
            except AssertionError:
                if not transport_failed:
                    raise
    observation = probe.request_observations[-1]
    metrics = {
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "request_bytes": observation["request_bytes"],
        "response_bytes": observation["reply_bytes"],
        "reported_execution_steps": observation.get("execution_steps"),
        "child_peak_rss_bytes": (observation.get("final_process") or {}).get("rss_high_water_bytes"),
        "backend": env["MNCS_PROBE_BACKEND"],
        "program_released_after_identities": env["MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES"] == "1",
    }
    return response, metrics


def run_attempt(case, page_bound, artifact_path, *, step_budget=DEFAULT_STEP_BUDGET):
    target, sources = select_sources(case)
    selected_bound, pages = frontier._project_page_capacity(sources)
    page_bound = selected_bound if page_bound is None else page_bound
    if page_bound not in frontier.PROJECT_PROBE_PAGE_TIERS:
        raise ValueError(f"unsupported project page tier {page_bound}")
    manifest = source_manifest(sources)
    report = {
        "target": target,
        "specialization": f"{DECL_MODULE}::{case}<4,32>" if case != "control" else None,
        "compiler_revision": batch.single_emit._git_head(ROOT),
        "emitter_source_sha256": compiler_diagnosis.file_sha256(ROOT / compiler_diagnosis.EMITTER_REL_PATH),
        "project_source_sha256": compiler_diagnosis.file_sha256(ROOT / "src/compiler/project.mncs"),
        "stage0_revision": batch.single_emit._stage0_lock_rev(),
        "emitter_probe_sha256": compiler_diagnosis.file_sha256(batch.single_emit.PROBE_BIN),
        "locked_oracle_sha256": compiler_diagnosis.file_sha256(LOCKED_ORACLE_PROBE),
        "vm_revision": batch.single_emit._git_head(batch.single_emit.VM_REPO),
        "producer": f"{EMITTER}::{PRODUCER}",
        "sources": manifest,
        "source_closure_sha256": compiler_diagnosis.canonical_digest(manifest),
        "total_pages": pages,
        "selected_emitter_specialization": [page_bound, PROJECT_STRIDE],
        "minimum_supported_emitter_specialization": [selected_bound, PROJECT_STRIDE],
        "memory_guard_bytes": MEMORY_LIMIT,
        "emitter_step_budget": step_budget,
        "emission": {"status": "not_started"},
        "admission": {"status": "not_started"},
        "execution": {"status": "not_started"},
    }
    emitted = None
    request_failure = None
    if pages > page_bound:
        request_failure = {
            "phase": "project_request_capacity",
            "rejecting_component": "mncs-compiler:tools/test_native_decl_vm_emit.py",
            "supplied_capacity": page_bound,
            "required_capacity": pages,
        }
    else:
        response, metrics = run_request(target, sources, page_bound, step_budget)
        report["emitter_probe"] = metrics
        if response is None:
            request_failure = metrics["failure"]
        else:
            report["probe_status"] = response["status"]
            if response["status"] == "returned":
                emitted = response["returned"][0]
            else:
                request_failure = {
                    "phase": "stage0_probe_execution",
                    "rejecting_component": "mncs-compiler:tools/stage0-probe",
                    "response": response,
                }
    if request_failure is not None:
        report["emission"] = {"status": "refused", **request_failure}
    else:
        report["emission"] = {
            "status": "emitted" if emitted["ok"] else "refused",
            "detail": batch._emit_detail_summary(emitted),
        }

    if emitted and emitted["ok"]:
        report["emission"].update({key: emitted[key] for key in (
            "function_count", "record_type_count", "finite_type_count", "block_count", "instruction_count",
        )})
        try:
            if case != "control":
                assert_callable_specialization(emitted, case)
            artifact = batch._package(emitted, sources, len(sources) - 1)
            artifact_bytes = json.dumps(artifact, separators=(",", ":")).encode()
            artifact_path.write_bytes(artifact_bytes)
        except (AssertionError, KeyError, OSError, ValueError) as exc:
            report["seal"] = {"status": "failed", "detail": str(exc)[:2000]}
        else:
            report["emission"].update({
                "artifact_id": artifact["artifact_id"],
                "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
                "artifact_bytes": len(artifact_bytes),
            })
            admitted = batch._admit_artifact(artifact_path)
            report["admission"] = {"status": "admitted" if admitted["ok"] else "refused", **admitted}
            if admitted["ok"]:
                try:
                    report["execution"] = (
                        run_control(artifact_path) if case == "control"
                        else run_execution(case, target, artifact_path)
                    )
                except (AssertionError, KeyError, OSError, ValueError) as exc:
                    report["execution"] = {"status": "failed", "detail": str(exc)[:4000]}
    if case != "control":
        diagnosis = compiler_diagnosis.build_diagnosis_document(
            ROOT,
            target_callable=target,
            target_specialization=report["specialization"],
            declaration_identity=f"mncs:0.2:function:{DECL_MODULE}::{case}",
            emit_res=emitted,
            request_failure=request_failure,
            source_paths=[path for _, path, _ in sources] + [
                ROOT / "src" / "compiler" / f"{module}.mncs"
                for module in ("flow", "ssa", "project")
            ],
            admitted=report["admission"]["status"] == "admitted",
            executed=report["execution"]["status"] == "returned",
            artifact_id=report["emission"].get("artifact_id"),
        )
        compiler_diagnosis.write_cached_diagnosis(ROOT, diagnosis)
    return report


def guard_host_memory():
    soft, hard = resource.getrlimit(resource.RLIMIT_AS)
    limit = MEMORY_LIMIT if soft == resource.RLIM_INFINITY else min(soft, MEMORY_LIMIT)
    resource.setrlimit(resource.RLIMIT_AS, (limit, hard))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("control", *DECLARATION_CASES))
    parser.add_argument("--page-bound", type=int)
    parser.add_argument("--step-budget", type=int, default=DEFAULT_STEP_BUDGET)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--artifact-out", type=Path)
    args = parser.parse_args()
    if args.step_budget <= 0:
        parser.error("--step-budget must be positive")

    guard_host_memory()
    with tempfile.TemporaryDirectory(prefix="mncs-decl-emit-") as scratch:
        artifact_path = args.artifact_out or Path(scratch) / "artifact.json"
        report = run_attempt(args.case, args.page_bound, artifact_path, step_budget=args.step_budget)
    report["host_peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
    rendered = json.dumps(report, indent=2) + "\n"
    if args.json_out:
        args.json_out.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
