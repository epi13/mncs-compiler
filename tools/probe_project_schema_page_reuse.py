#!/usr/bin/env python3
"""Measure target-only project lowering with the real declaration schema corpus.

The target is a small imported-enum consumer, while source/lexer/segment/decl
are supplied as its project context. This keeps proof work bounded and makes
schema construction over the 802-page decl module the variable under study.
The Stage-0 oracle independently checks the exact synthetic provider/consumer
pair; it does not claim to compile the compiler-module context.
"""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

PAGE_CAPACITY = int(os.environ.get("MNCS_PROJECT_PAGE_CAPACITY", "1024"))
PAGE_WIDTH = int(os.environ.get("MNCS_PROJECT_PAGE_WIDTH", "1024"))
os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps([
    {
        "module": "mncs.compiler.project.v1",
        "function": "compile_project_target",
        "type_arguments": [
            {"kind": "nat", "value": PAGE_CAPACITY},
            {"kind": "nat", "value": PAGE_WIDTH},
        ],
    },
])

import test_project as project
from probe_compiler_module_frontier import (
    _load_sources,
    _module_name_from_source,
    _module_summary,
    _seed_target_specialization,
)
from test_project_target_module import _target_request


ROOT = Path(__file__).resolve().parents[1]
MODULES = ["source", "lexer", "parser", "segment", "decl"]
PROVIDER = """mncs 0.18; module demo.schema_provider;
enum Token { Empty, Pair { first: u64, second: u64 } }
"""
TARGET = """mncs 0.18; module demo.schema_target; use demo.schema_provider as dep;
fn inspect(x: dep.Token) -> (r: u64) {
    return match x {
        dep.Token.Empty => 0,
        dep.Token.Pair { first: value, second: _ } => value
    };
}
"""
RECORD_PROVIDER = """mncs 0.18; module demo.record_provider;
record Inner { value: u64 }
record Token { kind: u64, inner: Inner }
"""
RECORD_TARGET = """mncs 0.18; module demo.record_target; use demo.record_provider as dep;
fn kind(token: dep.Token) -> (r: u64) { return token.kind; }
fn nested(token: dep.Token) -> (r: u64) {
    let inner: dep.Inner = token.inner;
    return inner.value;
}
"""
SEQUENCE_RECORD_PROVIDER = """mncs 0.18; module demo.record_provider;
record Inner { value: u64 }
record Token { entries: [Inner; 2] }
"""
SEQUENCE_RECORD_TARGET = """mncs 0.18; module demo.record_target; use demo.record_provider as dep;
fn entries(token: dep.Token) -> (r: [dep.Inner; 2]) { return token.entries; }
"""
NESTED_SEQUENCE_PROVIDER = """mncs 0.18; module demo.record_provider;
record Inner { value: u64 }
record Token { nested: [[Inner; 2]; 3] }
"""
NESTED_SEQUENCE_TARGET = """mncs 0.18; module demo.record_target; use demo.record_provider as dep;
fn nested_entries(token: dep.Token) -> (r: [[dep.Inner; 2]; 3]) { return token.nested; }
"""
BAD_RECORD_TARGET = """mncs 0.18; module demo.record_target; use demo.record_provider as dep;
fn missing(token: dep.Token) -> (r: u64) { return token.absent; }
"""


def _source_identity(sources):
    return hashlib.sha256(b"".join(
        source_id.encode() + b"\0" + hashlib.sha256(raw).digest()
        for source_id, _, raw in sources
    )).hexdigest()


def _loaded_program_sources():
    """Bind the candidate MNCS source closure loaded by the probe child."""
    selected = [part.strip() for part in
                os.environ.get("MNCS_PROBE_MODULES", "source,lexer,parser,segment,decl,flow,ssa,project").split(",")
                if part.strip()]
    paths = sorted(
        (ROOT / "src" / "compiler" / f"{name}.mncs" for name in selected),
        key=lambda path: path.name.encode(),
    )
    sources = [(path.relative_to(ROOT).as_posix(), path, path.read_bytes())
               for path in paths]
    return sources, selected


def _record_case(probe, identities, compiler_sources, target_text,
                 provider_text=RECORD_PROVIDER):
    sources = compiler_sources + [
        ("zz-record-provider.mncs", Path("zz-record-provider.mncs"), provider_text.encode()),
        ("zzz-record-target.mncs", Path("zzz-record-target.mncs"), target_text.encode()),
    ]
    request = project.request_value(identities, sources, stride=PAGE_WIDTH)
    request["type_arguments"] = [
        {"kind": "nat", "value": PAGE_CAPACITY},
        {"kind": "nat", "value": PAGE_WIDTH},
    ]
    request = _target_request(request, len(sources) - 1)
    response = probe.send(request)
    if response.get("status") != "returned":
        return {"request_status": response.get("status", "UNKNOWN"),
                "request_observation": response}
    native = project.decode(response["returned"][0])
    oracle = probe.send({"project_oracle": {
        "root": target_text,
        "modules": {"demo.record_provider": provider_text},
    }})
    target_modules = project.flist(native["modules"])
    if len(target_modules) != 1:
        raise AssertionError(f"expected exactly one record target: {len(target_modules)}")
    module = target_modules[0]
    proof = module["flow"]["proof"]
    result = {
        "request_status": response.get("status", "UNKNOWN"),
        "native_valid": native.get("valid"),
        "native_verified_ssa": native.get("value_ssa_valid"),
        "native_target_summary": _module_summary(module, target_text.encode()),
        "stage0_oracle_valid": oracle.get("valid"),
        "stage0_diagnostics": oracle.get("diagnostics"),
        "input_source_identity": _source_identity(sources),
        "input_pages": sum((len(raw) + PAGE_WIDTH - 1) // PAGE_WIDTH
                            for _, _, raw in sources),
    }
    if target_text == BAD_RECORD_TARGET:
        marker = target_text.index("absent")
        native_span = [proof["err_start"], proof["err_end"]]
        oracle_spans = [
            [item.get("span", {}).get("start"), item.get("span", {}).get("end")]
            for item in oracle.get("diagnostics", [])
            if item.get("code") == "MNE162"
        ]
        expected = [marker, marker + len("absent")]
        if native_span != expected or expected not in oracle_spans:
            raise AssertionError({"expected_span": expected,
                                  "native_span": native_span,
                                  "stage0_spans": oracle_spans})
        result["missing_field_span_matches_native_and_stage0"] = True
    if target_text == NESTED_SEQUENCE_TARGET:
        start = target_text.index("[[dep.Inner; 2]; 3]")
        expected = [start, start + len("[[dep.Inner; 2]; 3]")]
        if (native.get("valid") is not False or oracle.get("valid") is not True
                or [proof["err_start"], proof["err_end"]] != expected):
            raise AssertionError({"expected_known_gap": expected,
                                  "native_valid": native.get("valid"),
                                  "stage0_valid": oracle.get("valid"),
                                  "native_span": [proof["err_start"], proof["err_end"]]})
        result["classification"] = "stage0_accepts_nested_imported_sequence_but_native_proof_rejects"
        result["native_gap_span"] = expected
    return result


def run():
    _seed_target_specialization(target_only=True)
    compiler_sources = _load_sources(MODULES)
    loaded_program_sources, loaded_program_modules = _loaded_program_sources()
    sources = compiler_sources + [
        ("zz-schema-provider.mncs", Path("zz-schema-provider.mncs"), PROVIDER.encode()),
        ("zzz-schema-target.mncs", Path("zzz-schema-target.mncs"), TARGET.encode()),
    ]
    page_count = sum(
        (len(raw) + PAGE_WIDTH - 1) // PAGE_WIDTH
        for _, _, raw in sources
    )
    if page_count > PAGE_CAPACITY:
        raise ValueError(f"input needs {page_count} pages, bound is {PAGE_CAPACITY}")

    probe = project.Probe()
    started = time.monotonic_ns()
    terminal_exit_observed = False
    try:
        try:
            execution = probe.send({"execution_status": True})
        except (project.ProbeTimeout, project.ProbeExited) as error:
            observation = error.observation
            return {
                "schema_version": 1,
                "kind": "project-schema-page-reuse-probe",
                "compiler_head": subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                    capture_output=True, text=True,
                ).stdout.strip(),
                "compiler_branch": subprocess.run(
                    ["git", "branch", "--show-current"], cwd=ROOT, check=True,
                    capture_output=True, text=True,
                ).stdout.strip(),
                "backend": os.environ.get("MNCS_PROBE_BACKEND", "reference_interpreter"),
                "request_context_source_identity": _source_identity(sources),
                "loaded_program_modules": loaded_program_modules,
                "loaded_program_source_identity": _source_identity(loaded_program_sources),
                "project_program_source_sha256": hashlib.sha256(
                    (ROOT / "src/compiler/project.mncs").read_bytes()
                ).hexdigest(),
                "input_modules": [Path(source_id).stem for source_id, _, _ in sources],
                "total_input_pages": page_count,
                "decl_source_bytes": len(next(raw for source_id, _, raw in compiler_sources
                                               if Path(source_id).stem == "decl")),
                "admission_status": observation.get("status", "UNKNOWN"),
                "admission_classification_reason": observation.get("classification_reason"),
                "admission_observation": observation,
                "request_status": "NOT_RUN",
                "native_valid": None,
                "native_verified_ssa": None,
                "stage0_oracle_valid": None,
                "session_wall_ns": time.monotonic_ns() - started,
            }
        identities = project.identity_map(probe)
        case_mode = os.environ.get("MNCS_SCHEMA_PROBE_CASE")
        if case_mode in {"record", "missing-record", "record-sequence", "nested-sequence"}:
            case_inputs = {
                "record": (RECORD_PROVIDER, RECORD_TARGET),
                "missing-record": (RECORD_PROVIDER, BAD_RECORD_TARGET),
                "record-sequence": (SEQUENCE_RECORD_PROVIDER, SEQUENCE_RECORD_TARGET),
                "nested-sequence": (NESTED_SEQUENCE_PROVIDER, NESTED_SEQUENCE_TARGET),
            }
            provider_text, target_text = case_inputs[case_mode]
            case_sources = compiler_sources + [
                ("zz-record-provider.mncs", Path("zz-record-provider.mncs"),
                 provider_text.encode()),
                ("zzz-record-target.mncs", Path("zzz-record-target.mncs"),
                 target_text.encode()),
            ]
            try:
                case_result = _record_case(
                    probe, identities, compiler_sources, target_text, provider_text
                )
            except (project.ProbeTimeout, project.ProbeExited) as error:
                terminal_exit_observed = isinstance(error, project.ProbeExited)
                case_result = {
                    "request_status": (probe.request_observations[-1].get("status", "UNKNOWN")
                                       if probe.request_observations else "UNKNOWN"),
                    "classification_reason": (
                        probe.request_observations[-1].get("classification_reason")
                        if probe.request_observations else str(error)
                    ),
                    "request_observation": (
                        probe.request_observations[-1] if probe.request_observations
                        else error.observation
                    ),
                }
            return {
                "schema_version": 1,
                "kind": "project-schema-page-reuse-case",
                "case": case_mode,
                "compiler_head": subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                    capture_output=True, text=True,
                ).stdout.strip(),
                "compiler_branch": subprocess.run(
                    ["git", "branch", "--show-current"], cwd=ROOT, check=True,
                    capture_output=True, text=True,
                ).stdout.strip(),
                "project_program_source_sha256": hashlib.sha256(
                    (ROOT / "src/compiler/project.mncs").read_bytes()
                ).hexdigest(),
                "loaded_program_modules": loaded_program_modules,
                "loaded_program_source_identity": _source_identity(loaded_program_sources),
                "stage0_revision": json.loads(
                    (ROOT / "mncs-language.lock.json").read_text()
                )["revision"],
                "backend": execution["backend"],
                "retained_sessions": execution["retained_sessions"],
                "operation": f"{project.PROJECT_MODULE}::compile_project_target<{PAGE_CAPACITY},{PAGE_WIDTH}>",
                "input_source_identity": _source_identity(case_sources),
                "input_modules": [Path(source_id).stem for source_id, _, _ in case_sources],
                "total_input_pages": sum(
                    (len(raw) + PAGE_WIDTH - 1) // PAGE_WIDTH
                    for _, _, raw in case_sources
                ),
                "decl_source_pages": sum(
                    (len(raw) + PAGE_WIDTH - 1) // PAGE_WIDTH
                    for source_id, _, raw in compiler_sources
                    if Path(source_id).stem == "decl"
                ),
                "request_status": case_result.get("request_status", "UNKNOWN"),
                "case_result": case_result,
                "request_observations": probe.request_observations,
                "session_wall_ns": time.monotonic_ns() - started,
            }
        request = project.request_value(identities, sources, stride=PAGE_WIDTH)
        request["type_arguments"] = [
            {"kind": "nat", "value": PAGE_CAPACITY},
            {"kind": "nat", "value": PAGE_WIDTH},
        ]
        request = _target_request(request, len(sources) - 1)
        try:
            response = probe.send(request)
        except (project.ProbeTimeout, project.ProbeExited) as error:
            return {
                "schema_version": 1,
                "kind": "project-schema-page-reuse-probe",
                "compiler_head": subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                    capture_output=True, text=True,
                ).stdout.strip(),
                "compiler_branch": subprocess.run(
                    ["git", "branch", "--show-current"], cwd=ROOT, check=True,
                    capture_output=True, text=True,
                ).stdout.strip(),
                "backend": os.environ.get("MNCS_PROBE_BACKEND", "reference_interpreter"),
                "request_context_source_identity": _source_identity(sources),
                "loaded_program_modules": loaded_program_modules,
                "loaded_program_source_identity": _source_identity(loaded_program_sources),
                "project_program_source_sha256": hashlib.sha256(
                    (ROOT / "src/compiler/project.mncs").read_bytes()
                ).hexdigest(),
                "input_modules": [Path(source_id).stem for source_id, _, _ in sources],
                "total_input_pages": page_count,
                "request_status": error.observation.get("status", "UNKNOWN"),
                "request_classification_reason": error.observation.get("classification_reason"),
                "request_observation": error.observation,
                "native_valid": None,
                "native_verified_ssa": None,
                "stage0_oracle_valid": None,
                "session_wall_ns": time.monotonic_ns() - started,
            }
        native = project.decode(response["returned"][0]) if response.get("returned") else None

        # The independent oracle checks the focused enum semantics without
        # claiming that unrelated compiler modules were lowered by Stage-0.
        oracle = probe.send({"project_oracle": {
            "root": TARGET,
            "modules": {"demo.schema_provider": PROVIDER},
        }})

        target_summary = None
        if native:
            target_module = project.flist(native["modules"])
            if len(target_module) != 1:
                raise AssertionError(f"expected exactly one lowered target: {len(target_module)}")
            target_summary = _module_summary(target_module[0], TARGET.encode())
        assert oracle.get("valid") is True, oracle

        compile_observation = next(
            item for item in reversed(probe.request_observations)
            if item.get("kind") == f"{project.PROJECT_MODULE}::compile_project_target"
        )
        record_case = _record_case(probe, identities, compiler_sources, RECORD_TARGET)
        bad_record_case = _record_case(probe, identities, compiler_sources,
                                       BAD_RECORD_TARGET)
        assert record_case["native_valid"] is True, record_case
        assert record_case["native_verified_ssa"] is True, record_case
        assert record_case["stage0_oracle_valid"] is True, record_case
        assert bad_record_case["native_valid"] is False, bad_record_case
        assert bad_record_case["native_verified_ssa"] is False, bad_record_case
        assert bad_record_case["stage0_oracle_valid"] is False, bad_record_case
        decl_source = next(raw for source_id, _, raw in compiler_sources
                           if Path(source_id).stem == "decl")
        return {
            "schema_version": 1,
            "kind": "project-schema-page-reuse-probe",
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "compiler_head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                capture_output=True, text=True,
            ).stdout.strip(),
            "compiler_branch": subprocess.run(
                ["git", "branch", "--show-current"], cwd=ROOT, check=True,
                capture_output=True, text=True,
            ).stdout.strip(),
            "request_context_source_identity": _source_identity(compiler_sources),
            "input_source_identity": _source_identity(sources),
            "loaded_program_modules": loaded_program_modules,
            "loaded_program_source_identity": _source_identity(loaded_program_sources),
            "project_program_source_sha256": hashlib.sha256(
                (ROOT / "src/compiler/project.mncs").read_bytes()
            ).hexdigest(),
            "stage0_revision": json.loads(
                (ROOT / "mncs-language.lock.json").read_text()
            )["revision"],
            "operation": f"{project.PROJECT_MODULE}::compile_project_target<{PAGE_CAPACITY},{PAGE_WIDTH}>",
            "backend": execution["backend"],
            "retained_sessions": execution["retained_sessions"],
            "input_modules": [Path(source_id).stem for source_id, _, _ in sources],
            "decl_source_bytes": len(decl_source),
            "decl_source_pages": (len(decl_source) + PAGE_WIDTH - 1) // PAGE_WIDTH,
            "total_input_pages": page_count,
            "step_budget": request["step_budget"],
            "request_status": response.get("status", "UNKNOWN"),
            "execution_steps": response.get("steps"),
            "native_valid": native.get("valid") if native else None,
            "native_verified_ssa": native.get("value_ssa_valid") if native else None,
            "native_diagnostics": native.get("diagnostics") if native else None,
            "target_summary": target_summary,
            "stage0_oracle_valid": oracle.get("valid"),
            "record_target": record_case,
            "record_negative_target": bad_record_case,
            "compile_request_observation": compile_observation,
            "session_wall_ns": time.monotonic_ns() - started,
            "semantic_result_sha256": probe.digest.hexdigest(),
        }
    finally:
        if terminal_exit_observed:
            for proc in probe._procs.values():
                for stream in (proc.stdin, proc.stdout):
                    if stream is not None and not stream.closed:
                        stream.close()
        else:
            probe.close()


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, sort_keys=True))
