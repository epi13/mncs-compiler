#!/usr/bin/env python3
"""Probe real compiler modules through native project proof, flow, and SSA."""

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import test_project as project

sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "compiler"
DEFAULT_STEP_BUDGET = 8_000_000


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


def _run_stage0_project_oracle(probe, sources, root_index):
    module_map = {
        _module_name_from_source(source): source.decode()
        for i, (_, _, source) in enumerate(sources) if i != root_index
    }
    oracle = probe.send({"project_oracle": {
        "root": sources[root_index][2].decode(),
        "modules": module_map,
    }})
    return {
        "status": ("valid" if oracle.get("valid") else
                   "invalid" if "valid" in oracle else
                   oracle.get("status", "not-returned")),
        "valid": oracle.get("valid"),
        "diagnostics": oracle.get("diagnostics"),
        "linked_functions": len(oracle.get("program", {}).get("functions", [])) if oracle.get("program") else 0,
        "ssa_functions": len(oracle.get("ssa", {}).get("functions", [])) if oracle.get("ssa") else 0,
    }


def run_stage0_oracle_only(module_names):
    if not module_names:
        raise ValueError("Stage-0 oracle-only mode requires an explicit module selection")
    # The Rust Stage-0 project oracle does not need the retained native
    # compiler artifact. Run it independently so native admission and
    # execution limits cannot suppress the reference result.
    os.environ.pop("MNCS_PROBE_BACKEND", None)
    os.environ["MNCS_PROBE_EXECUTION_MODULES"] = ""
    os.environ["MNCS_PROBE_GENERIC_SEEDS"] = "[]"
    os.environ["MNCS_PROBE_CACHE_DIR"] = ""
    sources = _load_sources(module_names)
    root_stem = module_names[-1]
    root_index = next(i for i, (source_id, _, _) in enumerate(sources)
                      if Path(source_id).stem == root_stem)
    probe = project.Probe()
    try:
        execution = probe.send({"execution_status": True})
        oracle = _run_stage0_project_oracle(probe, sources, root_index)
        return {
            "schema_version": 1,
            "kind": "stage0-project-oracle-frontier",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "selected_modules": module_names,
            "root_module_source": root_stem,
            "compiler_source_count": len(sources),
            "input_total_page_count_at_stride_1024": sum(
                (len(source) + project.STRIDE_DEFAULT - 1) // project.STRIDE_DEFAULT
                for _, _, source in sources
            ),
            "compiler_source_sha256": hashlib.sha256(b"".join(
                source_id.encode() + b"\0" + hashlib.sha256(source).digest()
                for source_id, _, source in sources
            )).hexdigest(),
            "execution_backend": execution["backend"],
            "retained_sessions": execution["retained_sessions"],
            "stage0_oracle": oracle,
            "requests": probe.requests,
            "result_sha256": probe.digest.hexdigest(),
        }
    finally:
        probe.close()


def run_imported_signature_cache_fixture(step_budget=DEFAULT_STEP_BUDGET):
    _seed_target_specialization(target_only=True)
    probe = project.Probe()
    provider = (
        "mncs 0.18; module demo.provider; "
        "fn first(x: u64) -> (r: u64) { return x; } "
        "fn second(x: u64) -> (r: u64) { return x; }"
    )
    root = (
        "mncs 0.18; module demo.root; use demo.provider as api; "
        "fn run(x: u64) -> (r: u64) { "
        "let first: u64 = api.first(x); return api.second(first); }"
    )
    sources = [
        ("a-provider.mncs", Path("a-provider.mncs"), provider.encode()),
        ("b-root.mncs", Path("b-root.mncs"), root.encode()),
    ]
    try:
        execution = probe.send({"execution_status": True})
        identities = project.identity_map(probe)
        request = project.request_value(identities, sources)
        request["step_budget"] = step_budget
        request["target"] = {"module": project.PROJECT_MODULE, "function": "compile_project_target"}
        request["arguments"].append(project.integer(1))
        response = probe.send(request)
        if response.get("steps") is not None:
            probe.steps.append(response["steps"])
        native = project.decode(response["returned"][0]) if response.get("returned") else None
        stage0_oracle = _run_stage0_project_oracle(probe, sources, 1)
        native_modules = project.flist(native["modules"]) if native else []
        root_module = next((module for module in native_modules if module["source_index"] == 1), None)
        root_proof = root_module["flow"]["proof"] if root_module else None
        root_ssa = root_module["value_ssa"] if root_module else None
        report = {
            "schema_version": 1,
            "kind": "project-imported-signature-cache-fixture",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler_head": subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                capture_output=True, text=True,
            ).stdout.strip(),
            "compiler_branch": subprocess.run(
                ["git", "branch", "--show-current"], cwd=ROOT, check=True,
                capture_output=True, text=True,
            ).stdout.strip(),
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "fixture_scope": "two imported calls to one immutable provider table in one target-module lowering",
            "step_budget": step_budget,
            "bounded_prefix": step_budget < DEFAULT_STEP_BUDGET,
            "fixture_source_sha256": hashlib.sha256(b"a-provider.mncs\0" + hashlib.sha256(provider.encode()).digest() + b"b-root.mncs\0" + hashlib.sha256(root.encode()).digest()).hexdigest(),
            "execution_backend": execution["backend"],
            "retained_sessions": execution["retained_sessions"],
            "native_request_status": response.get("status"),
            "native_execution_steps": response.get("steps"),
            "native_project_valid": native.get("valid") if native else None,
            "native_project_ssa_valid": native.get("value_ssa_valid") if native else None,
            "native_root_proof_ok": root_proof.get("ok") if root_proof else None,
            "native_root_ssa_valid": root_ssa.get("valid") if root_ssa else None,
            "native_root_ssa_call_count": root_ssa.get("call_count") if root_ssa else None,
            "stage0_oracle": stage0_oracle,
            "requests": probe.requests,
            "result_sha256": probe.digest.hexdigest(),
        }
        if report["native_request_status"] == "returned":
            assert report["native_project_valid"] is True and report["native_project_ssa_valid"] is True, report
            assert report["native_root_proof_ok"] is True and report["native_root_ssa_valid"] is True, report
            assert report["native_root_ssa_call_count"] == 2, report
        elif not report["bounded_prefix"]:
            raise AssertionError(f"complete imported-signature fixture did not return: {report}")
        assert report["stage0_oracle"]["valid"] is True, report
        return report
    finally:
        probe.close()


def _seed_target_specialization(*, target_only=False):
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
    if target_only:
        # The target entry does not call compile_project or request the
        # stand-alone SSA verifier. Specializing those entries and retaining
        # a second SSA session adds work before a target-only request starts.
        seeds = [item for item in seeds if not (
            isinstance(item, dict)
            and (
                (item.get("module") == project.PROJECT_MODULE
                 and item.get("function") == "compile_project")
                or (item.get("module") == project.SSA_MODULE
                    and item.get("function") == "verify_function")
            )
        )]
        os.environ["MNCS_PROBE_EXECUTION_MODULES"] = project.PROJECT_MODULE
    if not any(item.get("module") == seed["module"]
               and item.get("function") == seed["function"]
               and item.get("type_arguments") == seed["type_arguments"]
               for item in seeds if isinstance(item, dict)):
        seeds.append(seed)
    os.environ["MNCS_PROBE_GENERIC_SEEDS"] = json.dumps(seeds)


def _target_execution_started(phase_timings_ns, response):
    """Return whether target execution is confirmed, false, or still unknown.

    The native request phase is recorded only when the target request is
    submitted. A transport failure before that phase means the target did not
    start. Once submitted, a returned executor status confirms execution;
    timeout or child exit leaves semantic execution UNKNOWN.
    """
    if "native_request_transport_and_execution" not in phase_timings_ns:
        return False
    if response.get("status") in {"returned", "budget_exhausted", "step_limit_exceeded"}:
        return True
    return None


def run(
    module_names=None,
    target_last=False,
    step_budget=DEFAULT_STEP_BUDGET,
    prepare_program_cache_only=False,
    admission_only=False,
):
    campaign_started = time.monotonic_ns()
    phase_timings_ns = {}
    transport_failure = None
    execution = {}
    response = {}
    native = None
    module_rows = []
    sources = []
    target_stem = None
    target_source_index = None
    identities = {}
    stage0_oracle = {
        "status": "UNKNOWN",
        "reason": "native request and Stage-0 oracle have not completed",
    }
    skip_stage0_oracle = os.environ.get("MNCS_PROBE_SKIP_STAGE0_ORACLE", "0") == "1"
    if (os.environ.get("MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES") == "1"
            and not skip_stage0_oracle):
        raise ValueError(
            "program-release profiling requires MNCS_PROBE_SKIP_STAGE0_ORACLE=1"
        )
    if target_last:
        _seed_target_specialization(target_only=True)
    lock = json.loads((ROOT / "mncs-language.lock.json").read_text())
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    phase_started = time.monotonic_ns()
    sources = _load_sources(module_names)
    phase_timings_ns["source_resolution_and_dependency_selection"] = time.monotonic_ns() - phase_started
    page_count = sum((len(source) + project.STRIDE_DEFAULT - 1) // project.STRIDE_DEFAULT
                     for _, _, source in sources)
    source_identity = hashlib.sha256(b"".join(
        source_id.encode() + b"\0" + hashlib.sha256(source).digest()
        for source_id, _, source in sources
    )).hexdigest()
    if target_last:
        if not module_names:
            raise ValueError("--target-last requires an explicit --modules selection")
        target_stem = module_names[-1]
        target_source_index = next(i for i, (source_id, _, _) in enumerate(sources)
                                   if Path(source_id).stem == target_stem)
    selection = "all" if not module_names else "-".join(module_names)
    if target_stem is not None:
        selection += "-target-" + target_stem
    artifact_label = os.environ.get("MNCS_PROBE_ARTIFACT_LABEL")
    if artifact_label and not re.fullmatch(r"[A-Za-z0-9_.-]+", artifact_label):
        raise ValueError("MNCS_PROBE_ARTIFACT_LABEL must be a simple filename label")
    artifact_prefix = (
        f"campaign-{artifact_label}"
        if artifact_label
        else f"campaign-{datetime.now(timezone.utc):%Y%m%d}"
    )
    artifact = ROOT / ".build" / "campaign-artifacts" / "compiler-facts" / (
        f"{artifact_prefix}-compiler-module-pipeline-{selection}.json"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    previous_trace_path = os.environ.get("MNCS_PROBE_TRACE_PATH")
    previous_stderr_path = os.environ.get("MNCS_PROBE_STDERR_PATH")
    previous_mncs_timings = os.environ.get("MNCS_TIMINGS")
    trace_path = None
    stderr_path = None
    if os.environ.get("MNCS_PROBE_TELEMETRY") == "1":
        suffix = time.time_ns()
        trace_path = (
            Path(previous_trace_path).expanduser().resolve()
            if previous_trace_path
            else artifact.with_name(f"{artifact.stem}-trace-{suffix}.jsonl").resolve()
        )
        stderr_path = (
            Path(previous_stderr_path).expanduser().resolve()
            if previous_stderr_path
            else artifact.with_name(f"{artifact.stem}-stderr-{suffix}.log").resolve()
        )
        os.environ["MNCS_PROBE_TRACE_PATH"] = str(trace_path)
        os.environ["MNCS_PROBE_STDERR_PATH"] = str(stderr_path)
        os.environ["MNCS_TIMINGS"] = "1"
    probe = project.Probe()
    try:
        phase_started = time.monotonic_ns()
        try:
            execution = probe.send({"execution_status": True})
        except (project.ProbeTimeout, project.ProbeExited) as error:
            transport_failure = {"stage": "retained_session_admission", **error.observation}
        phase_timings_ns["retained_session_admission"] = time.monotonic_ns() - phase_started

        if transport_failure is None and prepare_program_cache_only:
            # The readiness exchange follows backend-independent frontend
            # elaboration and cache publication. This mode stops before any
            # backend admission or target compiler execution.
            response = {"status": "not_run"}
        elif transport_failure is None:
            phase_started = time.monotonic_ns()
            try:
                identities = project.identity_map(probe)
            except (project.ProbeTimeout, project.ProbeExited) as error:
                transport_failure = {"stage": "record_type_identity_admission", **error.observation}
            phase_timings_ns["record_type_identity_admission"] = time.monotonic_ns() - phase_started

        if transport_failure is None and admission_only:
            # Exercise backend construction, cache publication, retained-session
            # admission, and record identities without entering target execution.
            response = {"status": "not_run", "reason": "backend_admission_only"}

        request = None
        if transport_failure is None and not prepare_program_cache_only and not admission_only:
            phase_started = time.monotonic_ns()
            request = project.request_value(identities, sources)
            request["step_budget"] = step_budget
            if target_last:
                request["target"] = {"module": project.PROJECT_MODULE, "function": "compile_project_target"}
                request["arguments"].append(project.integer(target_source_index))
            phase_timings_ns["native_request_construction"] = time.monotonic_ns() - phase_started
            phase_started = time.monotonic_ns()
            try:
                response = probe.send(request)
            except (project.ProbeTimeout, project.ProbeExited) as error:
                transport_failure = {"stage": "native_compiler_execution", **error.observation}
            phase_timings_ns["native_request_transport_and_execution"] = time.monotonic_ns() - phase_started

        if response.get("status") == "returned" and response.get("returned"):
            if response.get("steps") is not None:
                probe.steps.append(response["steps"])
            phase_started = time.monotonic_ns()
            native = project.decode(response["returned"][0])
            for module in project.flist(native["modules"]):
                source_index = module["source_index"]
                source = sources[source_index][2]
                module_rows.append(_module_summary(module, source))
            phase_timings_ns["response_decode_and_summary"] = time.monotonic_ns() - phase_started
        elif response.get("steps") is not None:
            probe.steps.append(response["steps"])

        if (transport_failure is None and not prepare_program_cache_only and not admission_only
                and module_names and not skip_stage0_oracle and response.get("status") in
                {"returned", "budget_exhausted", "step_limit_exceeded"}):
            phase_started = time.monotonic_ns()
            try:
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
            except (project.ProbeTimeout, project.ProbeExited) as error:
                transport_failure = {"stage": "stage0_project_oracle", **error.observation}
                stage0_oracle = {"status": "UNKNOWN", "reason": str(error)}
            phase_timings_ns["stage0_oracle"] = time.monotonic_ns() - phase_started
        elif prepare_program_cache_only:
            stage0_oracle = {
                "status": "not-run",
                "reason": "program-cache preparation mode does not execute the compiler target or Stage-0 oracle",
            }
        elif admission_only:
            stage0_oracle = {
                "status": "not-run",
                "reason": "backend admission mode does not execute the compiler target or Stage-0 oracle",
            }
        elif transport_failure is None and module_names and skip_stage0_oracle:
            stage0_oracle = {
                "status": "not-run",
                "reason": "skipped for bounded native execution profiling; this run makes no Stage-0 differential claim",
            }
        elif transport_failure is None and module_names:
            stage0_oracle = {
                "status": "not-run",
                "reason": "native request was rejected before project execution",
            }

        target_request_attempted = "native_request_transport_and_execution" in phase_timings_ns
        target_execution_started = _target_execution_started(phase_timings_ns, response)
        target_execution_status = (
            "NOT_ATTEMPTED" if not target_request_attempted
            else "CONFIRMED" if target_execution_started is True
            else "UNKNOWN"
        )
        report = {
            "schema_version": 1,
            "kind": (
                "compiler-program-cache-preparation"
                if prepare_program_cache_only
                else "compiler-backend-admission-only"
                if admission_only
                else "compiler-module-pipeline-frontier"
            ),
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "compiler_head": head,
            "compiler_identity_role": "tested candidate head; delivered main identity is recorded separately in Git state",
            "compiler_branch": subprocess.run(
                ["git", "branch", "--show-current"], cwd=ROOT, check=True,
                capture_output=True, text=True,
            ).stdout.strip(),
            "stage0_revision": lock["revision"],
            "stage0_source_profile": lock["source_profile"],
            "selected_modules": module_names or "all",
            "lowering_scope": "target-module" if target_last else "all-modules",
            "target_source_index": target_source_index,
            "step_budget": step_budget,
            "compiler_source_count": len(sources),
            "input_total_page_count_at_stride_1024": page_count,
            "input_page_bound": 1024,
            "compiler_source_sha256": source_identity,
            "compiler_source_identity_kind": "sha256 of selected compiler module ids and bytes",
            "execution_backend_requested": os.environ.get("MNCS_PROBE_BACKEND"),
            "execution_backend": execution.get("backend"),
            "execution_backend_admitted": execution.get("backend"),
            "reference_execution_session_preparation": "lazy_on_demand",
            "program_release_after_identities": (
                os.environ.get("MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES") == "1"
            ),
            "program_cache_preparation_only": prepare_program_cache_only,
            "backend_admission_only": admission_only,
            "target_request_attempted": target_request_attempted,
            "target_execution_started": target_execution_started,
            "target_execution_status": target_execution_status,
            "target_input_modules": [Path(source_id).stem for source_id, _, _ in sources],
            "stage0_admission_source_modules": os.environ.get("MNCS_PROBE_MODULES"),
            "execution_modules_requested": os.environ.get("MNCS_PROBE_EXECUTION_MODULES"),
            "generic_seed_requests": json.loads(os.environ.get("MNCS_PROBE_GENERIC_SEEDS", "[]")),
            "retained_sessions": execution.get("retained_sessions"),
            "native_request_status": response.get("status"),
            "native_failure": response.get("failure"),
            "native_transport_outcome": (
                transport_failure.get("status")
                if transport_failure
                else "NOT_RUN" if prepare_program_cache_only or admission_only
                else "RESPONSE_RECEIVED"
            ),
            "transport_failure": transport_failure,
            "native_project_valid": native["valid"] if native else None,
            "native_project_ssa_valid": native["value_ssa_valid"] if native else None,
            "module_count": len(module_rows),
            "modules": module_rows,
            "diagnostics": project.flist(native["diagnostics"]) if native else [],
            "stage0_oracle": stage0_oracle,
            "stage0_oracle_skipped": skip_stage0_oracle,
            "requests": probe.requests,
            "request_attempts": len(probe.request_observations),
            "request_observations": probe.request_observations,
            "native_execution_steps_total": sum(probe.steps),
            "result_sha256": probe.digest.hexdigest(),
            "phase_timings_ns": phase_timings_ns,
            "campaign_elapsed_ns_before_close": time.monotonic_ns() - campaign_started,
            "probe_spawn_elapsed_ns": sum(
                getattr(proc, "_mncs_spawn_elapsed_ns", 0) for proc in probe._procs.values()
            ),
            "resource_telemetry_enabled": probe._telemetry,
            "request_timeout_s": probe._request_timeout_s,
            "address_space_limit_bytes": probe._address_space_limit_bytes,
            "process_observations": probe.process_observations,
        }
        report["evidence_artifact"] = str(artifact.relative_to(ROOT))
    finally:
        try:
            probe.close()
        except AssertionError as error:
            if "report" in locals():
                report["probe_close_error"] = str(error)
        if previous_trace_path is None:
            os.environ.pop("MNCS_PROBE_TRACE_PATH", None)
        else:
            os.environ["MNCS_PROBE_TRACE_PATH"] = previous_trace_path
        if previous_stderr_path is None:
            os.environ.pop("MNCS_PROBE_STDERR_PATH", None)
        else:
            os.environ["MNCS_PROBE_STDERR_PATH"] = previous_stderr_path
        if previous_mncs_timings is None:
            os.environ.pop("MNCS_TIMINGS", None)
        else:
            os.environ["MNCS_TIMINGS"] = previous_mncs_timings
    if "report" in locals():
        report["process_observations"] = probe.process_observations
        report["campaign_elapsed_ns"] = time.monotonic_ns() - campaign_started
        if trace_path is not None:
            trace_events = []
            try:
                trace_events = [json.loads(line) for line in trace_path.read_text().splitlines() if line]
                report["probe_internal_trace"] = {
                    "status": "OBSERVED",
                    "path": str(trace_path.relative_to(ROOT)),
                    "sha256": hashlib.sha256(trace_path.read_bytes()).hexdigest(),
                    "event_count": len(trace_events),
                    "events": trace_events,
                }
            except OSError as error:
                report["probe_internal_trace"] = {
                    "status": "UNKNOWN",
                    "path": str(trace_path.relative_to(ROOT)),
                    "reason": type(error).__name__,
                }
        else:
            report["probe_internal_trace"] = {"status": "UNKNOWN", "reason": "MNCS_PROBE_TELEMETRY is disabled"}
        if stderr_path is not None:
            try:
                stderr_bytes = stderr_path.read_bytes()
                stderr_lines = stderr_bytes.decode(errors="replace").splitlines()
                report["probe_stderr"] = {
                    "status": "OBSERVED",
                    "path": str(stderr_path.relative_to(ROOT)),
                    "sha256": hashlib.sha256(stderr_bytes).hexdigest(),
                    "bytes": len(stderr_bytes),
                    "mncs_timings": [line for line in stderr_lines if line.startswith("mncs-timing ")],
                    "tail": stderr_lines[-128:],
                }
            except OSError as error:
                report["probe_stderr"] = {
                    "status": "UNKNOWN",
                    "path": str(stderr_path.relative_to(ROOT)),
                    "reason": type(error).__name__,
                }
        else:
            report["probe_stderr"] = {"status": "UNKNOWN", "reason": "MNCS_PROBE_TELEMETRY is disabled"}
        serialization_started = time.monotonic_ns()
        serialized_report = json.dumps(report, indent=2) + "\n"
        report["phase_timings_ns"]["result_serialization"] = time.monotonic_ns() - serialization_started
        # Include serialization cost in the persisted report. Its value is
        # measured immediately before the final encoding/write pass.
        artifact.write_text(json.dumps(report, indent=2) + "\n")
        return report


if __name__ == "__main__":
    names = None
    args = sys.argv[1:]
    target_last = "--target-last" in args
    oracle_only = "--oracle-only" in args
    signature_cache_fixture = "--signature-cache-fixture" in args
    prepare_program_cache_only = "--prepare-program-cache-only" in args
    admission_only = "--admission-only" in args
    step_budget = DEFAULT_STEP_BUDGET
    if "--step-budget" in args:
        position = args.index("--step-budget")
        if position + 1 >= len(args):
            raise SystemExit("--step-budget requires an integer")
        try:
            step_budget = int(args[position + 1])
        except ValueError as error:
            raise SystemExit("--step-budget requires an integer") from error
        del args[position:position + 2]
        if not 1 <= step_budget <= DEFAULT_STEP_BUDGET:
            raise SystemExit(f"--step-budget must be between 1 and {DEFAULT_STEP_BUDGET}")
    args = [arg for arg in args if arg != "--target-last"]
    args = [arg for arg in args if arg != "--oracle-only"]
    args = [arg for arg in args if arg != "--signature-cache-fixture"]
    args = [arg for arg in args if arg != "--prepare-program-cache-only"]
    args = [arg for arg in args if arg != "--admission-only"]
    if args:
        if args[0] != "--modules" or len(args) < 2:
            raise SystemExit("usage: probe_compiler_module_frontier.py [--modules module_stem ...] [--target-last] [--oracle-only] [--signature-cache-fixture] [--prepare-program-cache-only] [--admission-only]")
        names = args[1:]
    if admission_only and not target_last:
        raise SystemExit("--admission-only requires --target-last")
    if admission_only and (prepare_program_cache_only or oracle_only or signature_cache_fixture):
        raise SystemExit("--admission-only cannot be combined with cache preparation, oracle-only, or signature-cache fixture modes")
    if signature_cache_fixture:
        report = run_imported_signature_cache_fixture(step_budget)
    elif oracle_only:
        report = run_stage0_oracle_only(names)
    else:
        if prepare_program_cache_only and not target_last:
            raise SystemExit("--prepare-program-cache-only requires --target-last")
        report = run(
            names,
            target_last=target_last,
            step_budget=step_budget,
            prepare_program_cache_only=prepare_program_cache_only,
            admission_only=admission_only,
        )
    print(json.dumps(report, indent=2))
