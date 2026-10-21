#!/usr/bin/env python3
"""Bounded contract tests for the Environment-facing application build provider."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_vm_native_application as app
import test_native_application_incremental as incremental
import test_vm_native_application_batch as batch_app
import test_vm_test_digest_app as digest_app
import application_build_provider as provider

configured_probe = os.environ.get("MNCS_COMPILER_PROBE")
configured_native_probe = os.environ.get("MNCS_NATIVE_APP_PROBE_BIN")
if configured_probe:
    app.PROBE = Path(configured_probe).expanduser().resolve()

ROOT = app.ROOT
PROVIDER = ROOT / "tools/application_build_provider.py"
REQUEST_SCHEMA = "mncs.compiler.application-build-request/1"
RESPONSE_SCHEMA = "mncs.compiler.application-build/4"
MULTI_REQUEST_SCHEMA = "mncs.compiler.application-build-request/2"
MULTI_RESPONSE_SCHEMA = "mncs.compiler.application-build/5"
INVENTORY_REQUEST_SCHEMA = "mncs.compiler.application-build-request/3"
INVENTORY_RESPONSE_SCHEMA = "mncs.compiler.application-build/6"
TEST_SELECTION_REQUEST_SCHEMA = "mncs.compiler.application-build-request/4"
TEST_SELECTION_RESPONSE_SCHEMA = "mncs.compiler.application-build/7"
DECLARATION_FACT_REQUEST_SCHEMA = "mncs.compiler.application-build-request/5"
DECLARATION_FACT_RESPONSE_SCHEMA = "mncs.compiler.application-build/8"
SOURCE_PARSE_CACHE_REQUEST_SCHEMA = "mncs.compiler.application-build-request/6"
SOURCE_PARSE_CACHE_RESPONSE_SCHEMA = "mncs.compiler.application-build/9"
FINITE_TYPE_FACT_REQUEST_SCHEMA = "mncs.compiler.application-build-request/7"
FINITE_TYPE_FACT_RESPONSE_SCHEMA = "mncs.compiler.application-build/10"
RECORD_TYPE_FACT_REQUEST_SCHEMA = "mncs.compiler.application-build-request/8"
RECORD_TYPE_FACT_RESPONSE_SCHEMA = "mncs.compiler.application-build/11"
SSA_FUNCTION_FACT_REQUEST_SCHEMA = "mncs.compiler.application-build-request/9"
SSA_FUNCTION_FACT_RESPONSE_SCHEMA = "mncs.compiler.application-build/12"
SSA_FUNCTION_ARTIFACT_CACHE_REQUEST_SCHEMA = "mncs.compiler.application-build-request/10"
SSA_FUNCTION_ARTIFACT_CACHE_RESPONSE_SCHEMA = "mncs.compiler.application-build/13"
INVOCATION_SCHEMA = "mncs.compiler.application-build-invocation/1"
_DIGEST_EVIDENCE_PATH = Path(os.environ.get(
    "MNCS_NATIVE_APP_PROVIDER_EVIDENCE_PATH",
    ROOT / ".build/native-app-readiness" / (
        f"provider-digest-{os.getpid()}-{time.time_ns()}.json"
    ),
))
DIGEST_EVIDENCE_PATH = (
    _DIGEST_EVIDENCE_PATH
    if _DIGEST_EVIDENCE_PATH.is_absolute()
    else ROOT / _DIGEST_EVIDENCE_PATH
)


def validate_environment_contract_descriptor() -> None:
    """Keep Environment's advertised revision aligned with the wire contract."""
    manifest = json.loads((ROOT / ".mncs/project.json").read_text(encoding="utf-8"))
    contracts = [
        row for row in manifest.get("contracts", {}).get("provides", [])
        if isinstance(row, dict) and row.get("contract") == "native-application-build"
    ]
    if len(contracts) != 1:
        raise AssertionError("project manifest must declare one native application-build contract")
    contract = contracts[0]
    artifact = contract.get("artifact_contract", {})
    expected_pairs = {
        (REQUEST_SCHEMA, RESPONSE_SCHEMA),
        (MULTI_REQUEST_SCHEMA, MULTI_RESPONSE_SCHEMA),
        (INVENTORY_REQUEST_SCHEMA, INVENTORY_RESPONSE_SCHEMA),
        (TEST_SELECTION_REQUEST_SCHEMA, TEST_SELECTION_RESPONSE_SCHEMA),
        (DECLARATION_FACT_REQUEST_SCHEMA, DECLARATION_FACT_RESPONSE_SCHEMA),
        (SOURCE_PARSE_CACHE_REQUEST_SCHEMA, SOURCE_PARSE_CACHE_RESPONSE_SCHEMA),
        (FINITE_TYPE_FACT_REQUEST_SCHEMA, FINITE_TYPE_FACT_RESPONSE_SCHEMA),
        (RECORD_TYPE_FACT_REQUEST_SCHEMA, RECORD_TYPE_FACT_RESPONSE_SCHEMA),
        (SSA_FUNCTION_FACT_REQUEST_SCHEMA, SSA_FUNCTION_FACT_RESPONSE_SCHEMA),
        (SSA_FUNCTION_ARTIFACT_CACHE_REQUEST_SCHEMA, SSA_FUNCTION_ARTIFACT_CACHE_RESPONSE_SCHEMA),
    }
    actual_pairs = {
        (row.get("request_schema"), row.get("response_schema"))
        for row in artifact.get("supported_request_response_pairs", [])
        if isinstance(row, dict)
    }
    if contract.get("version") != "13":
        raise AssertionError("Compiler capability revision must match application-build/13")
    if (artifact.get("request_schema"), artifact.get("response_schema")) != (
        SSA_FUNCTION_ARTIFACT_CACHE_REQUEST_SCHEMA, SSA_FUNCTION_ARTIFACT_CACHE_RESPONSE_SCHEMA
    ):
        raise AssertionError("Compiler must advertise application-build/13 as its current contract")
    if actual_pairs != expected_pairs:
        raise AssertionError("Compiler descriptor must retain /4 through /13 request-response pairs")
    if (artifact.get("record_type_declaration_fact_schema")
            != "mncs.language-declaration-verification-fact/1"
            or artifact.get("record_type_declaration_fact_verifier_identity")
            != "mncs-language-record-type-validator/1"
            or artifact.get("record_type_declaration_fact_policy_identity")
            != "mncs-language-record-type-policy/1"):
        raise AssertionError("Compiler descriptor omitted the Language record fact verifier contract")
    if (artifact.get("verification_work_schema")
            != "mncs.compiler.application-build-verification-work/8"
            or artifact.get("application_build_cache_entry_schema")
            != "mncs.compiler.application-build-cache-entry/10"
            or artifact.get("ssa_function_artifact_cache_schema")
            != "mncs.language-ssa-function-artifact-cache/1"
            or artifact.get("ssa_function_artifact_cache_observation_schema")
            != "mncs.language-ssa-function-artifact-cache-observation/1"):
        raise AssertionError("Compiler descriptor omitted Language SSA artifact cache identities")
    if (artifact.get("source_test_inventory_schema") != "mncs.test-inventory/1"
            or artifact.get("source_test_inventory_scope") != "source_module"):
        raise AssertionError("application-build/7 must declare the Language source-module Test inventory")
    if (artifact.get("source_test_selection_schema") != "mncs.compiler.source-test-case-selection/1"
            or artifact.get("source_test_selection_maximum_count") != 256):
        raise AssertionError("application-build/7 must declare its bounded Language identity selection")
    if provider.RESPONSE_SCHEMA_BY_REQUEST != {
        REQUEST_SCHEMA: RESPONSE_SCHEMA,
        MULTI_REQUEST_SCHEMA: MULTI_RESPONSE_SCHEMA,
        INVENTORY_REQUEST_SCHEMA: INVENTORY_RESPONSE_SCHEMA,
        TEST_SELECTION_REQUEST_SCHEMA: TEST_SELECTION_RESPONSE_SCHEMA,
        DECLARATION_FACT_REQUEST_SCHEMA: DECLARATION_FACT_RESPONSE_SCHEMA,
        SOURCE_PARSE_CACHE_REQUEST_SCHEMA: SOURCE_PARSE_CACHE_RESPONSE_SCHEMA,
        FINITE_TYPE_FACT_REQUEST_SCHEMA: FINITE_TYPE_FACT_RESPONSE_SCHEMA,
        RECORD_TYPE_FACT_REQUEST_SCHEMA: RECORD_TYPE_FACT_RESPONSE_SCHEMA,
        SSA_FUNCTION_FACT_REQUEST_SCHEMA: SSA_FUNCTION_FACT_RESPONSE_SCHEMA,
        SSA_FUNCTION_ARTIFACT_CACHE_REQUEST_SCHEMA: SSA_FUNCTION_ARTIFACT_CACHE_RESPONSE_SCHEMA,
    }:
        raise AssertionError("Environment descriptor no longer matches provider schema support")


def request_for(root: str, modules: dict[str, str]) -> dict:
    return {
        "schema_version": REQUEST_SCHEMA,
        "target": "mncs-vm-0.1",
        "root_source_id": app.APP_SOURCE_ID,
        "root_source": root,
        "entry": {"module": app.APP_MODULE, "function": "run"},
        "modules": modules,
        "capabilities": [],
        "effect_assumptions": [],
        "optimization_policy": "default",
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def write_json(path: Path, value: object) -> bytes:
    content = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    path.write_bytes(content)
    return content


def _proc_row(pid: int) -> dict | None:
    root = Path("/proc") / str(pid)
    try:
        stat = (root / "stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()
        return {
            "pid": pid,
            "ppid": int(fields[1]),
            "cpu_ticks": int(fields[11]) + int(fields[12]),
        }
    except (OSError, ValueError, IndexError):
        return None


def sample_process_tree(root_pid: int) -> dict:
    """Sample one provider invocation and its live descendants from /proc."""
    proc = Path("/proc")
    if not proc.is_dir():
        return {
            "cpu_seconds": None,
            "rss_bytes": None,
            "threads": None,
            "fd_count": None,
            "child_process_count": None,
            "process_count": None,
        }
    rows = {}
    children: dict[int, list[int]] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        row = _proc_row(int(entry.name))
        if row is None:
            continue
        rows[row["pid"]] = row
        children.setdefault(row["ppid"], []).append(row["pid"])

    selected = []
    pending = [root_pid]
    seen = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        row = rows.get(pid)
        if row is None:
            continue
        selected.append(row)
        pending.extend(children.get(pid, []))
    if not selected:
        return {
            "cpu_seconds": 0.0,
            "rss_bytes": 0,
            "threads": 0,
            "fd_count": 0,
            "child_process_count": 0,
            "process_count": 0,
        }
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    for row in selected:
        root = proc / str(row["pid"])
        try:
            status = {}
            for line in (root / "status").read_text().splitlines():
                if ":" in line:
                    key, value = line.split(":", 1)
                    status[key] = value.strip()
            row["rss_bytes"] = int(status.get("VmRSS", "0 kB").split()[0]) * 1024
            row["threads"] = int(status.get("Threads", "0"))
            row["fd_count"] = len(list((root / "fd").iterdir()))
        except (OSError, ValueError, IndexError):
            row["rss_bytes"] = 0
            row["threads"] = 0
            row["fd_count"] = 0
    return {
        "cpu_seconds": sum(row["cpu_ticks"] for row in selected) / ticks_per_second,
        "rss_bytes": sum(row["rss_bytes"] for row in selected),
        "threads": sum(row["threads"] for row in selected),
        "fd_count": sum(row["fd_count"] for row in selected),
        "child_process_count": len(selected) - 1,
        "process_count": len(selected),
    }


def summarize_process_tree(samples: list[dict], wall_seconds: float) -> dict:
    available = [row for row in samples if row.get("cpu_seconds") is not None]
    if not available:
        return {
            "wall_seconds": round(wall_seconds, 6),
            "cpu_seconds": None,
            "peak_rss_bytes": None,
            "peak_cgroup_memory_bytes": None,
            "peak_open_file_descriptors": None,
            "peak_threads": None,
            "peak_child_processes": None,
            "peak_process_count": None,
            "method": "unavailable",
        }
    return {
        "wall_seconds": round(wall_seconds, 6),
        "cpu_seconds": round(
            max(0.0, max(row["cpu_seconds"] for row in available)
                - available[0]["cpu_seconds"]),
            6,
        ),
        "peak_rss_bytes": max(row["rss_bytes"] for row in available),
        "peak_cgroup_memory_bytes": None,
        "peak_open_file_descriptors": max(row["fd_count"] for row in available),
        "peak_threads": max(row["threads"] for row in available),
        "peak_child_processes": max(row["child_process_count"] for row in available),
        "peak_process_count": max(row["process_count"] for row in available),
        "method": "sampled /proc process tree; 10 ms interval; cgroup peak unavailable",
    }


def verification_work(response: dict, *, label: str, frontend_invocations: int) -> dict:
    cache = response.get("cache", {})
    work = cache.get("verification_work", {})
    if (
        cache.get("schema_version")
        != "mncs.compiler.application-build-cache-observation/4"
        or work.get("schema_version")
        != "mncs.compiler.application-build-verification-work/2"
    ):
        raise AssertionError(
            f"{label} omitted versioned verification-work telemetry: cache={cache}"
        )
    if work.get("front_end_invocations") != frontend_invocations:
        raise AssertionError(f"{label} frontend invocation count is inaccurate: {work}")
    expected_stage = "recomputed_by_full_frontend" if frontend_invocations else "not_run"
    for stage in ("source_parsing", "declaration_resolution", "signature_verification"):
        if work.get(stage) != expected_stage:
            raise AssertionError(f"{label} has unexpected {stage} work: {work}")
    body_stage = (
        "not_run" if not frontend_invocations else
        "recomputed_with_language_fact_reuse" if work.get("reused_semantic_facts") else
        "recomputed_by_full_frontend"
    )
    if work.get("function_body_verification") != body_stage:
        raise AssertionError(f"{label} has unexpected function-body work: {work}")
    if (not isinstance(work.get("recomputed_semantic_facts"), list)
            or type(work.get("produced_semantic_fact_count")) is not int):
        raise AssertionError(f"{label} omitted Language fact work: {work}")
    if frontend_invocations and (
        type(work.get("resolved_module_count")) is not int
        or work["resolved_module_count"] < 1
    ):
        raise AssertionError(f"{label} omitted resolved-module work: {work}")
    return work


def ensure_current_probe() -> dict:
    if configured_probe or configured_native_probe:
        if not app.PROBE.is_file():
            raise SystemExit("selected MNCS_COMPILER_PROBE does not exist")
        return {
            "built_from_selected_checkout": False,
            "selected_binary_sha256": app.sha(app.PROBE.read_bytes()),
        }
    target_dir = ROOT / ".build/native-app-readiness/stage0-target"
    completed = subprocess.run(
        [
            "cargo", "build", "--offline", "--release",
            "--manifest-path", "tools/stage0-probe/Cargo.toml",
            "--target-dir", str(target_dir),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            "current Stage-0 compiler probe build failed: "
            + completed.stderr[-5000:]
        )
    built_probe = target_dir / "release/mncs-compiler-stage0-probe"
    if not built_probe.is_file():
        raise RuntimeError("current Stage-0 build did not produce its probe executable")
    app.PROBE.parent.mkdir(parents=True, exist_ok=True)
    if built_probe.resolve() != app.PROBE.resolve():
        with tempfile.NamedTemporaryFile(
            prefix=f".{app.PROBE.name}.", dir=app.PROBE.parent, delete=False
        ) as temporary_file:
            temporary_probe = Path(temporary_file.name)
        try:
            shutil.copy2(built_probe, temporary_probe)
            os.replace(temporary_probe, app.PROBE)
        finally:
            temporary_probe.unlink(missing_ok=True)
    return {
        "built_from_selected_checkout": True,
        "target_directory": target_dir.relative_to(ROOT).as_posix(),
        "binary_sha256": app.sha(app.PROBE.read_bytes()),
    }


def invoke(
    args: list[str],
    *,
    output_dir: Path,
    cache_dir: Path,
    input_dir: Path | None = None,
    input_manifest: dict | None = None,
) -> tuple[dict, dict, float, dict]:
    environment = os.environ.copy()
    environment["MNCS_COMPILER_PROBE"] = str(app.PROBE)
    environment["MNCS_ENV_SESSION_ARTIFACT_DIR"] = str(output_dir)
    environment.pop("MNCS_ENV_INPUT_ARTIFACT_DIR", None)
    environment.pop("MNCS_ENV_INPUT_ARTIFACTS_JSON", None)
    if input_dir is not None and input_manifest is not None:
        environment["MNCS_ENV_INPUT_ARTIFACT_DIR"] = str(input_dir)
        environment["MNCS_ENV_INPUT_ARTIFACTS_JSON"] = json.dumps(
            input_manifest, sort_keys=True, separators=(",", ":")
        )
    started = time.monotonic()
    completed = subprocess.Popen(
        [sys.executable, str(PROVIDER), "build", *args],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=(os.name == "posix"),
    )
    samples = []
    while completed.poll() is None:
        if time.monotonic() - started > 180:
            if os.name == "posix":
                try:
                    os.killpg(completed.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    stdout, stderr = completed.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(completed.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stdout, stderr = completed.communicate()
            else:
                completed.kill()
                stdout, stderr = completed.communicate()
            raise TimeoutError(
                f"provider build exceeded 180 seconds: {stderr[-2000:]}"
            )
        samples.append(sample_process_tree(completed.pid))
        time.sleep(0.01)
    stdout, stderr = completed.communicate(timeout=5)
    elapsed = time.monotonic() - started
    metrics = summarize_process_tree(samples, elapsed)
    if completed.returncode != 0:
        raise AssertionError(
            json.dumps(
                {
                    "returncode": completed.returncode,
                    "stdout": stdout[-1000:],
                    "stderr": stderr[-2000:],
                },
                indent=2,
            )
        )
    summary = json.loads(stdout)
    if summary.get("schema_version") != INVOCATION_SCHEMA:
        raise AssertionError("Environment invocation summary schema is missing")
    result_path = output_dir / "native-application-build.json"
    response = json.loads(result_path.read_text())
    if response.get("schema_version") not in {
        RESPONSE_SCHEMA,
        MULTI_RESPONSE_SCHEMA,
        INVENTORY_RESPONSE_SCHEMA,
        TEST_SELECTION_RESPONSE_SCHEMA,
        DECLARATION_FACT_RESPONSE_SCHEMA,
        SOURCE_PARSE_CACHE_RESPONSE_SCHEMA,
        FINITE_TYPE_FACT_RESPONSE_SCHEMA,
        RECORD_TYPE_FACT_RESPONSE_SCHEMA,
        SSA_FUNCTION_FACT_RESPONSE_SCHEMA,
    }:
        raise AssertionError("stored application build response schema is unsupported")
    if summary.get("response_schema") != response.get("schema_version"):
        raise AssertionError("provider summary changed the application response schema")
    return summary, response, elapsed, metrics


def main() -> None:
    validate_environment_contract_descriptor()
    large_summary_response = {
        "schema_version": MULTI_RESPONSE_SCHEMA,
        "status": "verified",
        "source_snapshot": {"identity": "sha256:" + "a" * 64},
        "dependency_graph": {"identity": "sha256:" + "b" * 64},
        "artifact": {"identity": "sha256:" + "c" * 64},
        "callable_scope": {
            "callable_roots": [
                {
                    "module": "m" * 96 + str(index),
                    "function": "f" * 96 + str(index),
                    "status": "verified",
                    "interface": {"identity": "sha256:" + "d" * 64},
                }
                for index in range(256)
            ]
        },
    }
    bounded_summary = provider.summary(large_summary_response, "result.json")
    bounded_summary_bytes = provider.json_bytes(bounded_summary) + b"\n"
    if (
        len(bounded_summary_bytes) > provider.MAX_SUMMARY_BYTES
        or bounded_summary.get("callable_root_count") != 256
        or bounded_summary.get("callable_roots_in_result_artifact") is not True
    ):
        raise AssertionError("large callable selection exceeded the bounded summary contract")

    probe_build = ensure_current_probe()
    if not app.PROBE.is_file():
        raise SystemExit("campaign Stage-0 compiler probe is missing")
    modules = {name: path.read_text() for name, path in app.MODULE_FILES.items()}
    request = request_for(app.APP_SOURCE, modules)
    bad_request = request_for(
        app.APP_SOURCE.replace("summary.total", "summary.total_missing", 1), modules
    )
    with tempfile.TemporaryDirectory(
        prefix="provider-contract-", dir=ROOT / ".build/native-app-readiness"
    ) as temporary:
        base = Path(temporary)
        request_path = base / "request.json"
        request_bytes = write_json(request_path, request)
        bad_path = base / "invalid-request.json"
        write_json(bad_path, bad_request)
        wrong_schema_path = base / "wrong-schema-request.json"
        wrong_schema_request = dict(request)
        wrong_schema_request["schema_version"] = "mncs.compiler.application-build-request/999"
        write_json(wrong_schema_path, wrong_schema_request)
        cache_dir = base / "cache"
        cache_arg = cache_dir.relative_to(ROOT).as_posix()

        cold_dir = base / "cold-output"
        cold_dir.mkdir()
        cold, cold_response, cold_elapsed, cold_metrics = invoke(
            [
                "--request-file", request_path.relative_to(ROOT).as_posix(),
                "--cache-dir", cache_arg,
            ],
            output_dir=cold_dir,
            cache_dir=cache_dir,
        )
        cold_work = verification_work(
            cold_response, label="cold application build", frontend_invocations=1
        )
        invocation_summary_bytes = (
            json.dumps(cold, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")).encode("utf-8") + b"\n"
        )
        if len(invocation_summary_bytes) > 8192:
            raise AssertionError(
                "provider stdout summary exceeds the Environment's per-stream capture bound"
            )
        summary_interface = cold.get("entry", {}).get("interface", {})
        if not isinstance(summary_interface, dict) or "types" in summary_interface:
            raise AssertionError(
                "provider stdout must keep the full typed closure in its shareable artifact"
            )
        if summary_interface.get("identity") != cold_response.get("entry", {}).get(
            "interface", {}
        ).get("identity"):
            raise AssertionError("provider stdout omitted the exact callable interface identity")
        artifact = cold_response.get("artifact", {})
        if cold_response.get("entry", {}).get("status") != "verified":
            raise AssertionError("native application entry is not verified")
        required_contract_fields = {
            "source_snapshot": {"identity", "files"},
            "dependency_graph": {"identity", "details"},
            "producer": {"stage0_toolchain", "probe", "artifact_backend"},
            "build_assumptions": {
                "backend", "target", "capabilities", "effect_assumptions",
                "optimization_policy", "verification_policy",
            },
            "artifact": {
                "schema_version", "identity", "vm_contract", "target",
                "interface_identity", "required_capabilities",
                "unresolved_obligations", "value",
            },
            "capability_dispatch": {
                "schema_version", "status", "bindings", "unresolved_obligations",
            },
            "entry": {"module", "function", "status", "interface"},
            "execution_target_compatibility": {
                "target", "status", "compatible", "execution_status",
            },
            "resource_intent": {
                "compile_cpu_ms", "compile_peak_memory_bytes",
                "execution_step_budget", "uncertainty",
            },
        }
        for field, children in required_contract_fields.items():
            value = cold_response.get(field)
            if not isinstance(value, dict) or not children.issubset(value):
                raise AssertionError(f"application response field {field} is incomplete")
        if not isinstance(cold_response.get("diagnostics"), list):
            raise AssertionError("application response diagnostics are not structured")
        if not isinstance(cold_response.get("unresolved_obligations"), list):
            raise AssertionError("application response obligations are not structured")
        if artifact.get("identity") != artifact.get("value", {}).get("artifact_id"):
            raise AssertionError("contract artifact identity does not match VM artifact")
        if artifact.get("schema_version") != "mncs.vm.artifact/1":
            raise AssertionError("application response does not expose canonical VM artifact schema")
        capability_dispatch = cold_response["capability_dispatch"]
        if (
            capability_dispatch.get("schema_version") != "mncs.compiler.capability-dispatch/1"
            or capability_dispatch.get("status") != "verified"
            or not isinstance(capability_dispatch.get("bindings"), list)
            or capability_dispatch.get("unresolved_obligations") != []
        ):
            raise AssertionError("application response capability dispatch contract is unresolved")
        if not isinstance(artifact.get("interface_identity"), str) or not artifact["interface_identity"]:
            raise AssertionError("application response has no callable interface identity")
        callable_interface = cold_response["entry"]["interface"]
        if (
            not isinstance(callable_interface, dict)
            or callable_interface.get("schema_version") != "mncs.compiler.callable-interface/1"
            or callable_interface.get("status") != "verified"
            or not isinstance(callable_interface.get("identity"), str)
            or not isinstance(callable_interface.get("signature"), dict)
            or not isinstance(callable_interface.get("types"), dict)
            or callable_interface["types"].get("status") != "verified"
            or callable_interface["types"].get("unresolved_references") != []
        ):
            raise AssertionError("application response has no versioned typed callable interface")
        request_types = [
            row for row in callable_interface["types"].get("records", [])
            if row.get("name") == "ReadinessRequest"
        ]
        if len(request_types) != 1 or not request_types[0].get("identity"):
            raise AssertionError("callable interface does not expose the structured input identity")
        if cold.get("status") != "stored" or cold.get("build_status") != cold_response.get("status"):
            raise AssertionError("Environment summary does not distinguish storage from build status")
        if cold.get("source_snapshot_identity") != cold_response["source_snapshot"].get("identity"):
            raise AssertionError("Environment summary changed source snapshot identity")
        if cold.get("dependency_graph_identity") != cold_response["dependency_graph"].get("identity"):
            raise AssertionError("Environment summary changed dependency identity")
        if cold.get("artifact_identity") != artifact.get("identity"):
            raise AssertionError("Environment summary changed artifact identity")
        summary_dispatch = cold.get("capability_dispatch", {})
        if (
            summary_dispatch.get("status") != capability_dispatch.get("status")
            or summary_dispatch.get("binding_count") != len(capability_dispatch["bindings"])
            or summary_dispatch.get("unresolved_obligation_count")
            != len(capability_dispatch["unresolved_obligations"])
        ):
            raise AssertionError("Environment summary changed compiler capability dispatch facts")
        if cold_response.get("cache", {}).get("status") != "miss":
            raise AssertionError("first provider build did not compile the request")

        warm_dir = base / "warm-output"
        warm_dir.mkdir()
        warm, warm_response, warm_elapsed, warm_metrics = invoke(
            [
                "--request-file", request_path.relative_to(ROOT).as_posix(),
                "--cache-dir", cache_arg,
            ],
            output_dir=warm_dir,
            cache_dir=cache_dir,
        )
        if warm_response.get("cache", {}).get("status") != "hit":
            raise AssertionError("fresh provider process did not reuse the exact cached build")
        warm_work = verification_work(
            warm_response, label="warm exact-snapshot build", frontend_invocations=0
        )
        if warm_response.get("artifact", {}).get("identity") != artifact.get("identity"):
            raise AssertionError("warm provider build changed the canonical artifact identity")

        parse_request = dict(request)
        parse_request["schema_version"] = SOURCE_PARSE_CACHE_REQUEST_SCHEMA
        parse_request.pop("entry", None)
        parse_request["callable_roots"] = [{"module": app.APP_MODULE, "function": "run"}]
        parse_request["test_selection"] = {"kind": "all_source_cases"}
        parse_request_path = base / "source-parse-request.json"
        write_json(parse_request_path, parse_request)
        parse_cache_dir = base / "source-parse-cache"
        parse_cache_arg = parse_cache_dir.relative_to(ROOT).as_posix()
        parse_cold_dir = base / "source-parse-cold-output"
        parse_cold_dir.mkdir()
        parse_cold, parse_cold_response, parse_cold_elapsed, parse_cold_metrics = invoke(
            ["--request-file", parse_request_path.relative_to(ROOT).as_posix(),
             "--cache-dir", parse_cache_arg],
            output_dir=parse_cold_dir,
            cache_dir=parse_cache_dir,
        )
        if parse_cold_response.get("schema_version") != SOURCE_PARSE_CACHE_RESPONSE_SCHEMA:
            raise AssertionError("provider did not route request /6 to response /9")
        parse_cold_work = parse_cold_response.get("cache", {}).get("verification_work", {})
        parse_cold_observation = parse_cold_work.get("source_parse_cache", {}).get("observation", {})
        if (parse_cold_work.get("schema_version") != "mncs.compiler.application-build-verification-work/4"
                or parse_cold_observation.get("schema_version") != "mncs.language-source-parse-cache-observation/2"
                or not parse_cold_observation.get("parsed_source_identities")):
            raise AssertionError("provider /9 response omitted Language parse execution evidence")
        parse_cold_reused_facts = parse_cold_work.get("reused_semantic_facts", [])
        parse_cold_recomputed_facts = parse_cold_work.get("recomputed_semantic_facts", [])
        if (not isinstance(parse_cold_reused_facts, list)
                or not isinstance(parse_cold_recomputed_facts, list)
                or not parse_cold_recomputed_facts
                or parse_cold_reused_facts):
            raise AssertionError(
                "real mncs-test cold build did not report its Language body-verification units"
            )

        finite_type_request = dict(parse_request)
        finite_type_request["schema_version"] = FINITE_TYPE_FACT_REQUEST_SCHEMA
        finite_type_request_path = base / "finite-type-fact-request.json"
        write_json(finite_type_request_path, finite_type_request)
        finite_type_cache_dir = base / "finite-type-fact-cache"
        finite_type_cache_arg = finite_type_cache_dir.relative_to(ROOT).as_posix()
        finite_type_cold_dir = base / "finite-type-fact-cold-output"
        finite_type_cold_dir.mkdir()
        finite_type_cold, finite_type_cold_response, finite_type_cold_elapsed, finite_type_cold_metrics = invoke(
            ["--request-file", finite_type_request_path.relative_to(ROOT).as_posix(),
             "--cache-dir", finite_type_cache_arg],
            output_dir=finite_type_cold_dir,
            cache_dir=finite_type_cache_dir,
        )
        finite_type_work = finite_type_cold_response.get("cache", {}).get(
            "verification_work", {}
        )
        if (finite_type_cold_response.get("schema_version") != FINITE_TYPE_FACT_RESPONSE_SCHEMA
                or finite_type_cold_response.get("status") != "verified"
                or finite_type_work.get("schema_version")
                != "mncs.compiler.application-build-verification-work/5"
                or finite_type_work.get("front_end_invocations") != 1
                or not finite_type_work.get("recomputed_finite_type_declaration_facts")
                or finite_type_work.get("reused_finite_type_declaration_facts")
                or finite_type_work.get("finite_type_declaration_verification") != "recomputed"):
            raise AssertionError(
                "Environment provider did not route /7 through verified finite-type Language work: "
                + json.dumps(finite_type_work, sort_keys=True)[:4000]
            )
        finite_type_key = finite_type_cold_response.get("cache", {}).get("key", "")
        finite_type_entry_path = (
            finite_type_cache_dir / "application-builds"
            / f"{finite_type_key.removeprefix('sha256:')}.json"
        )
        finite_type_entry = json.loads(finite_type_entry_path.read_text(encoding="utf-8"))
        finite_type_cached_facts = finite_type_entry.get(
            "finite_type_declaration_verification_facts"
        )
        if (finite_type_entry.get("schema_version")
                != "mncs.compiler.application-build-cache-entry/7"
                or not isinstance(finite_type_cached_facts, list)
                or not finite_type_cached_facts
                or not finite_type_entry.get(
                    "finite_type_declaration_verification_facts_sha256"
                )):
            raise AssertionError("Environment provider did not publish separately bound finite-type facts")

        finite_type_warm_dir = base / "finite-type-fact-warm-output"
        finite_type_warm_dir.mkdir()
        finite_type_warm, finite_type_warm_response, finite_type_warm_elapsed, finite_type_warm_metrics = invoke(
            ["--request-file", finite_type_request_path.relative_to(ROOT).as_posix(),
             "--cache-dir", finite_type_cache_arg],
            output_dir=finite_type_warm_dir,
            cache_dir=finite_type_cache_dir,
        )
        finite_type_warm_work = finite_type_warm_response.get("cache", {}).get(
            "verification_work", {}
        )
        if (finite_type_warm_response.get("schema_version") != FINITE_TYPE_FACT_RESPONSE_SCHEMA
                or finite_type_warm_response.get("cache", {}).get("status") != "hit"
                or finite_type_warm_work.get("front_end_invocations") != 0
                or finite_type_warm_work.get("reused_finite_type_declaration_facts")
                or finite_type_warm_work.get("finite_type_declaration_verification") != "not_run"):
            raise AssertionError("Environment exact cache hit claimed Language finite-type verification")

        record_type_request = dict(parse_request)
        record_type_request["schema_version"] = RECORD_TYPE_FACT_REQUEST_SCHEMA
        record_type_request_path = base / "record-type-fact-request.json"
        write_json(record_type_request_path, record_type_request)
        record_type_cache_dir = base / "record-type-fact-cache"
        record_type_cache_arg = record_type_cache_dir.relative_to(ROOT).as_posix()
        record_type_cold_dir = base / "record-type-fact-cold-output"
        record_type_cold_dir.mkdir()
        record_type_cold, record_type_cold_response, record_type_cold_elapsed, record_type_cold_metrics = invoke(
            ["--request-file", record_type_request_path.relative_to(ROOT).as_posix(),
             "--cache-dir", record_type_cache_arg],
            output_dir=record_type_cold_dir,
            cache_dir=record_type_cache_dir,
        )
        record_type_work = record_type_cold_response.get("cache", {}).get(
            "verification_work", {}
        )
        if (record_type_cold_response.get("schema_version") != RECORD_TYPE_FACT_RESPONSE_SCHEMA
                or record_type_cold_response.get("status") != "verified"
                or record_type_work.get("schema_version")
                != "mncs.compiler.application-build-verification-work/6"
                or record_type_work.get("front_end_invocations") != 1
                or not record_type_work.get("recomputed_record_type_declaration_facts")
                or record_type_work.get("reused_record_type_declaration_facts")
                or record_type_work.get("record_type_declaration_verification") != "recomputed"):
            raise AssertionError(
                "Environment provider did not route /8 through verified record-type Language work: "
                + json.dumps(record_type_work, sort_keys=True)[:4000]
            )
        record_type_key = record_type_cold_response.get("cache", {}).get("key", "")
        record_type_entry_path = (
            record_type_cache_dir / "application-builds"
            / f"{record_type_key.removeprefix('sha256:')}.json"
        )
        record_type_entry = json.loads(record_type_entry_path.read_text(encoding="utf-8"))
        record_type_cached_facts = record_type_entry.get(
            "record_type_declaration_verification_facts"
        )
        if (record_type_entry.get("schema_version")
                != "mncs.compiler.application-build-cache-entry/8"
                or not isinstance(record_type_cached_facts, list)
                or not record_type_cached_facts
                or not record_type_entry.get(
                    "record_type_declaration_verification_facts_sha256"
                )):
            raise AssertionError("Environment provider did not publish separately bound record-type facts")

        record_type_warm_dir = base / "record-type-fact-warm-output"
        record_type_warm_dir.mkdir()
        record_type_warm, record_type_warm_response, record_type_warm_elapsed, record_type_warm_metrics = invoke(
            ["--request-file", record_type_request_path.relative_to(ROOT).as_posix(),
             "--cache-dir", record_type_cache_arg],
            output_dir=record_type_warm_dir,
            cache_dir=record_type_cache_dir,
        )
        record_type_warm_work = record_type_warm_response.get("cache", {}).get(
            "verification_work", {}
        )
        if (record_type_warm_response.get("schema_version") != RECORD_TYPE_FACT_RESPONSE_SCHEMA
                or record_type_warm_response.get("cache", {}).get("status") != "hit"
                or record_type_warm_work.get("front_end_invocations") != 0
                or record_type_warm_work.get("reused_record_type_declaration_facts")
                or record_type_warm_work.get("record_type_declaration_verification") != "not_run"):
            raise AssertionError("Environment exact cache hit claimed Language record-type verification")

        ssa_fact_request = dict(parse_request)
        ssa_fact_request["schema_version"] = SSA_FUNCTION_FACT_REQUEST_SCHEMA
        ssa_fact_request["generic_seeds"] = []
        ssa_fact_request_path = base / "ssa-function-fact-request.json"
        write_json(ssa_fact_request_path, ssa_fact_request)
        ssa_fact_cache_dir = base / "ssa-function-fact-cache"
        ssa_fact_cache_arg = ssa_fact_cache_dir.relative_to(ROOT).as_posix()
        ssa_fact_cold_dir = base / "ssa-function-fact-cold-output"
        ssa_fact_cold_dir.mkdir()
        ssa_fact_cold, ssa_fact_cold_response, ssa_fact_cold_elapsed, ssa_fact_cold_metrics = invoke(
            ["--request-file", ssa_fact_request_path.relative_to(ROOT).as_posix(),
             "--cache-dir", ssa_fact_cache_arg],
            output_dir=ssa_fact_cold_dir,
            cache_dir=ssa_fact_cache_dir,
        )
        ssa_fact_work = ssa_fact_cold_response.get("cache", {}).get(
            "verification_work", {}
        )
        if (ssa_fact_cold_response.get("schema_version") != SSA_FUNCTION_FACT_RESPONSE_SCHEMA
                or ssa_fact_cold_response.get("status") != "verified"
                or ssa_fact_work.get("schema_version")
                != "mncs.compiler.application-build-verification-work/7"
                or ssa_fact_work.get("front_end_invocations") != 1
                or not ssa_fact_work.get("recomputed_ssa_function_facts")
                or ssa_fact_work.get("reused_ssa_function_facts")
                or ssa_fact_work.get("ssa_function_verification") != "recomputed"):
            raise AssertionError(
                "Environment provider did not route /9 through Language SSA function verification: "
                + json.dumps(ssa_fact_work, sort_keys=True)[:4000]
            )
        ssa_fact_key = ssa_fact_cold_response.get("cache", {}).get("key", "")
        ssa_fact_entry_path = (
            ssa_fact_cache_dir / "application-builds"
            / f"{ssa_fact_key.removeprefix('sha256:')}.json"
        )
        ssa_fact_entry = json.loads(ssa_fact_entry_path.read_text(encoding="utf-8"))
        ssa_function_facts = ssa_fact_entry.get("ssa_function_verification_facts")
        if (ssa_fact_entry.get("schema_version")
                != "mncs.compiler.application-build-cache-entry/9"
                or not isinstance(ssa_function_facts, list)
                or not ssa_function_facts
                or not ssa_fact_entry.get("ssa_function_verification_facts_sha256")):
            raise AssertionError("Environment provider did not persist bound Language SSA facts")

        ssa_fact_warm_dir = base / "ssa-function-fact-warm-output"
        ssa_fact_warm_dir.mkdir()
        ssa_fact_warm, ssa_fact_warm_response, ssa_fact_warm_elapsed, ssa_fact_warm_metrics = invoke(
            ["--request-file", ssa_fact_request_path.relative_to(ROOT).as_posix(),
             "--cache-dir", ssa_fact_cache_arg],
            output_dir=ssa_fact_warm_dir,
            cache_dir=ssa_fact_cache_dir,
        )
        ssa_fact_warm_work = ssa_fact_warm_response.get("cache", {}).get(
            "verification_work", {}
        )
        if (ssa_fact_warm_response.get("schema_version") != SSA_FUNCTION_FACT_RESPONSE_SCHEMA
                or ssa_fact_warm_response.get("cache", {}).get("status") != "hit"
                or ssa_fact_warm_work.get("front_end_invocations") != 0
                or ssa_fact_warm_work.get("reused_ssa_function_facts")
                or ssa_fact_warm_work.get("ssa_function_verification") != "not_run"):
            raise AssertionError("Environment exact cache hit claimed Language SSA verification reuse")

        parse_comment_request = dict(parse_request)
        parse_comment_request["root_source"] = (
            "// source cache comment edit\n" + parse_request["root_source"]
        )
        parse_comment_path = base / "source-parse-comment-request.json"
        write_json(parse_comment_path, parse_comment_request)
        parse_comment_dir = base / "source-parse-comment-output"
        parse_comment_dir.mkdir()
        parse_comment, parse_comment_response, parse_comment_elapsed, parse_comment_metrics = invoke(
            ["--request-file", parse_comment_path.relative_to(ROOT).as_posix(),
             "--cache-dir", parse_cache_arg],
            output_dir=parse_comment_dir,
            cache_dir=parse_cache_dir,
        )
        parse_comment_work = parse_comment_response.get("cache", {}).get("verification_work", {})
        parse_comment_observation = parse_comment_work.get("source_parse_cache", {}).get("observation", {})
        if (parse_comment_response.get("schema_version") != SOURCE_PARSE_CACHE_RESPONSE_SCHEMA
                or not parse_comment_observation.get("reused_source_identities")
                or not parse_comment_observation.get("parsed_source_identities")):
            raise AssertionError("provider /9 fresh process did not reuse unchanged imported parses")
        parse_comment_reused_facts = parse_comment_work.get("reused_semantic_facts", [])
        parse_comment_recomputed_facts = parse_comment_work.get("recomputed_semantic_facts", [])
        if (not isinstance(parse_comment_reused_facts, list)
                or not isinstance(parse_comment_recomputed_facts, list)
                or not parse_comment_reused_facts
                or len(parse_comment_recomputed_facts) >= len(parse_cold_recomputed_facts)
                or parse_comment_work.get("function_body_verification")
                != "recomputed_with_language_fact_reuse"):
            raise AssertionError(
                "real mncs-test comment edit did not avoid Language function-body verification"
            )

        # Change one uncalled function body in the real imported Test modules.
        # The reordered equality is semantically equivalent, but its body
        # fingerprint changes, so Language must reverify that unit while
        # reusing independent bodies. The root readiness workload does not
        # call mncs.test.snapshot::same.
        function_edit_request = dict(parse_comment_request)
        function_edit_request["modules"] = dict(parse_comment_request["modules"])
        snapshot_source = function_edit_request["modules"]["mncs.test.snapshot"]
        old_snapshot_body = (
            "return left.length == right.length && left.checksum == right.checksum;"
        )
        new_snapshot_body = (
            "return left.checksum == right.checksum && left.length == right.length;"
        )
        if snapshot_source.count(old_snapshot_body) != 1:
            raise AssertionError("selected mncs-test snapshot source changed unexpectedly")
        function_edit_request["modules"]["mncs.test.snapshot"] = snapshot_source.replace(
            old_snapshot_body, new_snapshot_body, 1
        )
        function_edit_path = base / "source-parse-function-edit-request.json"
        write_json(function_edit_path, function_edit_request)
        function_edit_dir = base / "source-parse-function-edit-output"
        function_edit_dir.mkdir()
        function_edit, function_edit_response, function_edit_elapsed, function_edit_metrics = invoke(
            ["--request-file", function_edit_path.relative_to(ROOT).as_posix(),
             "--cache-dir", parse_cache_arg],
            output_dir=function_edit_dir,
            cache_dir=parse_cache_dir,
        )
        function_edit_work = function_edit_response.get("cache", {}).get(
            "verification_work", {}
        )
        function_edit_observation = function_edit_work.get(
            "source_parse_cache", {}
        ).get("observation", {})
        function_edit_reused_facts = function_edit_work.get("reused_semantic_facts", [])
        function_edit_recomputed_facts = function_edit_work.get(
            "recomputed_semantic_facts", []
        )
        edited_body_identity = "mncs:0.2:body:mncs.test.snapshot::same"
        if (function_edit_response.get("schema_version") != SOURCE_PARSE_CACHE_RESPONSE_SCHEMA
                or function_edit_response.get("status") != "verified"
                or len(function_edit_observation.get("parsed_source_identities", [])) != 1
                or len(function_edit_observation.get("reused_source_identities", [])) != 4
                or edited_body_identity not in function_edit_recomputed_facts
                or edited_body_identity in function_edit_reused_facts
                or not function_edit_reused_facts
                or len(function_edit_recomputed_facts) >= len(parse_cold_recomputed_facts)
                or function_edit_work.get("function_body_verification")
                != "recomputed_with_language_fact_reuse"):
            raise AssertionError(
                "real mncs-test uncalled body edit did not isolate Language revalidation: "
                + json.dumps(function_edit_work, sort_keys=True)[:5000]
            )
        if (function_edit_response.get("artifact", {}).get("identity")
                != parse_comment_response.get("artifact", {}).get("identity")):
            raise AssertionError(
                "an uncalled mncs-test body edit changed the selected executable artifact"
            )
        parse_cold_vm, _ = app.run_vm(
            parse_cold_response.get("artifact", {}).get("value"),
            app.call_request()["arguments"],
        )
        function_edit_vm, _ = app.run_vm(
            function_edit_response.get("artifact", {}).get("value"),
            app.call_request()["arguments"],
        )
        parse_cold_returned = parse_cold_vm.get("record", {}).get("returned")
        function_edit_returned = function_edit_vm.get("record", {}).get("returned")
        if not parse_cold_returned or parse_cold_returned != function_edit_returned:
            raise AssertionError(
                "canonical VM behavior changed after an uncalled mncs-test body edit"
            )

        # Rename an imported generic function's output field in the real Test
        # suite. The source call remains type-correct, but Language must
        # invalidate the callee interface, its specialization, and the actual
        # application caller while retaining independent imported facts.
        signature_edit_request = dict(function_edit_request)
        signature_edit_request["modules"] = dict(function_edit_request["modules"])
        suite_source = signature_edit_request["modules"]["mncs.test.suite"]
        old_summarize_signature = (
            "fn summarize<N: Nat>(tests: [TestResult; N]) -> (result: SuiteSummary)"
        )
        new_summarize_signature = (
            "fn summarize<N: Nat>(tests: [TestResult; N]) -> (updated: SuiteSummary)"
        )
        if suite_source.count(old_summarize_signature) != 1:
            raise AssertionError("selected mncs-test suite signature changed unexpectedly")
        signature_edit_request["modules"]["mncs.test.suite"] = suite_source.replace(
            old_summarize_signature, new_summarize_signature, 1
        )
        signature_edit_path = base / "source-parse-signature-edit-request.json"
        write_json(signature_edit_path, signature_edit_request)
        signature_edit_dir = base / "source-parse-signature-edit-output"
        signature_edit_dir.mkdir()
        signature_edit, signature_edit_response, signature_edit_elapsed, signature_edit_metrics = invoke(
            ["--request-file", signature_edit_path.relative_to(ROOT).as_posix(),
             "--cache-dir", parse_cache_arg],
            output_dir=signature_edit_dir,
            cache_dir=parse_cache_dir,
        )
        signature_edit_work = signature_edit_response.get("cache", {}).get(
            "verification_work", {}
        )
        signature_edit_parse = signature_edit_work.get("source_parse_cache", {}).get(
            "observation", {}
        )
        signature_reused_facts = signature_edit_work.get("reused_semantic_facts", [])
        signature_recomputed_facts = signature_edit_work.get(
            "recomputed_semantic_facts", []
        )
        signature_recomputed_declarations = signature_edit_work.get(
            "recomputed_declaration_facts", []
        )
        signature_reused_declarations = signature_edit_work.get(
            "reused_declaration_facts", []
        )
        summarize_fact = "mncs:0.2:body:mncs.test.suite::summarize"
        application_caller_fact = "mncs:0.2:body:tests.native_readiness::run"
        expected_signature_recomputed = {
            summarize_fact,
            application_caller_fact,
        }
        specialized_summarize = [
            subject for subject in signature_recomputed_facts
            if subject.startswith(summarize_fact + "__spec_")
        ]
        signature_reasons = {
            row["subject"]: row["reason"]
            for row in signature_edit_work.get(
                "semantic_fact_recomputation_reasons", []
            )
        }
        signature_declaration_reasons = {
            row["subject"]: row["reason"]
            for row in signature_edit_work.get(
                "declaration_fact_recomputation_reasons", []
            )
        }
        expected_signature_declarations = {
            "mncs:0.2:function:mncs.test.suite::summarize",
        }
        specialized_summarize_declaration = [
            subject for subject in signature_recomputed_declarations
            if subject.startswith(
                "mncs:0.2:function:mncs.test.suite::summarize__spec_"
            )
        ]
        if (signature_edit_response.get("schema_version") != SOURCE_PARSE_CACHE_RESPONSE_SCHEMA
                or signature_edit_response.get("status") != "verified"
                or signature_edit_response.get("cache", {}).get("status") != "miss"
                or signature_edit_work.get("schema_version")
                != "mncs.compiler.application-build-verification-work/4"
                or signature_edit_work.get("function_body_verification")
                != "recomputed_with_language_fact_reuse"
                or len(signature_edit_parse.get("parsed_source_identities", [])) != 1
                or len(signature_edit_parse.get("reused_source_identities", [])) != 4
                or summarize_fact not in signature_recomputed_facts
                or application_caller_fact not in signature_recomputed_facts
                or len(specialized_summarize) != 1
                or set(signature_recomputed_facts)
                != (expected_signature_recomputed | set(specialized_summarize))
                or signature_reasons.get(summarize_fact)
                != "declaration_interface_changed"
                or signature_reasons.get(specialized_summarize[0])
                != "declaration_interface_changed"
                or signature_reasons.get(application_caller_fact)
                != "dependencies_changed"
                or signature_edit_work.get(
                    "language_declaration_verification_fact_schema"
                ) != "mncs.language-declaration-verification-fact/1"
                or len(specialized_summarize_declaration) != 1
                or set(signature_recomputed_declarations)
                != (expected_signature_declarations
                    | set(specialized_summarize_declaration))
                or signature_declaration_reasons.get(
                    "mncs:0.2:function:mncs.test.suite::summarize"
                ) != "subject_changed"
                or signature_declaration_reasons.get(
                    specialized_summarize_declaration[0]
                ) != "subject_changed"
                or "mncs:0.2:function:tests.native_readiness::run"
                not in signature_reused_declarations
                or "mncs:0.2:body:mncs.test.snapshot::same"
                not in signature_reused_facts):
            raise AssertionError(
                "real mncs-test imported interface edit did not invalidate its exact Language dependency closure: "
                + json.dumps(signature_edit_work, sort_keys=True)[:5000]
            )
        if (signature_edit_response.get("artifact", {}).get("identity")
                == parse_comment_response.get("artifact", {}).get("identity")):
            raise AssertionError(
                "an imported callable-interface edit reused the old executable closure"
            )
        signature_oracle_probe = app.Probe(
            cache_dir=base / "signature-oracle-probe-cache"
        )
        try:
            _, signature_execution, signature_oracle_metrics = incremental.oracle_one(
                signature_oracle_probe,
                signature_edit_request["root_source"],
                signature_edit_request["modules"],
            )
        finally:
            signature_oracle_probe.close()
        signature_vm_document, signature_vm_metrics, signature_vm_returned = (
            incremental.run_and_compare(
                signature_edit_response["artifact"]["value"], signature_execution
            )
        )
        parse_cold_normalized = [
            app.decode_wire(app.vm_to_wire(row)) for row in parse_cold_returned
        ]
        if signature_vm_returned != parse_cold_normalized:
            raise AssertionError(
                "the imported interface-only edit changed the real mncs-test result"
            )

        batch_roots = batch_app.ROOTS
        batch_request = batch_app.build_request(batch_roots)
        batch_request_path = base / "multi-callable-request.json"
        write_json(batch_request_path, batch_request)
        batch_cache_dir = base / "multi-callable-cache"
        batch_cache_arg = batch_cache_dir.relative_to(ROOT).as_posix()
        batch_output_dir = base / "multi-callable-output"
        batch_output_dir.mkdir()
        batch_summary, batch_response, batch_elapsed, batch_metrics = invoke(
            [
                "--request-file", batch_request_path.relative_to(ROOT).as_posix(),
                "--cache-dir", batch_cache_arg,
            ],
            output_dir=batch_output_dir,
            cache_dir=batch_cache_dir,
        )
        batch_work = verification_work(
            batch_response, label="multi-callable application build",
            frontend_invocations=1,
        )
        batch_callable_roots = batch_response.get("callable_scope", {}).get(
            "callable_roots", []
        )
        batch_expected = [
            {"module": batch_app.MODULE, "function": function, "status": "verified"}
            for function in sorted(row["function"] for row in batch_roots)
        ]
        batch_summary_roots = batch_summary.get("callable_roots")
        if (
            batch_response.get("schema_version") != MULTI_RESPONSE_SCHEMA
            or batch_response.get("status") != "verified"
            or [
                {key: row.get(key) for key in ("module", "function", "status")}
                for row in batch_callable_roots
            ] != batch_expected
            or not all(
                isinstance(row.get("interface", {}).get("identity"), str)
                and row["interface"]["identity"]
                for row in batch_callable_roots
            )
            or len(batch_summary_roots or []) != len(batch_expected)
            or [
                {key: row.get(key) for key in ("module", "function", "status")}
                for row in batch_summary_roots or []
            ] != batch_expected
            or any(not row.get("interface_identity") for row in batch_summary_roots or [])
        ):
            raise AssertionError(
                "provider did not preserve the /2-to-/5 selected callable identities"
            )
        batch_artifact = batch_response.get("artifact", {}).get("value")
        batch_artifact_callables = {
            (row.get("module"), row.get("name"))
            for row in (batch_artifact or {}).get("callables", [])
        }
        if not {
            (batch_app.MODULE, row["function"]) for row in batch_roots
        }.issubset(batch_artifact_callables):
            raise AssertionError(
                "provider multi-callable build emitted an unexpected callable set"
            )
        batch_vm_result, batch_vm_elapsed, batch_vm_peak_rss_kib = batch_app.vm_batch(
            batch_artifact,
            [{"id": "snapshot-witness", "function": "snapshot_witness"}],
        )
        batch_vm_rows = batch_vm_result.get("results", [])
        if (len(batch_vm_rows) != 1
                or batch_vm_rows[0].get("outcome") != {"kind": "completed"}
                or len(batch_vm_rows[0].get("record", {}).get("returned", [])) != 1):
            raise AssertionError(
                "provider-produced multi-callable artifact did not pass canonical VM calls"
            )

        digest_modules = {
            name: path.read_text(encoding="utf-8")
            for name, path in digest_app.MODULE_PATHS.items()
        }
        digest_request = digest_app.compile_request(
            digest_app.ROOT_SOURCE_PATH.read_text(encoding="utf-8"), digest_modules
        )
        digest_request_path = base / "digest-app-request.json"
        write_json(digest_request_path, digest_request)
        digest_cache_dir = base / "digest-cache"
        digest_cache_arg = digest_cache_dir.relative_to(ROOT).as_posix()
        digest_cold_dir = base / "digest-cold-output"
        digest_cold_dir.mkdir()
        digest_cold, digest_response, digest_cold_elapsed, digest_cold_metrics = invoke(
            [
                "--request-file", digest_request_path.relative_to(ROOT).as_posix(),
                "--cache-dir", digest_cache_arg,
            ],
            output_dir=digest_cold_dir,
            cache_dir=digest_cache_dir,
        )
        digest_cold_work = verification_work(
            digest_response, label="digest_app cold build", frontend_invocations=1
        )
        digest_artifact = digest_response.get("artifact", {})
        digest_dispatch = digest_response.get("capability_dispatch", {})
        digest_summary_dispatch = digest_cold.get("capability_dispatch", {})
        expected_capability = (
            "mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact"
        )
        digest_bindings = digest_dispatch.get("bindings", [])
        if (
            digest_response.get("status") != "verified"
            or digest_response.get("entry", {}).get("module") != "mncs.test.digest"
            or digest_response.get("entry", {}).get("function") != "digest_app"
            or digest_response.get("unresolved_obligations") != []
            or digest_response.get("diagnostics") != []
        ):
            raise AssertionError(
                "Environment-facing provider did not verify the real digest_app source: "
                + json.dumps(
                    {
                        "status": digest_response.get("status"),
                        "entry": digest_response.get("entry"),
                        "diagnostics": digest_response.get("diagnostics"),
                    },
                    sort_keys=True,
                )[:5000]
            )
        if (
            digest_cold.get("status") != "stored"
            or digest_cold.get("build_status") != digest_response.get("status")
            or digest_cold.get("source_snapshot_identity")
            != digest_response.get("source_snapshot", {}).get("identity")
            or digest_cold.get("dependency_graph_identity")
            != digest_response.get("dependency_graph", {}).get("identity")
            or digest_cold.get("artifact_identity") != digest_artifact.get("identity")
            or digest_summary_dispatch.get("status") != digest_dispatch.get("status")
            or digest_summary_dispatch.get("binding_count") != len(digest_bindings)
            or digest_summary_dispatch.get("unresolved_obligation_count") != 0
        ):
            raise AssertionError(
                "provider summary changed digest_app build or capability identities"
            )
        if (
            digest_dispatch.get("schema_version")
            != "mncs.compiler.capability-dispatch/1"
            or digest_dispatch.get("status") != "verified"
            or digest_dispatch.get("unresolved_obligations") != []
            or sorted(row.get("operation") for row in digest_bindings)
            != ["structured_read", "structured_write"]
            or any(
                row.get("capability_identity") != expected_capability
                or row.get("function_identity")
                != "mncs:0.2:function:mncs.test.digest::digest_app"
                or row.get("runtime_capability_key") != "test_artifact"
                or row.get("runtime_key_kind") != "source-alias"
                or not row.get("instruction_identity")
                or not row.get("source_operation_identity")
                for row in digest_bindings
            )
        ):
            raise AssertionError(
                "digest_app provider build lost its exact verified capability bindings: "
                + json.dumps(digest_dispatch, sort_keys=True)[:5000]
            )
        if (
            digest_artifact.get("identity")
            != digest_artifact.get("value", {}).get("artifact_id")
            or digest_artifact.get("required_capabilities") != [expected_capability]
            or digest_artifact.get("value", {}).get("requirements", {}).get(
                "capabilities"
            )
            != [expected_capability]
        ):
            raise AssertionError(
                "digest_app VM artifact does not preserve its verified capability identity"
            )

        digest_warm_dir = base / "digest-warm-output"
        digest_warm_dir.mkdir()
        digest_warm, digest_warm_response, digest_warm_elapsed, digest_warm_metrics = invoke(
            [
                "--request-file", digest_request_path.relative_to(ROOT).as_posix(),
                "--cache-dir", digest_cache_arg,
            ],
            output_dir=digest_warm_dir,
            cache_dir=digest_cache_dir,
        )
        digest_warm_work = verification_work(
            digest_warm_response, label="digest_app warm exact-snapshot build",
            frontend_invocations=0,
        )
        if (
            digest_response.get("cache", {}).get("status") != "miss"
            or digest_warm_response.get("cache", {}).get("status") != "hit"
            or digest_warm_response.get("artifact", {}).get("identity")
            != digest_artifact.get("identity")
            or digest_warm_response.get("capability_dispatch") != digest_dispatch
            or digest_warm.get("artifact_identity") != digest_artifact.get("identity")
            or digest_warm.get("source_snapshot_identity")
            != digest_response.get("source_snapshot", {}).get("identity")
        ):
            raise AssertionError(
                "fresh provider process did not preserve digest_app artifact and dispatch identities"
            )
        for label, metrics in (
            ("native application cold build", cold_metrics),
            ("digest_app cold build", digest_cold_metrics),
        ):
            if (
                metrics.get("cpu_seconds") is None
                or metrics["cpu_seconds"] <= 0
                or metrics.get("peak_rss_bytes") is None
                or metrics["peak_rss_bytes"] <= 0
                or metrics.get("peak_process_count", 0) < 1
            ):
                raise AssertionError(f"{label} process-tree resource metrics are incomplete")

        store_input_dir = base / "store-input"
        store_input_dir.mkdir()
        store_input_path = store_input_dir / "0000.artifact"
        store_input_path.write_bytes(request_bytes)
        input_manifest = {
            "schema_version": "mncs.environment.provider-input-artifacts/1",
            "session_id": "ses_provider_contract_test",
            "artifacts": [{
                "name": "request",
                "local_path": store_input_path.name,
                "byte_count": len(request_bytes),
                "sha256": hashlib.sha256(request_bytes).hexdigest(),
                "store_reference": "mncs-store:ses_provider_contract_test:artifact:request",
            }],
        }
        store_output_dir = base / "store-input-output"
        store_output_dir.mkdir()
        store_input, store_response, store_elapsed, store_metrics = invoke(
            ["--request-artifact", "request", "--cache-dir", cache_arg],
            output_dir=store_output_dir,
            cache_dir=cache_dir,
            input_dir=store_input_dir,
            input_manifest=input_manifest,
        )
        store_input_work = verification_work(
            store_response, label="Store-input exact-snapshot build",
            frontend_invocations=0,
        )
        if store_response.get("cache", {}).get("status") != "hit":
            raise AssertionError("named Store input did not resolve to the exact cached build")
        if store_response.get("artifact", {}).get("identity") != artifact.get("identity"):
            raise AssertionError("Store-supplied request changed the application artifact")

        invalid_dir = base / "invalid-output"
        invalid_dir.mkdir()
        invalid, invalid_response, invalid_elapsed, invalid_metrics = invoke(
            [
                "--request-file", bad_path.relative_to(ROOT).as_posix(),
                "--cache-dir", cache_arg,
            ],
            output_dir=invalid_dir,
            cache_dir=cache_dir,
        )
        invalid_work = verification_work(
            invalid_response, label="invalid-source build", frontend_invocations=1
        )
        diagnostic_codes = [
            row.get("code") for row in invalid_response.get("diagnostics", [])
        ]
        if invalid_response.get("status") != "rejected" or "MNE162" not in diagnostic_codes:
            raise AssertionError("invalid source did not retain its precise MNE162 result")
        if invalid_response.get("cache", {}).get("publication") != "not_published":
            raise AssertionError("rejected source was published as a successful build")

        corrupt_input_dir = base / "corrupt-input"
        corrupt_input_dir.mkdir()
        (corrupt_input_dir / "0000.artifact").write_bytes(request_bytes)
        corrupt_manifest = json.loads(json.dumps(input_manifest))
        corrupt_manifest["artifacts"][0]["sha256"] = "0" * 64
        corrupt_output_dir = base / "corrupt-output"
        corrupt_output_dir.mkdir()
        (corrupt_output_dir / "native-application-build.json").write_bytes(
            (cold_dir / "native-application-build.json").read_bytes()
        )
        corrupt_environment = os.environ.copy()
        corrupt_environment["MNCS_COMPILER_PROBE"] = str(app.PROBE)
        corrupt_environment["MNCS_ENV_SESSION_ARTIFACT_DIR"] = str(corrupt_output_dir)
        corrupt_environment["MNCS_ENV_INPUT_ARTIFACT_DIR"] = str(corrupt_input_dir)
        corrupt_environment["MNCS_ENV_INPUT_ARTIFACTS_JSON"] = json.dumps(
            corrupt_manifest, sort_keys=True, separators=(",", ":")
        )
        corrupt = subprocess.run(
            [
                sys.executable, str(PROVIDER), "build",
                "--request-artifact", "request", "--cache-dir", cache_arg,
            ],
            cwd=ROOT,
            env=corrupt_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if corrupt.returncode == 0 or list(corrupt_output_dir.iterdir()):
            raise AssertionError(
                "corrupt Store input was accepted or a prior successful result survived"
            )

        schema_output_dir = base / "wrong-schema-output"
        schema_output_dir.mkdir()
        schema_environment = os.environ.copy()
        schema_environment["MNCS_COMPILER_PROBE"] = str(app.PROBE)
        schema_environment["MNCS_ENV_SESSION_ARTIFACT_DIR"] = str(schema_output_dir)
        schema_check = subprocess.run(
            [sys.executable, str(PROVIDER), "build", "--request-file",
             wrong_schema_path.relative_to(ROOT).as_posix(), "--cache-dir", cache_arg],
            cwd=ROOT,
            env=schema_environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if schema_check.returncode == 0 or list(schema_output_dir.iterdir()):
            raise AssertionError("unsupported request schema was accepted or published")

        response_bytes = (cold_dir / "native-application-build.json").read_bytes()
        digest_response_bytes = (
            digest_cold_dir / "native-application-build.json"
        ).read_bytes()
        digest_metrics = digest_response.get("metrics")
        if not isinstance(digest_metrics, dict):
            digest_metrics = None
        result = {
            "schema_version": 1,
            "kind": "mncs-native-application-build-provider-test",
            "status": "pass",
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "request_schema": REQUEST_SCHEMA,
            "response_schema": RESPONSE_SCHEMA,
            "producer": {
                "compiler_revision": subprocess.check_output(
                    ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
                ).strip(),
                "provider_test_sha256": hashlib.sha256(
                    Path(__file__).read_bytes()
                ).hexdigest(),
                "mncs_test_revision": digest_app.git(
                    digest_app.TEST_ROOT, "rev-parse", "HEAD"
                ),
                "mncs_stdlib_revision": digest_app.git(
                    digest_app.STDLIB_ROOT, "rev-parse", "HEAD"
                ),
                "probe_sha256": app.sha(app.PROBE.read_bytes()),
                "probe_build": probe_build,
                "stage0_revision": json.loads(
                    (ROOT / "mncs-language.lock.json").read_text()
                )["revision"],
            },
            "response_file": {
                "name": "native-application-build.json",
                "byte_count": len(response_bytes),
                "sha256": hashlib.sha256(response_bytes).hexdigest(),
            },
            "application": {
                "source_snapshot_identity": cold_response["source_snapshot"]["identity"],
                "dependency_graph_identity": cold_response["dependency_graph"]["identity"],
                "artifact_identity": artifact["identity"],
                "entry_status": cold_response["entry"]["status"],
                "overall_status": cold_response["status"],
            },
            "effectful_application": {
                "module": "mncs.test.digest",
                "function": "digest_app",
                "source_snapshot_identity": digest_response["source_snapshot"]["identity"],
                "dependency_graph_identity": digest_response["dependency_graph"]["identity"],
                "artifact_identity": digest_artifact["identity"],
                "capability_identity": expected_capability,
                "capability_operations": sorted(
                    row["operation"] for row in digest_bindings
                ),
                "response_file": {
                    "name": "native-application-build.json",
                    "byte_count": len(digest_response_bytes),
                    "sha256": hashlib.sha256(digest_response_bytes).hexdigest(),
                },
                "entry_status": digest_response["entry"]["status"],
                "overall_status": digest_response["status"],
                "compiler_metrics_available": digest_metrics is not None,
                "compiler_metrics": {
                    key: digest_metrics.get(key)
                    for key in (
                        "wall_seconds",
                        "cpu_seconds",
                        "peak_rss_bytes",
                        "peak_cgroup_memory_bytes",
                        "peak_child_processes",
                        "peak_threads",
                    )
                } if digest_metrics is not None else None,
            },
            "multi_callable_application": {
                "request_schema": MULTI_REQUEST_SCHEMA,
                "response_schema": batch_response["schema_version"],
                "source_snapshot_identity": batch_response.get(
                    "source_snapshot", {}
                ).get("identity"),
                "callable_roots": batch_summary_roots,
                "artifact_identity": batch_response.get("artifact", {}).get(
                    "identity"
                ),
                "artifact_callable_set": [
                    {"module": module, "function": function}
                    for module, function in sorted(batch_artifact_callables)
                ],
                "verification_work": batch_work,
                "provider_wall_seconds": round(batch_elapsed, 6),
                "provider_metrics": batch_metrics,
                "canonical_vm_wall_seconds": round(batch_vm_elapsed, 6),
                "canonical_vm_peak_rss_kib": batch_vm_peak_rss_kib,
                "canonical_vm_results": batch_vm_rows,
                "large_selection_summary": {
                    "root_count": bounded_summary["callable_root_count"],
                    "summary_bytes": len(bounded_summary_bytes),
                    "detail_in_result_artifact": bounded_summary[
                        "callable_roots_in_result_artifact"
                    ],
                },
            },
            "language_source_parse_cache": {
                "request_schema": SOURCE_PARSE_CACHE_REQUEST_SCHEMA,
                "response_schema": parse_cold_response.get("schema_version"),
                "cold": {
                    "cache_status": parse_cold_response.get("cache", {}).get("status"),
                    "source_parse_cache": parse_cold_response.get("cache", {}).get(
                        "verification_work", {}
                    ).get("source_parse_cache"),
                    "provider_wall_seconds": round(parse_cold_elapsed, 6),
                    "provider_metrics": parse_cold_metrics,
                },
                "comment_edit": {
                    "cache_status": parse_comment_response.get("cache", {}).get("status"),
                    "source_parse_cache": parse_comment_response.get("cache", {}).get(
                        "verification_work", {}
                    ).get("source_parse_cache"),
                    "verification_work": parse_comment_work,
                    "provider_wall_seconds": round(parse_comment_elapsed, 6),
                    "provider_metrics": parse_comment_metrics,
                },
                "uncalled_body_edit": {
                    "cache_status": function_edit_response.get("cache", {}).get("status"),
                    "source_parse_cache": function_edit_work.get("source_parse_cache"),
                    "verification_work": function_edit_work,
                    "edited_body_identity": edited_body_identity,
                    "provider_wall_seconds": round(function_edit_elapsed, 6),
                    "provider_metrics": function_edit_metrics,
                    "canonical_vm_returned": function_edit_returned,
                },
                "imported_signature_edit": {
                    "cache_status": signature_edit_response.get("cache", {}).get("status"),
                    "changed_module": "mncs.test.suite",
                    "changed_function": "mncs.test.suite::summarize",
                    "source_parse_cache": signature_edit_work.get("source_parse_cache"),
                    "verification_work": signature_edit_work,
                    "recomputed_semantic_facts": signature_recomputed_facts,
                    "recomputation_reasons": signature_reasons,
                    "reused_semantic_fact_count": len(signature_reused_facts),
                    "recomputed_declaration_facts": signature_recomputed_declarations,
                    "declaration_recomputation_reasons": signature_declaration_reasons,
                    "reused_declaration_fact_count": len(signature_reused_declarations),
                    "caller_fact": application_caller_fact,
                    "specialization_facts": specialized_summarize,
                    "artifact_identity": signature_edit_response.get("artifact", {}).get(
                        "identity"
                    ),
                    "artifact_replaced": (
                        signature_edit_response.get("artifact", {}).get("identity")
                        != parse_comment_response.get("artifact", {}).get("identity")
                    ),
                    "provider_wall_seconds": round(signature_edit_elapsed, 6),
                    "provider_metrics": signature_edit_metrics,
                    "stage0_wall_seconds": signature_oracle_metrics.get("wall_seconds"),
                    "stage0_metrics": signature_oracle_metrics,
                    "canonical_vm_wall_seconds": signature_vm_metrics.get("wall_seconds"),
                    "canonical_vm_peak_rss_bytes": signature_vm_metrics.get(
                        "peak_rss_bytes"
                    ),
                    "canonical_vm_artifact_id": signature_vm_document.get(
                        "record", {}
                    ).get("artifact_id"),
                    "canonical_vm_metrics": signature_vm_metrics,
                    "stage0_canonical_vm_match": True,
                    "unchanged_application_result": True,
                },
                "provider_summary_status": parse_cold.get("status"),
            },
            "language_finite_type_declaration_fact_reuse": {
                "request_schema": FINITE_TYPE_FACT_REQUEST_SCHEMA,
                "response_schema": finite_type_cold_response.get("schema_version"),
                "cache_entry_schema": finite_type_entry.get("schema_version"),
                "cold": {
                    "provider_status": finite_type_cold.get("status"),
                    "cache_status": finite_type_cold_response.get("cache", {}).get("status"),
                    "recomputed_finite_type_fact_count": len(
                        finite_type_work.get("recomputed_finite_type_declaration_facts", [])
                    ),
                    "stored_finite_type_fact_count": len(finite_type_cached_facts),
                    "artifact_identity": finite_type_cold_response.get("artifact", {}).get("identity"),
                    "provider_wall_seconds": round(finite_type_cold_elapsed, 6),
                    "provider_metrics": finite_type_cold_metrics,
                },
                "fresh_process_warm": {
                    "provider_status": finite_type_warm.get("status"),
                    "cache_status": finite_type_warm_response.get("cache", {}).get("status"),
                    "front_end_invocations": finite_type_warm_work.get("front_end_invocations"),
                    "finite_type_declaration_verification": finite_type_warm_work.get(
                        "finite_type_declaration_verification"
                    ),
                    "provider_wall_seconds": round(finite_type_warm_elapsed, 6),
                    "provider_metrics": finite_type_warm_metrics,
                },
            },
            "language_record_type_declaration_fact_reuse": {
                "request_schema": RECORD_TYPE_FACT_REQUEST_SCHEMA,
                "response_schema": record_type_cold_response.get("schema_version"),
                "cache_entry_schema": record_type_entry.get("schema_version"),
                "cold": {
                    "provider_status": record_type_cold.get("status"),
                    "cache_status": record_type_cold_response.get("cache", {}).get("status"),
                    "recomputed_record_type_fact_count": len(
                        record_type_work.get("recomputed_record_type_declaration_facts", [])
                    ),
                    "stored_record_type_fact_count": len(record_type_cached_facts),
                    "provider_wall_seconds": round(record_type_cold_elapsed, 6),
                    "provider_metrics": record_type_cold_metrics,
                },
                "fresh_process_warm": {
                    "provider_status": record_type_warm.get("status"),
                    "cache_status": record_type_warm_response.get("cache", {}).get("status"),
                    "front_end_invocations": record_type_warm_work.get("front_end_invocations"),
                    "record_type_declaration_verification": record_type_warm_work.get(
                        "record_type_declaration_verification"
                    ),
                    "provider_wall_seconds": round(record_type_warm_elapsed, 6),
                    "provider_metrics": record_type_warm_metrics,
                },
            },
            "language_ssa_function_verification_fact_reuse": {
                "request_schema": SSA_FUNCTION_FACT_REQUEST_SCHEMA,
                "response_schema": ssa_fact_cold_response.get("schema_version"),
                "cache_entry_schema": ssa_fact_entry.get("schema_version"),
                "cold": {
                    "provider_status": ssa_fact_cold.get("status"),
                    "cache_status": ssa_fact_cold_response.get("cache", {}).get("status"),
                    "recomputed_ssa_function_fact_count": len(
                        ssa_fact_work.get("recomputed_ssa_function_facts", [])
                    ),
                    "stored_ssa_function_fact_count": len(ssa_function_facts),
                    "provider_wall_seconds": round(ssa_fact_cold_elapsed, 6),
                    "provider_metrics": ssa_fact_cold_metrics,
                },
                "fresh_process_warm": {
                    "provider_status": ssa_fact_warm.get("status"),
                    "cache_status": ssa_fact_warm_response.get("cache", {}).get("status"),
                    "front_end_invocations": ssa_fact_warm_work.get("front_end_invocations"),
                    "ssa_function_verification": ssa_fact_warm_work.get(
                        "ssa_function_verification"
                    ),
                    "provider_wall_seconds": round(ssa_fact_warm_elapsed, 6),
                    "provider_metrics": ssa_fact_warm_metrics,
                },
            },
            "limitations": [
                "This invokes the local canonical VM but does not exercise Forge, the live Environment provider route, or canonical Store execution.",
                "CPU and peak RSS are sampled across the provider process tree; brief peaks between 10 ms samples may be missed, and cgroup memory was unavailable.",
                "A warm application artifact cache hit is not Language verified-fact reuse.",
            ],
            "checks": {
                "cold_cache_miss": cold_response["cache"]["status"] == "miss",
                "fresh_process_warm_hit": warm_response["cache"]["status"] == "hit",
                "digest_app_provider_verified": digest_response["status"] == "verified",
                "digest_app_capability_dispatch_verified": (
                    digest_dispatch.get("status") == "verified"
                ),
                "digest_app_fresh_process_warm_hit": (
                    digest_warm_response.get("cache", {}).get("status") == "hit"
                ),
                "provider_multi_callable_build_verified": (
                    batch_response.get("schema_version") == MULTI_RESPONSE_SCHEMA
                    and batch_response.get("status") == "verified"
                ),
                "provider_multi_callable_vm_batch_completed": len(batch_vm_rows) == 1
                and all(row.get("outcome") == {"kind": "completed"} for row in batch_vm_rows),
                "language_source_parse_contract_routed": (
                    parse_cold_response.get("schema_version") == SOURCE_PARSE_CACHE_RESPONSE_SCHEMA
                ),
                "language_finite_type_fact_contract_routed": (
                    finite_type_cold_response.get("schema_version")
                    == FINITE_TYPE_FACT_RESPONSE_SCHEMA
                ),
                "language_record_type_fact_contract_routed": (
                    record_type_cold_response.get("schema_version")
                    == RECORD_TYPE_FACT_RESPONSE_SCHEMA
                ),
                "language_ssa_function_fact_contract_routed": (
                    ssa_fact_cold_response.get("schema_version")
                    == SSA_FUNCTION_FACT_RESPONSE_SCHEMA
                ),
                "language_ssa_function_facts_stored_separately": (
                    ssa_fact_entry.get("schema_version")
                    == "mncs.compiler.application-build-cache-entry/9"
                    and bool(ssa_function_facts)
                ),
                "language_ssa_function_exact_cache_hit_did_not_claim_reuse": (
                    ssa_fact_warm_response.get("cache", {}).get("status") == "hit"
                    and ssa_fact_warm_work.get("front_end_invocations") == 0
                    and not ssa_fact_warm_work.get("reused_ssa_function_facts")
                ),
                "language_record_type_facts_stored_separately": (
                    record_type_entry.get("schema_version")
                    == "mncs.compiler.application-build-cache-entry/8"
                    and bool(record_type_cached_facts)
                ),
                "language_record_type_exact_cache_hit_did_not_claim_reuse": (
                    record_type_warm_response.get("cache", {}).get("status") == "hit"
                    and record_type_warm_work.get("front_end_invocations") == 0
                    and not record_type_warm_work.get(
                        "reused_record_type_declaration_facts"
                    )
                ),
                "language_finite_type_facts_stored_separately": (
                    finite_type_entry.get("schema_version")
                    == "mncs.compiler.application-build-cache-entry/7"
                    and bool(finite_type_cached_facts)
                ),
                "language_finite_type_exact_cache_hit_did_not_claim_reuse": (
                    finite_type_warm_response.get("cache", {}).get("status") == "hit"
                    and finite_type_warm_work.get("front_end_invocations") == 0
                    and not finite_type_warm_work.get(
                        "reused_finite_type_declaration_facts"
                    )
                ),
                "fresh_process_reused_unchanged_import_parse": bool(
                    parse_comment_response.get("cache", {})
                    .get("verification_work", {})
                    .get("source_parse_cache", {})
                    .get("observation", {})
                    .get("reused_source_identities")
                ),
                "language_function_body_facts_reused_after_comment_edit": bool(
                    parse_comment_reused_facts
                ),
                "language_function_body_reverification_reduced_after_comment_edit": (
                    len(parse_comment_recomputed_facts)
                    < len(parse_cold_recomputed_facts)
                ),
                "language_uncalled_body_edit_invalidated_exact_fact": (
                    edited_body_identity in function_edit_recomputed_facts
                    and edited_body_identity not in function_edit_reused_facts
                ),
                "language_independent_body_facts_reused_after_uncalled_edit": bool(
                    function_edit_reused_facts
                ),
                "uncalled_body_edit_preserved_executable_and_vm_behavior": (
                    function_edit_response.get("artifact", {}).get("identity")
                    == parse_comment_response.get("artifact", {}).get("identity")
                    and parse_cold_returned == function_edit_returned
                ),
                "language_imported_signature_invalidated_exact_fact_closure": (
                    set(signature_recomputed_facts)
                    == (expected_signature_recomputed | set(specialized_summarize))
                    and signature_reasons.get(summarize_fact)
                    == "declaration_interface_changed"
                    and signature_reasons.get(application_caller_fact)
                    == "dependencies_changed"
                ),
                "language_imported_signature_invalidated_exact_declaration_facts": (
                    set(signature_recomputed_declarations)
                    == (expected_signature_declarations
                        | set(specialized_summarize_declaration))
                    and "mncs:0.2:function:tests.native_readiness::run"
                    in signature_reused_declarations
                ),
                "language_independent_body_facts_reused_after_signature_edit": (
                    "mncs:0.2:body:mncs.test.snapshot::same"
                    in signature_reused_facts
                ),
                "signature_edit_regenerated_executable_closure": (
                    signature_edit_response.get("artifact", {}).get("identity")
                    != parse_comment_response.get("artifact", {}).get("identity")
                ),
                "signature_edit_matches_stage0_and_preserves_application_result": (
                    signature_vm_returned == parse_cold_normalized
                ),
                "store_input_artifact_reused": store_response["cache"]["status"] == "hit",
                "invalid_source_precise_rejection": diagnostic_codes,
                "invalid_success_publication": invalid_response["cache"]["publication"],
                "corrupt_store_input_rejected": corrupt.returncode != 0,
                "unsupported_request_schema_rejected": schema_check.returncode != 0,
                "environment_output_artifact": cold.get("result_file"),
            },
            "verification_work_observation": {
                "cold": cold_work,
                "warm_exact_snapshot": warm_work,
                "store_input_exact_snapshot": store_input_work,
                "invalid_source": invalid_work,
                "digest_app_cold": digest_cold_work,
                "digest_app_warm_exact_snapshot": digest_warm_work,
                "multi_callable_provider_build": batch_work,
                "language_source_parse_cold": parse_cold_work,
                "language_source_parse_comment_edit": parse_comment_work,
                "language_source_parse_uncalled_body_edit": function_edit_work,
                "language_source_parse_imported_signature_edit": signature_edit_work,
                "language_finite_type_fact_cold": finite_type_work,
                "language_finite_type_fact_warm_exact_hit": finite_type_warm_work,
            },
            "wall_seconds": {
                "cold": round(cold_elapsed, 6),
                "warm": round(warm_elapsed, 6),
                "store_input": round(store_elapsed, 6),
                "invalid": round(invalid_elapsed, 6),
                "digest_app_cold": round(digest_cold_elapsed, 6),
                "digest_app_warm": round(digest_warm_elapsed, 6),
                "multi_callable_provider": round(batch_elapsed, 6),
                "multi_callable_vm_batch": round(batch_vm_elapsed, 6),
                "language_source_parse_cold": round(parse_cold_elapsed, 6),
                "language_source_parse_comment_edit": round(parse_comment_elapsed, 6),
                "language_source_parse_uncalled_body_edit": round(function_edit_elapsed, 6),
                "language_source_parse_imported_signature_edit": round(
                    signature_edit_elapsed, 6
                ),
                "language_finite_type_fact_cold": round(finite_type_cold_elapsed, 6),
                "language_finite_type_fact_warm": round(finite_type_warm_elapsed, 6),
                "signature_edit_canonical_vm": signature_vm_metrics.get("wall_seconds"),
            },
            "process_tree_metrics": {
                "method": "sampled /proc process tree at 10 ms intervals",
                "cold": cold_metrics,
                "warm": warm_metrics,
                "store_input": store_metrics,
                "invalid": invalid_metrics,
                "digest_app_cold": digest_cold_metrics,
                "digest_app_warm": digest_warm_metrics,
                "multi_callable_provider": batch_metrics,
                "language_source_parse_cold": parse_cold_metrics,
                "language_source_parse_comment_edit": parse_comment_metrics,
                "language_source_parse_uncalled_body_edit": function_edit_metrics,
                "language_source_parse_imported_signature_edit": signature_edit_metrics,
                "language_finite_type_fact_cold": finite_type_cold_metrics,
                "language_finite_type_fact_warm": finite_type_warm_metrics,
            },
        }
        if DIGEST_EVIDENCE_PATH.exists():
            raise SystemExit(
                f"refusing to overwrite immutable evidence: {DIGEST_EVIDENCE_PATH}"
            )
        DIGEST_EVIDENCE_PATH.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result["evidence_file"] = str(DIGEST_EVIDENCE_PATH.relative_to(ROOT))
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
