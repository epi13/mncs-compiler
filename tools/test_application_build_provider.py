#!/usr/bin/env python3
"""Bounded contract tests for the Environment-facing application build provider."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_vm_native_application as app

configured_probe = os.environ.get("MNCS_COMPILER_PROBE")
if configured_probe:
    app.PROBE = Path(configured_probe).expanduser().resolve()

ROOT = app.ROOT
PROVIDER = ROOT / "tools/application_build_provider.py"
REQUEST_SCHEMA = "mncs.compiler.application-build-request/1"
RESPONSE_SCHEMA = "mncs.compiler.application-build/4"
INVOCATION_SCHEMA = "mncs.compiler.application-build-invocation/1"


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


def invoke(
    args: list[str],
    *,
    output_dir: Path,
    cache_dir: Path,
    input_dir: Path | None = None,
    input_manifest: dict | None = None,
) -> tuple[dict, dict, float]:
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
    completed = subprocess.run(
        [sys.executable, str(PROVIDER), "build", *args],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        raise AssertionError(
            json.dumps(
                {
                    "returncode": completed.returncode,
                    "stdout": completed.stdout[-1000:],
                    "stderr": completed.stderr[-2000:],
                },
                indent=2,
            )
        )
    summary = json.loads(completed.stdout)
    if summary.get("schema_version") != INVOCATION_SCHEMA:
        raise AssertionError("Environment invocation summary schema is missing")
    result_path = output_dir / "native-application-build.json"
    response = json.loads(result_path.read_text())
    if response.get("schema_version") != RESPONSE_SCHEMA:
        raise AssertionError("stored application build response schema is wrong")
    return summary, response, elapsed


def main() -> None:
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
        cold, cold_response, cold_elapsed = invoke(
            [
                "--request-file", request_path.relative_to(ROOT).as_posix(),
                "--cache-dir", cache_arg,
            ],
            output_dir=cold_dir,
            cache_dir=cache_dir,
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
        warm, warm_response, warm_elapsed = invoke(
            [
                "--request-file", request_path.relative_to(ROOT).as_posix(),
                "--cache-dir", cache_arg,
            ],
            output_dir=warm_dir,
            cache_dir=cache_dir,
        )
        if warm_response.get("cache", {}).get("status") != "hit":
            raise AssertionError("fresh provider process did not reuse the exact cached build")
        if warm_response.get("artifact", {}).get("identity") != artifact.get("identity"):
            raise AssertionError("warm provider build changed the canonical artifact identity")

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
        store_input, store_response, store_elapsed = invoke(
            ["--request-artifact", "request", "--cache-dir", cache_arg],
            output_dir=store_output_dir,
            cache_dir=cache_dir,
            input_dir=store_input_dir,
            input_manifest=input_manifest,
        )
        if store_response.get("cache", {}).get("status") != "hit":
            raise AssertionError("named Store input did not resolve to the exact cached build")
        if store_response.get("artifact", {}).get("identity") != artifact.get("identity"):
            raise AssertionError("Store-supplied request changed the application artifact")

        invalid_dir = base / "invalid-output"
        invalid_dir.mkdir()
        invalid, invalid_response, invalid_elapsed = invoke(
            [
                "--request-file", bad_path.relative_to(ROOT).as_posix(),
                "--cache-dir", cache_arg,
            ],
            output_dir=invalid_dir,
            cache_dir=cache_dir,
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
            raise AssertionError("corrupt Store input was accepted or published")

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
        result = {
            "schema_version": 1,
            "kind": "mncs-native-application-build-provider-test",
            "status": "pass",
            "request_schema": REQUEST_SCHEMA,
            "response_schema": RESPONSE_SCHEMA,
            "producer": {
                "probe_sha256": app.sha(app.PROBE.read_bytes()),
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
            "checks": {
                "cold_cache_miss": cold_response["cache"]["status"] == "miss",
                "fresh_process_warm_hit": warm_response["cache"]["status"] == "hit",
                "store_input_artifact_reused": store_response["cache"]["status"] == "hit",
                "invalid_source_precise_rejection": diagnostic_codes,
                "invalid_success_publication": invalid_response["cache"]["publication"],
                "corrupt_store_input_rejected": corrupt.returncode != 0,
                "unsupported_request_schema_rejected": schema_check.returncode != 0,
                "environment_output_artifact": cold.get("result_file"),
            },
            "wall_seconds": {
                "cold": round(cold_elapsed, 6),
                "warm": round(warm_elapsed, 6),
                "store_input": round(store_elapsed, 6),
                "invalid": round(invalid_elapsed, 6),
            },
        }
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
