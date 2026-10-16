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
INVOCATION_SCHEMA = "mncs.compiler.application-build-invocation/1"
DIGEST_EVIDENCE_PATH = ROOT / "evidence/native-app-readiness-provider-digest.json"


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
        != "mncs.compiler.application-build-verification-work/1"
    ):
        raise AssertionError(
            f"{label} omitted versioned verification-work telemetry: cache={cache}"
        )
    if work.get("front_end_invocations") != frontend_invocations:
        raise AssertionError(f"{label} frontend invocation count is inaccurate: {work}")
    expected_stage = "recomputed_by_full_frontend" if frontend_invocations else "not_run"
    for stage in (
        "source_parsing",
        "declaration_resolution",
        "signature_verification",
        "function_body_verification",
    ):
        if work.get(stage) != expected_stage:
            raise AssertionError(f"{label} has unexpected {stage} work: {work}")
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
    if response.get("schema_version") not in {RESPONSE_SCHEMA, MULTI_RESPONSE_SCHEMA}:
        raise AssertionError("stored application build response schema is unsupported")
    if summary.get("response_schema") != response.get("schema_version"):
        raise AssertionError("provider summary changed the application response schema")
    return summary, response, elapsed, metrics


def main() -> None:
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

        batch_roots = [
            {"module": batch_app.MODULE, "function": "increment"},
            {"module": batch_app.MODULE, "function": "decrement"},
        ]
        batch_request = batch_app.request(batch_roots)
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
            {
                "module": batch_app.MODULE,
                "function": "decrement",
                "status": "verified",
            },
            {
                "module": batch_app.MODULE,
                "function": "increment",
                "status": "verified",
            },
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
        if batch_artifact_callables != {
            (batch_app.MODULE, "increment"),
            (batch_app.MODULE, "decrement"),
        }:
            raise AssertionError(
                "provider multi-callable build emitted an unexpected callable set"
            )
        batch_vm_result, batch_vm_elapsed, batch_vm_peak_rss_kib = batch_app.vm_batch(
            batch_artifact
        )
        batch_vm_rows = batch_vm_result.get("results", [])
        if len(batch_vm_rows) != 2 or any(
            row.get("outcome") != {"kind": "completed"}
            or len(row.get("record", {}).get("returned", [])) != 1
            or app.vm_to_wire(row["record"]["returned"][0])
            != batch_app.integer(42)
            for row in batch_vm_rows
        ):
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
                "provider_multi_callable_vm_batch_completed": len(batch_vm_rows) == 2
                and all(row.get("outcome") == {"kind": "completed"} for row in batch_vm_rows),
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
            },
        }
        DIGEST_EVIDENCE_PATH.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        result["evidence_file"] = str(DIGEST_EVIDENCE_PATH.relative_to(ROOT))
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
