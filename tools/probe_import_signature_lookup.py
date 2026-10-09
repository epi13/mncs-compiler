#!/usr/bin/env python3
"""Measure imported-call lookup across bounded provider sizes.

The generated modules are reduced compiler fixtures. MNCS owns parsing,
signature resolution, proof, CFG, and SSA; Python only builds the request,
measures the retained child, and checks the independent Stage-0 oracle.
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import test_project as project
from probe_compiler_module_frontier import _load_sources, _seed_target_specialization


ROOT = Path(project.__file__).resolve().parents[1]
MODULES = ["source", "lexer", "parser", "segment", "decl", "flow", "ssa", "project"]
PROVIDER_DUMMY_COUNTS = (128, 512, 1024)
CALL_COUNT = int(os.environ.get("MNCS_IMPORT_SIGNATURE_CALL_COUNT", "8"))
UNIQUE_CALL_NAMES = os.environ.get("MNCS_IMPORT_SIGNATURE_UNIQUE_CALL_NAMES", "0") == "1"
STEP_BUDGET = 8_000_000


def source_identity(sources):
    return hashlib.sha256(b"".join(
        source_id.encode() + b"\0" + hashlib.sha256(source).digest()
        for source_id, _, source in sources
    )).hexdigest()


def fixture(identities, dummy_count, call_count):
    if UNIQUE_CALL_NAMES and dummy_count < call_count:
        raise ValueError("unique callee fixtures need at least one provider per call")
    dummies = "\n".join(
        f"fn d{i:04}(x: u64) -> (r: u64) {{ return x; }}"
        for i in range(dummy_count)
    )
    dependency = (
        "mncs 0.18; module demo.dep; "
        "fn late(x: u64) -> (r: u64) { return x; }\n"
        f"{dummies}\n"
    ).encode()
    callee_names = (
        [f"d{i:04}" for i in range(call_count)]
        if UNIQUE_CALL_NAMES else ["late"] * call_count
    )
    calls = "\n".join(
        f"fn call{i:04}(x: u64) -> (r: u64) {{ return dep.{callee}(x); }}"
        for i, callee in enumerate(callee_names)
    )
    root = (
        "mncs 0.18; module demo.root; use demo.dep as dep; "
        f"{calls}\n"
    ).encode()
    sources = [
        ("a_dep.mncs", Path("a_dep.mncs"), dependency),
        ("b_root.mncs", Path("b_root.mncs"), root),
    ]
    request = project.request_value(identities, sources)
    request["target"] = {
        "module": project.PROJECT_MODULE,
        "function": "compile_project_target",
    }
    request["arguments"].append(project.integer(1))
    request["step_budget"] = STEP_BUDGET
    return request, dependency, root, callee_names


def run():
    _seed_target_specialization(target_only=True)
    sources = _load_sources(MODULES)
    probe = project.Probe()
    rows = []
    try:
        try:
            execution = probe.send({"execution_status": True})
        except (project.ProbeTimeout, project.ProbeExited) as error:
            return {
                "schema_version": "mncs.compiler.import-signature-lookup-probe/1",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
                "compiler_source_identity_sha256": source_identity(sources),
                "compiler_source_identity_kind": "selected compiler module ids and source bytes",
                "selected_modules": MODULES,
                "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
                "status": error.observation.get("status", "UNKNOWN"),
                "failure_stage": "retained_session_admission",
                "observation": error.observation,
                "step_budget": STEP_BUDGET,
                "request_timeout_seconds": float(os.environ.get("MNCS_PROBE_REQUEST_TIMEOUT_S", "0")),
                "address_space_limit_bytes": int(os.environ.get("MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES", "0")),
            }
        identities = project.identity_map(probe)
        for dummy_count in PROVIDER_DUMMY_COUNTS:
            request, dependency, root, callee_names = fixture(identities, dummy_count, CALL_COUNT)
            started = time.monotonic_ns()
            try:
                response = probe.send(request)
            except (project.ProbeTimeout, project.ProbeExited) as error:
                rows.append({
                    "provider_function_count": dummy_count + 1,
                    "qualified_call_count": CALL_COUNT,
                    "status": error.observation.get("status", "UNKNOWN"),
                    "failure_stage": "native_compiler_execution",
                    "observation": error.observation,
                })
                break
            elapsed = time.monotonic_ns() - started
            if response.get("status") != "returned" or not response.get("returned"):
                rows.append({
                    "provider_function_count": dummy_count + 1,
                    "qualified_call_count": CALL_COUNT,
                    "status": response.get("status", "UNKNOWN"),
                    "failure": response.get("failure"),
                    "elapsed_ns": elapsed,
                })
                break

            native = project.decode(response["returned"][0])
            modules = project.flist(native["modules"])
            assert len(modules) == 1, len(modules)
            proof = modules[0]["flow"]["proof"]
            ssa = modules[0]["value_ssa"]
            try:
                oracle = probe.send({"project_oracle": {
                    "root": root.decode(),
                    "modules": {"demo.dep": dependency.decode()},
                }})
            except (project.ProbeTimeout, project.ProbeExited) as error:
                rows.append({
                    "provider_function_count": dummy_count + 1,
                    "qualified_call_count": CALL_COUNT,
                    "status": "UNKNOWN",
                    "failure_stage": "stage0_oracle",
                    "native_status": "SUCCESS" if native["valid"] and native["value_ssa_valid"] else "FAILURE",
                    "native_wall_seconds": elapsed / 1e9,
                    "native_proof_function_count": proof["fn_count"],
                    "native_ssa_verified_function_count": ssa["verified_function_count"],
                    "observation": error.observation,
                })
                break
            observation = probe.request_observations[-2]
            resource = observation.get("resource_summary", {})
            native_ops = [
                op["$p"] for body in project.flist(proof["tops"])
                for op in project.flist(body) if "identity" in op.get("$p", {})
            ]
            stage0_callables = {
                function.get("name"): function.get("identity")
                for function in oracle.get("program", {}).get("functions", [])
                if function.get("home_module") == "demo.dep"
            }
            expected_identities = [stage0_callables.get(name) for name in callee_names]
            stage0_identity = expected_identities[0] if expected_identities else None
            native_identities = [project.identity_text(op["identity"]) for op in native_ops]
            # Stage-0's project JSON already exposes each callable identity
            # as canonical text; native proof values still need decoding.
            expected_identity_texts = expected_identities
            ssa_first_identity = project.identity_text(ssa["first_call_identity"])
            identity_multiset_matches = (
                len(native_identities) == CALL_COUNT
                and sorted(native_identities) == sorted(expected_identity_texts)
            )
            ssa_identity_is_native = ssa_first_identity in native_identities
            identity_parity = (
                all(identity is not None for identity in expected_identities)
                and identity_multiset_matches
                and ssa_identity_is_native
            )
            semantically_valid = (
                native["valid"] and native["value_ssa_valid"] and proof["ok"]
                and ssa["valid"] and oracle.get("valid") is True
            )
            result_status = "SUCCESS" if semantically_valid and identity_parity else "FAILURE"
            rows.append({
                "provider_function_count": dummy_count + 1,
                "qualified_call_count": CALL_COUNT,
                "status": result_status,
                "wall_seconds": elapsed / 1e9,
                "reported_step_value": response.get("steps"),
                "reported_step_scope": (
                    "request marker; Cranelift does not report MNCS instruction count"
                    if execution.get("backend") == "cranelift"
                    else "backend-defined; interpret only with backend-specific contract"
                ),
                "child": observation.get("process"),
                "child_cpu_time_delta_ns": resource.get("cpu_time_delta_ns"),
                "child_rss_high_water_bytes": resource.get("max_kernel_rss_high_water_bytes"),
                "sample_count": resource.get("sample_count"),
                "fd_high_water": resource.get("peak_fd_count"),
                "resource_limits": observation.get("resource_limits"),
                "cgroup_before": observation.get("cgroup_before"),
                "cgroup_after": observation.get("cgroup_after"),
                "native_valid": native["valid"],
                "native_ssa_valid": native["value_ssa_valid"],
                "native_proof_function_count": proof["fn_count"],
                "native_ssa_verified_function_count": ssa["verified_function_count"],
                "stage0_valid": oracle["valid"],
                "stage0_linked_function_count": len(oracle.get("program", {}).get("functions", [])),
                "native_callable_identity_count": len(native_identities),
                "stage0_callable_identity": stage0_identity,
                "all_native_call_identities_match_stage0": identity_parity,
                "identity_parity_detail": None if identity_parity else {
                    "expected_stage0_identities": expected_identity_texts,
                    "native_call_identities": native_identities,
                    "ssa_first_call_identity": ssa_first_identity,
                    "identity_multiset_matches": identity_multiset_matches,
                    "ssa_identity_is_native": ssa_identity_is_native,
                },
                "dependency_source_sha256": hashlib.sha256(dependency).hexdigest(),
                "root_source_sha256": hashlib.sha256(root).hexdigest(),
            })
        return {
            "schema_version": "mncs.compiler.import-signature-lookup-probe/1",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "compiler_source_identity_sha256": source_identity(sources),
            "compiler_source_identity_kind": "selected compiler module ids and source bytes",
            "selected_modules": MODULES,
            "stage0_revision": json.loads((ROOT / "mncs-language.lock.json").read_text())["revision"],
            "backend": execution.get("backend"),
            "status": "SUCCESS" if rows and all(row.get("status") == "SUCCESS" for row in rows) else (
                rows[-1].get("status", "UNKNOWN") if rows else "UNKNOWN"
            ),
            "step_budget": STEP_BUDGET,
            "request_timeout_seconds": float(os.environ.get("MNCS_PROBE_REQUEST_TIMEOUT_S", "0")),
            "address_space_limit_bytes": int(os.environ.get("MNCS_PROBE_MAX_ADDRESS_SPACE_BYTES", "0")),
            "program_release_after_identities": os.environ.get("MNCS_PROBE_RELEASE_PROGRAMS_AFTER_IDENTITIES") == "1",
            "resource_sampling_enabled": probe._telemetry,
            "call_count_per_fixture": CALL_COUNT,
            "callee_pattern": "unique" if UNIQUE_CALL_NAMES else "repeated-one-name",
            "rows": rows,
        }
    finally:
        probe.close()


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
    if result.get("status") != "SUCCESS":
        raise SystemExit(1)
