#!/usr/bin/env python3
"""Verify the production Test provider application's capability boundary.

The harness compiles the real application and protocol, checks denied and
invalid calls, and executes real inventory results through bounded structured
host operations. It also combines Test callables from distinct verified
artifacts and compares the native provider fold with Stage-0. This local VM
adapter is not a Forge or canonical Store integration test.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import time
from datetime import datetime, timezone

import test_vm_native_application as support
import test_vm_test_digest_app as digest_app
import test_application_build_test_inventory as test_inventory

ROOT = Path(__file__).resolve().parents[1]
TEST_ROOT = digest_app.TEST_ROOT
STDLIB_ROOT = digest_app.STDLIB_ROOT
ENTRY_MODULE = "mncs.test.provider"
ENTRY_FUNCTION = "provider_batch_app"
ROOT_SOURCE_PATH = TEST_ROOT / "native/mncs/test/provider.mncs"
MODULE_PATHS = {
    "mncs.test.assertions": TEST_ROOT / "native/mncs/test/assertions.mncs",
    "mncs.test.provider_protocol.v1": TEST_ROOT / "native/mncs/test/provider_protocol/v1.mncs",
    "mncs.core.sequences.v1": STDLIB_ROOT / "library/core/sequences.mncs",
    "mncs.std.application.v1": STDLIB_ROOT / "library/std/application.mncs",
}
EVIDENCE_PATH = ROOT / os.environ.get(
    "MNCS_NATIVE_TEST_PROVIDER_APP_EVIDENCE_PATH",
    "evidence/native-test-provider-app-boundary-20261017.json",
)
PROVIDER_INTERFACE_IDENTITY = bytes([
    239, 160, 50, 155, 9, 221, 240, 227, 220, 117, 104, 17, 134, 215, 15, 192,
    206, 150, 165, 44, 250, 237, 241, 7, 176, 213, 182, 151, 52, 146, 186, 101,
])
PROVIDER_REVISION_IDENTITY = bytes([
    242, 141, 98, 241, 104, 235, 30, 81, 190, 31, 78, 85, 62, 100, 140, 222,
    6, 235, 59, 56, 33, 238, 68, 155, 119, 69, 38, 11, 227, 233, 132, 215,
])


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def byte_sequence(text: str) -> dict:
    return {"sequence": {"values": [{"byte": {"value": byte}} for byte in text.encode()]}}


def unsigned(value: int) -> dict:
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def signed(value: int) -> dict:
    return {"integer": {"type": {"bits": 64, "signed": True}, "value": value}}


def wire_record(name: str, fields: dict, identity: str) -> dict:
    return {
        "record": {
            "name": name,
            "type_identity": identity,
            "fields": [[key, value] for key, value in sorted(fields.items())],
        },
    }


def record_fields(value: dict) -> dict:
    record = value.get("record")
    if not isinstance(record, dict) or not isinstance(record.get("fields"), list):
        raise AssertionError(f"expected a typed record wire value, got {value}")
    return {key: item for key, item in record["fields"]}


def wire_finite(interface: dict, type_name: str, variant_name: str) -> dict:
    finite = next(row for row in interface["types"]["finite"] if row.get("name") == type_name)
    variant = next(row for row in finite["variants"] if row.get("name") == variant_name)
    return {"finite": {
        "type_identity": finite["identity"],
        "variant_identity": variant["identity"],
        "discriminant": variant["discriminant"],
        "payload": [],
    }}


def variant_name(interface: dict, type_name: str, discriminant: int) -> str:
    finite = next(row for row in interface["types"]["finite"] if row.get("name") == type_name)
    variant = next((row for row in finite["variants"]
                    if row.get("discriminant") == discriminant), None)
    if variant is None:
        raise AssertionError(f"{type_name} has no variant with discriminant {discriminant}")
    return variant["name"]


def test_result_wire(interface: dict, result: dict) -> dict:
    verdict_names = {0: "PASS", 1: "FAIL", 2: "SKIP", 3: "UNSUPPORTED"}
    verdict_name = verdict_names.get(result.get("verdict_code"))
    failure_kind = result.get("failure_kind", {}).get("$v")
    if verdict_name is None or not isinstance(failure_kind, int):
        raise AssertionError(f"canonical TestResult has unknown typed tags: {result}")
    expected_discriminant = next(
        row["discriminant"] for row in next(
            row for row in interface["types"]["finite"] if row.get("name") == "Verdict"
        )["variants"] if row.get("name") == verdict_name
    )
    if result.get("verdict", {}).get("$v") != expected_discriminant:
        raise AssertionError(f"canonical TestResult verdict and numeric code disagree: {result}")
    return wire_record("TestResult", {
        "verdict": wire_finite(interface, "Verdict", verdict_name),
        "verdict_code": unsigned(result["verdict_code"]),
        "failure_kind": wire_finite(
            interface, "FailureKind", variant_name(interface, "FailureKind", failure_kind)
        ),
        "failure_code": unsigned(result["failure_code"]),
        "assertions": unsigned(result["assertions"]),
        "failures": unsigned(result["failures"]),
        "expected": signed(result["expected"]),
        "actual": signed(result["actual"]),
        "assertion_code": unsigned(result["assertion_code"]),
    }, record_identity(interface, "TestResult"))


def provider_request_wire(interface: dict, provider_request_identity: str,
                          cases: list[dict]) -> dict:
    callable_entries = []
    executions = []
    selected = []
    for case in cases:
        required = {
            "artifact_identity", "test_case_identity", "declaration_identity",
            "callable_identity", "signature_identity", "native_result",
        }
        if not required <= set(case):
            raise AssertionError(f"real compiler Test binding omitted required identities: {case}")
        for key in required - {"native_result"}:
            if not isinstance(case[key], str) or not case[key]:
                raise AssertionError(f"real compiler Test binding has an invalid {key}: {case}")
        if not isinstance(case["native_result"], dict):
            raise AssertionError(f"real compiler Test binding has no typed native result: {case}")
        identity_fields = {
            key: byte_sequence(case[key]) for key in (
                "artifact_identity", "test_case_identity", "declaration_identity",
                "callable_identity", "signature_identity",
            )
        }
        callable_entries.append(wire_record(
            "TestCallableEntry", identity_fields,
            record_identity(interface, "TestCallableEntry"),
        ))
        selected.append(byte_sequence(case["test_case_identity"]))
        executions.append(wire_record("ProviderTestExecution", {
            **{key: identity_fields[key] for key in (
                "test_case_identity", "declaration_identity", "callable_identity",
                "signature_identity", "artifact_identity",
            )},
            "execution_status": wire_finite(interface, "CallableExecutionStatus", "RETURNED"),
            "native_result": test_result_wire(interface, case["native_result"]),
        }, record_identity(interface, "ProviderTestExecution")))
    if not cases or len(cases) > 8:
        raise AssertionError(f"provider self-suite selection is outside its bounded shape: {len(cases)}")
    return wire_record("ProviderRequest", {
        "compiler_callable_bindings": {"sequence": {"values": callable_entries}},
        "selected_test_identities": {"sequence": {"values": selected}},
        "selected_test_executions": {"sequence": {"values": executions}},
        "selection_count": unsigned(len(cases)),
        "interface_identity": byte_values(PROVIDER_INTERFACE_IDENTITY),
        "provider_revision_identity": byte_values(PROVIDER_REVISION_IDENTITY),
    }, provider_request_identity)


def record_identity(interface: dict, name: str) -> str:
    row = next((row for row in interface["types"]["records"] if row.get("name") == name), None)
    if row is None:
        raise AssertionError(f"verified provider protocol omitted record type {name}")
    identity = row.get("identity")
    if isinstance(identity, dict):
        identity = identity.get("0")
    if not isinstance(identity, str):
        raise AssertionError(f"verified provider protocol has no identity for {name}")
    return identity


def byte_values(values: bytes) -> dict:
    return {"sequence": {"values": [{"byte": {"value": byte}} for byte in values]}}


def compile_request(root: str, modules: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.compiler.application-build-request/1",
        "target": "mncs-vm-0.1",
        "root_source_id": "native/mncs/test/provider.mncs",
        "root_source": root,
        "entry": {"module": ENTRY_MODULE, "function": ENTRY_FUNCTION},
        "modules": modules,
        "generic_seeds": [],
        "capabilities": ["provider_artifact", "provider_digest"],
        "verification_policy": "stage0-semantic-check+canonical-vm-artifact/1",
    }


def application_context(interface: dict, current_dir: Path, *, valid_argv: bool = False,
                        artifact_paths: list[Path] | None = None) -> dict:
    context_type = next(
        row for row in interface["types"]["records"]
        if row.get("name") == "ApplicationContext"
    )
    return wire_record("ApplicationContext", {
        "application_identity": {"sequence": {"values": [
            {"byte": {"value": 0}} for _ in range(32)
        ]}},
        # The production entry requires three artifact paths. The default
        # proves its invalid-argument boundary; valid_argv reaches capability
        # admission without needing any host files to exist.
        "argv": {"sequence": {"values": [
            *([byte_sequence(str(path)) for path in artifact_paths]
              if artifact_paths is not None else [
                  byte_sequence("request.json"),
                  byte_sequence("result.json"),
                  *([byte_sequence("check.json")] if valid_argv else []),
              ]),
        ]}},
        "argv_count": unsigned(
            len(artifact_paths) if artifact_paths is not None else (3 if valid_argv else 2)
        ),
        "current_dir": byte_sequence(str(current_dir)),
        "environment": {"sequence": {"values": []}},
        "environment_count": unsigned(0),
        "stdin": byte_sequence(""),
    }, context_type["identity"])


def call_request(context: dict) -> dict:
    return {
        "schema_version": "0.1",
        "target": {"module": ENTRY_MODULE, "function": ENTRY_FUNCTION},
        "arguments": [context],
        "type_arguments": [],
        "step_budget": 200_000,
    }


def publish_environment_artifacts(payload: bytes) -> list[dict]:
    raw_root = os.environ.get("MNCS_ENV_SESSION_ARTIFACT_DIR")
    if not raw_root:
        return []
    artifact_root = Path(raw_root)
    try:
        root_descriptor = os.open(
            artifact_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as error:
        raise RuntimeError("Environment artifact directory is unavailable") from error
    try:
        if not stat.S_ISDIR(os.fstat(root_descriptor).st_mode):
            raise RuntimeError("Environment artifact root must be a real directory")
        name = EVIDENCE_PATH.name
        try:
            existing = os.stat(name, dir_fd=root_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(existing.st_mode):
                raise RuntimeError("Environment artifact destination must be a regular file")

        temporary_name = f".{name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=root_descriptor)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.replace(
                temporary_name, name,
                src_dir_fd=root_descriptor, dst_dir_fd=root_descriptor,
            )
            os.fsync(root_descriptor)
        except OSError as error:
            try:
                os.unlink(temporary_name, dir_fd=root_descriptor)
            except OSError:
                pass
            raise RuntimeError(f"cannot write bounded Environment artifact {name}") from error
    finally:
        os.close(root_descriptor)
    return [{"path": name, "byte_count": len(payload), "sha256": sha(payload)}]


def expect_runner_rejection(binary: Path, artifact_path: Path, identity: str,
                            alias: str, request_path: Path, input_path: Path,
                            output_path: Path, expected: str) -> str:
    process = subprocess.run([
        str(binary), str(artifact_path), identity, alias, "single",
        str(request_path), str(input_path), str(output_path), "none",
    ], cwd=ROOT, capture_output=True, text=True, check=False)
    if process.returncode == 0 or expected not in process.stderr:
        raise AssertionError(
            f"runner accepted or misdiagnosed invalid capability binding: "
            f"rc={process.returncode}, stderr={process.stderr[-2000:]}"
        )
    return expected


def compile_cross_module_provider_case(cache_dir: Path) -> tuple[dict, dict]:
    """Compile and execute one Test from its own real source module artifact."""
    source_path = TEST_ROOT / "tests/provider_cross_module.mncs"
    source = source_path.read_text(encoding="utf-8")
    modules = {
        "mncs.test.assertions": MODULE_PATHS["mncs.test.assertions"].read_text(encoding="utf-8"),
    }
    build_request = test_inventory.request(
        source,
        [],
        root_source_id="mncs-test/tests/provider_cross_module.mncs",
        modules=modules,
    )
    build_request["schema_version"] = "mncs.compiler.application-build-request/4"
    build_request["test_selection"] = {"kind": "all_source_cases"}
    probe = support.Probe(
        cache_dir / "program-cache",
        executable=support.PROBE,
        application_cache_dir=cache_dir / "application-cache",
    )
    try:
        built, build_metrics = probe.send(
            {"compile_application": build_request}, timeout=180.0,
        )
    finally:
        probe.close()

    inventory = test_inventory.require_inventory(
        built,
        {"separate_module_identity"},
        expected_module="tests.provider_cross_module",
        response_schema="mncs.compiler.application-build/7",
    )
    if (built.get("unresolved_obligations") != []
            or test_inventory.frontend_invocations(built) != 1):
        raise AssertionError("separate Test module was not verified in one application frontend pass")
    artifact = built.get("artifact", {}).get("value")
    artifact_identity = built.get("artifact", {}).get("identity")
    if (not isinstance(artifact, dict) or not isinstance(artifact_identity, str)
            or artifact.get("artifact_id") != artifact_identity):
        raise AssertionError("separate Test module omitted its verified executable artifact identity")

    test_case = inventory["tests"][0]
    vm, vm_wall, vm_peak_rss = test_inventory.execute_inventory_batch(
        artifact, inventory, step_budget=200_000,
    )
    vm_rows = vm.get("results", [])
    if (vm.get("artifact_id") != artifact_identity or len(vm_rows) != 1
            or vm_rows[0].get("id") != test_case["test_case_identity"]
            or vm_rows[0].get("outcome") != {"kind": "completed"}
            or vm_rows[0].get("record", {}).get("artifact_id") != artifact_identity):
        raise AssertionError("separate Test case did not execute under its own selected artifact identity")
    actual_values = vm_rows[0].get("record", {}).get("returned", [])
    if len(actual_values) != 1:
        raise AssertionError("separate Test case returned the wrong result arity")
    native_result = test_inventory.app.decode_wire(
        test_inventory.app.vm_to_wire(actual_values[0])
    )

    oracle_probe = support.Probe(cache_dir / "stage0-cache", executable=support.PROBE)
    try:
        oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
            "root": source,
            "modules": modules,
            "calls": [{
                "schema_version": "0.1",
                "target": {
                    "module": test_case["module"],
                    "function": test_case["name"],
                },
                "arguments": [],
                "type_arguments": [],
                "step_budget": 200_000,
            }],
        }}, timeout=180.0)
    finally:
        oracle_probe.close()
    executions = oracle.get("executions", [])
    if (not oracle.get("valid") or len(executions) != 1
            or executions[0].get("status") != "returned"
            or len(executions[0].get("returned", [])) != 1):
        raise AssertionError("Stage-0 did not return the separate Test module's typed result")
    expected_result = test_inventory.app.decode_wire(executions[0]["returned"][0])
    if native_result != expected_result or native_result.get("verdict_code") != 0:
        raise AssertionError("separate Test result differs between canonical VM and Stage-0")

    callable_row = next((row for row in artifact.get("callables", [])
                         if row.get("module") == test_case["module"]
                         and row.get("name") == test_case["name"]), None)
    binding = callable_row.get("test_binding") if callable_row else None
    required_binding_fields = {
        "test_case_identity", "declaration_identity", "callable_identity",
        "signature_identity",
    }
    if (not isinstance(binding, dict)
            or not required_binding_fields <= set(binding)
            or binding.get("test_case_identity") != test_case["test_case_identity"]
            or binding.get("declaration_identity") != test_case["declaration_identity"]):
        raise AssertionError("separate Test artifact omitted its Language inventory callable binding")
    case = {
        "name": test_case["name"],
        "artifact_identity": artifact_identity,
        "test_case_identity": binding["test_case_identity"],
        "declaration_identity": binding["declaration_identity"],
        "callable_identity": binding["callable_identity"],
        "signature_identity": binding["signature_identity"],
        "native_result": native_result,
    }
    proof = {
        "source_path": str(source_path),
        "source_sha256": sha(source.encode("utf-8")),
        "module": test_case["module"],
        "test_name": test_case["name"],
        "test_case_identity": case["test_case_identity"],
        "artifact_identity": artifact_identity,
        "artifact_bytes": len(json.dumps(
            artifact, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")),
        "frontend_invocations": test_inventory.frontend_invocations(built),
        "build_metrics": build_metrics,
        "canonical_vm_result_matches_stage0": True,
        "canonical_vm_wall_seconds": round(vm_wall, 6),
        "canonical_vm_peak_rss_kib": vm_peak_rss,
        "stage0_oracle_metrics": oracle_metrics,
    }
    return case, proof


def main() -> None:
    if not support.PROBE.is_file() or not digest_app.RUNNER_SOURCE.is_file():
        raise SystemExit("pinned Stage-0 probe or native VM runner source is missing")
    root = ROOT_SOURCE_PATH.read_text(encoding="utf-8")
    modules = {name: path.read_text(encoding="utf-8") for name, path in MODULE_PATHS.items()}
    request = compile_request(root, modules)

    with tempfile.TemporaryDirectory(prefix="native-provider-app-build-") as raw_cache:
        cache = Path(raw_cache)
        probe = support.Probe(cache / "program-cache", application_cache_dir=cache / "application-cache")
        try:
            built, build_metrics = probe.send({"compile_application": request}, timeout=600)
            interface = built.get("entry", {}).get("interface", {})
            if (built.get("schema_version") != "mncs.compiler.application-build/4"
                    or built.get("status") != "verified"
                    or built.get("unresolved_obligations") != []
                    or interface.get("status") != "verified"):
                raise AssertionError(json.dumps({
                    "schema_version": built.get("schema_version"),
                    "status": built.get("status"),
                    "interface_status": interface.get("status"),
                    "unresolved_obligations": built.get("unresolved_obligations"),
                    "diagnostics": built.get("diagnostics"),
                }, indent=2)[:10000])

            # The application entry takes ApplicationContext, so its callable
            # interface intentionally does not expose ProviderRequest. Ask the
            # same compiler to verify the actual protocol consumer as a second
            # entry and take the nominal identity from that compiler interface.
            protocol_request = json.loads(json.dumps(request))
            protocol_request["entry"] = {
                "module": ENTRY_MODULE,
                "function": "run_selected_batch",
            }
            protocol_built, protocol_build_metrics = probe.send(
                {"compile_application": protocol_request}, timeout=600,
            )
            protocol_interface = protocol_built.get("entry", {}).get("interface", {})
            if (protocol_built.get("schema_version") != "mncs.compiler.application-build/4"
                    or protocol_built.get("status") != "verified"
                    or protocol_built.get("unresolved_obligations") != []
                    or protocol_interface.get("status") != "verified"
                    or protocol_interface.get("types", {}).get("status") != "verified"
                    or protocol_interface.get("types", {}).get("unresolved_references") != []):
                raise AssertionError(json.dumps({
                    "schema_version": protocol_built.get("schema_version"),
                    "status": protocol_built.get("status"),
                    "interface_status": protocol_interface.get("status"),
                    "types_status": protocol_interface.get("types", {}).get("status"),
                    "unresolved_obligations": protocol_built.get("unresolved_obligations"),
                    "diagnostics": protocol_built.get("diagnostics"),
                }, indent=2)[:10000])
            provider_request_type = next(
                (row for row in protocol_interface["types"]["records"]
                 if row.get("name") == "ProviderRequest"),
                None,
            )
            if provider_request_type is None:
                raise AssertionError("verified protocol callable interface omitted ProviderRequest")
            provider_request_identity = provider_request_type.get("identity")
            if isinstance(provider_request_identity, dict):
                provider_request_identity = provider_request_identity.get("0")
            if not isinstance(provider_request_identity, str):
                raise AssertionError("verified protocol interface has no ProviderRequest identity")
            artifact = built.get("artifact", {}).get("value")
            if not isinstance(artifact, dict):
                raise AssertionError("verified provider app build omitted its VM artifact")
            artifact_identity = built["artifact"]["identity"]
            if artifact.get("artifact_id") != artifact_identity:
                raise AssertionError("provider app artifact identity differs from compiler receipt")
            runtime_copy_lowering_ref = next((
                value for value in artifact.get("source", {}).get("lowering_refs", [])
                if value.startswith("vm-runtime-checked-sequence-copy-lowering/1:")
            ), None)
            if runtime_copy_lowering_ref is None:
                raise AssertionError("production provider artifact omitted runtime-checked copy lowering")
            if "vm-runtime-checked-sequence-copy-to-project-replace/1" not in json.dumps(artifact):
                raise AssertionError("production provider artifact omitted its copy transformation record")

            dispatch = built.get("capability_dispatch", {})
            rows = dispatch.get("bindings", [])
            host_rows = [row for row in rows if row.get("operation") in {
                "structured_read", "structured_write", "structured_digest",
            }]
            expected_operations = {
                "provider_artifact": {"structured_read", "structured_write"},
                "provider_digest": {"structured_digest"},
            }
            expected_canonical_identities = {
                "provider_artifact": {
                    "mncs:0.2:capability:mncs.test.provider::provider_batch_app::provider_artifact",
                },
                "provider_digest": {
                    "mncs:0.2:capability:mncs.test.provider::run_selected_batch::provider_digest",
                },
            }
            actual_operations: dict[str, set[str]] = {}
            canonical_identities: dict[str, set[str]] = {}
            for row in host_rows:
                if row.get("runtime_key_kind") != "source-alias":
                    raise AssertionError(f"host operation lacks a source-alias binding: {row}")
                actual_operations.setdefault(row.get("runtime_capability_key"), set()).add(row.get("operation"))
                canonical_identities.setdefault(row.get("runtime_capability_key"), set()).add(
                    row.get("capability_identity")
                )
            if (dispatch.get("schema_version") != "mncs.compiler.capability-dispatch/1"
                    or dispatch.get("status") != "verified"
                    or actual_operations != expected_operations
                    or canonical_identities != expected_canonical_identities):
                raise AssertionError(f"provider app capability dispatch differs from source contract: {dispatch}")
            artifact_identity_for_files = next(
                row["capability_identity"] for row in host_rows
                if row.get("runtime_capability_key") == "provider_artifact"
                and row.get("operation") == "structured_read"
            )
            artifact_identity_for_digest = next(
                row["capability_identity"] for row in host_rows
                if row.get("runtime_capability_key") == "provider_digest"
                and row.get("operation") == "structured_digest"
            )
            if artifact_identity_for_files not in artifact.get("requirements", {}).get("capabilities", []):
                raise AssertionError("artifact requirements omit the provider_artifact host-call identity")
            if artifact_identity_for_digest not in artifact.get("requirements", {}).get("capabilities", []):
                raise AssertionError("artifact requirements omit the provider_digest host-call identity")
        finally:
            probe.close()

    binary, runner_sha, runner_lock_sha = digest_app.build_runner()
    artifact_fd, artifact_name = tempfile.mkstemp(prefix="provider-app-", suffix=".vm.json")
    os.close(artifact_fd)
    artifact_path = Path(artifact_name)
    try:
        artifact_bytes = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
        artifact_path.write_bytes(artifact_bytes)
        with tempfile.TemporaryDirectory(prefix="native-provider-app-call-") as raw:
            call_dir = Path(raw)
            context = application_context(interface, call_dir)
            call = call_request(context)
            request_path = call_dir / "execution.json"
            request_path.write_text(json.dumps(call, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            absent_input = call_dir / "request.json"
            absent_output = call_dir / "result.json"
            wrong_identity_diagnostic = expect_runner_rejection(
                binary, artifact_path, artifact_identity_for_files + ":mismatch",
                "provider_artifact", request_path, absent_input, absent_output,
                "artifact does not require authorized capability",
            )
            wrong_alias_diagnostic = expect_runner_rejection(
                binary, artifact_path, artifact_identity_for_files,
                "provider_artifact:mismatch", request_path, absent_input, absent_output,
                "application contract dispatch alias differs from admitted SSA",
            )
            command = [
                "/usr/bin/time", "-v", str(binary), str(artifact_path),
                artifact_identity_for_files, "provider_artifact", "single",
                str(request_path), str(absent_input), str(absent_output), "none",
            ]
            started = time.monotonic()
            process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
            wall_seconds = time.monotonic() - started
            if process.returncode != 0:
                raise AssertionError(f"canonical VM provider app call failed: {process.stderr[-4000:]}")
            document = json.loads(process.stdout)
            returned_wire = document.get("returned_wire", [])
            returned = [digest_app.support.decode_wire(value) for value in returned_wire]
            record = document.get("record", {})
            outcome = document.get("outcome", {})
            if (outcome.get("kind") != "completed"
                    or record.get("artifact_id") != artifact["artifact_id"]
                    or len(returned) != 1
                    or returned[0].get("exit_code") != 2
                    or bytes(returned[0].get("stderr", [])).decode() != "invalid provider args"
                    or record.get("effects") != []
                    or record.get("admitted_capabilities") != []
                    or absent_output.exists()):
                raise AssertionError(json.dumps({
                    "outcome": outcome,
                    "record": record,
                    "returned": returned,
                    "output_exists": absent_output.exists(),
                }, indent=2)[:10000])

            missing_cap_dir = call_dir / "missing-capability"
            missing_cap_dir.mkdir()
            missing_cap_context = application_context(
                interface, missing_cap_dir, valid_argv=True,
            )
            missing_cap_request_path = missing_cap_dir / "execution.json"
            missing_cap_request_path.write_text(
                json.dumps(call_request(missing_cap_context), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            missing_cap_paths = [
                missing_cap_dir / "request.json",
                missing_cap_dir / "result.json",
                missing_cap_dir / "check.json",
            ]
            missing_cap_process = subprocess.run([
                str(binary), str(artifact_path), artifact_identity_for_files,
                "provider_artifact", "single", str(missing_cap_request_path),
                str(missing_cap_paths[0]), str(missing_cap_paths[1]), "none",
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            if missing_cap_process.returncode != 0:
                raise AssertionError(
                    "canonical VM rejected the missing-capability probe before returning a typed result: "
                    f"rc={missing_cap_process.returncode}, stderr={missing_cap_process.stderr[-3000:]}"
                )
            missing_cap_document = json.loads(missing_cap_process.stdout)
            missing_cap_record = missing_cap_document.get("record", {})
            missing_cap_effects = missing_cap_record.get("effects", [])
            if (
                missing_cap_document.get("outcome", {}).get("kind") != "capability_denied"
                or missing_cap_record.get("admitted_capabilities") != []
                or len(missing_cap_effects) != 1
                or missing_cap_effects[0].get("outcome_tag") != "capability_denied"
                or missing_cap_effects[0].get("request", {}).get("operation") != "structured_read"
                or any(path.exists() for path in missing_cap_paths)
            ):
                raise AssertionError(json.dumps({
                    "outcome": missing_cap_document.get("outcome"),
                    "record": missing_cap_record,
                    "artifact_paths_exist": [path.exists() for path in missing_cap_paths],
                }, indent=2)[:10000])

            missing_digest_dir = call_dir / "missing-provider-digest"
            missing_digest_dir.mkdir()
            digest_input = missing_digest_dir / "request.json"
            digest_output = missing_digest_dir / "result.json"
            digest_check = missing_digest_dir / "check.json"
            digest_request_value = wire_record("ProviderRequest", {
                "compiler_callable_bindings": {"sequence": {"values": []}},
                "selected_test_identities": {"sequence": {"values": []}},
                "selected_test_executions": {"sequence": {"values": []}},
                "selection_count": unsigned(0),
                "interface_identity": byte_sequence("\x00" * 32),
                "provider_revision_identity": byte_sequence("\x00" * 32),
            }, provider_request_identity)
            digest_input.write_text(
                json.dumps(digest_request_value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            digest_paths = [digest_input, digest_output, digest_check]
            digest_context = application_context(
                interface, missing_digest_dir, artifact_paths=digest_paths,
            )
            digest_call_path = missing_digest_dir / "execution.json"
            digest_call_path.write_text(
                json.dumps(call_request(digest_context), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            digest_process = subprocess.run([
                str(binary), str(artifact_path), artifact_identity_for_files,
                "provider_artifact", "single", str(digest_call_path),
                str(digest_input), str(digest_output), "success",
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            if digest_process.returncode != 0:
                raise AssertionError(
                    "canonical VM rejected the single-capability provider probe before a typed result: "
                    f"rc={digest_process.returncode}, stderr={digest_process.stderr[-3000:]}"
                )
            digest_document = json.loads(digest_process.stdout)
            digest_record = digest_document.get("record", {})
            digest_effects = digest_record.get("effects", [])
            if (
                digest_document.get("outcome", {}).get("kind") != "capability_denied"
                or digest_document.get("outcome", {}).get("capability")
                != "mncs:0.2:capability:mncs.test.provider::provider_batch_app::provider_digest"
                or digest_document.get("outcome", {}).get("operation")
                != "call mncs:0.2:function:mncs.test.provider::run_selected_batch"
                or digest_record.get("admitted_capabilities") != ["provider_artifact"]
                or len(digest_effects) != 1
                or digest_effects[0].get("outcome_tag") != "completed"
                or digest_effects[0].get("request", {}).get("operation") != "structured_read"
                or digest_effects[0].get("request", {}).get("capability") != "provider_artifact"
                or digest_output.exists() or digest_check.exists()
            ):
                raise AssertionError(json.dumps({
                    "outcome": digest_document.get("outcome"),
                    "record": digest_record,
                    "published_outputs": [digest_output.exists(), digest_check.exists()],
                }, indent=2)[:12000])

            oracle_cache = ROOT / ".build/native-app-readiness/provider-app-oracle-cache"
            oracle_cache.mkdir(parents=True, exist_ok=True)
            oracle_probe = support.Probe(oracle_cache)
            try:
                oracle, oracle_metrics = oracle_probe.send({"project_oracle": {
                    "root": root,
                    "modules": modules,
                    "calls": [call],
                }}, timeout=120)
            finally:
                oracle_probe.close()
            expected_wires = oracle.get("executions", [])
            if (not oracle.get("valid") or len(expected_wires) != 1
                    or expected_wires[0].get("status") != "returned"
                    or expected_wires[0].get("returned") != returned_wire):
                raise AssertionError(f"Stage-0 provider app rejection differs from canonical VM: {oracle}")

            positive_dir = call_dir / "positive-provider-run"
            positive_dir.mkdir()
            test_artifact_identity = "mncs:fixture:provider-artifact"
            test_identity = "mncs:fixture:test-case:provider-pass"
            declaration_identity = "mncs:fixture:declaration:provider-pass"
            callable_identity = "mncs:fixture:callable:provider-pass"
            signature_identity = "mncs:fixture:signature:provider-pass"
            callable_entry = wire_record("TestCallableEntry", {
                "artifact_identity": byte_sequence(test_artifact_identity),
                "test_case_identity": byte_sequence(test_identity),
                "declaration_identity": byte_sequence(declaration_identity),
                "callable_identity": byte_sequence(callable_identity),
                "signature_identity": byte_sequence(signature_identity),
            }, record_identity(protocol_interface, "TestCallableEntry"))
            test_result = wire_record("TestResult", {
                "verdict": wire_finite(protocol_interface, "Verdict", "PASS"),
                "verdict_code": unsigned(0),
                "failure_kind": wire_finite(protocol_interface, "FailureKind", "NoFailure"),
                "failure_code": unsigned(0),
                "assertions": unsigned(1),
                "failures": unsigned(0),
                "expected": {"integer": {"type": {"bits": 64, "signed": True}, "value": 1}},
                "actual": {"integer": {"type": {"bits": 64, "signed": True}, "value": 1}},
                "assertion_code": unsigned(0),
            }, record_identity(protocol_interface, "TestResult"))
            execution = wire_record("ProviderTestExecution", {
                "test_case_identity": byte_sequence(test_identity),
                "declaration_identity": byte_sequence(declaration_identity),
                "callable_identity": byte_sequence(callable_identity),
                "signature_identity": byte_sequence(signature_identity),
                "artifact_identity": byte_sequence(test_artifact_identity),
                "execution_status": wire_finite(protocol_interface, "CallableExecutionStatus", "RETURNED"),
                "native_result": test_result,
            }, record_identity(protocol_interface, "ProviderTestExecution"))
            provider_request_value = wire_record("ProviderRequest", {
                "compiler_callable_bindings": {"sequence": {"values": [callable_entry]}},
                "selected_test_identities": {"sequence": {"values": [byte_sequence(test_identity)]}},
                "selected_test_executions": {"sequence": {"values": [execution]}},
                "selection_count": unsigned(1),
                "interface_identity": byte_values(bytes([
                    239, 160, 50, 155, 9, 221, 240, 227, 220, 117, 104, 17, 134, 215, 15, 192,
                    206, 150, 165, 44, 250, 237, 241, 7, 176, 213, 182, 151, 52, 146, 186, 101,
                ])),
                "provider_revision_identity": byte_values(bytes([
                    242, 141, 98, 241, 104, 235, 30, 81, 190, 31, 78, 85, 62, 100, 140, 222,
                    6, 235, 59, 56, 33, 238, 68, 155, 119, 69, 38, 11, 227, 233, 132, 215,
                ])),
            }, provider_request_identity)
            positive_input = positive_dir / "request.json"
            positive_output = positive_dir / "result.json"
            positive_check = positive_dir / "check.json"
            positive_input.write_text(
                json.dumps(provider_request_value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            positive_context = application_context(
                interface, positive_dir,
                artifact_paths=[positive_input, positive_output, positive_check],
            )
            positive_call_path = positive_dir / "execution.json"
            positive_call_path.write_text(
                json.dumps(call_request(positive_context), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            positive_command = [
                "/usr/bin/time", "-v", str(binary), str(artifact_path),
                f"{artifact_identity_for_files};{artifact_identity_for_digest}",
                "provider_artifact;provider_digest", "single", str(positive_call_path),
                str(positive_input), str(positive_output), "success", str(positive_check),
            ]
            positive_started = time.monotonic()
            positive_process = subprocess.run(
                positive_command, cwd=ROOT, capture_output=True, text=True, check=False,
            )
            positive_wall_seconds = time.monotonic() - positive_started
            if positive_process.returncode != 0:
                raise AssertionError(
                    "canonical VM rejected the positive two-capability provider application: "
                    f"rc={positive_process.returncode}, stderr={positive_process.stderr[-5000:]}"
                )
            positive_document = json.loads(positive_process.stdout)
            positive_record = positive_document.get("record", {})
            positive_effects = positive_record.get("effects", [])
            positive_returned = [digest_app.support.decode_wire(value)
                                 for value in positive_document.get("returned_wire", [])]
            positive_bindings = positive_document.get("capability_binding", {}).get("bindings", [])
            expected_admitted = sorted([
                "provider_artifact", "provider_digest",
                *[
                    identity
                    for binding in positive_bindings
                    for identity in binding.get("call_authority_identities", [])
                ],
            ])
            observed_operations = [row.get("request", {}).get("operation") for row in positive_effects]
            if (
                positive_document.get("outcome", {}).get("kind") != "completed"
                or positive_record.get("admitted_capabilities") != expected_admitted
                or observed_operations != [
                    "structured_read",
                    *(["structured_digest"] * 5),
                    "structured_write",
                    "structured_write",
                ]
                or len(positive_effects) != 8
                or any(row.get("outcome_tag") != "completed" for row in positive_effects)
                or not positive_output.exists() or not positive_check.exists()
                or len(positive_returned) != 1 or positive_returned[0].get("exit_code") != 0
            ):
                raise AssertionError(json.dumps({
                    "outcome": positive_document.get("outcome"),
                    "returned": positive_returned,
                    "admitted_capabilities": positive_record.get("admitted_capabilities"),
                    "expected_admitted_capabilities": expected_admitted,
                    "observed_operations": observed_operations,
                    "effect_count": len(positive_effects),
                    "published": [positive_output.exists(), positive_check.exists()],
                    "effects": positive_effects,
                }, indent=2)[:12000])

            published_result = digest_app.support.decode_wire(json.loads(
                positive_output.read_text(encoding="utf-8")
            ))
            published_check = digest_app.support.decode_wire(json.loads(
                positive_check.read_text(encoding="utf-8")
            ))
            verdict_row = next(
                row for row in protocol_interface["types"]["finite"]
                if row.get("name") == "Verdict"
            )
            pass_discriminant = next(
                row["discriminant"] for row in verdict_row["variants"]
                if row.get("name") == "PASS"
            )
            result_identity = published_result.get("result_identity")
            if (
                published_result.get("native_result", {}).get("verdict", {}).get("$v")
                != pass_discriminant
                or published_result.get("native_result", {}).get("failures") != 0
                or published_check.get("verdict", {}).get("$v") != pass_discriminant
                or published_check.get("test_result_identity") != result_identity
            ):
                raise AssertionError(json.dumps({
                    "published_result": published_result,
                    "published_check": published_check,
                    "pass_discriminant": pass_discriminant,
                }, indent=2)[:8000])

            batch_dir = call_dir / "two-test-batch"
            batch_dir.mkdir()
            failed_test_identity = "mncs:fixture:test-case:provider-fail"
            failed_declaration_identity = "mncs:fixture:declaration:provider-fail"
            failed_callable_identity = "mncs:fixture:callable:provider-fail"
            failed_signature_identity = "mncs:fixture:signature:provider-fail"
            failed_callable_entry = wire_record("TestCallableEntry", {
                "artifact_identity": byte_sequence(test_artifact_identity),
                "test_case_identity": byte_sequence(failed_test_identity),
                "declaration_identity": byte_sequence(failed_declaration_identity),
                "callable_identity": byte_sequence(failed_callable_identity),
                "signature_identity": byte_sequence(failed_signature_identity),
            }, record_identity(protocol_interface, "TestCallableEntry"))
            failed_test_result = wire_record("TestResult", {
                "verdict": wire_finite(protocol_interface, "Verdict", "FAIL"),
                "verdict_code": unsigned(1),
                "failure_kind": wire_finite(protocol_interface, "FailureKind", "Assertion"),
                "failure_code": unsigned(9201),
                "assertions": unsigned(1),
                "failures": unsigned(1),
                "expected": {"integer": {"type": {"bits": 64, "signed": True}, "value": 5}},
                "actual": {"integer": {"type": {"bits": 64, "signed": True}, "value": 3}},
                "assertion_code": unsigned(9201),
            }, record_identity(protocol_interface, "TestResult"))
            failed_execution = wire_record("ProviderTestExecution", {
                "test_case_identity": byte_sequence(failed_test_identity),
                "declaration_identity": byte_sequence(failed_declaration_identity),
                "callable_identity": byte_sequence(failed_callable_identity),
                "signature_identity": byte_sequence(failed_signature_identity),
                "artifact_identity": byte_sequence(test_artifact_identity),
                "execution_status": wire_finite(
                    protocol_interface, "CallableExecutionStatus", "RETURNED"
                ),
                "native_result": failed_test_result,
            }, record_identity(protocol_interface, "ProviderTestExecution"))
            batch_request_value = wire_record("ProviderRequest", {
                "compiler_callable_bindings": {"sequence": {"values": [
                    callable_entry, failed_callable_entry,
                ]}},
                "selected_test_identities": {"sequence": {"values": [
                    byte_sequence(test_identity), byte_sequence(failed_test_identity),
                ]}},
                "selected_test_executions": {"sequence": {"values": [
                    execution, failed_execution,
                ]}},
                "selection_count": unsigned(2),
                "interface_identity": byte_values(bytes([
                    239, 160, 50, 155, 9, 221, 240, 227, 220, 117, 104, 17, 134, 215, 15, 192,
                    206, 150, 165, 44, 250, 237, 241, 7, 176, 213, 182, 151, 52, 146, 186, 101,
                ])),
                "provider_revision_identity": byte_values(bytes([
                    242, 141, 98, 241, 104, 235, 30, 81, 190, 31, 78, 85, 62, 100, 140, 222,
                    6, 235, 59, 56, 33, 238, 68, 155, 119, 69, 38, 11, 227, 233, 132, 215,
                ])),
            }, provider_request_identity)
            batch_input = batch_dir / "request.json"
            batch_output = batch_dir / "result.json"
            batch_check = batch_dir / "check.json"
            batch_input.write_text(
                json.dumps(batch_request_value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            batch_context = application_context(
                interface, batch_dir,
                artifact_paths=[batch_input, batch_output, batch_check],
            )
            batch_call_path = batch_dir / "execution.json"
            batch_call_path.write_text(
                json.dumps(call_request(batch_context), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            batch_started = time.monotonic()
            batch_process = subprocess.run([
                "/usr/bin/time", "-v", str(binary), str(artifact_path),
                f"{artifact_identity_for_files};{artifact_identity_for_digest}",
                "provider_artifact;provider_digest", "single", str(batch_call_path),
                str(batch_input), str(batch_output), "success", str(batch_check),
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            batch_wall_seconds = time.monotonic() - batch_started
            if batch_process.returncode != 0:
                raise AssertionError(
                    "canonical VM rejected the positive two-test provider application batch: "
                    f"rc={batch_process.returncode}, stderr={batch_process.stderr[-5000:]}"
                )
            batch_document = json.loads(batch_process.stdout)
            batch_record = batch_document.get("record", {})
            batch_effects = batch_record.get("effects", [])
            batch_returned = [digest_app.support.decode_wire(value)
                              for value in batch_document.get("returned_wire", [])]
            batch_published_result = digest_app.support.decode_wire(json.loads(
                batch_output.read_text(encoding="utf-8")
            ))
            batch_published_check = digest_app.support.decode_wire(json.loads(
                batch_check.read_text(encoding="utf-8")
            ))
            fail_discriminant = next(
                row["discriminant"] for row in verdict_row["variants"]
                if row.get("name") == "FAIL"
            )
            failure_kind_row = next(
                row for row in protocol_interface["types"]["finite"]
                if row.get("name") == "FailureKind"
            )
            assertion_discriminant = next(
                row["discriminant"] for row in failure_kind_row["variants"]
                if row.get("name") == "Assertion"
            )
            batch_rss_match = re.search(
                r"Maximum resident set size \(kbytes\): (\d+)", batch_process.stderr
            )
            batch_operations = [row.get("request", {}).get("operation") for row in batch_effects]
            expected_batch_admitted = sorted([
                "provider_artifact", "provider_digest",
                *[
                    identity
                    for binding in batch_document.get("capability_binding", {}).get("bindings", [])
                    for identity in binding.get("call_authority_identities", [])
                ],
            ])
            batch_native_result = batch_published_result.get("native_result", {})
            if (
                batch_document.get("outcome", {}).get("kind") != "completed"
                or batch_record.get("admitted_capabilities") != expected_batch_admitted
                or batch_operations != [
                    "structured_read",
                    *( ["structured_digest"] * 5 ),
                    "structured_write",
                    "structured_write",
                ]
                or any(row.get("outcome_tag") != "completed" for row in batch_effects)
                or len(batch_effects) != 8
                or not batch_output.exists() or not batch_check.exists()
                or len(batch_returned) != 1 or batch_returned[0].get("exit_code") != 0
                or batch_published_result.get("selection_count") != 2
                or batch_published_result.get("selected_test_identities") != [
                    list(test_identity.encode()), list(failed_test_identity.encode()),
                ]
                or batch_native_result.get("verdict", {}).get("$v") != fail_discriminant
                or batch_native_result.get("verdict_code") != 1
                or batch_native_result.get("assertions") != 2
                or batch_native_result.get("failures") != 1
                or batch_native_result.get("failure_code") != 9201
                or batch_native_result.get("failure_kind", {}).get("$v") != assertion_discriminant
                or batch_native_result.get("assertion_code") != 9201
                or batch_native_result.get("expected") != 5
                or batch_native_result.get("actual") != 3
                or batch_published_check.get("verdict", {}).get("$v") != fail_discriminant
                or batch_published_check.get("test_result_identity")
                != batch_published_result.get("result_identity")
            ):
                raise AssertionError(json.dumps({
                    "outcome": batch_document.get("outcome"),
                    "returned": batch_returned,
                    "admitted_capabilities": batch_record.get("admitted_capabilities"),
                    "expected_admitted_capabilities": expected_batch_admitted,
                    "operations": batch_operations,
                    "effects": batch_effects,
                    "published_result": batch_published_result,
                    "published_check": batch_published_check,
                }, indent=2)[:15000])

            failed_write_dir = call_dir / "rejected-provider-publication"
            failed_write_dir.mkdir()
            failed_write_input = failed_write_dir / "request.json"
            failed_write_output = failed_write_dir / "result.json"
            failed_write_check = failed_write_dir / "check.json"
            failed_write_input.write_text(
                json.dumps(provider_request_value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            failed_write_context = application_context(
                interface, failed_write_dir,
                artifact_paths=[failed_write_input, failed_write_output, failed_write_check],
            )
            failed_write_call_path = failed_write_dir / "execution.json"
            failed_write_call_path.write_text(
                json.dumps(call_request(failed_write_context), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            failed_write_process = subprocess.run([
                str(binary), str(artifact_path),
                f"{artifact_identity_for_files};{artifact_identity_for_digest}",
                "provider_artifact;provider_digest", "single", str(failed_write_call_path),
                str(failed_write_input), str(failed_write_output), "fail-write",
                str(failed_write_check),
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            if failed_write_process.returncode != 0:
                raise AssertionError(
                    "canonical VM rejected the failed-publication provider run: "
                    f"rc={failed_write_process.returncode}, stderr={failed_write_process.stderr[-3000:]}"
                )
            failed_write_document = json.loads(failed_write_process.stdout)
            failed_write_returned = [digest_app.support.decode_wire(value)
                                     for value in failed_write_document.get("returned_wire", [])]
            failed_write_effects = failed_write_document.get("record", {}).get("effects", [])
            if (
                failed_write_document.get("outcome", {}).get("kind") != "completed"
                or len(failed_write_returned) != 1
                or failed_write_returned[0].get("exit_code") != 3
                or [row.get("request", {}).get("operation") for row in failed_write_effects]
                != ["structured_read", *(["structured_digest"] * 5),
                    "structured_write", "structured_write"]
                or failed_write_output.exists() or failed_write_check.exists()
            ):
                raise AssertionError(json.dumps({
                    "outcome": failed_write_document.get("outcome"),
                    "returned": failed_write_returned,
                    "effects": failed_write_effects,
                    "published": [failed_write_output.exists(), failed_write_check.exists()],
                }, indent=2)[:9000])

            real_suite = test_inventory.run_real_mncs_test_suite()
            real_cases = real_suite.get("provider_integration_cases", [])
            if (len(real_cases) != 7
                    or real_suite.get("passing_test_count") != 6
                    or real_suite.get("skipped_test_count") != 1
                    or real_suite.get("stage0_equivalent") is not True):
                raise AssertionError(
                    "real mncs-test self-suite did not produce the verified seven-case provider input: "
                    + json.dumps(real_suite, indent=2)[:6000]
                )

            cross_case, cross_case_proof = compile_cross_module_provider_case(
                call_dir / "cross-module-test-build"
            )
            self_suite_case = next(
                (case for case in real_cases if case.get("name") == "arithmetic"), None
            )
            if self_suite_case is None:
                raise AssertionError("real mncs-test self-suite omitted its arithmetic callable")
            heterogeneous_cases = [self_suite_case, cross_case]
            heterogeneous_artifact_identities = sorted({
                case["artifact_identity"] for case in heterogeneous_cases
            })
            if len(heterogeneous_artifact_identities) != 2:
                raise AssertionError("separate Test modules did not produce distinct executable artifacts")
            heterogeneous_request = provider_request_wire(
                protocol_interface, provider_request_identity, heterogeneous_cases,
            )
            heterogeneous_dir = call_dir / "heterogeneous-artifact-batch"
            heterogeneous_dir.mkdir()
            heterogeneous_input = heterogeneous_dir / "request.json"
            heterogeneous_output = heterogeneous_dir / "result.json"
            heterogeneous_check = heterogeneous_dir / "check.json"
            heterogeneous_input.write_text(
                json.dumps(heterogeneous_request, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            heterogeneous_context = application_context(
                interface, heterogeneous_dir,
                artifact_paths=[heterogeneous_input, heterogeneous_output, heterogeneous_check],
            )
            heterogeneous_call_path = heterogeneous_dir / "execution.json"
            heterogeneous_call_path.write_text(
                json.dumps(call_request(heterogeneous_context), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            heterogeneous_oracle_probe = support.Probe(
                ROOT / ".build/native-app-readiness/provider-heterogeneous-oracle-cache",
                executable=support.PROBE,
            )
            try:
                heterogeneous_oracle, heterogeneous_oracle_metrics = heterogeneous_oracle_probe.send(
                    {"project_oracle": {
                        "root": root,
                        "modules": modules,
                        "calls": [{
                            "schema_version": "0.1",
                            "target": {"module": ENTRY_MODULE, "function": "run_selected_batch"},
                            "arguments": [heterogeneous_request],
                            "type_arguments": [],
                            "step_budget": 200_000,
                            "policy": {"effects": "realize"},
                            "host_grants": [{
                                "capability": "provider_digest",
                                "locator": "bounded-heterogeneous-test-provider-oracle",
                                "bytes": [],
                            }],
                        }],
                    }}, timeout=180.0,
                )
            finally:
                heterogeneous_oracle_probe.close()
            heterogeneous_oracle_rows = heterogeneous_oracle.get("executions", [])
            if (not heterogeneous_oracle.get("valid")
                    or len(heterogeneous_oracle_rows) != 1
                    or heterogeneous_oracle_rows[0].get("status") != "returned"
                    or len(heterogeneous_oracle_rows[0].get("returned", [])) != 1):
                raise AssertionError(
                    "Stage-0 rejected or failed the real heterogeneous-artifact provider request: "
                    + json.dumps(heterogeneous_oracle, indent=2)[:10000]
                )
            heterogeneous_stage0 = digest_app.support.decode_wire(
                heterogeneous_oracle_rows[0]["returned"][0]
            )
            heterogeneous_started = time.monotonic()
            heterogeneous_process = subprocess.run([
                "/usr/bin/time", "-v", str(binary), str(artifact_path),
                f"{artifact_identity_for_files};{artifact_identity_for_digest}",
                "provider_artifact;provider_digest", "single", str(heterogeneous_call_path),
                str(heterogeneous_input), str(heterogeneous_output), "success",
                str(heterogeneous_check),
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            heterogeneous_wall_seconds = time.monotonic() - heterogeneous_started
            if heterogeneous_process.returncode != 0:
                raise AssertionError(
                    "canonical VM rejected the real heterogeneous-artifact Test provider batch: "
                    f"rc={heterogeneous_process.returncode}, stderr={heterogeneous_process.stderr[-5000:]}"
                )
            heterogeneous_document = json.loads(heterogeneous_process.stdout)
            heterogeneous_record = heterogeneous_document.get("record", {})
            heterogeneous_effects = heterogeneous_record.get("effects", [])
            heterogeneous_returned = [digest_app.support.decode_wire(value)
                                       for value in heterogeneous_document.get("returned_wire", [])]
            if not heterogeneous_output.is_file() or not heterogeneous_check.is_file():
                raise AssertionError("heterogeneous-artifact provider batch did not publish typed outputs")
            heterogeneous_published_result = digest_app.support.decode_wire(json.loads(
                heterogeneous_output.read_text(encoding="utf-8")
            ))
            heterogeneous_published_check = digest_app.support.decode_wire(json.loads(
                heterogeneous_check.read_text(encoding="utf-8")
            ))
            heterogeneous_operations = [
                row.get("request", {}).get("operation") for row in heterogeneous_effects
            ]
            heterogeneous_native_result = heterogeneous_published_result.get("native_result", {})
            heterogeneous_rss_match = re.search(
                r"Maximum resident set size \(kbytes\): (\d+)", heterogeneous_process.stderr
            )
            expected_heterogeneous_admitted = sorted([
                "provider_artifact", "provider_digest",
                *[
                    identity
                    for binding in heterogeneous_document.get("capability_binding", {}).get("bindings", [])
                    for identity in binding.get("call_authority_identities", [])
                ],
            ])
            heterogeneous_test_identities = [
                case["test_case_identity"] for case in heterogeneous_cases
            ]
            if (
                heterogeneous_document.get("outcome", {}).get("kind") != "completed"
                or heterogeneous_record.get("admitted_capabilities") != expected_heterogeneous_admitted
                or heterogeneous_operations != [
                    "structured_read", *( ["structured_digest"] * 5 ),
                    "structured_write", "structured_write",
                ]
                or len(heterogeneous_effects) != 8
                or any(row.get("outcome_tag") != "completed" for row in heterogeneous_effects)
                or len(heterogeneous_returned) != 1
                or heterogeneous_returned[0].get("exit_code") != 0
                or heterogeneous_published_result != heterogeneous_stage0.get("test_result")
                or heterogeneous_published_check != heterogeneous_stage0.get("check_result")
                or heterogeneous_published_result.get("selection_count") != 2
                or heterogeneous_published_result.get("selected_test_identities")
                != [list(identity.encode()) for identity in heterogeneous_test_identities]
                or heterogeneous_native_result.get("verdict_code") != 0
                or heterogeneous_native_result.get("assertions") != 2
                or heterogeneous_native_result.get("failures") != 0
                or heterogeneous_published_check.get("test_result_identity")
                != heterogeneous_published_result.get("result_identity")
            ):
                raise AssertionError(json.dumps({
                    "outcome": heterogeneous_document.get("outcome"),
                    "return": heterogeneous_returned,
                    "operations": heterogeneous_operations,
                    "admitted_capabilities": heterogeneous_record.get("admitted_capabilities"),
                    "published_result": heterogeneous_published_result,
                    "published_check": heterogeneous_published_check,
                    "stage0": heterogeneous_stage0,
                }, indent=2)[:16000])
            heterogeneous_batch_proof = {
                "selection_count": 2,
                "test_case_identities": heterogeneous_test_identities,
                "test_module_artifact_identities": [
                    case["artifact_identity"] for case in heterogeneous_cases
                ],
                "distinct_test_module_artifact_count": len(heterogeneous_artifact_identities),
                "second_module_compilation_and_execution": cross_case_proof,
                "provider_artifact_identity": artifact_identity,
                "provider_outcome": heterogeneous_document.get("outcome"),
                "provider_exit": heterogeneous_returned[0],
                "effects": [
                    {
                        "operation": row.get("request", {}).get("operation"),
                        "capability": row.get("request", {}).get("capability"),
                        "outcome_tag": row.get("outcome_tag"),
                    }
                    for row in heterogeneous_effects
                ],
                "published_result": heterogeneous_published_result,
                "published_check": heterogeneous_published_check,
                "stage0_provider_run_exact_match": True,
                "stage0_oracle_metrics": heterogeneous_oracle_metrics,
                "vm_wall_seconds": round(heterogeneous_wall_seconds, 6),
                "vm_peak_rss_bytes": (
                    int(heterogeneous_rss_match.group(1)) * 1024
                    if heterogeneous_rss_match else None
                ),
            }

            real_request_value = provider_request_wire(
                protocol_interface, provider_request_identity, real_cases,
            )
            real_dir = call_dir / "real-mncs-test-self-suite"
            real_dir.mkdir()
            real_input = real_dir / "request.json"
            real_output = real_dir / "result.json"
            real_check = real_dir / "check.json"
            real_input.write_text(
                json.dumps(real_request_value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            real_context = application_context(
                interface, real_dir,
                artifact_paths=[real_input, real_output, real_check],
            )
            real_call_path = real_dir / "execution.json"
            real_call_path.write_text(
                json.dumps(call_request(real_context), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            stage0_oracle_call = {
                "schema_version": "0.1",
                "target": {"module": ENTRY_MODULE, "function": "run_selected_batch"},
                "arguments": [real_request_value],
                "type_arguments": [],
                "step_budget": 200_000,
                "policy": {"effects": "realize"},
                "host_grants": [{
                    "capability": "provider_digest",
                    "locator": "bounded-self-suite-provider-oracle",
                    "bytes": [],
                }],
            }
            real_oracle_probe = support.Probe(
                ROOT / ".build/native-app-readiness/provider-self-suite-oracle-cache",
                executable=support.PROBE,
            )
            try:
                real_oracle, real_oracle_metrics = real_oracle_probe.send({"project_oracle": {
                    "root": root,
                    "modules": modules,
                    "calls": [stage0_oracle_call],
                }}, timeout=180)
            finally:
                real_oracle_probe.close()
            real_oracle_rows = real_oracle.get("executions", [])
            if (not real_oracle.get("valid") or len(real_oracle_rows) != 1
                    or real_oracle_rows[0].get("status") != "returned"
                    or len(real_oracle_rows[0].get("returned", [])) != 1):
                raise AssertionError(
                    "Stage-0 did not return a typed production provider fold for the real Test batch: "
                    + json.dumps(real_oracle, indent=2)[:10000]
                )
            stage0_provider_run = digest_app.support.decode_wire(
                real_oracle_rows[0]["returned"][0]
            )

            real_started = time.monotonic()
            real_process = subprocess.run([
                "/usr/bin/time", "-v", str(binary), str(artifact_path),
                f"{artifact_identity_for_files};{artifact_identity_for_digest}",
                "provider_artifact;provider_digest", "single", str(real_call_path),
                str(real_input), str(real_output), "success", str(real_check),
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            real_wall_seconds = time.monotonic() - real_started
            if real_process.returncode != 0:
                raise AssertionError(
                    "canonical VM rejected the real mncs-test provider batch: "
                    f"rc={real_process.returncode}, stderr={real_process.stderr[-5000:]}"
                )
            real_document = json.loads(real_process.stdout)
            real_record = real_document.get("record", {})
            real_effects = real_record.get("effects", [])
            real_returned = [digest_app.support.decode_wire(value)
                             for value in real_document.get("returned_wire", [])]
            if not real_output.is_file() or not real_check.is_file():
                raise AssertionError("real provider batch did not publish both typed result records")
            real_published_result = digest_app.support.decode_wire(json.loads(
                real_output.read_text(encoding="utf-8")
            ))
            real_published_check = digest_app.support.decode_wire(json.loads(
                real_check.read_text(encoding="utf-8")
            ))
            real_operations = [row.get("request", {}).get("operation") for row in real_effects]
            real_expected_admitted = sorted([
                "provider_artifact", "provider_digest",
                *[
                    identity
                    for binding in real_document.get("capability_binding", {}).get("bindings", [])
                    for identity in binding.get("call_authority_identities", [])
                ],
            ])
            expected_test_identities = [
                list(case["test_case_identity"].encode()) for case in real_cases
            ]
            real_native_result = real_published_result.get("native_result", {})
            real_pass_discriminant = next(
                row["discriminant"] for row in verdict_row["variants"]
                if row.get("name") == "PASS"
            )
            real_rss_match = re.search(
                r"Maximum resident set size \(kbytes\): (\d+)", real_process.stderr
            )
            real_user_match = re.search(r"User time \(seconds\): ([0-9.]+)", real_process.stderr)
            real_sys_match = re.search(r"System time \(seconds\): ([0-9.]+)", real_process.stderr)
            if (
                real_document.get("outcome", {}).get("kind") != "completed"
                or real_record.get("admitted_capabilities") != real_expected_admitted
                or real_operations != [
                    "structured_read", *( ["structured_digest"] * 5 ),
                    "structured_write", "structured_write",
                ]
                or any(row.get("outcome_tag") != "completed" for row in real_effects)
                or len(real_effects) != 8
                or len(real_returned) != 1 or real_returned[0].get("exit_code") != 0
                or real_published_result != stage0_provider_run.get("test_result")
                or real_published_check != stage0_provider_run.get("check_result")
                or real_published_result.get("selection_count") != 7
                or real_published_result.get("selected_test_identities") != expected_test_identities
                or real_native_result.get("verdict", {}).get("$v") != real_pass_discriminant
                or real_native_result.get("verdict_code") != 0
                or real_native_result.get("assertions") != 6
                or real_native_result.get("failures") != 0
                or real_published_check.get("test_result_identity")
                != real_published_result.get("result_identity")
            ):
                raise AssertionError(json.dumps({
                    "outcome": real_document.get("outcome"),
                    "returned": real_returned,
                    "admitted_capabilities": real_record.get("admitted_capabilities"),
                    "expected_admitted_capabilities": real_expected_admitted,
                    "operations": real_operations,
                    "published_result": real_published_result,
                    "published_check": real_published_check,
                    "stage0_provider_run": stage0_provider_run,
                }, indent=2)[:18000])

            invalid_request_fields = record_fields(
                json.loads(json.dumps(heterogeneous_request))
            )
            invalid_execution_values = invalid_request_fields[
                "selected_test_executions"
            ]["sequence"]["values"]
            invalid_execution_fields = record_fields(invalid_execution_values[1])
            invalid_execution_fields["artifact_identity"] = byte_sequence(
                self_suite_case["artifact_identity"]
            )
            invalid_execution_values[1] = wire_record(
                "ProviderTestExecution", invalid_execution_fields,
                record_identity(protocol_interface, "ProviderTestExecution"),
            )
            invalid_request_value = wire_record(
                "ProviderRequest", invalid_request_fields, provider_request_identity,
            )
            invalid_oracle_probe = support.Probe(
                ROOT / ".build/native-app-readiness/provider-invalid-binding-oracle-cache",
                executable=support.PROBE,
            )
            try:
                invalid_oracle, invalid_oracle_metrics = invalid_oracle_probe.send(
                    {"project_oracle": {
                        "root": root,
                        "modules": modules,
                        "calls": [{
                            **stage0_oracle_call,
                            "arguments": [invalid_request_value],
                        }],
                    }}, timeout=180,
                )
            finally:
                invalid_oracle_probe.close()
            invalid_oracle_rows = invalid_oracle.get("executions", [])
            if (not invalid_oracle.get("valid") or len(invalid_oracle_rows) != 1
                    or invalid_oracle_rows[0].get("status") != "returned"
                    or len(invalid_oracle_rows[0].get("returned", [])) != 1):
                raise AssertionError(
                    "Stage-0 did not return the provider's typed invalid-binding result: "
                    + json.dumps(invalid_oracle, indent=2)[:10000]
                )
            invalid_stage0_run = digest_app.support.decode_wire(
                invalid_oracle_rows[0]["returned"][0]
            )
            invalid_dir = real_dir / "artifact-identity-mismatch"
            invalid_dir.mkdir()
            invalid_input = invalid_dir / "request.json"
            invalid_output = invalid_dir / "result.json"
            invalid_check = invalid_dir / "check.json"
            invalid_input.write_text(
                json.dumps(invalid_request_value, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            invalid_context = application_context(
                interface, invalid_dir,
                artifact_paths=[invalid_input, invalid_output, invalid_check],
            )
            invalid_call_path = invalid_dir / "execution.json"
            invalid_call_path.write_text(
                json.dumps(call_request(invalid_context), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            invalid_started = time.monotonic()
            invalid_process = subprocess.run([
                "/usr/bin/time", "-v", str(binary), str(artifact_path),
                f"{artifact_identity_for_files};{artifact_identity_for_digest}",
                "provider_artifact;provider_digest", "single", str(invalid_call_path),
                str(invalid_input), str(invalid_output), "success", str(invalid_check),
            ], cwd=ROOT, capture_output=True, text=True, check=False)
            invalid_wall_seconds = time.monotonic() - invalid_started
            if invalid_process.returncode != 0 or not invalid_output.is_file() or not invalid_check.is_file():
                raise AssertionError(
                    "canonical VM failed to publish the provider's typed invalid-binding result: "
                    f"rc={invalid_process.returncode}, stderr={invalid_process.stderr[-5000:]}"
                )
            invalid_document = json.loads(invalid_process.stdout)
            invalid_record = invalid_document.get("record", {})
            invalid_effects = invalid_record.get("effects", [])
            invalid_returned = [digest_app.support.decode_wire(value)
                                for value in invalid_document.get("returned_wire", [])]
            invalid_published_result = digest_app.support.decode_wire(json.loads(
                invalid_output.read_text(encoding="utf-8")
            ))
            invalid_published_check = digest_app.support.decode_wire(json.loads(
                invalid_check.read_text(encoding="utf-8")
            ))
            unsupported_discriminant = next(
                row["discriminant"] for row in verdict_row["variants"]
                if row.get("name") == "UNSUPPORTED"
            )
            invalid_operations = [row.get("request", {}).get("operation")
                                  for row in invalid_effects]
            invalid_rss_match = re.search(
                r"Maximum resident set size \(kbytes\): (\d+)", invalid_process.stderr
            )
            if (
                invalid_document.get("outcome", {}).get("kind") != "completed"
                or len(invalid_returned) != 1 or invalid_returned[0].get("exit_code") != 0
                or invalid_operations != [
                    "structured_read", *( ["structured_digest"] * 5 ),
                    "structured_write", "structured_write",
                ]
                or len(invalid_effects) != 8
                or any(row.get("outcome_tag") != "completed" for row in invalid_effects)
                or invalid_published_result != invalid_stage0_run.get("test_result")
                or invalid_published_check != invalid_stage0_run.get("check_result")
                or invalid_published_result.get("native_result", {}).get(
                    "verdict", {}
                ).get("$v") != unsupported_discriminant
                or invalid_published_result.get("native_result", {}).get(
                    "verdict_code"
                ) != 3
                or invalid_published_result.get("native_result", {}).get(
                    "failure_code"
                ) != 9001
                or invalid_published_result.get("selection_count") != len(heterogeneous_cases)
                or invalid_published_result.get("selected_test_identities")
                != [list(identity.encode()) for identity in heterogeneous_test_identities]
                or invalid_published_result.get("result_identity")
                == heterogeneous_published_result.get("result_identity")
                or invalid_published_check.get("test_result_identity")
                != invalid_published_result.get("result_identity")
            ):
                raise AssertionError(json.dumps({
                    "outcome": invalid_document.get("outcome"),
                    "returned": invalid_returned,
                    "operations": invalid_operations,
                    "published_result": invalid_published_result,
                    "published_check": invalid_published_check,
                    "stage0_provider_run": invalid_stage0_run,
                }, indent=2)[:18000])

            heterogeneous_batch_proof["per_row_artifact_identity_mismatch"] = {
                "test_case_identity": cross_case["test_case_identity"],
                "declared_artifact_identity": cross_case["artifact_identity"],
                "execution_artifact_identity": self_suite_case["artifact_identity"],
                "execution_identity_is_another_valid_batch_artifact": True,
                "application_outcome": invalid_document.get("outcome"),
                "application_exit": invalid_returned[0],
                "published_result": invalid_published_result,
                "published_check": invalid_published_check,
                "stage0_provider_run_exact_match": True,
                "provider_did_not_report_pass": (
                    invalid_published_result.get("native_result", {}).get("verdict_code") != 0
                ),
                "stage0_oracle_metrics": invalid_oracle_metrics,
                "vm_wall_seconds": round(invalid_wall_seconds, 6),
                "vm_peak_rss_bytes": (
                    int(invalid_rss_match.group(1)) * 1024
                    if invalid_rss_match else None
                ),
            }

            rss_match = re.search(r"Maximum resident set size \(kbytes\): (\d+)", process.stderr)
            user_match = re.search(r"User time \(seconds\): ([0-9.]+)", process.stderr)
            sys_match = re.search(r"System time \(seconds\): ([0-9.]+)", process.stderr)
            source_rows = [
                {"module": name, "path": str(path), "sha256": sha(path.read_bytes())}
                for name, path in sorted(MODULE_PATHS.items())
            ]
            source_rows.append({"module": ENTRY_MODULE, "path": str(ROOT_SOURCE_PATH),
                                "sha256": sha(ROOT_SOURCE_PATH.read_bytes())})
            evidence = {
                "schema_version": "mncs.compiler.native-application-proof/3",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "objective": "Compile and execute the production mncs-test provider application with typed capability results and bounded output publication",
                "application": {
                    "root_source_id": "native/mncs/test/provider.mncs",
                    "entry": f"{ENTRY_MODULE}::{ENTRY_FUNCTION}",
                    "module_sources": source_rows,
                    "target": "mncs-vm-0.1",
                    "capability_dispatch": {
                        "status": dispatch.get("status"),
                        "host_operation_aliases": {
                            alias: sorted(operations) for alias, operations in sorted(actual_operations.items())
                        },
                        "canonical_capability_identities": {
                            alias: sorted(identities)
                            for alias, identities in sorted(canonical_identities.items())
                        },
                        "validated_artifact_identity": artifact_identity,
                    },
                },
                "producer": {
                    "compiler_revision": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "language_revision": subprocess.check_output(["git", "-C", str(digest_app.LANGUAGE_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "test_revision": subprocess.check_output(["git", "-C", str(TEST_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "stdlib_revision": subprocess.check_output(["git", "-C", str(STDLIB_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "vm_revision": subprocess.check_output(["git", "-C", str(digest_app.VM_ROOT), "rev-parse", "HEAD"], text=True).strip(),
                    "stage0_probe_sha256": sha(support.PROBE.read_bytes()),
                    "vm_runner_source_sha256": sha(digest_app.RUNNER_SOURCE.read_bytes()),
                    "verification_harness_sha256": sha(Path(__file__).read_bytes()),
                    "test_inventory_harness_sha256": sha(
                        Path(test_inventory.__file__).read_bytes()
                    ),
                    "vm_runner_binary_sha256": runner_sha,
                    "vm_runner_lockfile_sha256": runner_lock_sha,
                },
                "build": {
                    "request_schema": request["schema_version"],
                    "result_schema": built["schema_version"],
                    "status": built["status"],
                    "artifact_identity": artifact_identity,
                    "artifact_sha256": sha(artifact_bytes),
                    "artifact_bytes": len(artifact_bytes),
                    "runtime_checked_sequence_copy_lowering_ref": runtime_copy_lowering_ref,
                    "unresolved_obligations": built.get("unresolved_obligations"),
                    "metrics": build_metrics,
                    "provider_request_interface": {
                        "identity": protocol_interface.get("identity"),
                        "signature": protocol_interface.get("signature"),
                        "provider_request_type_identity": provider_request_identity,
                        "metrics": protocol_build_metrics,
                    },
                },
                "execution": {
                    "outcome": outcome,
                    "application_exit": returned[0],
                    "effects": record.get("effects"),
                    "admitted_capabilities": record.get("admitted_capabilities"),
                    "no_output_publication": not absent_output.exists(),
                    "stage0_exact_return_match": True,
                    "stage0_oracle_metrics": oracle_metrics,
                    "vm_wall_seconds": round(wall_seconds, 6),
                    "vm_user_seconds": float(user_match.group(1)) if user_match else None,
                    "vm_system_seconds": float(sys_match.group(1)) if sys_match else None,
                    "vm_peak_rss_bytes": int(rss_match.group(1)) * 1024 if rss_match else None,
                    "capability_admission": "none; invalid arguments return before host calls, while valid bounded arguments are denied at the first structured_read",
                    "wrong_identity_rejected": wrong_identity_diagnostic,
                    "wrong_alias_rejected": wrong_alias_diagnostic,
                },
                "two_capability_admission_attempt": {
                    "outcome": positive_document.get("outcome"),
                    "application_exit": positive_returned[0],
                    "admitted_capabilities": positive_record.get("admitted_capabilities"),
                    "effects": [
                        {
                            "operation": row.get("request", {}).get("operation"),
                            "capability": row.get("request", {}).get("capability"),
                            "outcome_tag": row.get("outcome_tag"),
                        }
                        for row in positive_effects
                    ],
                    "published_result": positive_output.exists(),
                    "published_check": positive_check.exists(),
                    "typed_result": published_result,
                    "typed_check": published_check,
                    "vm_wall_seconds": round(positive_wall_seconds, 6),
                    "vm_peak_rss_bytes": (
                        int(re.search(r"Maximum resident set size \(kbytes\): (\d+)", positive_process.stderr).group(1)) * 1024
                        if re.search(r"Maximum resident set size \(kbytes\): (\d+)", positive_process.stderr)
                        else None
                    ),
                    "dispatch_bindings": positive_document.get("capability_binding", {}).get("bindings"),
                },
                "multi_test_batch": {
                    "selection_count": 2,
                    "selected_test_identities": [test_identity, failed_test_identity],
                    "outcome": batch_document.get("outcome"),
                    "application_exit": batch_returned[0],
                    "admitted_capabilities": batch_record.get("admitted_capabilities"),
                    "effects": [
                        {
                            "operation": row.get("request", {}).get("operation"),
                            "capability": row.get("request", {}).get("capability"),
                            "outcome_tag": row.get("outcome_tag"),
                        }
                        for row in batch_effects
                    ],
                    "published_result": batch_output.exists(),
                    "published_check": batch_check.exists(),
                    "typed_result": batch_published_result,
                    "typed_check": batch_published_check,
                    "vm_wall_seconds": round(batch_wall_seconds, 6),
                    "vm_peak_rss_bytes": int(batch_rss_match.group(1)) * 1024 if batch_rss_match else None,
                },
                "real_mncs_test_self_suite_batch": {
                    "test_revision": real_suite["test_repository_revision"],
                    "language_locked_revision": real_suite["language_locked_revision"],
                    "self_suite_source": real_suite["root_source"],
                    "dependency_sources": real_suite["dependency_sources"],
                    "inventory_artifact_identity": real_suite["selected_artifact_identity"],
                    "compiler_inventory_count": real_suite["distinct_test_count"],
                    "compiler_inventory_test_case_identities": [
                        case["test_case_identity"] for case in real_cases
                    ],
                    "compiler_inventory_callable_bindings": [
                        {
                            "test_case_identity": case["test_case_identity"],
                            "declaration_identity": case["declaration_identity"],
                            "callable_identity": case["callable_identity"],
                            "signature_identity": case["signature_identity"],
                        }
                        for case in real_cases
                    ],
                    "individual_canonical_vm_results_match_stage0": real_suite["stage0_equivalent"],
                    "individual_canonical_vm_batch_count": real_suite["canonical_vm_batch_count"],
                    "individual_canonical_vm_batch_wall_seconds": (
                        real_suite["canonical_vm_batch_wall_seconds"]
                    ),
                    "individual_canonical_vm_batch_peak_rss_kib": (
                        real_suite["canonical_vm_batch_peak_rss_kib"]
                    ),
                    "build_process_metrics": real_suite["build_process_metrics"],
                    "provider_application_outcome": real_document.get("outcome"),
                    "provider_application_exit": real_returned[0],
                    "admitted_capabilities": real_record.get("admitted_capabilities"),
                    "effects": [
                        {
                            "operation": row.get("request", {}).get("operation"),
                            "capability": row.get("request", {}).get("capability"),
                            "outcome_tag": row.get("outcome_tag"),
                        }
                        for row in real_effects
                    ],
                    "published_result": real_published_result,
                    "published_check": real_published_check,
                    "stage0_provider_run_exact_match": True,
                    "stage0_oracle_metrics": real_oracle_metrics,
                    "vm_wall_seconds": round(real_wall_seconds, 6),
                    "vm_user_seconds": float(real_user_match.group(1)) if real_user_match else None,
                    "vm_system_seconds": float(real_sys_match.group(1)) if real_sys_match else None,
                    "vm_peak_rss_bytes": (
                        int(real_rss_match.group(1)) * 1024 if real_rss_match else None
                    ),
                },
                "heterogeneous_artifact_batch": heterogeneous_batch_proof,
                "rejected_publication": {
                    "application_exit": failed_write_returned[0],
                    "effect_operations": [
                        row.get("request", {}).get("operation") for row in failed_write_effects
                    ],
                    "no_output_published": (
                        not failed_write_output.exists() and not failed_write_check.exists()
                    ),
                },
                "missing_capability": {
                    "outcome": missing_cap_document.get("outcome"),
                    "admitted_capabilities": missing_cap_record.get("admitted_capabilities"),
                    "first_effect": missing_cap_effects[0],
                    "no_artifact_publication": not any(path.exists() for path in missing_cap_paths),
                },
                "missing_provider_digest": {
                    "outcome": digest_document.get("outcome"),
                    "admitted_capabilities": digest_record.get("admitted_capabilities"),
                    "effects": digest_effects,
                    "digest_operation_executed": False,
                    "no_result_publication": not any(path.exists() for path in digest_paths[1:]),
                },
                "limitations": [
                    "The local bounded structured-file provider completes single-test, two-test, heterogeneous-artifact, and real seven-case mncs-test self-suite paths; this does not prove invocation through Forge, Environment, and canonical Store or fresh-process Store readback."
                ],
                "language_verified_facts_reused": 0,
                "self_consumption_frontier_advanced": False,
            }
            evidence_bytes = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if EVIDENCE_PATH.exists():
                raise SystemExit(f"refusing to overwrite immutable evidence: {EVIDENCE_PATH}")
            EVIDENCE_PATH.write_bytes(evidence_bytes)
            environment_artifacts = publish_environment_artifacts(evidence_bytes)
            print(json.dumps({
                "evidence": str(EVIDENCE_PATH),
                "environment_artifacts": environment_artifacts,
                "artifact_identity": artifact_identity,
                "vm_wall_seconds": evidence["execution"]["vm_wall_seconds"],
                "vm_peak_rss_bytes": evidence["execution"]["vm_peak_rss_bytes"],
                "capability_aliases": evidence["application"]["capability_dispatch"]["host_operation_aliases"],
                "stage0_exact_return_match": True,
                "missing_capability_outcome": evidence["missing_capability"]["outcome"].get("kind"),
                "two_capability_runtime_outcome": evidence["two_capability_admission_attempt"]["outcome"].get("kind"),
                "multi_test_batch_runtime_outcome": evidence["multi_test_batch"]["outcome"].get("kind"),
                "heterogeneous_artifact_batch_outcome": evidence[
                    "heterogeneous_artifact_batch"
                ]["provider_outcome"].get("kind"),
                "heterogeneous_artifact_count": evidence[
                    "heterogeneous_artifact_batch"
                ]["distinct_test_module_artifact_count"],
            }, indent=2))
    finally:
        artifact_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
