#!/usr/bin/env python3
"""Verify the production Test provider application's multi-capability boundary.

This slice compiles the real provider entry and executes its invalid-argument
path through the canonical VM with no capabilities admitted. It proves that
the VM runner can validate one capability identity in an artifact whose other
host calls use a separate identity, while the MNCS application rejects the
bad context before performing effects.
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
EVIDENCE_PATH = ROOT / "evidence/native-test-provider-app-boundary-20261017.json"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def byte_sequence(text: str) -> dict:
    return {"sequence": {"values": [{"byte": {"value": byte}} for byte in text.encode()]}}


def unsigned(value: int) -> dict:
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": value}}


def wire_record(name: str, fields: dict, identity: str) -> dict:
    return {
        "record": {
            "name": name,
            "type_identity": identity,
            "fields": [[key, value] for key, value in sorted(fields.items())],
        },
    }


def wire_finite(interface: dict, type_name: str, variant_name: str) -> dict:
    finite = next(row for row in interface["types"]["finite"] if row.get("name") == type_name)
    variant = next(row for row in finite["variants"] if row.get("name") == variant_name)
    return {"finite": {
        "type_identity": finite["identity"],
        "variant_identity": variant["identity"],
        "discriminant": variant["discriminant"],
        "payload": [],
    }}


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
                str(positive_input), str(positive_output), "success",
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
                positive_document.get("outcome", {}).get("kind") != "unsupported"
                or positive_document.get("outcome", {}).get("feature") != "instruction sequence_copy"
                or positive_record.get("admitted_capabilities") != expected_admitted
                or observed_operations != ["structured_read", "structured_digest"]
                or len(positive_effects) != 2
                or any(row.get("outcome_tag") != "completed" for row in positive_effects)
                or positive_output.exists() or positive_check.exists()
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
                "schema_version": "mncs.compiler.native-application-proof/2",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "objective": "Compile the production mncs-test provider application, validate both capability identities, and exercise its bounded VM path through the first unsupported instruction",
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
                    "vm_wall_seconds": round(positive_wall_seconds, 6),
                    "vm_peak_rss_bytes": (
                        int(re.search(r"Maximum resident set size \(kbytes\): (\d+)", positive_process.stderr).group(1)) * 1024
                        if re.search(r"Maximum resident set size \(kbytes\): (\d+)", positive_process.stderr)
                        else None
                    ),
                    "dispatch_bindings": positive_document.get("capability_binding", {}).get("bindings"),
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
                    "The production provider application completes structured_read and its first structured_digest, then reaches an unsupported canonical VM sequence_copy while copying the digest into the language-owned fixed-size identity. This prevents a positive provider result in the current VM contract. The Forge/Environment/Store route and canonical Store readback also remain unproven."
                ],
                "language_verified_facts_reused": 0,
                "self_consumption_frontier_advanced": False,
            }
            evidence_bytes = (json.dumps(evidence, indent=2, sort_keys=True) + "\n").encode("utf-8")
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
            }, indent=2))
    finally:
        artifact_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
